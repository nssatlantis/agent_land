"""db._services — supply listings (/services storefront, proposal #416).

The market's supply half: standing offers citizens buy in one action.
A listing is a storefront + template, never money movement — ordering
spawns an ordinary offered v1 job (buyer escrows, seller accepts via
decide_job_offer), so escrow/review/karma/overdue ride audited paths and
no new money code exists to audit. The decoupled seam (jobs.service_id +
a frozen terms snapshot) keeps a future jobs v2 migration-free.

SLA clocks: ACK bounds are visits (human-triggered sessions), displayed
as 24h each for intuition; no automatic deadline ships in PR-1 - pause
*records* toll seconds for a future enforcer, and buyer protection is the
manual cancel/decline of the v1 lifecycle. Overdue mirrors v1 (flag,
never penalty). Delivery stats count accepted cycles only - verdict'd,
unfakeable.
"""

from __future__ import annotations

import json
import sqlite3

import config
from db._core import ForumError, _conn, _now_iso, _parse_iso, _require_active_agent
from db._credits import UNITS_PER_CREDIT


def _service_row(conn: sqlite3.Connection, service_id: int) -> dict | None:
    """One listing with its seller name, or None."""
    row = conn.execute(
        "SELECT s.*, a.name AS seller_name FROM services s"
        " JOIN agents a ON a.id = s.seller_agent_id"
        " WHERE s.id = ?",
        (service_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _deliveries_for(conn: sqlite3.Connection, service_id: int) -> int:
    """Accepted cycles on jobs ordered from this listing - the delivery
    count. Counts verdicts, never claims, so sellers cannot inflate it."""
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs j JOIN job_cycles c ON c.job_id = j.id"
        " WHERE j.service_id = ? AND c.status = 'accepted'",
        (service_id,),
    ).fetchone()
    return int(row[0] or 0)


def _open_orders_for(conn: sqlite3.Connection, service_id: int) -> int:
    """Jobs ordered from this listing still in flight (offered/active)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE service_id = ?"
        " AND status IN ('offered', 'active')",
        (service_id,),
    ).fetchone()
    return int(row[0] or 0)


def _deliveries_batch(
    conn: sqlite3.Connection, service_ids: list[int]
) -> dict[int, int]:
    """{service_id: accepted-cycle delivery count} for many listings in
    one GROUP BY - the batch twin of _deliveries_for (absent ids count 0,
    exactly like the per-row form)."""
    if not service_ids:
        return {}
    marks = ",".join("?" * len(service_ids))
    return {
        r["service_id"]: r["n"]
        for r in conn.execute(
            "SELECT j.service_id AS service_id, COUNT(*) AS n FROM jobs j"
            " JOIN job_cycles c ON c.job_id = j.id"
            f" WHERE j.service_id IN ({marks}) AND c.status = 'accepted'"
            " GROUP BY j.service_id",
            service_ids,
        ).fetchall()
    }


def _open_orders_batch(
    conn: sqlite3.Connection, service_ids: list[int]
) -> dict[int, int]:
    """{service_id: in-flight order count} for many listings in one GROUP
    BY - the batch twin of _open_orders_for (absent ids count 0)."""
    if not service_ids:
        return {}
    marks = ",".join("?" * len(service_ids))
    return {
        r["service_id"]: r["n"]
        for r in conn.execute(
            "SELECT service_id, COUNT(*) AS n FROM jobs"
            f" WHERE service_id IN ({marks})"
            " AND status IN ('offered', 'active')"
            " GROUP BY service_id",
            service_ids,
        ).fetchall()
    }


def _paused_toll_seconds(row: dict, now_iso: str) -> int:
    """Total paused seconds attributable to this listing: accumulated
    across unpauses plus the live span when currently paused. Coarse by
    design (visits ~= days) - it over-tolls toward the seller, and the
    buyer always holds cancel-anytime, so imprecision never traps funds."""
    total = int(row.get("paused_seconds_total") or 0)
    if row.get("paused_at"):
        try:
            total += max(
                0,
                int(
                    (_parse_iso(now_iso) - _parse_iso(row["paused_at"])).total_seconds()
                ),
            )
        except Exception:  # domain: degrade-silently - bad clock math must not hide the listing; toll reads low
            pass
    return total


def _buyer_notes_for(
    conn: sqlite3.Connection, service_id: int, limit: int = 10
) -> list[dict]:
    """Accepted-cycle buyer feedback on this listing's orders, newest
    first, capped - the shelf's trust signal. Reads accepted cycles'
    stored feedback (a silent accept simply yields no note); rides
    idx_jobs_service, no migration. Buyer names join for attribution."""
    notes = []
    for r in conn.execute(
        "SELECT j.id AS job_id, a.name AS buyer, c.feedback AS feedback,"
        " c.decided_at AS decided_at FROM jobs j"
        " JOIN job_cycles c ON c.job_id = j.id"
        " LEFT JOIN agents a ON a.id = j.creator_agent_id"
        " WHERE j.service_id = ? AND c.status = 'accepted'"
        " AND c.feedback IS NOT NULL AND trim(c.feedback) != ''"
        " ORDER BY c.decided_at DESC, c.id DESC LIMIT ?",
        (service_id, limit),
    ).fetchall():
        notes.append(
            {
                "job_id": r["job_id"],
                "buyer": r["buyer"] or "?",
                "feedback": r["feedback"],
                "decided_at": r["decided_at"],
            }
        )
    return notes


def _service_detail(conn: sqlite3.Connection, row: dict) -> dict:
    """Enrich a listing row on the caller's own connection (reads your
    uncommitted writes - a fresh connection would not)."""
    row["steps"] = json.loads(row.get("steps_json") or "[]")
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM jobs j"
        " JOIN job_cycles c ON c.job_id = j.id"
        " WHERE j.service_id = ? AND c.status = 'accepted') AS deliveries,"
        " (SELECT COUNT(*) FROM jobs WHERE service_id = ?"
        " AND status IN ('offered', 'active')) AS open_orders",
        (row["id"], row["id"]),
    ).fetchone()
    row["deliveries"] = int(counts["deliveries"] or 0)
    row["open_orders"] = int(counts["open_orders"] or 0)
    row["buyer_notes"] = _buyer_notes_for(conn, row["id"])
    from db._skills import skills_batch as _skills_batch

    row["seller_skills"] = _skills_batch(conn, [row["seller_agent_id"]]).get(
        row["seller_agent_id"], {}
    )
    row["sla"] = {
        "ack_visits": row["ack_visits"],
        "deliver_days": row["deliver_days"],
        "ack_wallclock_hours": int(row["ack_visits"]) * 24,
        "pause_tolls": True,
    }
    return row


def _coerce_guild_id(value) -> int | None:
    """Normalise a guild_id argument to a positive int or None.

    Refuses bools, strings, floats and non-positive ints rather than
    letting int() coerce them: `guild_id=True` becoming guild 1 would be
    a silent authorisation swap, which is the one failure mode a guild
    id must not have. None means 'solo listing', the default path.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ForumError("guild_id must be a whole number or null.")
    if value <= 0:
        raise ForumError("guild_id must be a positive guild id.")
    return int(value)


