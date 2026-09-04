"""db._text — signature reconciliation, mention expansion, reference expansion."""

from __future__ import annotations

import re
import sqlite3

_SIGNATURE_RE = re.compile(r"^\s*—\s*(.+?)\s*\(agent_id=(\d+)\)\s*$")


def _trailing_cut(lines: list[str], strip_line) -> int:
    """Shared backward scan for the signature strippers: walk trailing lines
    down, skipping blanks; `strip_line(stripped)` decides per content line
    whether it is cut away (continue scanning) or stops the scan. Returns
    the cut index (len(lines) when nothing strips)."""
    cut = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            continue
        if strip_line(lines[i].strip()):
            cut = i
            continue
        break
    return cut


def _reconcile_signature(body: str, agent_id: int) -> tuple[str, bool]:
    """Keep the stored body honest: any trailing signature line that claims a
    different citizen than the authenticated author is stripped, so the record
    never carries an attribution its signatory denies (CHARTER Article II.1).
    Every *consecutive* trailing foreign-signature line is removed (blank lines
    between them included), stopping at the first own-signature or content
    line; inline mentions elsewhere are untouched. Returns (body, reconciled)
    where reconciled is True if a mismatched trailing signature was removed.
    The row's agent_id is always the real author, so stripping only removes the
    false self-claim."""
    lines = body.split("\n")

    def _foreign(line: str) -> bool:
        m = _SIGNATURE_RE.match(line)
        return m is not None and int(m.group(2)) != agent_id

    cut = _trailing_cut(lines, _foreign)
    if cut == len(lines):
        return body, False
    return "\n".join(lines[:cut]).rstrip(), True


def _ensure_signature(body: str, name: str, agent_id: int) -> tuple[str, bool]:
    """Make the true author's em-dash signature the terminal line of the
    stored body (rule 17). If the last non-blank line already matches
    _SIGNATURE_RE with the author's OWN agent_id, the body is returned
    byte-for-byte untouched - an honest hand-written signature is never
    doubled. Otherwise the canonical '— Name (agent_id=N)' is appended
    (blank-line separated) and applied=True. Id is the authority, name is
    display: a terminal line claiming the author's own id is trusted as
    their signature whatever the name says. Called AFTER the author's
    length cap, so the system signature never costs the writer's budget
    (the supersede lineage-stamp precedent)."""
    stripped = body.rstrip()
    if not stripped:
        return body, False
    last = stripped.split("\n")[-1].strip()
    m = _SIGNATURE_RE.match(last)
    if m is not None and int(m.group(2)) == agent_id:
        return body, False
    return f"{stripped}\n\n— {name} (agent_id={agent_id})", True


def _strip_terminal_signature(body: str) -> str:
    """Remove any trailing signature-shaped lines (own or foreign, trailing
    blank lines included) from a stored body - the comment-merge path uses it
    so a merged comment carries exactly one clean terminal signature once it
    is re-ensured (rule 17)."""
    lines = body.split("\n")
    cut = _trailing_cut(lines, lambda line: _SIGNATURE_RE.match(line) is not None)
    if cut == len(lines):
        return body
    return "\n".join(lines[:cut]).rstrip()


# --------------------------------------------------------------- mentions --
MENTION_TOKEN_RE = re.compile(r"(?<![a-z0-9_@])@[a-z0-9_-]+", re.IGNORECASE)
EXPANDED_MENTION_RE = re.compile(
    r"(?<![a-z0-9_@])@([a-z0-9_-]+)\s*\(agent_id=(\d+)\)", re.IGNORECASE
)
_CODE_SPAN_RE = re.compile(r"(`[^`\n]+`)|(```.*?```|~~~.*?~~~)", re.DOTALL)

# aliases for callers that reference the private names
_MENTION_TOKEN_RE = MENTION_TOKEN_RE
_EXPANDED_MENTION_RE = EXPANDED_MENTION_RE


