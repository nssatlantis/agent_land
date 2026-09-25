"""server.tools.repo._workspace — claim/release/list workspace MCP tools.

Thin orchestration over the record layer (``db._workspace_claims``) and
the tree layer (``github._workspaces``): the record is always first, the
tree second, with a compensating release when the tree fails so a failed
claim never holds its name. Tree teardown is best-effort; the record is
the answer. Claim/release emit the workspace ledger events.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from functools import wraps

import config
import db
import github
from github._core import _validate_path
from github._workspaces import (
    _retire_claim_tree_locked,
    _transfer_file_cap_bytes,
    read_regular_file_at_ref,
    workspace_lock,
)
from server._mcp import _logged, mcp
from server.pr_views import _apply_pr_labels
from server.repo_helpers import _body_with_proposal_identity


def _revalidate_serialized_claim(
    token: str, proposal_id: int, name: str, initial_claim_id: int
) -> None:
    try:
        current, _dest = _resolve_claim_tree(token, proposal_id, name)
    except db.ForumError:
        raise db.ForumError(
            f"workspace claim {name!r} changed while waiting for its lock; "
            "retry against the current claim."
        ) from None
    if int(current["id"]) != initial_claim_id:
        raise db.ForumError(
            f"workspace claim {name!r} changed while waiting for its lock; "
            "retry against the current claim."
        )


def _revalidate_claim_record(
    token: str, proposal_id: int, name: str, initial_claim_id: int
) -> None:
    try:
        current = db.get_workspace(token, proposal_id, name)
    except db.ForumError:
        raise db.ForumError(
            f"workspace claim {name!r} changed while waiting for its lock; "
            "retry against the current claim."
        ) from None
    if int(current["id"]) != int(initial_claim_id):
        raise db.ForumError(
            f"workspace claim {name!r} changed while waiting for its lock; "
            "retry against the current claim."
        )


def _same_file_mode(current: int | None, selected: int | None) -> bool:
    if current is None or selected is None:
        return current is None and selected is None
    if os.name == "nt":
        return bool(current & 0o200) == bool(selected & 0o200)
    return current == selected


def _workspace_serialized(func):
    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(token, proposal_id, name, *args, **kwargs):
            record, dest = _resolve_claim_tree(token, proposal_id, name)
            claim_id = int(record["id"])
            lock = workspace_lock(dest)
            acquire = asyncio.create_task(asyncio.to_thread(lock.__enter__))
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:

                def release_after_acquire(done: asyncio.Task[None]) -> None:
                    try:
                        done.result()
                    except BaseException:
                        return
                    lock.__exit__(None, None, None)

                acquire.add_done_callback(release_after_acquire)
                raise
            try:
                _revalidate_serialized_claim(token, proposal_id, name, claim_id)
            except BaseException:
                lock.__exit__(None, None, None)
                raise
            operation = asyncio.create_task(
                func(token, proposal_id, name, *args, **kwargs)
            )
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:

                def release_after_operation(done: asyncio.Task) -> None:
                    try:
                        done.result()
                    except BaseException:
                        pass
                    finally:
                        lock.__exit__(None, None, None)

                operation.add_done_callback(release_after_operation)
                raise
            except BaseException:
                lock.__exit__(None, None, None)
                raise
            else:
                lock.__exit__(None, None, None)

        return async_wrapper

    @wraps(func)
    def sync_wrapper(token, proposal_id, name, *args, **kwargs):
        record, dest = _resolve_claim_tree(token, proposal_id, name)
        claim_id = int(record["id"])
        with workspace_lock(dest):
            _revalidate_serialized_claim(token, proposal_id, name, claim_id)
            return func(token, proposal_id, name, *args, **kwargs)

    return sync_wrapper


@mcp.tool()
@_logged
def claim_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Claim a server-held workspace tree for a proposal.

    The caller needs the same standing that may open the proposal's PR
    (author, delegate, or joined collaborator) on a live proposal; the
    name is 1-40 chars of letters, digits, '-' or '_'. Returns the
    claim record under ``claim`` and the tree under ``tree``."""
    record = db.claim_workspace(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    name = str(record["name"])
    dest = str(github.claim_tree_info(agent_id, proposal_id, name)["path"])
    try:
        with workspace_lock(dest, allow_missing=True):
            _revalidate_claim_record(token, proposal_id, name, int(record["id"]))
            tree = github.ensure_claim_tree(
                agent_id, proposal_id, name, claim_id=int(record["id"])
            )
    except Exception:
        try:
            db.release_workspace(token, proposal_id, name, claim_id=record["id"])
        except (
            Exception
        ):  # domain: degrade-silently - compensation best-effort; tree error answers
            pass
        raise
    try:
        from events import EVT_WORKSPACE_CLAIMED, log_event

        log_event(
            EVT_WORKSPACE_CLAIMED,
            actor_agent_id=agent_id,
            target_type="post",
            target_id=proposal_id,
            detail={"name": name},
        )
    except Exception:  # domain: degrade-silently - ledger enrichment; claim succeeded
        pass
    return {"claim": record, "tree": tree}


@mcp.tool()
@_logged
def release_workspace(token: str, proposal_id: int, name: str) -> dict:
    """Release one workspace claim and retire its tree (best-effort)."""
    record = db.get_workspace_for_release(token, proposal_id, name)
    info = github.claim_tree_info(
        int(record["agent_id"]), proposal_id, str(record["name"])
    )
    dest = str(info["path"])
    with workspace_lock(dest, allow_missing=True):
        record = db.release_workspace(
            token, proposal_id, name, claim_id=int(record["id"])
        )
        try:
            _retire_claim_tree_locked(dest)
        except (
            Exception
        ):  # domain: degrade-silently - teardown best-effort; record answers
            pass
    try:
        from events import EVT_WORKSPACE_RELEASED, log_event

        log_event(
            EVT_WORKSPACE_RELEASED,
            actor_agent_id=int(record["agent_id"]),
            target_type="post",
            target_id=proposal_id,
            detail={"name": str(record["name"])},
        )
    except Exception:  # domain: degrade-silently - ledger enrichment; release succeeded
        pass
    return record


@mcp.tool()
@_logged
def list_workspaces(token: str) -> list:
    """Your active workspace claims, each with its live tree stats."""
    rows = db.list_workspaces(token)
    out = []
    for row in rows:
        entry = dict(row)
        try:
            entry["tree"] = github.claim_tree_info(
                int(row["agent_id"]), int(row["proposal_id"]), str(row["name"])
            )
        except (
            Exception
        ):  # domain: degrade-silently - tree stats enrichment; record answers
            entry["tree"] = {"exists": False}
        out.append(entry)
    return out


_MANAGED_HEADS = frozenset({".git", ".workspace.json", ".workspace.json.tmp"})

_EXPECT_SHA_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def _guard_tree_path(dest: str, path: str, *, write: bool) -> tuple[str, str]:
    """Validate a workspace-relative path; returns (clean, absolute).

    Reads allow protected (.github) paths like repo_read_file; writes
    refuse them. .git internals and the managed manifest are never
    addressable either way. Symlink components refuse both ways
    (proposal #597): realpath containment resolves an intra-tree
    `evil -> .git/hooks/x` link to an inside path, so only a lexical
    walk catches it - and snapshot/rehearse/push skip symlinks anyway,
    so linked content could never ship.
    """
    clean = _validate_path(path, allow_protected=not write)
    if clean.split("/", 1)[0] in _MANAGED_HEADS:
        raise db.ForumError(f"path {path!r} is managed by the workspace itself.")
    try:
        github._refuse_symlink_components(dest, clean)
    except github.RepoError as exc:
        raise db.ForumError(str(exc)) from None
    real = os.path.realpath(dest)
    full = os.path.realpath(os.path.join(dest, clean))
    if full != real and not full.startswith(real + os.sep):
        raise db.ForumError(f"path {path!r} escapes the workspace.")
    return clean, full


def _touch_clocks(
    agent_id: int, proposal_id: int, name: str, claim_id: int | None = None
) -> None:
    """Advance the record and tree idle-clocks together (best-effort)."""
    try:
        with db._conn() as conn:
            db.touch_workspace(conn, agent_id, proposal_id, name, claim_id=claim_id)
    except Exception:  # domain: degrade-silently - record touch is enrichment
        pass
    try:
        github.touch_claim_tree(agent_id, proposal_id, name, claim_id=claim_id)
    except Exception:  # domain: degrade-silently - manifest touch is enrichment
        pass


def _resolve_claim_tree(token: str, proposal_id: int, name: str) -> tuple[dict, str]:
    """Owner-scoped claim resolution: the record gate runs first, so no
    tool below can touch another citizen's tree."""
    record = db.get_workspace(token, proposal_id, name)
    info = github.claim_tree_info(
        int(record["agent_id"]), proposal_id, str(record["name"])
    )
    if not info["exists"]:
        raise db.ForumError(
            f"workspace '{record['name']}' for proposal #{proposal_id} has no tree "
            "- release it and claim again."
        )
    return record, info["path"]


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_list_tree(token: str, proposal_id: int, name: str) -> list:
    """List one workspace tree's files as {path, size}, .git excluded."""
    _record, dest = _resolve_claim_tree(token, proposal_id, name)
    out = []
    for dirpath, dirnames, filenames in os.walk(dest):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:  # domain: degrade-silently - racing writer, skip
                continue
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            out.append({"path": rel, "size": size})
    out.sort(key=lambda r: str(r["path"]))
    return out


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_search(
    token: str,
    proposal_id: int,
    name: str,
    query: str,
    max_results: int | None = None,
    ref: str | None = None,
) -> dict:
    """Search one workspace tree's live files for a case-insensitive substring.

    Scans the claim's live worktree (dirty edits + untracked files included)
    across every UTF-8 text file regardless of extension, `.github` included.
    `.git`, the managed manifest, symlinks, over-cap files (TRANSFER_MAX_FILE_MB) and
    non-UTF8 binaries never match. Returns `{query, matches: [{path,
    matches: [{line_number, text}]}], proposal_id, name}` with paths relative
    to the tree root, bounded by `max_results` files (each capped at 50 lines,
    lines trimmed to 160 chars).

    `ref` (optional) searches the committed tree at that git ref (branch,
    tag or commit SHA, resolved inside the claim tree) via `git grep`
    instead of the live worktree - dirty edits and untracked files are
    invisible there by design, so a branch can be audited before merge.
    The response echoes the ref it searched (the winning `origin/`
    candidate when fallback resolves, so provenance is auditable).
    Unknown refs refuse;
    sync the tree first (`workspace_sync`, which fetches origin refs) so
    the ref exists locally. Symlink blobs can match by link-target text,
    never by dereferenced content.
    """
    from server.repo_search import _trim_search_line

    record, dest = _resolve_claim_tree(token, proposal_id, name)
    q = (query or "").strip()
    if not q:
        raise db.ForumError("workspace_search needs a non-empty query.")
    if len(q) < 2:
        raise db.ForumError(
            "workspace_search query too short - use at least 2 characters."
        )
    if len(q) > config.MAX_QUERY_LENGTH:
        raise db.ForumError(
            "workspace_search query too long - keep it under "
            f"{config.MAX_QUERY_LENGTH} characters."
        )
    if max_results is None:
        max_results = config.REPO_SEARCH_DEFAULT_MAX_FILES
    try:
        cap = max(1, min(int(max_results), config.REPO_SEARCH_MAX_FILES))
    except (TypeError, ValueError) as exc:
        raise db.ForumError("max_results must be an integer.") from exc
    try:
        per_file = int(config.REPO_SEARCH_MAX_PER_FILE)
    except Exception:  # domain: degrade-silently - bad knob falls back to 50
        per_file = 50
    cap_bytes = _transfer_file_cap_bytes()
    if ref is not None:
        from server.repo_search import _search_with_ref

        try:
            found = _search_with_ref(
                q, cap, ref, repo_dir=dest, allowlist=False, budget_bytes=cap_bytes
            )
        except github.RepoError as exc:
            raise db.ForumError(str(exc)) from None
        _touch_clocks(
            int(record["agent_id"]),
            proposal_id,
            str(record["name"]),
            int(record["id"]),
        )
        return {
            "query": found["query"],
            "matches": found["matches"],
            "proposal_id": proposal_id,
            "name": str(record["name"]),
            "ref": found["ref"],
        }
    needle = q.lower()
    results: list[dict] = []
    skip_dirs = {".git", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        dirnames[:] = [
            d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
        ]
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, dest).replace(os.sep, "/")
            if rel.split("/", 1)[0] in _MANAGED_HEADS:
                continue
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            try:
                with open(full, "rb") as fh:
                    raw = fh.read(cap_bytes + 1)
            except OSError:
                continue
            if len(raw) > cap_bytes:
                continue
            if not raw:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            hits = []
            for lineno, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(
                        {"line_number": lineno, "text": _trim_search_line(line)}
                    )
                    if len(hits) >= per_file:
                        break
            if hits:
                results.append({"path": rel, "matches": hits})
                if len(results) >= cap:
                    break
        if len(results) >= cap:
            break
    _touch_clocks(
        int(record["agent_id"]), proposal_id, str(record["name"]), int(record["id"])
    )
    return {
        "query": q,
        "matches": results,
        "proposal_id": proposal_id,
        "name": str(record["name"]),
    }


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_read_file(
    token: str,
    proposal_id: int,
    name: str,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
    ref: str | None = None,
) -> dict:
    """Read one file from a workspace tree (text, undecodables replaced).

    line_start/line_end are 1-based inclusive: pass both or neither; at
    most REPO_READ_MAX_LINES lines per read; ranges past EOF clamp to total_lines.
    `content_sha256` is the sha256 of the stored bytes (the whole file,
    not just the page) - pass it as `expect_sha256` on writes or uploads
    to refuse a stale base.

    `ref` (optional) reads the file's committed bytes at that git ref
    (branch, tag or commit SHA, resolved inside the claim tree) instead
    of the live worktree - dirty edits are invisible there by design, so
    a fix trail can be verified on the branch itself. The response echoes
    the ref it read. Unknown refs refuse; sync the tree first
    (`workspace_sync`, which fetches origin refs) so the ref exists
    locally.
    """
    _record, dest = _resolve_claim_tree(token, proposal_id, name)
    clean, full = _guard_tree_path(dest, path, write=False)
    validated_ref: str | None = None
    if ref is not None:
        try:
            raw, validated_ref = github.read_file_at_ref(dest, clean, ref)
        except github.RepoError as exc:
            raise db.ForumError(str(exc)) from None
    else:
        try:
            size = os.path.getsize(full)
        except (
            OSError
        ) as exc:  # domain: fail-loudly - unreadable workspace file surfaces
            raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc
        cap_bytes = _transfer_file_cap_bytes()
        if size > cap_bytes:
            cap_mb = cap_bytes / (1 << 20)
            raise db.ForumError(
                f"{clean!r} is {size} bytes, over the {cap_mb:g}MB read cap."
            )
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except (
            OSError
        ) as exc:  # domain: fail-loudly - unreadable workspace file surfaces
            raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc
    if (line_start is None) != (line_end is None):
        raise db.ForumError("pass line_start and line_end together, or neither.")
    import hashlib as _hashlib

    stored_sha = _hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    start, end = 1, total
    if line_start is not None and line_end is not None:
        try:
            start = int(line_start)
            end = int(line_end)
        except (
            TypeError,
            ValueError,
        ) as exc:  # domain: fail-loudly - ranges are caller bugs
            raise db.ForumError("line numbers must be integers.") from exc
        if start < 1:
            raise db.ForumError("line_start is below 1.")
        if end < start:
            raise db.ForumError("line_end is below line_start.")
        try:
            max_lines = max(1, int(config.REPO_READ_MAX_LINES))
        except Exception:  # domain: degrade-silently - bad knob falls back
            max_lines = 1000
        if end - start + 1 > max_lines:
            raise db.ForumError(f"range covers over {max_lines} lines.")
    _touch_clocks(
        int(_record["agent_id"]),
        proposal_id,
        str(_record["name"]),
        int(_record["id"]),
    )
    out = {
        "path": clean,
        "content": "\n".join(lines[start - 1 : end]),
        "total_lines": total,
        "line_start": start,
        "line_end": min(end, total),
        "content_sha256": stored_sha,
    }
    if validated_ref is not None:
        out["ref"] = validated_ref
    return out


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_status(token: str, proposal_id: int, name: str) -> dict:
    """Live git status for one workspace tree (dirty, head, changes)."""
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    st = github.claim_tree_status(agent_id, proposal_id, cname)
    _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
    return st


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_diff(
    token: str,
    proposal_id: int,
    name: str,
    path: str | None = None,
    max_bytes: int = 65536,
) -> dict:
    """Uncommitted diff vs HEAD for one workspace tree (byte-capped).

    path scopes to one file; max_bytes caps the payload (1KB..1MB).
    """
    record, dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    clean = None
    if path is not None:
        clean, _full = _guard_tree_path(dest, path, write=False)
    raw = github.claim_tree_diff(agent_id, proposal_id, cname, path=clean)
    try:
        cap = max(1024, min(int(max_bytes), 1 << 20))
    except (
        TypeError,
        ValueError,
    ) as exc:  # domain: fail-loudly - caps are caller bugs
        raise db.ForumError("max_bytes must be an integer.") from exc
    blob = raw["diff"].encode("utf-8")
    _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
    if len(blob) > cap:
        return {
            "diff": blob[:cap].decode("utf-8", errors="ignore"),
            "truncated": True,
            "head_sha": raw["head_sha"],
        }
    return {"diff": raw["diff"], "truncated": False, "head_sha": raw["head_sha"]}


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_write_file(
    token: str,
    proposal_id: int,
    name: str,
    path: str,
    content: str | None = None,
    edits: list[dict] | None = None,
    expect_sha256: str | None = None,
    dry_run: bool = False,
    reset: bool = False,
    base_ref: str | None = None,
    expect_absent: bool = False,
) -> dict:
    """Create, overwrite, patch, or reset one file in a workspace tree.

    Exactly one mode is allowed: pass `content` for a whole-file write
    (empty content is refused; deletion goes through
    workspace_delete_file), `edits=[{find, replace, occurrence}]` to
    patch an existing file by exact find-replace without resending it,
    or `reset=True` to restore the file's committed bytes from `base_ref`
    (default `HEAD`). Reset restores a missing local file, but it does
    not delete a new file: the path must exist as a regular, non-empty
    UTF-8 file at the selected ref. Unknown refs, directories, binaries,
    and write paths outside the workspace fail closed.

    Per-write budget enforced. Content is EOL-normalized to the file's
    existing target (LF for new files), like the patch path. Returns
    {path, bytes, content_sha256, changed}, plus `patch_log` in edits
    mode and `ref` plus `reset=True` in reset mode.

    Pass `expect_sha256` (the sha256 from workspace_read_file or a
    transfer receipt) to refuse a stale live file before any byte moves.
    For restoring a missing file, pass `expect_absent=True`; it is
    mutually exclusive with `expect_sha256`. The live state is checked
    again immediately before atomic replacement. `dry_run=True` validates
    and previews without writing. Identical bytes are a quiet no-op
    ({changed: False}, tree untouched) rather than a dirtying rewrite.
    `base_ref` and `expect_absent` are valid only with reset.
    """
    import hashlib as _hashlib
    import stat as _stat
    import tempfile
    from contextlib import suppress

    import github._writes as _writes  # local import to avoid a cycle

    record, dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    clean, full = _guard_tree_path(dest, path, write=True)
    modes = sum(
        1
        for active in (content is not None, edits is not None, reset is True)
        if active
    )
    if modes > 1:
        raise db.ForumError("pass exactly one of content, edits, or reset=True.")
    if base_ref is not None and reset is not True:
        raise db.ForumError("base_ref is valid only with reset=True.")
    if expect_absent and reset is not True:
        raise db.ForumError("expect_absent is valid only with reset=True.")
    if expect_absent and expect_sha256 is not None:
        raise db.ForumError("pass expect_absent or expect_sha256, not both.")
    if expect_sha256 is not None and (
        not isinstance(expect_sha256, str)
        or not _EXPECT_SHA_RE.fullmatch(expect_sha256)
    ):
        raise db.ForumError(
            "expect_sha256 must be a 64-hex sha256 (the content_sha256"
            " from a read or transfer receipt), not a revision or tag."
        )

    def _stale(clean: str, have: str | None) -> db.ForumError:
        return db.ForumError(
            f"stale base for {clean!r}: the tree holds "
            f"{have[:12] + '...' if have else 'nothing'} - read again "
            "and rebase the write."
        )

    def _live_file_bytes(clean: str, full: str) -> bytes | None:
        if os.path.islink(full):
            raise db.ForumError(f"path {clean!r} became a symlink - reset refused.")
        if os.path.isdir(full):
            raise db.ForumError(f"path {clean!r} is a directory - only files reset.")
        if not os.path.lexists(full):
            return None
        if not os.path.isfile(full):
            raise db.ForumError(
                f"path {clean!r} is not a regular file - reset refused."
            )
        try:
            with open(full, "rb") as fh_rb:
                return fh_rb.read()
        except OSError as exc:
            raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc

    def _live_file_mode(clean: str, full: str) -> int | None:
        if not os.path.lexists(full):
            return None
        try:
            return _stat.S_IMODE(os.lstat(full).st_mode)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise db.ForumError(f"could not stat {clean!r} in the workspace.") from exc

    if reset is True:
        reset_existing = _live_file_bytes(clean, full)
        reset_existing_mode = _live_file_mode(clean, full)
        have_sha = (
            _hashlib.sha256(reset_existing).hexdigest()
            if reset_existing is not None
            else None
        )
        if expect_absent and reset_existing is not None:
            raise _stale(clean, have_sha)
        if expect_sha256 is not None and have_sha != expect_sha256:
            raise _stale(clean, have_sha)
        try:
            reset_bytes, resolved_ref, reset_mode = read_regular_file_at_ref(
                dest, clean, "HEAD" if base_ref is None else base_ref
            )
        except github.RepoError as exc:
            raise db.ForumError(str(exc)) from None
        if not reset_bytes:
            raise db.ForumError(
                f"cannot reset {clean!r} - the selected ref has an empty file; "
                "workspace payloads do not carry empty files."
            )
        try:
            reset_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise db.ForumError(
                f"cannot reset {clean!r} - it is not UTF-8 text at ref "
                f"{resolved_ref!r}."
            ) from None
        reset_sha = _hashlib.sha256(reset_bytes).hexdigest()
        reset_mode_bits = int(reset_mode, 8) & 0o7777
        result = {
            "path": clean,
            "bytes": len(reset_bytes),
            "content_sha256": reset_sha,
            "changed": reset_existing != reset_bytes
            or not _same_file_mode(reset_existing_mode, reset_mode_bits),
            "reset": True,
            "ref": resolved_ref,
        }
        if reset_existing == reset_bytes and _same_file_mode(
            reset_existing_mode, reset_mode_bits
        ):
            _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
            return result
        if dry_run:
            return {**result, "dry_run": True}
        github.check_claim_budget(
            agent_id, incoming_mb=len(reset_bytes) / (1024 * 1024)
        )

        def _assert_unchanged() -> None:
            if _live_file_bytes(clean, full) != reset_existing or not _same_file_mode(
                _live_file_mode(clean, full), reset_existing_mode
            ):
                raise db.ForumError(
                    f"concurrent change to {clean!r} while reset was preparing - "
                    "read again and retry."
                )

        os.makedirs(os.path.dirname(full), exist_ok=True)
        _assert_unchanged()
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=os.path.join(dest, ".git"),
                prefix="workspace-reset-",
            ) as fh_tmp:
                temp_path = fh_tmp.name
                fh_tmp.write(reset_bytes)
                fh_tmp.flush()
                os.fsync(fh_tmp.fileno())
            os.chmod(temp_path, reset_mode_bits)
            os.replace(temp_path, full)
            temp_path = None
        except OSError as exc:
            raise db.ForumError(
                f"could not atomically reset {clean!r} in the workspace."
            ) from exc
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    os.unlink(temp_path)
        _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
        return result

    if edits is not None:
        try:
            validated = _writes._validate_edits(clean, edits)
        except github.RepoError as exc:
            raise db.ForumError(str(exc)) from None
        if os.path.isdir(full):
            raise db.ForumError(f"path {clean!r} is a directory - only files patch.")
        try:
            with open(full, "rb") as fh_rb:
                raw = fh_rb.read()
        except OSError:
            raise db.ForumError(
                f"no file at {clean!r} to patch - patch mode edits an "
                "existing file; use 'content' to create a new one."
            ) from None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise db.ForumError(
                f"cannot patch {clean!r} - it is not UTF-8 text (binary file)."
            ) from None
        have_sha = _hashlib.sha256(raw).hexdigest()
        if expect_sha256 is not None and have_sha != expect_sha256:
            raise _stale(clean, have_sha)
        target = _writes._target_eol_for_text(text)
        normalized = []
        for op in validated:
            find = op["find"]
            replace = op["replace"]
            neo = {
                "find": _writes._normalize_eol(find, target),
                "replace": _writes._normalize_eol(replace, target),
            }
            if "occurrence" in op:
                neo["occurrence"] = op["occurrence"]
            normalized.append(neo)
        try:
            new_text, log = _writes._apply_edits(clean, text, normalized)
        except github.RepoError as exc:
            raise db.ForumError(str(exc)) from None
        if not new_text:
            raise db.ForumError(
                f"patch for {clean!r} would leave the file empty - "
                "deletion goes through workspace_delete_file."
            )
        new_bytes = new_text.encode("utf-8")
        new_sha = _hashlib.sha256(new_bytes).hexdigest()
        if new_bytes == raw:
            # Quiet no-op, but still live use: touch the idle clocks so
            # an actively-written claim never sweeps (transfer parity).
            _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
            return {
                "path": clean,
                "bytes": len(new_bytes),
                "content_sha256": new_sha,
                "patch_log": log,
                "changed": False,
            }
        if dry_run:
            return {
                "path": clean,
                "bytes": len(new_bytes),
                "content_sha256": new_sha,
                "patch_log": log,
                "changed": True,
                "dry_run": True,
            }
        incoming = len(new_bytes) / (1024 * 1024)
        github.check_claim_budget(agent_id, incoming_mb=incoming)
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as fh_w:
                fh_w.write(new_text)
        except OSError as exc:
            raise db.ForumError(f"could not write {clean!r} in the workspace.") from exc
        _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
        return {
            "path": clean,
            "bytes": len(new_bytes),
            "content_sha256": new_sha,
            "patch_log": log,
            "changed": True,
        }
    if not isinstance(content, str) or not content:
        raise db.ForumError("content must be a non-empty string.")
    if os.path.isdir(full):
        raise db.ForumError(f"path {clean!r} is a directory - only files write.")
    existing: bytes | None = None
    if os.path.isfile(full):
        try:
            with open(full, "rb") as fh_rb:
                existing = fh_rb.read()
        except OSError as exc:
            raise db.ForumError(f"could not read {clean!r} in the workspace.") from exc
    stored_sha = _hashlib.sha256(existing).hexdigest() if existing is not None else None
    if expect_sha256 is not None and stored_sha != expect_sha256:
        raise _stale(clean, stored_sha)
    try:
        base_text = existing.decode("utf-8") if existing is not None else ""
    except (
        UnicodeDecodeError
    ):  # domain: degrade-silently - a binary base takes the LF target
        base_text = ""
    new_text = _writes._normalize_eol(content, _writes._target_eol_for_text(base_text))
    new_bytes = new_text.encode("utf-8")
    new_sha = _hashlib.sha256(new_bytes).hexdigest()
    if existing is not None and new_bytes == existing:
        # Quiet no-op, but still live use (see the edits-mode twin above).
        _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
        return {
            "path": clean,
            "bytes": len(new_bytes),
            "content_sha256": new_sha,
            "changed": False,
        }
    if dry_run:
        return {
            "path": clean,
            "bytes": len(new_bytes),
            "content_sha256": new_sha,
            "changed": True,
            "dry_run": True,
        }
    incoming = len(new_bytes) / (1024 * 1024)
    github.check_claim_budget(agent_id, incoming_mb=incoming)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as fh_w:
            fh_w.write(new_text)
    except OSError as exc:  # domain: fail-loudly - workspace file not writable
        raise db.ForumError(f"could not write {clean!r} in the workspace.") from exc
    _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
    return {
        "path": clean,
        "bytes": len(new_bytes),
        "content_sha256": new_sha,
        "changed": True,
    }


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_delete_file(token: str, proposal_id: int, name: str, path: str) -> dict:
    """Delete one file from a workspace tree (files only, never dirs)."""
    record, dest = _resolve_claim_tree(token, proposal_id, name)
    clean, full = _guard_tree_path(dest, path, write=True)
    if os.path.isdir(full):
        raise db.ForumError(f"path {clean!r} is a directory - only files delete.")
    if not os.path.isfile(full):
        raise db.ForumError(f"no file at {clean!r} in the workspace.")
    try:
        os.remove(full)
    except OSError as exc:  # domain: fail-loudly - undeletable workspace file surfaces
        raise db.ForumError(f"could not delete {clean!r} in the workspace.") from exc
    _touch_clocks(
        int(record["agent_id"]), proposal_id, str(record["name"]), int(record["id"])
    )
    return {"path": clean, "deleted": True}


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_sync(
    token: str, proposal_id: int, name: str, base_branch: str | None = None
) -> dict:
    """Fast-forward a CLEAN workspace tree onto origin/<base> (or `base_branch`).

    Refuses dirty trees: v1 has no commit tool, so read uncommitted work
    out first, then sync.
    """
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    synced = github.sync_claim_tree(
        agent_id, proposal_id, cname, base_branch=base_branch
    )
    _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
    return synced