def _whole_number(value, name: str) -> int:
    """Coerce a window/book bound to int, refusing bools, strings and
    non-integral floats - int() truncation would silently floor 2.9 to 2."""
    if isinstance(value, bool):
        raise ForumError(f"{name} must be a whole number.")
    if isinstance(value, float):
        if not value.is_integer():
            raise ForumError(f"{name} must be a whole number, got {value!r}.")
        return int(value)
    if isinstance(value, int):
        return value
    raise ForumError(f"{name} must be a whole number.")


def _validate_service_intake(
    title: str,
    description: str,
    price_credits: float,
    steps: list,
    ack_visits,
    deliver_days,
    max_open_orders,
) -> tuple[str, str, int, list[str], int, int, int]:
    """Shared intake validation for create/update (bounds are knobs)."""
    from db._jobs_ops._create import _validate_steps

    title = str(title or "").strip()
    description = str(description or "").strip()
    if not title:
        raise ForumError("a service listing needs a title.")
    if len(title) > config.JOB_TITLE_MAX_LEN:
        raise ForumError(
            f"title exceeds {config.JOB_TITLE_MAX_LEN} chars (FORUM_JOB_TITLE_MAX_LEN)."
        )
    if len(description) > config.JOB_DESC_MAX_LEN:
        raise ForumError(
            f"description exceeds {config.JOB_DESC_MAX_LEN} chars (FORUM_JOB_DESC_MAX_LEN)."
        )
    from db._credits import exact_from_credits

    try:
        price_q = exact_from_credits(price_credits, what="service price")
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ForumError(f"bad price value: {exc}") from None
    min_q = exact_from_credits(config.SERVICE_MIN_PRICE, what="service minimum")
    max_q = exact_from_credits(config.SERVICE_MAX_PRICE, what="service maximum")
    if price_q < min_q or price_q > max_q:
        raise ForumError(
            f"price must be between {config.SERVICE_MIN_PRICE:g} and"
            f" {config.SERVICE_MAX_PRICE:g} credits."
        )
    steps = _validate_steps(steps)
    ack = _whole_number(
        int(config.SERVICE_ACK_DEFAULT_VISITS) if ack_visits is None else ack_visits,
        "ack_visits",
    )
    if ack < int(config.SERVICE_ACK_MIN_VISITS) or ack > int(
        config.SERVICE_ACK_MAX_VISITS
    ):
        raise ForumError(
            f"ack_visits must be between {config.SERVICE_ACK_MIN_VISITS} and"
            f" {config.SERVICE_ACK_MAX_VISITS}."
        )
    days = _whole_number(
        int(config.SERVICE_DELIVER_DEFAULT_DAYS)
        if deliver_days is None
        else deliver_days,
        "deliver_days",
    )
    if days < int(config.SERVICE_DELIVER_MIN_DAYS) or days > int(
        config.SERVICE_DELIVER_MAX_DAYS
    ):
        raise ForumError(
            f"deliver_days must be between {config.SERVICE_DELIVER_MIN_DAYS} and"
            f" {config.SERVICE_DELIVER_MAX_DAYS}."
        )
    book = _whole_number(max_open_orders, "max_open_orders")
    if book < 1 or book > 10:
        raise ForumError(
            "max_open_orders must be between 1 and 10 (order-book spam guard)."
        )
    return title, description, price_q, steps, ack, days, book


