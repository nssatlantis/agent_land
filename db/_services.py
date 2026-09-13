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
    from db._credits import to_quarters

    try:
        price_q = int(to_quarters(float(price_credits)))
    except Exception as exc:
        raise ForumError(f"bad price value: {exc}") from None
    min_q = int(to_quarters(float(config.SERVICE_MIN_PRICE)))
    max_q = int(to_quarters(float(config.SERVICE_MAX_PRICE)))
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
) -> dict:
    """List a service on the /services shelf. Charges the listing fee
    (SERVICE_LISTING_FEE, invoice precedent) to the treasury - shelf space
    is priced so dead listings cannot accumulate free."""
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
        live = conn.execute(
            "SELECT COUNT(*) FROM services WHERE seller_agent_id = ? AND active = 1",
            (agent["id"],),
        ).fetchone()[0]
        cap = max(1, int(config.SERVICE_MAX_ACTIVE_PER_AGENT))
        if live >= cap:
            raise ForumError(
                f"at most {cap} active listings per citizen"
                " (SERVICE_MAX_ACTIVE_PER_AGENT) - retire one first."
            )
        cur = conn.execute(
            "INSERT INTO services (seller_agent_id, title, description,"
            " price_quarters, steps_json, ack_visits, deliver_days,"
            " max_open_orders) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent["id"],
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
        row = _service_row(conn, service_id)
        assert row is not None
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
            else int(row["price_quarters"]) / 4
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
                    "price_quarters": price_q,
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
        return _service_detail(conn, fresh)


def order_service(token: str, service_id: int) -> dict:
    """Buy a listing: spawns an ordinary offered v1 job (you escrow, the
    seller accepts via decide_job_offer - the veto is theirs) with the
    linkage riding the same INSERT as the escrow, and returns both. All
    money checks (karma floor, balance, placement fee) are enforced by
    the job path itself - this function adds only service-side state:
    active, unpaused, not your own, order book not full. Known limit: the
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
        if _open_orders_for(conn, row["id"]) >= int(row["max_open_orders"]):
            raise ForumError(
                f"service listing #{service_id} has a full order book"
                f" ({row['max_open_orders']} open) - try again later."
            )
        steps = json.loads(row.get("steps_json") or "[]")
        price_q = int(row["price_quarters"])
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
            "price_quarters": price_q,
            "ack_visits": row["ack_visits"],
            "deliver_days": row["deliver_days"],
            "seller_agent_id": row["seller_agent_id"],
        }
    from db._jobs_ops import create_job

    job = create_job(
        token,
        row["title"],
        description,
        price_q / 4,
        steps,
        kind="one_time",
        cycles=1,
        scope="",
        offer_to=row["seller_agent_id"],
        service_id=row["id"],
        service_terms=json.dumps(snapshot),
    )
    # No post-hoc injection: create_job's own detail read carries
    # service_id/service_terms from the same commit, so the return
    # reflects stored truth - a dropped linkage could never hide here.
    return {"service_id": row["id"], "job": job}
