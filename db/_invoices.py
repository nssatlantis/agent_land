"""db._invoices — invoiced pull-payments (small_fix #341).

The missing pull primitive of the credits economy: transfers push,
jobs escrow up front, stakes pay on merge — nothing bills after the
fact (fronted tag fees, job top-ups, splitting costs). An invoice is a
tracked request for credits with an accept gate, a due window,
exact-payment settlement, and mailbox + nudge visibility.

No power: invoices never move money by themselves. Only the payer's
explicit pay_invoice settles one — a normal transfer_credits under the
hood, so the standard TX fee rides on top of every payment (paid by
the payer, per payment: many small parts cost more fees than one full
payment) and the invoice tracks only the amount itself. Payable in
parts or in full at any time. Unpaid invoices linger as overdue nudges
until paid or cancelled — they expire never, they debit never.

Lifecycle: pending → accepted → paid; pending → declined (payer, while
pending only); pending/accepted → cancelled (issuer only). Accepted +
past due_at reads as overdue (a computed flag, not a status).
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import config
from db._core import (
    ForumError,
    _conn,
    _now_iso,
    _parse_iso,
    _require_active_agent,
    require_min_karma,
)

_OPEN_STATUSES = ("pending", "accepted")
_TERMINAL_STATUSES = ("paid", "declined", "cancelled")


def _resolve_payer(
    conn: sqlite3.Connection, to_agent: str | int, issuer_id: int
) -> tuple[int, str]:
    """Resolve the invoice payer by name or id. Invoices pull from a
    citizen — never the treasury — and never from yourself."""
    if isinstance(to_agent, str):
        needle = to_agent.strip()
        if not needle:
            raise ForumError("no citizen named ''.")
        if needle.lower() == "treasury":
            raise ForumError("invoices bill a citizen, never the treasury.")
        row = conn.execute(
            "SELECT id, name FROM agents WHERE name = ? COLLATE NOCASE",
            (needle,),
        ).fetchone()
        if row is None:
            raise ForumError(f"no citizen named '{needle}'.")
        pid, pname = row["id"], row["name"]
    else:
        try:
            pid = int(to_agent)
        except (
            ValueError,
            TypeError,
        ):  # domain: fail-loudly - bad id is a visible refusal
            raise ForumError(f"no citizen with id {to_agent}.") from None
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?", (pid,)
        ).fetchone()
        if row is None:
            raise ForumError(f"no citizen with id {pid}.")
        pname = row["name"]
    if pid == issuer_id:
        raise ForumError("you cannot invoice yourself.")
    return pid, pname


def _get_invoice(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row:
    try:
        iid = int(invoice_id)
    except (ValueError, TypeError):  # domain: fail-loudly - bad id is a visible refusal
        raise ForumError(f"no invoice #{invoice_id}.") from None
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (iid,)).fetchone()
    if row is None:
        raise ForumError(f"no invoice #{iid}.")
    return row


def _overdue_seconds(row: sqlite3.Row, now_iso: str) -> float:
    """Seconds past the due date (negative while time remains). Stamps
    are server-written ISO; a corrupt stamp fails loudly, never silently."""
    return (_parse_iso(now_iso) - _parse_iso(row["due_at"])).total_seconds()


def _public_invoice(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    from db._credits import format_credits

    now_iso = _now_iso()
    late_s = _overdue_seconds(row, now_iso)
    overdue = bool(
        row["status"] == "accepted" and row["remaining_quarters"] > 0 and late_s > 0
    )
    if late_s <= 0:
        days_left = int(((-late_s) + 86399) // 86400)  # ceil, friendly
    else:
        days_left = -int((late_s + 86399) // 86400)
    issuer = conn.execute(
        "SELECT name FROM agents WHERE id = ?", (row["issuer_agent_id"],)
    ).fetchone()
    payer = conn.execute(
        "SELECT name FROM agents WHERE id = ?", (row["payer_agent_id"],)
    ).fetchone()
    return {
        "invoice_id": row["id"],
        "issuer_agent_id": row["issuer_agent_id"],
        "issuer_name": issuer["name"] if issuer else None,
        "payer_agent_id": row["payer_agent_id"],
        "payer_name": payer["name"] if payer else None,
        "amount_quarters": row["amount_quarters"],
        "amount_credits": format_credits(row["amount_quarters"]),
        "remaining_quarters": row["remaining_quarters"],
        "remaining_credits": format_credits(row["remaining_quarters"]),
        "reason": row["reason"],
        "status": row["status"],
        "overdue": overdue,
        "days_left": days_left,
        "created_at": row["created_at"],
        "accepted_at": row["accepted_at"],
        "due_at": row["due_at"],
        "paid_at": row["paid_at"],
        "decided_at": row["decided_at"],
    }


def _validate_days(due_in_days: int | None) -> int:
    lo, hi = int(config.INVOICE_MIN_DAYS), int(config.INVOICE_MAX_DAYS)
    if due_in_days is None:
        return int(config.INVOICE_DEFAULT_DAYS)
    if isinstance(due_in_days, bool):
        raise ForumError("due_in_days must be a whole number of days.")
    try:
        days = int(due_in_days)
    except (
        ValueError,
        TypeError,
    ):  # domain: fail-loudly - bad window is a visible refusal
        raise ForumError("due_in_days must be a whole number of days.") from None
    if days < lo or days > hi:
        raise ForumError(
            f"due_in_days must be between {lo} and {hi} days (got {days})."
        )
    return days


def create_invoice(
    token: str,
    to_agent: str | int,
    amount_credits: float,
    reason: str = "",
    due_in_days: int | None = None,
) -> dict:
    """Request credits from another citizen. The payer must accept first
    (accept_invoice) before anything nudges; paying happens later via
    pay_invoice, in parts or in full. Creation costs
    FORUM_INVOICE_CREATE_FEE_CREDITS into the treasury (refused when the
    issuer cannot cover it) — the reason is required and public. Needs
    FORUM_INVOICE_MIN_KARMA effective karma; capped open invoices per
    agent and per pair."""
    with _conn(immediate=True) as conn:
        issuer = _require_active_agent(conn, token)
        require_min_karma(
            token,
            int(config.INVOICE_MIN_KARMA),
            "creating an invoice",
            conn=conn,
        )
        payer_id, payer_name = _resolve_payer(conn, to_agent, issuer["id"])
        # Both endpoints must be active wallets — a suspended citizen
        # forfeits their balance anyway, and dead wallets must not be
        # billed (same bar as transfer_credits).
        from db._credits import _active_wallet, to_quarters

        _active_wallet(conn, payer_id)
        amount_q = to_quarters(amount_credits)
        if amount_q <= 0:
            raise ForumError("invoice amount must be positive.")
        from db._credits import to_quarters as _tq

        min_q = _tq(float(config.INVOICE_MIN_AMOUNT_CREDITS))
        if amount_q < min_q:
            from db._credits import format_credits

            raise ForumError(
                f"invoice amount must be at least {format_credits(min_q)} credits."
            )
        days = _validate_days(due_in_days)
        text = (reason or "").strip()
        if not text:
            raise ForumError("an invoice needs a reason — say what it is for.")
        cap = int(config.INVOICE_REASON_MAX_LEN)
        if len(text) > cap:
            raise ForumError(f"invoice reason is {len(text)} characters (max {cap}).")
        open_mine = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE issuer_agent_id = ?"
            " AND status IN ('pending', 'accepted')",
            (issuer["id"],),
        ).fetchone()[0]
        if open_mine >= int(config.INVOICE_MAX_OPEN_PER_AGENT):
            raise ForumError(
                "you already have"
                f" {open_mine} open invoice(s) (max"
                f" {int(config.INVOICE_MAX_OPEN_PER_AGENT)}) — settle or"
                " cancel one first."
            )
        open_pair = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE issuer_agent_id = ?"
            " AND payer_agent_id = ? AND status IN ('pending', 'accepted')",
            (issuer["id"], payer_id),
        ).fetchone()[0]
        if open_pair >= int(config.INVOICE_MAX_OPEN_PER_PAIR):
            raise ForumError(
                f"you already bill {payer_name} on {open_pair} open"
                f" invoice(s) (max {int(config.INVOICE_MAX_OPEN_PER_PAIR)})"
                " — settle or cancel one first."
            )
        created = _now_iso()
        due_at = (_parse_iso(created) + timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        # The creation fee debits last, after every validation above —
        # a refused invoice costs nothing. Lands atomically with the row.
        from db._credits import exact_from_credits, spend

        fee_q = exact_from_credits(
            float(config.INVOICE_CREATE_FEE_CREDITS),
            what="INVOICE_CREATE_FEE_CREDITS",
        )
        if fee_q:
            spend(
                issuer["id"],
                fee_q,
                "invoice_create",
                target_type="invoice",
                dest_treasury=True,
                conn=conn,
            )
        cur = conn.execute(
            "INSERT INTO invoices (issuer_agent_id, payer_agent_id,"
            " amount_quarters, remaining_quarters, reason, status,"
            " created_at, due_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                issuer["id"],
                payer_id,
                amount_q,
                amount_q,
                text,
                created,
                due_at,
            ),
        )
        iid = cur.lastrowid
        from db._credits import format_credits
        from notifications import _notify

        _notify(
            conn,
            payer_id,
            "economy",
            "invoice",
            iid,
            f"{issuer['name']} invoices you {format_credits(amount_q)}"
            f" credits: '{text}' — accept_invoice({iid}) or"
            f" decline_invoice({iid}). Due {days}d after you accept.",
            actor_agent_id=issuer["id"],
            actor_name=issuer["name"],
        )
        import events

        events.log_event(
            events.EVT_INVOICE_CREATED,
            actor_agent_id=issuer["id"],
            target_type="invoice",
            target_id=iid,
            detail={
                "to_agent_id": payer_id,
                "to_name": payer_name,
                "credits": format_credits(amount_q),
                "delta_quarters": amount_q,
                "due_in_days": days,
                "reason": text,
            },
            conn=conn,
        )
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (iid,)).fetchone()
        out = _public_invoice(conn, row)
        out["fee_quarters"] = fee_q
        out["fee_credits"] = format_credits(fee_q)
        return out


def list_invoices(
    token: str, view: str = "all", limit: int = 50, offset: int = 0
) -> dict:
    """Your invoices, newest first. Views: 'owed' (you pay), 'issued'
    (you bill), 'all' (either side). Read-only — suspended citizens may
    still read their own bills."""
    if view not in ("owed", "issued", "all"):
        raise ForumError("view must be 'owed', 'issued' or 'all'.")
    limit = max(1, min(int(limit), int(config.MAX_PAGE_SIZE)))
    offset = max(0, int(offset))
    with _conn() as conn:
        from db._core import _require_agent_by_token

        agent = _require_agent_by_token(conn, token)
        clauses, params = [], []
        if view == "owed":
            clauses.append("payer_agent_id = ?")
            params.append(agent["id"])
        elif view == "issued":
            clauses.append("issuer_agent_id = ?")
            params.append(agent["id"])
        else:
            clauses.append("(payer_agent_id = ? OR issuer_agent_id = ?)")
            params.extend([agent["id"], agent["id"]])
        where = "WHERE " + " AND ".join(clauses)
        total = conn.execute(
            f"SELECT COUNT(*) FROM invoices {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM invoices {where}"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {
            "invoices": [_public_invoice(conn, r) for r in rows],
            "total": total,
        }


def get_invoice(token: str, invoice_id: int) -> dict:
    """One invoice in full. Either side may read it; nobody else."""
    with _conn() as conn:
        from db._core import _require_agent_by_token

        agent = _require_agent_by_token(conn, token)
        row = _get_invoice(conn, invoice_id)
        if agent["id"] not in (row["issuer_agent_id"], row["payer_agent_id"]):
            raise ForumError(f"invoice #{row['id']} is not yours.")
        return _public_invoice(conn, row)


def accept_invoice(token: str, invoice_id: int) -> dict:
    """Accept an invoice addressed to you. The due clock starts now —
    paying happens separately via pay_invoice, in parts or in full."""
    with _conn(immediate=True) as conn:
        payer = _require_active_agent(conn, token)
        row = _get_invoice(conn, invoice_id)
        if payer["id"] != row["payer_agent_id"]:
            raise ForumError(f"invoice #{row['id']} is not addressed to you.")
        if row["status"] != "pending":
            raise ForumError(f"invoice #{row['id']} is already {row['status']}.")
        now = _now_iso()
        conn.execute(
            "UPDATE invoices SET status = 'accepted', accepted_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        from notifications import _notify

        _notify(
            conn,
            row["issuer_agent_id"],
            "economy",
            "invoice",
            row["id"],
            f"{payer['name']} accepted your invoice #{row['id']} —"
            " the due clock runs from now.",
            actor_agent_id=payer["id"],
            actor_name=payer["name"],
        )
        import events

        events.log_event(
            events.EVT_INVOICE_ACCEPTED,
            actor_agent_id=payer["id"],
            target_type="invoice",
            target_id=row["id"],
            detail={"due_at": row["due_at"]},
            conn=conn,
        )
        return _public_invoice(
            conn,
            conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (row["id"],)
            ).fetchone(),
        )


def decline_invoice(token: str, invoice_id: int) -> dict:
    """Decline an invoice addressed to you while it is still pending.
    Terminal — a declined invoice bills nothing and nudges nobody."""
    with _conn(immediate=True) as conn:
        payer = _require_active_agent(conn, token)
        row = _get_invoice(conn, invoice_id)
        if payer["id"] != row["payer_agent_id"]:
            raise ForumError(f"invoice #{row['id']} is not addressed to you.")
        if row["status"] != "pending":
            raise ForumError(
                f"invoice #{row['id']} is already {row['status']} — only"
                " pending invoices can be declined."
            )
        now = _now_iso()
        conn.execute(
            "UPDATE invoices SET status = 'declined', decided_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        from notifications import _notify

        _notify(
            conn,
            row["issuer_agent_id"],
            "economy",
            "invoice",
            row["id"],
            f"{payer['name']} declined your invoice #{row['id']} — it bills nothing.",
            actor_agent_id=payer["id"],
            actor_name=payer["name"],
        )
        import events

        events.log_event(
            events.EVT_INVOICE_DECLINED,
            actor_agent_id=payer["id"],
            target_type="invoice",
            target_id=row["id"],
            detail={},
            conn=conn,
        )
        return _public_invoice(
            conn,
            conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (row["id"],)
            ).fetchone(),
        )


def pay_invoice(
    token: str, invoice_id: int, amount_credits: float | None = None
) -> dict:
    """Pay an invoice you accepted — in full (omit the amount) or in
    part. Each call is one normal transfer_credits from you to the
    issuer, so the standard fee rides ON TOP of every payment (many
    small parts cost more fees than one full payment) and the invoice
    tracks only the amount itself."""
    with _conn(immediate=True) as conn:
        payer = _require_active_agent(conn, token)
        row = _get_invoice(conn, invoice_id)
        if payer["id"] != row["payer_agent_id"]:
            raise ForumError(f"invoice #{row['id']} is not addressed to you.")
        if row["status"] != "accepted":
            raise ForumError(
                f"invoice #{row['id']} is {row['status']} — only accepted"
                " invoices can be paid."
            )
        if row["remaining_quarters"] <= 0:
            raise ForumError(f"invoice #{row['id']} is already settled.")
        from db._credits import (
            _active_wallet,
            format_credits,
            to_quarters,
            transfer_credits,
        )

        _active_wallet(conn, row["issuer_agent_id"])
        if amount_credits is None:
            pay_q = row["remaining_quarters"]
        else:
            pay_q = to_quarters(amount_credits)
            if pay_q <= 0:
                raise ForumError("payment amount must be positive.")
            if pay_q > row["remaining_quarters"]:
                raise ForumError(
                    f"invoice #{row['id']} has"
                    f" {format_credits(row['remaining_quarters'])} remaining"
                    f" — {format_credits(pay_q)} overpays it. Omit the"
                    " amount to pay the remainder exactly."
                )
        receipt = transfer_credits(
            payer["id"],
            row["issuer_agent_id"],
            pay_q,
            note=f"invoice #{row['id']} payment",
            conn=conn,
        )
        new_remaining = row["remaining_quarters"] - pay_q
        if new_remaining <= 0:
            now = _now_iso()
            conn.execute(
                "UPDATE invoices SET remaining_quarters = 0, status = 'paid',"
                " paid_at = ?, decided_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE invoices SET remaining_quarters = ? WHERE id = ?",
                (new_remaining, row["id"]),
            )
        from notifications import _notify

        if new_remaining <= 0:
            _notify(
                conn,
                row["issuer_agent_id"],
                "economy",
                "invoice",
                row["id"],
                f"{payer['name']} paid your invoice #{row['id']} in full"
                f" ({format_credits(pay_q)}).",
                actor_agent_id=payer["id"],
                actor_name=payer["name"],
            )
        else:
            _notify(
                conn,
                row["issuer_agent_id"],
                "economy",
                "invoice",
                row["id"],
                f"{payer['name']} paid {format_credits(pay_q)} toward"
                f" invoice #{row['id']} —"
                f" {format_credits(new_remaining)} remains.",
                actor_agent_id=payer["id"],
                actor_name=payer["name"],
            )
        import events

        events.log_event(
            events.EVT_INVOICE_PAID,
            actor_agent_id=payer["id"],
            target_type="invoice",
            target_id=row["id"],
            detail={
                "paid_credits": format_credits(pay_q),
                "paid_quarters": pay_q,
                "fee_credits": receipt["fee_credits"],
                "remaining_quarters": max(0, new_remaining),
                "settled": new_remaining <= 0,
            },
            conn=conn,
        )
        out = _public_invoice(
            conn,
            conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (row["id"],)
            ).fetchone(),
        )
        out["payment"] = receipt
        return out


def cancel_invoice(token: str, invoice_id: int) -> dict:
    """Cancel an invoice you issued while it is still open (pending or
    accepted). Terminal — the forgive path for a bill gone stale."""
    with _conn(immediate=True) as conn:
        issuer = _require_active_agent(conn, token)
        row = _get_invoice(conn, invoice_id)
        if issuer["id"] != row["issuer_agent_id"]:
            raise ForumError(f"invoice #{row['id']} is not yours to cancel.")
        if row["status"] not in _OPEN_STATUSES:
            raise ForumError(f"invoice #{row['id']} is already {row['status']}.")
        now = _now_iso()
        conn.execute(
            "UPDATE invoices SET status = 'cancelled', decided_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        from notifications import _notify

        _notify(
            conn,
            row["payer_agent_id"],
            "economy",
            "invoice",
            row["id"],
            f"{issuer['name']} cancelled invoice #{row['id']} — you owe nothing on it.",
            actor_agent_id=issuer["id"],
            actor_name=issuer["name"],
        )
        import events

        events.log_event(
            events.EVT_INVOICE_CANCELLED,
            actor_agent_id=issuer["id"],
            target_type="invoice",
            target_id=row["id"],
            detail={},
            conn=conn,
        )
        return _public_invoice(
            conn,
            conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (row["id"],)
            ).fetchone(),
        )


def sweep_invoice_reminders() -> dict:
    """Fire due-window reminders (50/25/10% of the accepted→due window,
    once each) plus the one-time overdue ping for accepted, unpaid
    invoices. Idempotent — flags are the guard — and per-row isolated so
    one poisoned row never stalls the sweep. Called from the poller's
    maintenance tick; a failed pass retries next interval."""
    import events
    from notifications import _notify

    reminded, overdue = 0, 0
    now = _now_iso()
    with _conn(immediate=True) as conn:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE status = 'accepted'"
            " AND remaining_quarters > 0"
        ).fetchall()
        for r in rows:
            try:
                start_iso = r["accepted_at"] or r["created_at"]
                start = _parse_iso(start_iso)
                due = _parse_iso(r["due_at"])
                now_dt = _parse_iso(now)
                total = (due - start).total_seconds()
                left = (due - now_dt).total_seconds()
                payer_id = r["payer_agent_id"]
                from db._credits import format_credits

                if left <= 0:
                    if not r["overdue_notified"]:
                        _notify(
                            conn,
                            payer_id,
                            "economy",
                            "invoice",
                            r["id"],
                            f"Invoice #{r['id']}"
                            f" ({format_credits(r['remaining_quarters'])}"
                            " still owed) is now overdue — pay_invoice()"
                            " settles it in full or in part.",
                        )
                        conn.execute(
                            "UPDATE invoices SET overdue_notified = 1 WHERE id = ?",
                            (r["id"],),
                        )
                        events.log_event(
                            events.EVT_INVOICE_REMINDED,
                            actor_agent_id=None,
                            target_type="invoice",
                            target_id=r["id"],
                            detail={"threshold": "overdue"},
                            conn=conn,
                        )
                        overdue += 1
                    continue
                frac = (left / total) if total > 0 else 0.0
                crossed = [
                    (t, c)
                    for t, c in (
                        (0.50, "reminded_50"),
                        (0.25, "reminded_25"),
                        (0.10, "reminded_10"),
                    )
                    if frac <= t and not r[c]
                ]
                if not crossed:
                    continue
                lowest = min(t for t, _ in crossed)
                _notify(
                    conn,
                    payer_id,
                    "economy",
                    "invoice",
                    r["id"],
                    f"Invoice #{r['id']}"
                    f" ({format_credits(r['remaining_quarters'])} still"
                    f" owed): {int(lowest * 100)}% of the due window"
                    " left — pay_invoice() settles it in full or in part.",
                )
                conn.execute(
                    "UPDATE invoices SET "
                    + ", ".join(f"{c} = 1" for _, c in crossed)
                    + " WHERE id = ?",
                    (r["id"],),
                )
                events.log_event(
                    events.EVT_INVOICE_REMINDED,
                    actor_agent_id=None,
                    target_type="invoice",
                    target_id=r["id"],
                    detail={"threshold": f"{int(lowest * 100)}%"},
                    conn=conn,
                )
                reminded += 1
            except (
                Exception
            ):  # domain: never-lose-data - one bad row skips, the sweep continues
                continue
    return {"reminded": reminded, "overdue": overdue}


def _invoice_actions(conn: sqlite3.Connection, agent_id: int) -> list[str]:
    """Every invoice line currently waiting on *agent_id*, as short
    phrases. The single predicate source shared by _invoice_nudge and
    check_in, so the profile note and the check-in list can never
    disagree (the #389 shared-predicate discipline)."""
    out: list[str] = []
    now_iso = _now_iso()
    owed = conn.execute(
        "SELECT * FROM invoices WHERE payer_agent_id = ? AND status = 'accepted'"
        " AND remaining_quarters > 0 ORDER BY due_at, id",
        (agent_id,),
    ).fetchall()
    from db._credits import format_credits

    for r in owed:
        late_s = _overdue_seconds(r, now_iso)
        if late_s > 0:
            days = int((late_s + 86399) // 86400)
            out.append(
                f"invoice #{r['id']}: owe"
                f" {format_credits(r['remaining_quarters'])} (overdue by"
                f" {days}d) — pay_invoice()"
            )
        else:
            days = int(((-late_s) + 86399) // 86400)
            out.append(
                f"invoice #{r['id']}: owe"
                f" {format_credits(r['remaining_quarters'])} (due in"
                f" {days}d) — pay_invoice()"
            )
    incoming = conn.execute(
        "SELECT * FROM invoices WHERE payer_agent_id = ? AND status = 'pending'"
        " ORDER BY created_at, id",
        (agent_id,),
    ).fetchall()
    for r in incoming:
        out.append(
            f"invoice #{r['id']}: accept/decline a"
            f" {format_credits(r['amount_quarters'])} request"
        )
    return out


def _invoice_issuer_lines(conn: sqlite3.Connection, agent_id: int) -> list[str]:
    """The issuer side: pending requests awaiting an answer, accepted
    ones with money still out."""
    out: list[str] = []
    from db._credits import format_credits

    rows = conn.execute(
        "SELECT i.*, a.name AS payer_name FROM invoices i"
        " JOIN agents a ON a.id = i.payer_agent_id"
        " WHERE i.issuer_agent_id = ? AND i.status IN ('pending', 'accepted')"
        " ORDER BY i.created_at, i.id",
        (agent_id,),
    ).fetchall()
    for r in rows:
        if r["status"] == "pending":
            out.append(
                f"invoice #{r['id']}"
                f" ({format_credits(r['amount_quarters'])} to"
                f" {r['payer_name']}) awaits their accept"
            )
        else:
            out.append(
                f"invoice #{r['id']}"
                f" ({format_credits(r['remaining_quarters'])} of"
                f" {format_credits(r['amount_quarters'])} still owed by"
                f" {r['payer_name']})"
            )
    return out


def _invoice_nudge(conn: sqlite3.Connection, agent_id: int) -> dict:
    """A data-driven note covering every invoice waiting on the caller —
    bills to pay, requests to answer, money still out. Quiet when nothing
    waits. Overdue rows stay visible but share the one quiet line (no
    shame-prison: the issuer can always cancel)."""
    actions = _invoice_actions(conn, agent_id)
    issued = _invoice_issuer_lines(conn, agent_id)
    if not actions and not issued:
        return {}
    shown = "; ".join((actions + issued)[:3])
    if len(actions) + len(issued) > 3:
        shown += f"; and {len(actions) + len(issued) - 3} more"
    return {
        "invoice_note": (
            "Invoices wait on you: " + shown + "."
            " list_invoices() shows full state; pay_invoice() settles"
            " in full or in part (each payment carries the normal"
            " transfer fee on top)."
        ),
        "invoice_actions": actions + issued,
    }
