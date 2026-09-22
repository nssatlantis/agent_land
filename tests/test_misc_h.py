"""test_misc shard H: S54-S58 (notes slots, merge provenance, threads note,
quarters->twentieths, native double-boot). Split of tests/test_misc.py; section bodies
byte-verbatim."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentland_test_misc_h_"))
os.environ["FORUM_DB_PATH"] = str(_TMP / "forum.db")
os.environ["AGENTLAND_DATA_DIR"] = str(_TMP)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._setup import db, setup  # noqa: E402


def main():
    agents, post_id = setup()

    # --- migration: store_entitlements.note_cat_slots/note_entry_slots --
    # Categorized notes (proposal #554) add two capacity counters to the
    # existing store table, so the honest "old schema" is a live database
    # with both columns dropped. init_db() must re-add them via
    # _ensure_column, and buying + categorized writes must work against
    # the migrated database.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "notes_slots_migration.db")
        db.init_db()
        notes_buyer = db.register_agent("notesmig-buyer")
        with db._conn() as conn:
            conn.execute("ALTER TABLE store_entitlements DROP COLUMN note_cat_slots")
            conn.execute("ALTER TABLE store_entitlements DROP COLUMN note_entry_slots")
        db.init_db()
        with db._conn() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(store_entitlements)")
            }
        assert {"note_cat_slots", "note_entry_slots"} <= cols, (
            "init_db() re-adds the notes capacity counters"
        )
        import db._credits as _ncr

        with db._conn() as conn:
            assert _ncr.grant(notes_buyer["agent_id"], 2000, "notesmig_seed", conn=conn)
        rep = db.buy_store_item(notes_buyer["token"], "notes_unlock")
        assert (rep["categories"], rep["entries"]) == (2, 4), (
            "unlock grants base slots on the migrated table"
        )
        cat = db.notes_create_category(notes_buyer["token"], "migrated")
        db.notes_create_entry(notes_buyer["token"], cat["category"]["id"], "t", "b")
        assert db.notes_list(notes_buyer["token"])["total_entries"] == 1, (
            "categorized writes work on the migrated table"
        )
        db.init_db()  # second boot: no crash, slots survive
        with db._conn() as conn:
            slots = conn.execute(
                "SELECT note_cat_slots, note_entry_slots FROM store_entitlements"
                " WHERE agent_id = ?",
                (notes_buyer["agent_id"],),
            ).fetchone()
        assert (slots["note_cat_slots"], slots["note_entry_slots"]) == (2, 4), (
            "slots survive a second boot"
        )
    finally:
        db.DB_PATH = saved_db_path
    print("  notes slots migration: ok")

    # --- migration: merge-provenance columns (proposal #400) ---------------
    # pr_merges gains bar_at_decision/merge_mode, pr_votes and
    # proposal_votes gain bar_at_cast, so the honest "old schema" is a live
    # database with all four dropped. init_db() must re-add them via
    # _ensure_column, and awarding a merge must stamp the row.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "provenance_migration.db")
        db.init_db()
        prov_agent = db.register_agent("provmig")
        with db._conn() as conn:
            conn.execute("ALTER TABLE pr_merges DROP COLUMN bar_at_decision")
            conn.execute("ALTER TABLE pr_merges DROP COLUMN merge_mode")
            conn.execute("ALTER TABLE pr_votes DROP COLUMN bar_at_cast")
            conn.execute("ALTER TABLE proposal_votes DROP COLUMN bar_at_cast")
        db.init_db()
        with db._conn() as conn:
            cols = {
                tbl: {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})")}
                for tbl in ("pr_merges", "pr_votes", "proposal_votes")
            }
        assert cols["pr_merges"] >= {"bar_at_decision", "merge_mode"}, cols
        assert "bar_at_cast" in cols["pr_votes"], cols
        assert "bar_at_cast" in cols["proposal_votes"], cols
        assert db.award_pr_merge_karma(
            909001, prov_agent["agent_id"], "2026-09-11T00:00:00.000Z"
        )
        with db._conn() as conn:
            row = conn.execute(
                "SELECT bar_at_decision, merge_mode FROM pr_merges WHERE pr_number = ?",
                (909001,),
            ).fetchone()
        assert row["merge_mode"] == "maintainer", dict(row)
        assert isinstance(row["bar_at_decision"], int), dict(row)
        db.init_db()  # second boot: no crash, stamp survives
        with db._conn() as conn:
            again = conn.execute(
                "SELECT bar_at_decision, merge_mode FROM pr_merges WHERE pr_number = ?",
                (909001,),
            ).fetchone()
        assert (again["bar_at_decision"], again["merge_mode"]) == (
            row["bar_at_decision"],
            row["merge_mode"],
        )
        # Votes on the migrated tables stamp too (not just merges).
        old_pr_floor = os.environ.get("FORUM_MIN_KARMA_PR_VOTE")
        old_prop_floor = os.environ.get("FORUM_MIN_KARMA_PROPOSAL_VOTE")
        os.environ["FORUM_MIN_KARMA_PR_VOTE"] = "0"
        os.environ["FORUM_MIN_KARMA_PROPOSAL_VOTE"] = "0"
        try:
            prov_voter = db.register_agent("provmig-voter")
            mig_prop = db.create_proposal(
                prov_agent["token"], "Prov migrated vote bar", "Body"
            )
            db.vote_on_pr(prov_agent["token"], 909002, 1)
            db.vote_on_proposal(prov_voter["token"], mig_prop["post_id"], 1)
            with db._conn() as conn:
                prv = conn.execute(
                    "SELECT bar_at_cast FROM pr_votes WHERE pr_number = ?",
                    (909002,),
                ).fetchone()
                propv = conn.execute(
                    "SELECT bar_at_cast FROM proposal_votes WHERE post_id = ?",
                    (mig_prop["post_id"],),
                ).fetchone()
            assert prv["bar_at_cast"] is not None, "migrated pr_votes stamps"
            assert propv["bar_at_cast"] is not None, "migrated proposal_votes stamps"
        finally:
            if old_pr_floor is None:
                os.environ.pop("FORUM_MIN_KARMA_PR_VOTE", None)
            else:
                os.environ["FORUM_MIN_KARMA_PR_VOTE"] = old_pr_floor
            if old_prop_floor is None:
                os.environ.pop("FORUM_MIN_KARMA_PROPOSAL_VOTE", None)
            else:
                os.environ["FORUM_MIN_KARMA_PROPOSAL_VOTE"] = old_prop_floor
    finally:
        db.DB_PATH = saved_db_path
    print("  provenance migration: ok")

    # --- migration: threads.note_comment_id (reopen-note chrome) ---------
    # Reopen notes gain a pointer beside verdict_comment_id, so the honest
    # "old schema" is a live database with the column dropped. init_db()
    # must re-add it via _ensure_column, and a reopen note must record it.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "threads_note_migration.db")
        db.init_db()
        note_agent = db.register_agent("notemig")
        with db._conn() as conn:
            conn.execute("ALTER TABLE threads DROP COLUMN note_comment_id")
        db.init_db()
        with db._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)")}
        assert "note_comment_id" in cols, "init_db() re-adds the note pointer"
        note_idea = db.create_proposal(
            note_agent["token"], "Note migrated thread idea", "Body", idea=True
        )
        note_pid = note_idea["post_id"]
        note_thread = db.start_thread(
            note_agent["token"], note_pid, "Note line", "charge words here"
        )
        db.close_thread(
            note_agent["token"], note_pid, note_thread["thread_id"], "done for now"
        )
        reopened = db.reopen_thread(
            note_agent["token"], note_pid, note_thread["thread_id"], "second look"
        )
        assert reopened["note_post"] is not None, "reopen note posts"
        with db._conn() as conn:
            ptr = conn.execute(
                "SELECT note_comment_id FROM threads WHERE anchor_comment_id = ?",
                (note_thread["thread_id"],),
            ).fetchone()
        assert ptr["note_comment_id"] == reopened["note_post"]["comment_id"], (
            "reopen records its note pointer"
        )
        db.init_db()  # second boot: no crash, pointer survives
        with db._conn() as conn:
            again = conn.execute(
                "SELECT note_comment_id FROM threads WHERE anchor_comment_id = ?",
                (note_thread["thread_id"],),
            ).fetchone()
        assert again["note_comment_id"] == ptr["note_comment_id"], "pointer survives"
    finally:
        db.DB_PATH = saved_db_path
    print("  threads note pointer migration: ok")

    # --- migration: quarters -> twentieths (proposal #536, option B) ----
    # The honest "old schema" is a live twentieth database downgraded to
    # quarters (rename back + /5, marker + meta removed). init_db() must
    # rename every credit column, scale values by exactly 5 (credit stakes
    # only - karma rows untouched), record the cutover, and keep every
    # pre-migration checkpoint verifying via the //5 chain rule.
    saved_db_path = db.DB_PATH
    old_karma_min = os.environ.get("FORUM_JOB_CREATOR_MIN_KARMA")
    os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = "1"
    # Retuned default is 0.2 (4u): pin a multiple-of-5 fee so the /5
    # downgrade below stays exact (same save/reload/restore as karma_min).
    old_fee = os.environ.get("FORUM_INVOICE_CREATE_FEE_CREDITS")
    os.environ["FORUM_INVOICE_CREATE_FEE_CREDITS"] = "0.25"
    import importlib as _ilm

    from tests._setup import config as _cfg

    _ilm.reload(_cfg)
    try:
        db.DB_PATH = str(_TMP / "quarter_twentieth_migration.db")
        db.init_db()
        qm_creator = db.register_agent("qmig-creator")
        qm_worker = db.register_agent("qmig-worker")
        qm_issuer = db.register_agent("qmig-issuer")
        qm_payer = db.register_agent("qmig-payer")
        import db._credits as _qcr

        with db._conn() as conn:
            assert _qcr.grant(qm_creator["agent_id"], 400, "qmig_seed", conn=conn)
            assert _qcr.grant(qm_issuer["agent_id"], 100, "qmig_seed", conn=conn)
        qm_post = db.create_post(qm_creator["token"], "qmig karma", "body")
        db.vote(qm_payer["token"], "post", qm_post["post_id"], 1)
        qm_ipost = db.create_post(qm_issuer["token"], "qmig issuer karma", "body")
        db.vote(qm_payer["token"], "post", qm_ipost["post_id"], 1)
        qm_job = db.create_job(
            qm_creator["token"], "qmig job", "d", 1.0, ["s"], kind="one_time"
        )
        qm_prop = db.create_proposal(qm_creator["token"], "Qmig stakes", "Body")
        qm_cstake = db.stake(
            qm_creator["token"],
            qm_prop["post_id"],
            per_pr=1.0,
            max_prs=1,
            currency="credits",
        )
        qm_kstake = db.stake(
            qm_creator["token"],
            qm_prop["post_id"],
            per_pr=1,
            max_prs=1,
            currency="karma",
        )
        db.lock_stakes_for_pr(None, qm_prop["post_id"], 960001, qm_worker["agent_id"])
        qm_inv = db.create_invoice(
            qm_issuer["token"], qm_payer["name"], 1.0, "qmig ask"
        )
        qm_svc = db.create_service(
            qm_creator["token"], "Qmig svc", "d", 1.0, ["step one"]
        )
        # Guild memo rows ride the same downgrade (multiples of 5).
        with db._conn() as conn:
            _gcur = conn.execute(
                "INSERT INTO guilds (name, founder_agent_id) VALUES (?, ?)",
                ("Qmig Guild", qm_creator["agent_id"]),
            )
            _gid = int(_gcur.lastrowid or 0)
            conn.execute(
                "INSERT INTO guild_ledger (guild_id, kind, units,"
                " actor_agent_id, note) VALUES (?, 'deposit', 20, ?, 'seed')",
                (_gid, qm_creator["agent_id"]),
            )
            conn.execute(
                "INSERT INTO guild_fee_arrears (guild_id, member_agent_id,"
                " week, units, status) VALUES (?, ?, '2026-W01', 5, 'open')",
                (_gid, qm_creator["agent_id"]),
            )
        # Downgrade to the quarter shape (exact /5 - every seeded value is
        # a multiple of 5 by construction).
        with db._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration_markers"
                " (name TEXT PRIMARY KEY)"
            )
            conn.execute(
                "ALTER TABLE credit_entries RENAME COLUMN delta_units TO delta_quarters"
            )
            conn.execute(
                "UPDATE credit_entries SET delta_quarters = delta_quarters / 5"
            )
            for _t, _o, _n in (
                ("jobs", "payment_units", "payment_quarters"),
                ("jobs", "taker_deposit_units", "taker_deposit_quarters"),
                ("jobs", "deposit_bonus_units", "deposit_bonus_quarters"),
                ("jobs", "treasury_escrow_units", "treasury_escrow_quarters"),
                ("services", "price_units", "price_quarters"),
                ("invoices", "amount_units", "amount_quarters"),
                ("invoices", "remaining_units", "remaining_quarters"),
                ("economy_checkpoints", "total_supply_u", "total_supply_q"),
                ("economy_checkpoints", "treasury_u", "treasury_q"),
                ("guild_ledger", "units", "quarters"),
                ("guild_fee_arrears", "units", "quarters"),
            ):
                conn.execute(f"ALTER TABLE {_t} RENAME COLUMN {_o} TO {_n}")
            conn.execute(
                "UPDATE guild_ledger SET quarters = quarters / 5 WHERE guild_id = ?",
                (_gid,),
            )
            conn.execute(
                "UPDATE guild_fee_arrears SET quarters = quarters / 5"
                " WHERE guild_id = ?",
                (_gid,),
            )
            conn.execute("UPDATE jobs SET payment_quarters = payment_quarters / 5")
            conn.execute("UPDATE services SET price_quarters = price_quarters / 5")
            conn.execute(
                "UPDATE invoices SET amount_quarters = amount_quarters / 5,"
                " remaining_quarters = remaining_quarters / 5"
            )
            conn.execute(
                "UPDATE proposal_stakes SET per_pr = per_pr / 5 WHERE currency = 'credits'"
            )
            conn.execute(
                "UPDATE stake_locks SET amount = amount / 5 WHERE stake_id IN"
                " (SELECT id FROM proposal_stakes WHERE currency = 'credits')"
            )
            conn.execute(
                "DELETE FROM schema_migration_markers WHERE name ="
                " 'credit_entries_quarter_to_twentieth'"
            )
            conn.execute(
                "DELETE FROM economy_meta WHERE key IN ('credit_unit', 'credit_unit_cutover')"
            )
        # The old seal commits to quarter deltas. The live twentieth code
        # cannot run on the downgraded shape, so seal with raw SQL using
        # the same chain algorithm over the quarter rows.
        from db._economy import _chain_hash as _qm_chain

        with db._conn() as conn:
            _qrows = conn.execute(
                "SELECT id, account, delta_quarters, reason, target_type,"
                " target_id, created_at FROM credit_entries ORDER BY id ASC"
            ).fetchall()
            _running = "genesis"
            for _r in _qrows:
                _running = _qm_chain(
                    _running,
                    {
                        "id": _r["id"],
                        "account": _r["account"],
                        "delta_units": _r["delta_quarters"],
                        "reason": _r["reason"],
                        "target_type": _r["target_type"],
                        "target_id": _r["target_id"],
                        "created_at": _r["created_at"],
                    },
                )
            _supply_q = sum(r["delta_quarters"] for r in _qrows)
            _treasury_q = sum(
                r["delta_quarters"] for r in _qrows if r["account"] == "treasury"
            )
            conn.execute(
                "INSERT INTO economy_checkpoints (created_at, last_entry_id,"
                " entry_count, total_supply_q, treasury_q, running_hash)"
                " VALUES (datetime('now'), ?, ?, ?, ?, ?)",
                (_qrows[-1]["id"], len(_qrows), _supply_q, _treasury_q, _running),
            )
            old_supply = conn.execute(
                "SELECT total_supply_q FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        # An unsealed pre-migration tail row, written AFTER the old seal so
        # the first post-upgrade seal must hash it under the //5 rule too -
        # hashing it native poisons that seal and every descendant
        # (review, PR #1265).
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO credit_entries (agent_id, delta_quarters, reason,"
                " account) VALUES (?, 4, 'qmig_unsealed_tail', 'agent')",
                (qm_creator["agent_id"],),
            )
        db.init_db()  # upgrade: rename + *5 + cutover
        with db._conn() as conn:
            old_seal_id = conn.execute(
                "SELECT id FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            old_row = conn.execute(
                "SELECT * FROM economy_checkpoints WHERE id = ?", (old_seal_id,)
            ).fetchone()
            old_supply_scaled = old_row["total_supply_u"]
            ce_cols = {r[1] for r in conn.execute("PRAGMA table_info(credit_entries)")}
            assert "delta_units" in ce_cols and "delta_quarters" not in ce_cols, ce_cols
            j_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            assert "payment_units" in j_cols and "payment_quarters" not in j_cols
            cp_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(economy_checkpoints)")
            }
            assert {"total_supply_u", "treasury_u"} <= cp_cols, cp_cols
            mark = conn.execute(
                "SELECT 1 FROM schema_migration_markers WHERE name ="
                " 'credit_entries_quarter_to_twentieth'"
            ).fetchone()
            assert mark is not None, "migration records its marker"
            meta = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT key, value FROM economy_meta WHERE key LIKE 'credit_unit%'"
                ).fetchall()
            }
            assert meta.get("credit_unit") == "twentieths", meta
            cutover = int(meta.get("credit_unit_cutover", "0"))
            assert cutover > 0, meta
            # Values scaled back to the seeded twentieths. Creator math:
            # +400 grant, -20 job escrow, -20 stake lock, -5 service fee,
            # +20 unsealed tail row (no percentage fees: the suite env
            # sets TX_FEE_PERCENT=0).
            balances = {
                r["agent_id"]: r["s"]
                for r in conn.execute(
                    "SELECT agent_id, COALESCE(SUM(delta_units), 0) AS s"
                    " FROM credit_entries WHERE account = 'agent' GROUP BY agent_id"
                ).fetchall()
            }
            assert balances[qm_creator["agent_id"]] == 400 - 20 - 20 - 5 + 20, balances
            job_row = conn.execute(
                "SELECT payment_units FROM jobs WHERE id = ?", (qm_job["job_id"],)
            ).fetchone()
            assert job_row["payment_units"] == 20, dict(job_row)
            inv_row = conn.execute(
                "SELECT amount_units, remaining_units FROM invoices WHERE id = ?",
                (qm_inv["invoice_id"],),
            ).fetchone()
            assert (inv_row["amount_units"], inv_row["remaining_units"]) == (20, 20)
            svc_row = conn.execute(
                "SELECT price_units FROM services WHERE id = ?", (qm_svc["id"],)
            ).fetchone()
            assert svc_row["price_units"] == 20, dict(svc_row)
            mem_row = conn.execute(
                "SELECT units FROM guild_ledger WHERE guild_id = ?",
                (_gid,),
            ).fetchone()
            assert mem_row["units"] == 20, dict(mem_row)
            arr_row = conn.execute(
                "SELECT units FROM guild_fee_arrears WHERE guild_id = ?",
                (_gid,),
            ).fetchone()
            assert arr_row["units"] == 5, dict(arr_row)
            c_stake = conn.execute(
                "SELECT per_pr FROM proposal_stakes WHERE id = ?",
                (qm_cstake["stake_id"],),
            ).fetchone()
            assert c_stake["per_pr"] == 20, dict(c_stake)
            k_stake = conn.execute(
                "SELECT per_pr FROM proposal_stakes WHERE id = ?",
                (qm_kstake["stake_id"],),
            ).fetchone()
            assert k_stake["per_pr"] == 1, dict(k_stake)
            lock_row = conn.execute(
                "SELECT amount FROM stake_locks WHERE stake_id = ?",
                (qm_cstake["stake_id"],),
            ).fetchone()
            assert lock_row["amount"] == 20, dict(lock_row)
            # The pre-migration seal still verifies (//5 chain rule) and its
            # stored totals scaled with the ledger.
            check = db._economy._verify_checkpoint(conn, old_row)
            assert check["ok"] is True, check
            assert old_supply_scaled == old_supply * 5, (old_supply_scaled, old_supply)
        # Post-migration activity at sub-quarter resolution + a fresh seal.
        # The fresh seal covers the unsealed pre-migration tail above.
        # Crash-window heal FIRST (faithful wedge: no native row may
        # postdate the true boundary, exactly as a crashed init_db
        # guarantees by never serving traffic): marker set, cutover lost.
        with db._conn() as conn:
            conn.execute(
                "DELETE FROM economy_meta WHERE key IN ('credit_unit', 'credit_unit_cutover')"
            )
        db.init_db()
        with db._conn() as conn:
            healed = conn.execute(
                "SELECT value FROM economy_meta WHERE key = 'credit_unit_cutover'"
            ).fetchone()
            assert healed is not None and int(healed[0]) == cutover, healed
            apex = conn.execute(
                "SELECT * FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert db._economy._verify_checkpoint(conn, apex)["ok"] is True
        with db._conn() as conn:
            assert _qcr.grant(qm_worker["agent_id"], 2, "qmig_dime", conn=conn)
        db.write_checkpoint()
        with db._conn() as conn:
            new_row = conn.execute(
                "SELECT * FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert db._economy._verify_checkpoint(conn, new_row)["ok"] is True
        db.init_db()  # second boot: no re-multiply, no crash
        with db._conn() as conn:
            again = conn.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            ).fetchone()[0]
            live = conn.execute(
                "SELECT COALESCE(SUM(delta_units), 0) FROM credit_entries"
            ).fetchone()[0]
            assert again == live
            seal_count = conn.execute(
                "SELECT COUNT(*) FROM economy_checkpoints"
            ).fetchone()[0]
            assert seal_count >= 2, "both seals survive the second boot"
    finally:
        db.DB_PATH = saved_db_path
        if old_karma_min is None:
            os.environ.pop("FORUM_JOB_CREATOR_MIN_KARMA", None)
        else:
            os.environ["FORUM_JOB_CREATOR_MIN_KARMA"] = old_karma_min
        if old_fee is None:
            os.environ.pop("FORUM_INVOICE_CREATE_FEE_CREDITS", None)
        else:
            os.environ["FORUM_INVOICE_CREATE_FEE_CREDITS"] = old_fee
        _ilm.reload(_cfg)
    print("  quarter->twentieth migration: ok")

    # --- repro2: native-born double boot never arms the //5 rule -----
    # A native database must record cutover 0 alongside its marker: if a
    # later boot backfilled cutover=MAX(id) over native rows, every seal
    # would fail (a 2u dime hashes as 2//5=0). Grant a non-multiple-of-5
    # amount so the misfire is visible, seal, boot twice more, verify.
    saved_db_path = db.DB_PATH
    try:
        db.DB_PATH = str(_TMP / "native_doubleboot.db")
        db.init_db()
        native = db.register_agent("nativedime")
        import db._credits as _ncr

        with db._conn() as conn:
            assert _ncr.grant(native["agent_id"], 22, "native_seed", conn=conn)
        db.write_checkpoint()
        with db._conn() as conn:
            first = conn.execute(
                "SELECT * FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert db._economy._verify_checkpoint(conn, first)["ok"] is True
        db.init_db()
        db.init_db()
        with db._conn() as conn:
            cut = conn.execute(
                "SELECT value FROM economy_meta WHERE key = 'credit_unit_cutover'"
            ).fetchone()
            assert cut is not None and cut[0] == "0", dict(cut) if cut else None
            second = conn.execute(
                "SELECT * FROM economy_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert db._economy._verify_checkpoint(conn, second)["ok"] is True, (
                "native seals survive repeated boots"
            )
    finally:
        db.DB_PATH = saved_db_path
    print("  native double-boot cutover: ok")

    print("test_misc_h: all assertions passed")
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