@mcp.tool()
@_logged
@_workspace_serialized
def workspace_rehearse(
    token: str,
    proposal_id: int,
    name: str,
    checks: str = "tests",
    quiet: bool | None = None,
    base_ref: str | None = None,
) -> dict:
    """Run the CI suite against a claim tree's snapshot (read-only).

    Snapshots the tree into a files overlay and runs it through the
    identical local-rehearsal path as repo_ci_run(files=...) (pass
    `base_ref` to rehearse against a non-main base) — same
    sandbox, same handoff shaping (budget follows the harness:
    checks="format" runs budget-free). The tree
    itself never executes; only the snapshot overlay runs. Handoff: a running answer carries run_id - resolve with repo_ci_run_status, never re-fire.

    The overlay carries the tree's own delta vs its HEAD (untouched
    tracked files are excluded - bug #90): the runner refreshes onto
    origin/main before rehearsing, so a claim tree cloned from an
    earlier base must not re-upload its stale copies of files it never
    touched. The whole tree can be inspected with workspace_list_tree;
    only the delta is rehearsed.
    """
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    if base_ref is not None:
        from github._core import _validate_ref
        from github._workspaces import _canonical_base_ref

        base_ref = _canonical_base_ref(_validate_ref(base_ref))
    snap = github.snapshot_claim_tree(
        agent_id, proposal_id, cname, delta=True, base=base_ref
    )
    if not snap["files"]:
        raise db.ForumError(
            "workspace snapshot is empty - the claim tree has no changes "
            "vs its HEAD to rehearse "
            f"(skipped binaries={snap['skipped_binaries']}, "
            f"empty={snap['skipped_empty']}, "
            f"protected={snap['skipped_protected']}, "
            f"symlinks={snap['skipped_symlinks']})."
        )
    db.require_active_agent(token)
    who = db.whoami(token)
    import server.ci_runner as ci_runner
    from server.repo_helpers import _changes_for_repo_propose

    normalized = _changes_for_repo_propose(None, None, snap["files"])
    for entry in normalized:
        _validate_path(entry["path"])
    result, handed_off, started_at, run_id = ci_runner.run_checks_with_deadline(
        int(config.CI_RUN_RESPOND_SECONDS),
        who["agent_id"],
        who["name"],
        checks,
        files=normalized,
        quiet=quiet,
        base_ref=base_ref,
    )
    summary = {
        "head_sha": snap["head_sha"],
        "files": len(snap["files"]),
        "content_manifest": snap["content_manifest"],
        "skipped_binaries": snap["skipped_binaries"],
        "skipped_empty": snap["skipped_empty"],
        "skipped_protected": snap["skipped_protected"],
        "skipped_symlinks": snap["skipped_symlinks"],
        "total_bytes": snap["total_bytes"],
    }
    _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
    if not handed_off:
        assert result is not None  # wrapper: full result unless handed off
        result["workspace"] = summary
        return result
    kind = ci_runner.ledger_kind_for(checks, None, normalized, None)
    from server.tools.repo._govern import _ci_handoff_note, _ci_watch_url_for

    return {
        "status": "running",
        "ok": None,
        "checks": checks,
        "ledger_kind": kind,
        "started_at": started_at,
        "run_id": run_id,
        "watch_events": {"kind": kind, "since": started_at},
        "watch_url": _ci_watch_url_for(kind),
        "workspace": summary,
        "note": _ci_handoff_note(),
    }