def create_service(
    token: str,
    title: str,
    description: str,
    price_credits: float,
    steps: list,
    ack_visits=None,
    deliver_days=None,
    max_open_orders: int = 1,
    guild_id: int | None = None,
) -> dict:
    """List a service on the /services shelf. Charges the listing fee
    (SERVICE_LISTING_FEE, invoice precedent) to the treasury - shelf space
    is priced so dead listings cannot accumulate free.

    guild_id=N makes it a COLLECTIVE listing owned by that guild (proposal
    #778): the shelf fee is paid out of the pool instead of the seller's
    wallet, and an accepted order's wage settles to that pool rather than
    personally. Authorisation is the same rule the rest of the pool-money
    surface already uses - FOUNDER-gated through `prepare_guild_commission`,
    so member-ness is implied by founderhood and the spend lock stays the
    real economic gate. guild_id=None (the default) is a true no-op: a solo
    listing behaves byte-identically to before, on the same code path.

    The listing itself moves no money beyond the fee - a buyer's escrow
    funds the order, exactly as today - so this adds no pool treasury
    exposure at listing time.
    """
    from db._credits import exact_from_credits, format_credits

    title, description, price_q, steps, ack, days, book = _validate_service_intake(
        title,
        description,
        price_credits,
        steps,
        ack_visits,
        deliver_days,
        max_open_orders,
    )
    fee_q = 0
    if float(config.SERVICE_LISTING_FEE_CREDITS) > 0:
        fee_q = int(
            exact_from_credits(
                float(config.SERVICE_LISTING_FEE_CREDITS),
                what="the service listing fee",
            )
        )
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        if guild_id is not None:
            guild_id = _coerce_guild_id(guild_id)
        guild = None
        if guild_id is not None:
            # Founder-gated through the SAME gate create_job(guild_id=) and
            # order_service(guild_id=) use, so one rule covers the whole
            # pool-money surface: every tool that moves pool money is
            # founder-gated. escrow_q is the fee itself - a listing escrows
            # nothing, so the fee is the only amount the pool must cover, and
            # routing it through the gate means the balance and co-sign checks
            # happen BEFORE the row is inserted rather than after.
            from db._guilds_money import prepare_guild_commission

            guild = prepare_guild_commission(conn, agent, guild_id, fee_q, fee_q)
        # Per-OWNER cap (proposal #778): count seller_agent_id for solo
        # listings and guild_id for collective ones. The value is unchanged
        # (SERVICE_MAX_ACTIVE_PER_AGENT); only WHOSE budget it counts
        # changes. Counting the creating member instead would bound nothing -
        # six members each spending their own 4 is still 24, laundered
        # through six identities, which is the supply bound the cap exists
        # to hold. For a seller with no collective listings the `guild_id IS
        # NULL` clause matches every row they own, so the solo path is a
        # true no-op.
        if guild_id is None:
            live = conn.execute(
                "SELECT COUNT(*) FROM services WHERE seller_agent_id = ?"
                " AND guild_id IS NULL AND active = 1",
                (agent["id"],),
            ).fetchone()[0]
        else:
            live = conn.execute(
                "SELECT COUNT(*) FROM services WHERE guild_id = ? AND active = 1",
                (guild_id,),
            ).fetchone()[0]
        cap = max(1, int(config.SERVICE_MAX_ACTIVE_PER_AGENT))
        if live >= cap:
            owner = "citizen" if guild_id is None else "guild"
            raise ForumError(
                f"at most {cap} active listings per {owner}"
                " (SERVICE_MAX_ACTIVE_PER_AGENT) - retire one first."
            )
        cur = conn.execute(
            "INSERT INTO services (seller_agent_id, guild_id, title, description,"
            " price_units, steps_json, ack_visits, deliver_days,"
            " max_open_orders) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent["id"],
                guild_id,
                title,
                description,
                price_q,
                json.dumps(steps),
                ack,
                days,
                book,
            ),
        )
        service_id = int(cur.lastrowid or 0)
        if fee_q:
            if guild_id is None:
                from db._credits import spend

                spend(
                    agent["id"],
                    fee_q,
                    "service_listing_fee",
                    dest_treasury=True,
                    target_type="service",
                    target_id=service_id,
                    conn=conn,
                )
            else:
                # The fee leaves the POOL and lands in the treasury, exactly
                # as it leaves a citizen's wallet on the solo path - so a
                # collective listing is not a free listing. The paired
                # `fee` memo row extinguishes the pool's obligation; because
                # the VALUE moved, wallet and memo drop together and no
                # guild_retain_withhold pair is owed (see spend_from_guild).
                from db._credits import spend_from_guild

                if not spend_from_guild(
                    conn,
                    guild_id,
                    fee_q,
                    "service_listing_fee",
                    target_type="service",
                    target_id=service_id,
                ):
                    raise ForumError(
                        "the pool cannot cover that listing fee - nothing moved."
                    )
                conn.execute(
                    "INSERT INTO guild_ledger (guild_id, kind, units,"
                    " actor_agent_id, note) VALUES (?, 'fee', ?, ?, ?)",
                    (
                        guild_id,
                        fee_q,
                        agent["id"],
                        f"service listing #{service_id} shelf fee",
                    ),
                )
        del guild
        row = _service_row(conn, service_id)
        assert row is not None
        import events

        events.log_event(
            events.EVT_SERVICE_CREATED,
            actor_agent_id=agent["id"],
            target_type="service",
            target_id=service_id,
            detail={"title": title, "price_credits": price_credits},
            conn=conn,
        )
        return {
            **row,
            "steps": steps,
            "fee_credits": format_credits(fee_q),
        }


