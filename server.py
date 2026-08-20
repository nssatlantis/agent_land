def _changes_for_repo_propose(
    file_path: str | None, content: str | None, files: list[dict] | None
) -> list[dict]:
    """Normalise repo_propose_change's call styles into the files list
    github.propose_change expects: either files=[{path, content}, ...],
    files=[{path, edits: [...]}, ...] to find-replace an existing file
    without sending its full content, or the single-file file_path/content
    shorthand; never more than one. Path hygiene itself is enforced per-file
    in github._validate_path."""
    # FastMCP sometimes passes list[dict] as raw JSON string; parse it
    if isinstance(files, str):
        import json
        try:
            files = json.loads(files)
        except json.JSONDecodeError as e:
            raise db.ForumError(f"files parameter is invalid JSON: {e}")
    if files is not None:
        if file_path is not None or content is not None:
            raise db.ForumError(
                "repo_propose_change takes either files=[...] or file_path and "
                "content, not both."
            )
        if not isinstance(files, list) or not files:
            raise db.ForumError(
                "files must be a non-empty list of {path, content} or "
                "{path, edits} entries."
            )
        changes: list[dict] = []
        seen: set[str] = set()
        for i, entry in enumerate(files):
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                    or not entry["path"].strip():
                raise db.ForumError(f"files[{i}] needs a non-empty 'path'.")
            path = entry["path"].strip()
            if path in seen:
                raise db.ForumError(f"duplicate path in files: {path!r}.")
            seen.add(path)
            has_content = "content" in entry
            has_edits = entry.get("edits") is not None
            if has_content and has_edits:
                raise db.ForumError(
                    f"files[{i}] has both 'content' and 'edits' for {path!r} - "
                    "use one or the other."
                )
            if not (has_content or has_edits):
                raise db.ForumError(
                    f"files[{i}] needs 'content' to write {path!r} or "
                    "'edits' to find-replace an existing file."
                )
            if has_content:
                if not isinstance(entry["content"], str) or entry["content"] == "":
                    raise db.ForumError(
                        f"files[{i}] needs a non-empty 'content' string for {path!r} "
                        "- an empty file is not a valid change."
                    )
                changes.append({"path": path, "content": entry["content"]})
            else:
                changes.append({"path": path, "edits": _validate_edits(path, entry["edits"], i)})
        return changes
    if not file_path or content is None:
        raise db.ForumError(
            "repo_propose_change needs file_path and content, or files=[...]."
        )
    if content == "":
        raise db.ForumError(
            "content must not be empty - an empty file is not a valid change."
        )
    return [{"path": file_path, "content": content}]


def _changes_for_repo_update(files: list[dict] | None) -> list[dict]:
    """Normalise repo_update_pr's files list into github.update_pr's change
    shape: {"path", "content"} to create/overwrite, {"path", "edits": [...]}
    to find-replace an existing file on the PR branch, or {"path",
    "delete": True} to remove. Path hygiene is enforced per-file in
    github._validate_path."""
    # FastMCP sometimes passes list[dict] as raw JSON string; parse it
    if isinstance(files, str):
        import json
        try:
            files = json.loads(files)
        except json.JSONDecodeError as e:
            raise db.ForumError(f"files parameter is invalid JSON: {e}")
    if files is None:
        return []
    if not isinstance(files, list) or not files:
        raise db.ForumError(
            "files must be a non-empty list of {path, content}, {path, edits} "
            "or {path, delete: True} entries."
        )
    changes: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(files):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                or not entry["path"].strip():
            raise db.ForumError(f"files[{i}] needs a non-empty 'path'.")
        path = entry["path"].strip()
        if path in seen:
            raise db.ForumError(f"duplicate path in files: {path!r}.")
        seen.add(path)
        has_content = "content" in entry
        has_edits = entry.get("edits") is not None
        is_delete = entry.get("delete") is True
        modes = sum(1 for flag in (has_content, has_edits, is_delete) if flag)
        if modes == 0:
            raise db.ForumError(
                f"files[{i}] needs 'content' to write {path!r}, 'edits' to "
                "find-replace it, or 'delete': True to remove it."
            )
        if modes > 1:
            raise db.ForumError(
                f"files[{i}] has more than one of 'content', 'edits' and "
                f"'delete' for {path!r} - use one."
            )
        if has_content:
            if not isinstance(entry["content"], str) or entry["content"] == "":
                raise db.ForumError(
                    f"files[{i}] needs a non-empty 'content' string for {path!r} "
                    "- an empty file is not a valid change; use 'delete': True "
                    "to remove it."
                )
            changes.append({"path": path, "content": entry["content"]})
        elif has_edits:
            changes.append({"path": path, "edits": _validate_edits(path, entry["edits"], i)})
        else:
            changes.append({"path": "delete": True})
    return changes