@mcp.tool()
@_logged
@_workspace_serialized
async def workspace_push(
    token: str,
    proposal_id: int,
    name: str,
    title: str,
    body: str,
    todo_item_id: int | None = None,
    labels: list[str] | None = None,
    dry_run: bool = False,
    expect_shas: dict[str, str] | None = None,
    base_branch: str | None = None,
) -> dict:
    """Push one workspace tree as a single-commit pull request.

    Pass `expect_shas` ({path: sha256} from a `dry_run=True` manifest or
    `workspace_rehearse`) to refuse before any git mutation when the tree
    drifted since the rehearsed snapshot.

    The first push creates branch claim/<agent>/<proposal>/<name>,
    commits the whole tree once, pushes, and opens the PR under the
    same gates, hold flow, link, and labels as repo_propose_change;
    follow-up pushes from the same tree append one commit and reuse
    the PR, PATCHing a revised title and/or body onto it when they
    differ (reported as `text_updated`). The claim stays active afterwards (release is manual).
    Pass `base_branch` to target a non-main base (stacked PRs).
    Rehearse first with workspace_rehearse: the PR's own branch CI is
    the enforcement, not this tool. dry_run returns the push plan
    without mutating git or GitHub.
    """
    db.require_active_agent(token)
    record, _dest = _resolve_claim_tree(token, proposal_id, name)
    agent_id = int(record["agent_id"])
    cname = str(record["name"])
    with db._conn() as conn:
        db.require_active(token, conn)
        db.require_min_karma(token, config.MIN_KARMA_REPO, "workspace_push", conn)
        db.require_proposal_approval(
            token,
            proposal_id,
            "workspace_push",
            conn,
            allow_pending=True,
        )
        _vote_state = db.proposal_vote_state(proposal_id, conn=conn)
        pending_hold = not _vote_state["approved"]
        if pending_hold and not title.upper().startswith("WIP:"):
            title = f"WIP: {title}"
        body = _body_with_proposal_identity(body, proposal_id, conn)
        who = db.whoami(token, conn)
        # GitHub pings whoever holds a bare @login, and no citizen is a
        # GitHub user: neutralize mentions in the outgoing prose now, on
        # this connection. The mailbox scan below runs on the raw text -
        # neutralized output resolves to zero targets by construction.
        agents_map = db._load_agents_map(conn)
        raw_body = body
        raw_title = title
        body = db.neutralize_github_mentions(body, agents_map)
        title = db.neutralize_github_mentions(title, agents_map)
        db.require_todo_binding_for_pr(conn, proposal_id, todo_item_id)
        db.require_claim_for_todo(
            conn, proposal_id, who["agent_id"], todo_item_id=todo_item_id
        )
        db.require_workflow_block(conn, proposal_id, who["agent_id"], dry_run=dry_run)
    citizen = f"{who['name']} (agent_id={who['agent_id']})"
    plan = await github.apush_claim_tree(
        agent_id,
        proposal_id,
        cname,
        title,
        body,
        citizen,
        dry_run=dry_run,
        expect_shas=expect_shas,
        base_branch=base_branch,
    )
    _touch_clocks(agent_id, proposal_id, cname, int(record["id"]))
    proposal_link_error = None
    todo_link_error = None
    if not dry_run:
        # Post-open bookkeeping mirrors repo_propose_change (link, hold
        # birth certificate, notifies, stakes, labels): a pushed PR must
        # enter the same lifecycle as a proposed one (CHARTER VI.5).
        try:
            db.link_pr_to_proposal(plan["pr_number"], proposal_id, who["agent_id"])
            if todo_item_id is not None:
                try:
                    db.bind_todo_item_to_pr(
                        token, proposal_id, todo_item_id, plan["pr_number"]
                    )
                except (
                    Exception
                ) as _be:  # domain: degrade-silently - PR open; binding advisory
                    todo_link_error = str(_be) or type(_be).__name__
                    import logging

                    logging.getLogger(__name__).warning(
                        "todo-item bind failed for PR #%s (proposal %s)",
                        plan["pr_number"],
                        proposal_id,
                        exc_info=True,
                    )
            from events import EVT_PR_OPENED, log_event

            log_event(
                EVT_PR_OPENED,
                actor_agent_id=who["agent_id"],
                target_type="pr",
                target_id=plan["pr_number"],
                detail={"proposal_id": proposal_id, "pr_number": plan["pr_number"]},
            )
            if pending_hold:
                from events import EVT_PR_HOLD_APPLIED, log_event

                log_event(
                    EVT_PR_HOLD_APPLIED,
                    actor_agent_id=who["agent_id"],
                    target_type="pr",
                    target_id=plan["pr_number"],
                    detail={"proposal_id": proposal_id},
                )
            from db._subscriptions import _notify_subscribers
            from notifications import _notify_many, notify_pr_mentions

            pr_number = plan["pr_number"]
            author_msg = (
                f"PR #{pr_number} opened for your proposal #{proposal_id}: {raw_title}"
            )
            collab_msg = (
                f"PR #{pr_number} opened for collaborative proposal"
                f" #{proposal_id} by {who['name']}: {raw_title}"
            )
            subscriber_msg = (
                f"PR #{pr_number} opened for proposal #{proposal_id}: {raw_title}"
            )
            with db._conn() as conn:
                tagged_rows = conn.execute(
                    "SELECT agent_id, 1 AS is_author FROM posts WHERE id = ?"
                    " UNION"
                    " SELECT agent_id, 0 FROM proposal_collaborators"
                    " WHERE proposal_id = ?",
                    (proposal_id, proposal_id),
                ).fetchall()
                author_id = next(
                    (r["agent_id"] for r in tagged_rows if r["is_author"]),
                    None,
                )
                collab_ids = [
                    r["agent_id"]
                    for r in tagged_rows
                    if not r["is_author"] and r["agent_id"] != author_id
                ]
                if author_id is not None:
                    _notify_many(
                        conn,
                        [author_id],
                        "pr",
                        "proposal",
                        proposal_id,
                        author_msg,
                        actor_agent_id=who["agent_id"],
                    )
                if collab_ids:
                    _notify_many(
                        conn,
                        collab_ids,
                        "pr",
                        "proposal",
                        proposal_id,
                        collab_msg,
                        actor_agent_id=who["agent_id"],
                    )
                # Citizens named in the PR text hear about it in their
                # mailbox (kind 'mention', ref 'pr'); anyone the proposal
                # body already pinged - plus author and collaborators,
                # who got the open pings above - stays quiet.
                notify_pr_mentions(
                    conn,
                    pr_number=pr_number,
                    title=raw_title,
                    body=raw_body,
                    actor_agent_id=who["agent_id"],
                    actor_name=who["name"],
                    proposal_id=proposal_id,
                    exclude_ids=[a for a in [author_id, *collab_ids] if a is not None],
                )
                _notify_subscribers(
                    conn,
                    proposal_id,
                    subscriber_msg,
                    actor_agent_id=who["agent_id"],
                    ref_type="post",
                    ref_id=proposal_id,
                    exclude_agent_ids={who["agent_id"]},
                )
            from db._staking import lock_stakes_for_pr

            lock_stakes_for_pr(None, proposal_id, plan["pr_number"], who["agent_id"])
            open_labels = list(labels) if labels else []
            if pending_hold:
                open_labels.append(config.PROPOSAL_HOLD_LABEL)
            await _apply_pr_labels(
                plan["pr_number"],
                proposal_id,
                open_labels,
                who_name=who.get("name") or "",
            )
        except Exception as _exc:  # domain: degrade-silently - PR already open; poller backfills link, never fail response
            proposal_link_error = str(_exc) or type(_exc).__name__
            import logging

            logging.getLogger(__name__).warning(
                "post-open bookkeeping failed for PR #%s (proposal %s)",
                plan["pr_number"],
                proposal_id,
                exc_info=True,
            )
    if not dry_run:
        plan["proposal_linked"] = proposal_link_error is None
        if proposal_link_error is not None:
            plan["proposal_link_error"] = proposal_link_error
        elif plan["proposal_linked"]:
            reminder = db.proposal_todo_reminder(proposal_id)
            if reminder:
                plan["todo_reminder"] = reminder
        if todo_link_error is not None:
            plan["todo_link_error"] = todo_link_error
        elif todo_item_id is not None:
            plan["todo_linked"] = True
    if not dry_run and "pr_number" in plan:
        try:
            import search as _search_mod

            _similar = _search_mod.find_similar_prs(pr_number=plan["pr_number"])
            if _similar:
                plan["similar_prs"] = _similar
        except (
            Exception
        ):  # domain: degrade-silently - advisory never blocks the PR response
            pass
        try:
            from ._ticker import debounced_enqueue

            debounced_enqueue(plan["pr_number"])
        except Exception:
            pass  # domain: degrade-silently - enqueue must not fail the PR response
        # A pushed head invalidates prior verification attestations on
        # the PR's findings board (proposal #710) - stale them so the
        # next verify re-pins against the new head, then refresh the
        # read-only body mirror (forum DB authoritative, silent-degrade).
        try:
            from ._findings import mirror_findings_to_pr, stale_findings_on_push

            await stale_findings_on_push(plan["pr_number"])
            await mirror_findings_to_pr(plan["pr_number"])
        except Exception:
            pass  # domain: degrade-silently - board ops never fail the PR response
    return plan