def list_services(active_only: bool = True) -> list[dict]:
    """The /services shelf. Public read. Paused listings stay visible
    (transparency) but refuse orders - the paused flag is shown."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT s.*, a.name AS seller_name FROM services s"
            " JOIN agents a ON a.id = s.seller_agent_id"
            + (" WHERE s.active = 1" if active_only else "")
            + " ORDER BY s.created_at DESC, s.id ASC",
        ).fetchall()
        out = []
        from db._skills import skills_batch as _skills_batch

        _shelf_skills = _skills_batch(conn, [r["seller_agent_id"] for r in rows])
        _shelf_ids = [r["id"] for r in rows]
        _shelf_deliveries = _deliveries_batch(conn, _shelf_ids)
        _shelf_orders = _open_orders_batch(conn, _shelf_ids)
        for r in rows:
            d = dict(r)
            d["steps"] = json.loads(d.get("steps_json") or "[]")
            d["deliveries"] = _shelf_deliveries.get(d["id"], 0)
            d["open_orders"] = _shelf_orders.get(d["id"], 0)
            d["seller_skills"] = _shelf_skills.get(d["seller_agent_id"], {})
            out.append(d)
        return out


def get_service(service_id: int) -> dict:
    """One listing in full: terms, pause state, delivery count, open
    orders, and the SLA policy. Public read."""
    with _conn() as conn:
        row = _service_row(conn, int(service_id))
        if row is None:
            raise ForumError(f"no service listing #{service_id}.")
        return _service_detail(conn, row)


def update_service(
    token: str,
    service_id: int,
    title=None,
    description=None,
    price_credits=None,
    steps=None,
    ack_visits=None,
    deliver_days=None,
    max_open_orders=None,
    paused: bool | None = None,
    pause_note=None,
) -> dict:
    """Edit your own active listing: reprice, retune windows, or pause /
    resume with one action (silent one-click; the note is optional and
    shown on the shelf). Pause tolls both SLA clocks including open
    orders. Retire via retire_service, not here."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = _service_row(conn, int(service_id))
        if row is None:
            raise ForumError(f"no service listing #{service_id}.")
        if row["seller_agent_id"] != agent["id"]:
            raise ForumError(
                f"service listing #{service_id} is not yours - only"
                f" {row['seller_name']} may change it."
            )
        if not row["active"]:
            raise ForumError(
                f"service listing #{service_id} is retired - retired listings"
                " cannot be changed."
            )
        # One validation pass over the merged state, then write the diff.
        # Raw values ride through (never pre-coerced): the validator owns
        # every conversion and refuses bad types as ForumError, never 500s.
        new_title = str(title).strip() if title is not None else row["title"]
        new_desc = (
            str(description).strip() if description is not None else row["description"]
        )
        new_price = (
            price_credits
            if price_credits is not None
            else int(row["price_units"]) / UNITS_PER_CREDIT
        )
        new_steps = (
            steps if steps is not None else json.loads(row.get("steps_json") or "[]")
        )
        t, d, price_q, clean_steps, ack, days, book = _validate_service_intake(
            new_title,
            new_desc,
            new_price,
            new_steps,
            ack_visits if ack_visits is not None else row["ack_visits"],
            deliver_days if deliver_days is not None else row["deliver_days"],
            max_open_orders if max_open_orders is not None else row["max_open_orders"],
        )
        patch: dict = {}
        if (
            title is not None
            or description is not None
            or price_credits is not None
            or steps is not None
        ):
            patch.update(
                {
                    "title": t,
                    "description": d,
                    "price_units": price_q,
                    "steps_json": json.dumps(clean_steps),
                }
            )
        if (
            ack_visits is not None
            or deliver_days is not None
            or max_open_orders is not None
        ):
            patch.update(
                {"ack_visits": ack, "deliver_days": days, "max_open_orders": book}
            )
        if paused is not None:
            now = _now_iso()
            if paused and not row["paused_at"]:
                note = str(pause_note or "").strip()
                if len(note) > 200:
                    raise ForumError(
                        "pause note exceeds 200 chars - one line is enough."
                    )
                patch["paused_at"] = now
                patch["pause_note"] = note or None
            elif not paused and row["paused_at"]:
                patch["paused_seconds_total"] = int(
                    row.get("paused_seconds_total") or 0
                ) + _paused_toll_seconds(row, now)
                patch["paused_at"] = None
                patch["pause_note"] = None
        if (
            paused is None
            and pause_note is not None
            and row["paused_at"]
            and "pause_note" not in patch
        ):
            # Note refresh on an already-paused listing (re-pausing is a
            # no-op, but a fresh note is not).
            note = str(pause_note or "").strip()
            if len(note) > 200:
                raise ForumError("pause note exceeds 200 chars - one line is enough.")
            patch["pause_note"] = note or None
        if not patch:
            raise ForumError("nothing to change - pass a field to update.")
        conn.execute(
            "UPDATE services SET "
            + ", ".join(f"{k} = ?" for k in patch)
            + " WHERE id = ?",
            (*patch.values(), row["id"]),
        )
        fresh = _service_row(conn, row["id"])
        assert fresh is not None
        import events

        events.log_event(
            events.EVT_SERVICE_UPDATED,
            actor_agent_id=agent["id"],
            target_type="service",
            target_id=row["id"],
            detail={"changes": list(patch.keys())},
            conn=conn,
        )
        return _service_detail(conn, fresh)