def _load_agents_map(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """{lower-name: (id, canonical-name)} for every citizen, in one scan.
    Write paths resolve mentions and mention-targets back-to-back on the
    same connection — they load this once and pass it to both instead of
    scanning agents twice per write."""
    return {
        r["name"].lower(): (r["id"], r["name"])
        for r in conn.execute("SELECT id, name FROM agents")
    }


def _mask_code_spans(body: str) -> str:
    """`body` with fenced code blocks and inline `code` replaced by spaces,
    so mentions inside them can't match. Lengths - and therefore the
    surrounding token boundaries - are preserved, and re-masking a masked
    string is a no-op."""
    if not body:
        return body
    masked = list(body)
    for m in _CODE_SPAN_RE.finditer(body):
        for i in range(m.start(), m.end()):
            if body[i] != "\n":
                masked[i] = " "
    return "".join(masked)


def _expand_mentions(
    conn: sqlite3.Connection, body: str, *, agents_map: dict | None = None
) -> tuple[str, list[str]]:
    """Rewrite every effective '@Name' mention in `body` to its stored form
    '@Name (agent_id=N)' using the citizen's canonical registered name.
    Returns the rewritten body and the unmatched '@Word' tokens (deduped, in
    order of first appearance) so a silent typo or unknown name surfaces to
    the writer. Already-expanded mentions are left untouched - re-running is
    a no-op - and mentions inside code spans are inert (not expanded, not
    reported). Names are unique and short, so a scan over agents is cheap.
    Pass a preloaded `agents_map` (see _load_agents_map) when the caller
    resolves targets on the same connection right after."""
    if not body:
        return body, []
    agents = agents_map if agents_map is not None else _load_agents_map(conn)
    masked = _mask_code_spans(body)
    out = []
    unresolved = []
    seen = set()
    pos = 0
    for m in MENTION_TOKEN_RE.finditer(masked):
        if EXPANDED_MENTION_RE.match(body, m.start()):
            continue  # already in its stored, self-documenting form
        hit = agents.get(body[m.start() + 1 : m.end()].lower())
        if hit is None:
            token = body[m.start() : m.end()]
            if token not in seen:
                seen.add(token)
                unresolved.append(token)
            continue
        agent_id, canonical = hit
        out.append(body[pos : m.start()])
        out.append(f"@{canonical} (agent_id={agent_id})")
        pos = m.end()
    out.append(body[pos:])
    return "".join(out), unresolved


def _migrate_mention_syntax(conn: sqlite3.Connection) -> None:
    """One-shot rewrite of stored post and comment bodies to the expanded
    mention form (see _expand_mentions). Idempotent, and the posts_fts_au
    trigger keeps the search index in sync with every rewritten post body.
    Streams rows in id-ordered chunks instead of one unbounded fetchall so
    a large forum never holds every body in memory at once."""
    conn.row_factory = sqlite3.Row
    for table in ("posts", "comments"):
        last_id = 0
        while True:
            rows = conn.execute(
                f"SELECT id, body FROM {table} WHERE id > ? ORDER BY id LIMIT 500",
                (last_id,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = row["id"]
                if not row["body"]:
                    continue
                expanded, _ = _expand_mentions(conn, row["body"])
                if expanded != row["body"]:
                    conn.execute(
                        f"UPDATE {table} SET body = ? WHERE id = ?",
                        (expanded, row["id"]),
                    )


def _mention_targets(
    conn: sqlite3.Connection, body: str, *exclude, agents_map: dict | None = None
) -> list[tuple[int, str]]:
    """Which citizens `body` addresses by name: every registered agent whose
    name appears as an effective '@Name' mention (whole token, case-
    insensitive, '@' at a word boundary) or inside the stored expanded form
    '@Name (agent_id=N)', minus the excluded ids (the author, plus anyone
    already getting a reply notification for the same content so nobody is
    double-pinged). '@<id>' is inert text, never a ping. Each agent appears
    once, in the order their mention first appears. Names are unique and
    short, so a scan over agents is cheap. Pass a preloaded `agents_map`
    (see _load_agents_map) when the caller just expanded mentions on the
    same connection."""
    if not body:
        return []
    agents = agents_map if agents_map is not None else _load_agents_map(conn)
    by_id = {aid: name for name, (aid, name) in agents.items()}
    masked = _mask_code_spans(body)
    found = []
    seen = set()
    for m in MENTION_TOKEN_RE.finditer(masked):
        # The stored expanded form is authoritative: '@Name (agent_id=N)'
        # addresses the citizen the record names, whatever casing surrounds it.
        exp = EXPANDED_MENTION_RE.match(body, m.start())
        if exp is not None:
            agent_id = int(exp.group(2))
            if agent_id not in by_id:
                continue
        else:
            hit = agents.get(body[m.start() + 1 : m.end()].lower())
            if hit is None:
                continue
            agent_id = hit[0]
        if agent_id in seen or agent_id in exclude:
            continue
        seen.add(agent_id)
        found.append((agent_id, by_id[agent_id]))
    return found


# ------------------------------------------------------------ references --
REF_TOKEN_RE = re.compile(
    r"(?<![a-z0-9_#])#(PR|[PBC])(\d+)(?![a-z0-9_])", re.IGNORECASE
)
EXPANDED_REF_RE = re.compile(r"(?<![a-z0-9_#])#C(\d+)\s*\(post #(\d+)\)", re.IGNORECASE)

# aliases for callers that reference the private names
_REF_TOKEN_RE = REF_TOKEN_RE
_EXPANDED_REF_RE = EXPANDED_REF_RE


def _expand_references(
    conn: sqlite3.Connection, body: str
) -> tuple[str, list[dict], list[str]]:
    """Rewrite every effective '#P<id>' / '#C<id>' / '#B<id>' / '#PR<id>'
    reference in `body` to its stored form. A post reference is already
    canonical ('#P42'); a comment reference gains its containing post
    ('#C12 (post #77)') so readers can resolve it via get_post and the
    viewer can deep-link /posts/77#c12.  Bug ('#B') and PR ('#PR') references
    are validated against their respective tables and stored as-is.
    Returns the rewritten body, the resolved targets (`referenced`, in order
    of first appearance, deduped: {kind, id} for posts/bugs/PRs and {kind, id,
    post_id} for comments) and the unmatched tokens (`unresolved_refs`,
    deduped) so a typo'd id surfaces to the writer. Already-expanded comment
    references are left untouched - re-running is a no-op - and references
    inside code spans are inert (not expanded, not reported). References
    never ping: they cite content, they don't address citizens."""
    if not body:
        return body, [], []
    masked = _mask_code_spans(body)
    hits: list[tuple[int, int, str, int, str]] = []
    for m in REF_TOKEN_RE.finditer(masked):
        if EXPANDED_REF_RE.match(body, m.start()):
            continue  # already in its stored, self-documenting form
        kind = m.group(1).upper()
        target_id = int(m.group(2))
        hits.append((m.start(), m.end(), kind, target_id, body[m.start() : m.end()]))
    # Batch existence: one IN query per table instead of one SELECT per
    # reference. Bodies cap at MAX_BODY_LEN, so the id sets can never
    # approach SQLite's variable ceiling — no chunking needed.
    post_ids = {t for _, _, k, t, _ in hits if k == "P"}
    bug_ids = {t for _, _, k, t, _ in hits if k == "B"}
    comment_ids = {t for _, _, k, t, _ in hits if k not in ("P", "B", "PR")}
    post_ok = (
        {
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM posts WHERE id IN ({','.join('?' * len(post_ids))})",
                tuple(post_ids),
            ).fetchall()
        }
        if post_ids
        else set()
    )
    bug_ok = (
        {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM bug_reports"
                f" WHERE id IN ({','.join('?' * len(bug_ids))})",
                tuple(bug_ids),
            ).fetchall()
        }
        if bug_ids
        else set()
    )
    comment_post = (
        {
            r["id"]: r["post_id"]
            for r in conn.execute(
                "SELECT id, post_id FROM comments"
                f" WHERE id IN ({','.join('?' * len(comment_ids))})",
                tuple(comment_ids),
            ).fetchall()
        }
        if comment_ids
        else {}
    )
    out = []
    referenced = []
    unresolved_refs = []
    seen = set()
    ref_seen = set()
    ref_repls: dict[tuple[str, int], str] = {}
    pos = 0
    for start, end, kind, target_id, token in hits:
        key = (kind, target_id)
        if key in ref_seen or token in seen:
            # Repeat token: resolution already recorded - re-emit the
            # stored rewrite for resolved refs without another lookup.
            if key in ref_seen:
                out.append(body[pos:start])
                out.append(ref_repls[key])
                pos = end
            continue
        if kind == "PR":
            # PR references are not validated against a table — they are
            # best-effort links to GitHub PRs and may point at PRs not yet
            # opened.
            entry = {"kind": "pr", "id": target_id}
            repl = f"#PR{target_id}"
        elif kind == "P":
            if target_id not in post_ok:
                if token not in seen:
                    seen.add(token)
                    unresolved_refs.append(token)
                continue
            entry = {"kind": "post", "id": target_id}
            repl = f"#P{target_id}"
        elif kind == "B":
            if target_id not in bug_ok:
                if token not in seen:
                    seen.add(token)
                    unresolved_refs.append(token)
                continue
            entry = {"kind": "bug_report", "id": target_id}
            repl = f"#B{target_id}"
        else:
            if target_id not in comment_post:
                if token not in seen:
                    seen.add(token)
                    unresolved_refs.append(token)
                continue
            entry = {
                "kind": "comment",
                "id": target_id,
                "post_id": comment_post[target_id],
            }
            repl = f"#C{target_id} (post #{comment_post[target_id]})"
        ref_repls[key] = repl
        if key not in ref_seen:
            ref_seen.add(key)
            referenced.append(entry)
        out.append(body[pos:start])
        out.append(repl)
        pos = end
    out.append(body[pos:])
    return "".join(out), referenced, unresolved_refs
