def _recent_tabs(kind: str | None, proposal_kind: str | None = None) -> str:
    """Tab filters for the recent page: All, Posts (ordinary posts only),
    Proposals (proposal posts), Replies and Votes - so the activity feed can
    separate ordinary posts from proposals, like the /posts kind tabs do."""
    tabs = []
    # Normalize: when kind="posts" and proposal_kind is None/empty, treat as "none"
    # so the "Posts" tab (which uses proposal_kind="none") gets highlighted.
    pk_for_active = proposal_kind
    if kind == "posts" and not proposal_kind:
        pk_for_active = "none"
    for key, label, pk in (
        (None, "All", None),
        ("posts", "Posts", "none"),
        ("posts", "Proposals", "proposal"),
        ("comments", "Replies", None),
        ("votes", "Votes", None),
    ):
        href = _recent_href(key, "newest", proposal_kind=pk)
        active = (key == kind and pk == pk_for_active)
        tabs.append(
            f'<a href="{href}"'
            + (' class="active" aria-current="page"' if active else "")
            + f">{label}</a>"
        )
    return '<div class="tabs">' + "".join(tabs) + "</div>"