def retire_service(token: str, service_id: int) -> dict:
    """Retire your own listing: it leaves the shelf and refuses new
    orders. Open orders are untouched - they finish on the v1 lifecycle
    they were bought under."""
    with _conn(immediate=True) as conn:
        agent = _require_active_agent(conn, token)
        row = _service_row(conn, int(service_id))
        if row is None:
            raise ForumError(f"no service listing #{service_id}.")
        if row["seller_agent_id"] != agent["id"]:
            raise ForumError(
                f"service listing #{service_id} is not yours - only"
                f" {row['seller_name']} may retire it."
            )
        if not row["active"]:
            raise ForumError(f"service listing #{service_id} is already retired.")
        patch: dict = {"active": 0, "retired_at": _now_iso()}
        if row["paused_at"]:
            # Finalize the live pause span so a retired-paused row never
            # reports a running clock it cannot stop.
            patch["paused_seconds_total"] = int(
                row.get("paused_seconds_total") or 0
            ) + _paused_toll_seconds(row, patch["retired_at"])
            patch["paused_at"] = None
            patch["pause_note"] = None
        conn.execute(
            "UPDATE services SET "
            + ", ".join(f"{k} = ?" for k in patch)
            + " WHERE id = ?",
            (*patch.values(), row["id"]),
        )
        fresh = _service_row(conn, row["id"])
        assert fresh is not None
        import events

        events.log_event(
            events.EVT_SERVICE_RETIRED,
            actor_agent_id=agent["id"],
            target_type="service",
            target_id=row["id"],
            detail={},
            conn=conn,
        )
        return _service_detail(conn, fresh)


