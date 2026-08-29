def _stake_locks_detail(stake_id: int) -> str:
    """Render the drill-down detail for a stake's locked stakes."""
    locks = list_stake_locks(stake_id)
    if not locks:
        return ""
    rows = []
    for lk in locks:
        status = lk["status"]
        status_cls = {
            "locked": "stake-lock-locked",
            "paid": "stake-lock-paid",
            "refunded": "stake-lock-refunded",
        }.get(status, "")
        agent = esc(lk.get("agent_id") or "system")
        rows.append(
            f'<div class="stake-lock-row {status_cls}">'
            f'<span class="stake-lock-status">{status}</span>'
            f'<a href="/posts/{lk["pr_number"]}" class="stake-lock-pr">#PR {lk["pr_number"]}</a>'
            f'<span class="stake-lock-agent">{agent}</span>'
            f'<span class="stake-lock-amount">{lk["amount"]}</span>'
            f'<span class="stake-lock-ts">{_human_ts(lk["created_at"])}</span>'
            f'</div>'
        )
    return '<div class="stake-lock-list">' + "".join(rows) + '</div>'