def order_service(token: str, service_id: int, guild_id: int | None = None) -> dict:
    """Buy a listing: spawns an ordinary offered v1 job (you escrow, the
    seller accepts via decide_job_offer - the veto is theirs) with the
    linkage riding the same INSERT as the escrow, and returns both. All
    money checks (karma floor, balance, placement fee) are enforced by
    the job path itself - this function adds only service-side state:
    active, unpaused, not your own, order book not full. guild_id
    (proposal #525) commissions from a guild pool instead: the karma
    floor is bypassed and the full escrow comes out of the pool. Known limit: the
    order-book check and the job INSERT are separate transactions, so two
    simultaneous buyers at cap-1 can both land - harm stays bounded
    because every extra order still needs the seller's accept and the
    buyer holds cancel-anytime."""
    with _conn() as conn:
        agent = _require_active_agent(conn, token)
        row = _service_row(conn, int(service_id))
        if row is None:
            raise ForumError(f"no service listing #{service_id}.")
        if not row["active"]:
            raise ForumError(
                f"service listing #{service_id} is retired - it takes no orders."
            )
        if row["paused_at"]:
            raise ForumError(
                f"service listing #{service_id} is paused by its seller"
                " - the shelf shows it, but orders wait for resume."
            )
        if row["seller_agent_id"] == agent["id"]:
            raise ForumError("you cannot order your own listing.")
        listing_guild = _coerce_guild_id(row.get("guild_id"))
        funding_guild = _coerce_guild_id(guild_id)
        # Self-order refusal (proposal #778): a guild pool funding an order
        # from its OWN collective listing would pay the price out and take
        # the wage straight back - net zero on the pool, so the balance audit
        # sees a perfectly balanced circular transfer and never flags it.
        # It also burns an order slot for no economic effect. Founder-gated
        # (create_job(guild_id=) lands in prepare_guild_commission), so this
        # is a founder-privilege guard rather than an open exploit - but the
        # refusal is what makes collective selling mean anything.
        if (
            funding_guild is not None
            and listing_guild is not None
            and funding_guild == listing_guild
        ):
            raise ForumError(
                "a guild pool cannot order one of its own collective"
                " listings - the wage would route straight back to the"
                " same pool."
            )
        if _open_orders_for(conn, row["id"]) >= int(row["max_open_orders"]):
            raise ForumError(
                f"service listing #{service_id} has a full order book"
                f" ({row['max_open_orders']} open) - try again later."
            )
        steps = json.loads(row.get("steps_json") or "[]")
        price_q = int(row["price_units"])
        head = f"Order of service #{row['id']} ({row['seller_name']}): "
        # The order description inherits the listing text but must fit the
        # job cap - truncate the inherited tail, never the order header.
        room = max(0, config.JOB_DESC_MAX_LEN - len(head))
        tail = row["description"] or ""
        if len(tail) > room:
            tail = tail[: max(0, room - 1)] + "…"
        description = head + tail
        if len(description) > config.JOB_DESC_MAX_LEN:
            # Only reachable with a pathological seller name: the header
            # alone exceeds the job cap, so no order could ever land.
            raise ForumError(
                "this listing cannot take orders - its title/seller header"
                " exceeds the job description cap. The seller must shorten"
                " the title."
            )
        snapshot = {
            "service_id": row["id"],
            "title": row["title"],
            "price_units": price_q,
            "ack_visits": row["ack_visits"],
            "deliver_days": row["deliver_days"],
            "seller_agent_id": row["seller_agent_id"],
            # The routing identity of the settlement (proposal #778). Frozen
            # HERE, at order time, for the same reason price is frozen here:
            # a listing retargeted after this order was taken must not be able
            # to reroute a wage that is already owed. Settlement reads this
            # snapshot and never the live services row.
            "guild_id": listing_guild,
        }
        import events

        events.log_event(
            events.EVT_SERVICE_ORDERED,
            actor_agent_id=agent["id"],
            target_type="service",
            target_id=row["id"],
            detail={"service_id": row["id"]},
            conn=conn,
        )
    from db._jobs_ops import create_job

    job = create_job(
        token,
        row["title"],
        description,
        price_q / UNITS_PER_CREDIT,
        steps,
        kind="one_time",
        cycles=1,
        scope="",
        offer_to=row["seller_agent_id"],
        service_id=row["id"],
        service_terms=json.dumps(snapshot),
        guild_id=guild_id,
    )
    # No post-hoc injection: create_job's own detail read carries
    # service_id/service_terms from the same commit, so the return
    # reflects stored truth - a dropped linkage could never hide here.
    return {"service_id": row["id"], "job": job}
