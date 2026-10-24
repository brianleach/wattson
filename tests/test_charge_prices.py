"""Step 2 (Oct 2026) tests: charging-price annotations in charge_prices.

Covers the migration backfill, start_ts-only identity, orphan
detection and reassignment, ambiguous-start safety, zero prices,
clearing, and rebuild/concurrency survival.
"""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import api
import db
import report
from dashboard_auth import DashboardAuth


def charge_dict(start_ts):
    return {"start_ts": start_ts, "end_ts": start_ts + 1800, "minutes": 30,
            "soc_start": 20.0, "soc_end": 40.0, "kwh_added": 10.0,
            "miles_added": 30.0, "charge_type": "DC"}


class ChargePriceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "tracker.db"
        patcher = mock.patch("db.DB_PATH", self.database)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_charge(self, start, price=None):
        with db.connect() as con:
            con.execute(
                "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
                " soc_end, kwh_added, miles_added, charge_type)"
                " VALUES (?, ?, 30, 20.0, 40.0, 10.0, 30.0, 'DC')",
                (start, start + 1800))
            if price is not None:
                con.execute(
                    "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                    " updated_ts) VALUES (?, ?, 1700000000)",
                    (start, price))

    def prices(self):
        with db.connect() as con:
            return [(r[0], r[1]) for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charge_prices"
                " ORDER BY start_ts")]

    def test_backfill_preserves_annotations_including_zero(self):
        # Pre-migration shape: prices on the charge rows, init not run.
        with sqlite3.connect(self.database) as con:
            con.executescript(db.SCHEMA)
            for start, price in ((100, 0.5), (200, 0.0)):
                con.execute(
                    "INSERT INTO charges (start_ts, end_ts, minutes,"
                    " soc_start, soc_end, kwh_added, miles_added,"
                    " charge_type, user_cost_per_kwh)"
                    " VALUES (?, ?, 30, 20.0, 40.0, 10.0, 30.0, 'DC', ?)",
                    (start, start + 1800, price))
        db.init()
        self.assertEqual(self.prices(), [(100, 0.5), (200, 0.0)])
        # Idempotent across restarts.
        db.init()
        self.assertEqual(self.prices(), [(100, 0.5), (200, 0.0)])
        # A later clear is never resurrected by a rerun migration.
        with db.connect() as con:
            con.execute("DELETE FROM charge_prices WHERE start_ts = 200")
        db.init()
        self.assertEqual(self.prices(), [(100, 0.5)])
        with db.connect() as con:
            marker = con.execute(
                "SELECT value FROM meta"
                " WHERE key = 'charge_prices_backfill'").fetchone()
        self.assertEqual(marker[0], "1")

    def test_apply_rejects_missing_and_duplicate_starts(self):
        db.init()
        self.seed_charge(100)
        self.seed_charge(100)  # duplicate start
        with db.connect() as con:
            self.assertEqual(
                db.apply_charge_price(con, 999, 0.5), "not_found")
            self.assertEqual(
                db.apply_charge_price(con, 100, 0.5), "ambiguous")
        self.assertEqual(self.prices(), [])

    def test_apply_and_clear_round_trip_including_zero(self):
        db.init()
        self.seed_charge(100)
        with db.connect() as con:
            self.assertEqual(
                db.apply_charge_price(con, 100, 0.0), "ok")
        self.assertEqual(self.prices(), [(100, 0.0)])
        with db.connect() as con:
            self.assertEqual(
                db.apply_charge_price(con, 100, 0.48), "ok")
        self.assertEqual(self.prices(), [(100, 0.48)])
        with db.connect() as con:
            self.assertEqual(
                db.apply_charge_price(con, 100, None), "ok")
        self.assertEqual(self.prices(), [])

    def test_resolve_withholds_ambiguous_and_lists_orphans(self):
        db.init()
        self.seed_charge(100, price=0.5)
        self.seed_charge(200)
        self.seed_charge(200)  # duplicate start
        with db.connect() as con:
            con.execute(
                "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                " updated_ts) VALUES (200, 0.4, 1700000000)")
            con.execute(
                "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                " updated_ts) VALUES (300, 0.6, 1700000000)")
            prices, orphans, ambiguous = db.charge_price_info(
                con, [100, 200, 200])
        self.assertEqual(prices, {100: 0.5})
        self.assertEqual(orphans, [(300, 0.6)])
        self.assertEqual(ambiguous, {200})

    def test_reassign_moves_and_removes_only_on_exact_match(self):
        db.init()
        self.seed_charge(100)  # unpriced target
        self.seed_charge(200, price=0.5)  # priced session
        self.seed_charge(400)
        self.seed_charge(400)  # duplicate-start target
        with db.connect() as con:
            con.execute(
                "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                " updated_ts) VALUES (300, 0.6, 1700000000)")  # orphan
            con.execute(
                "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                " updated_ts) VALUES (500, 0.7, 1700000000)")  # orphan
            self.assertEqual(
                db.reassign_charge_price(con, 300, 200, "move"),
                "target_priced")
            self.assertEqual(
                db.reassign_charge_price(con, 300, 999, "move"),
                "target_not_found")
            self.assertEqual(
                db.reassign_charge_price(con, 300, 300, "move"),
                "same_session")
            self.assertEqual(
                db.reassign_charge_price(con, 500, 400, "move"),
                "target_ambiguous")
            self.assertEqual(
                db.reassign_charge_price(con, 300, 100, "move"), "ok")
        self.assertEqual(self.prices(),
                         [(100, 0.6), (200, 0.5), (500, 0.7)])
        with db.connect() as con:
            self.assertEqual(
                db.reassign_charge_price(con, 999, 100, "move"),
                "orphan_not_found")
            # 100 is attached again after the move, so the orphan path
            # must refuse to remove it; the still-orphaned 500 removes.
            self.assertEqual(
                db.reassign_charge_price(con, 100, None, "remove"),
                "source_attached")
            self.assertEqual(
                db.reassign_charge_price(con, 500, None, "remove"), "ok")
        self.assertEqual(self.prices(), [(100, 0.6), (200, 0.5)])

    def test_reattached_orphan_refuses_stale_move_and_remove(self):
        db.init()
        self.seed_charge(100)  # unpriced target
        with db.connect() as con:
            con.execute(
                "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                " updated_ts) VALUES (300, 0.6, 1700000000)")  # orphan
        # The session reappears (a later re-derive restores it), so the
        # annotation is attached again before the stale form arrives.
        self.seed_charge(300)
        with db.connect() as con:
            self.assertEqual(
                db.reassign_charge_price(con, 300, 100, "move"),
                "source_attached")
            self.assertEqual(
                db.reassign_charge_price(con, 300, None, "remove"),
                "source_attached")
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_annotation_survives_rebuild_despite_id_shift(self):
        db.init()
        self.seed_charge(200, price=0.5)
        # Re-derive with an extra earlier session: ids shift, but the
        # annotation follows the start, not the id.
        db.rebuild_derived([], [], [charge_dict(100), charge_dict(200)], [])
        with db.connect() as con:
            ids = [r[1] for r in con.execute(
                "SELECT id, start_ts FROM charges ORDER BY id")]
        self.assertEqual(ids, [100, 200])
        self.assertEqual(self.prices(), [(200, 0.5)])

    def test_price_written_mid_rebuild_sequence_survives(self):
        db.init()
        self.seed_charge(100, price=0.5)
        # The old failure sequence, step by step: the rebuild reads the
        # old state, the user prices a session, the rebuild then
        # deletes and recreates the sessions. The annotation must
        # survive because it lives in its own table.
        with db.connect() as con:
            old = [r[0] for r in con.execute("SELECT start_ts FROM charges")]
        self.assertEqual(old, [100])
        with db.connect() as con:
            self.assertEqual(
                db.apply_charge_price(con, 100, 0.75), "ok")
        db.rebuild_derived([], [], [charge_dict(100)], [])
        self.assertEqual(self.prices(), [(100, 0.75)])

    def test_price_written_during_open_rebuild_lands(self):
        db.init()
        self.seed_charge(100)
        # Hold a rebuild-style write transaction open; the price write
        # must wait for the lock, then land (never fail silently).
        holder = sqlite3.connect(self.database, timeout=30)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("DELETE FROM charges")
        errors = []

        def write_price():
            try:
                with db.connect() as con:
                    db.apply_charge_price(con, 100, 0.5)
            except Exception as e:  # noqa: BLE001 - captured for assert
                errors.append(e)

        t = threading.Thread(target=write_price)
        t.start()
        time.sleep(0.5)  # let the writer block on the held lock
        holder.execute(
            "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
            " soc_end, kwh_added, miles_added, charge_type)"
            " VALUES (100, 1900, 30, 20.0, 40.0, 10.0, 30.0, 'DC')")
        holder.commit()
        holder.close()
        t.join(timeout=30)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.prices(), [(100, 0.5)])


class ChargePriceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "tracker.db"
        for target, value in [
            ("api.DB_PATH", self.database),
            ("db.DB_PATH", self.database),
            ("api.auth", DashboardAuth(None, lambda: 1700000000)),
            ("report.REPORT_PATH", root / "dashboard.html"),
            ("report.TOKENS_PATH", root / "missing-tokens"),
            ("gasprice.get_gas_price", lambda day: 4.5),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = api.app.test_client()
        db.init()
        with db.connect() as con:
            con.execute(
                "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
                " soc_end, kwh_added, miles_added, charge_type, label)"
                " VALUES (100, 1900, 30, 20.0, 40.0, 10.0, 30.0, 'DC',"
                " 'Test charger')")
            con.execute(
                "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                " updated_ts) VALUES (300, 0.6, 1700000000)")  # orphan

    def prices(self):
        with db.connect() as con:
            return [(r[0], r[1]) for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charge_prices"
                " ORDER BY start_ts")]

    def test_stale_submit_returns_refresh_error_and_stores_nothing(self):
        resp = self.client.post("/api/charge-cost-form", data={
            "charge_id": "7", "start_ts": "999", "cost_per_kwh": "0.5"},
            headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("refresh", resp.get_data(as_text=True).lower())
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_valid_submit_ignores_wrong_charge_id(self):
        # Session 100 has id 1; submit a wrong id with the right start.
        resp = self.client.post("/api/charge-cost-form", data={
            "charge_id": "999", "start_ts": "100", "cost_per_kwh": "0.5"},
            headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [(100, 0.5), (300, 0.6)])

    def test_reassign_moves_orphan_via_endpoint(self):
        resp = self.client.post("/api/charge-price-reassign", data={
            "orphan_start_ts": "300", "target_start_ts": "100",
            "action": "move"}, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [(100, 0.6)])

    def test_reassign_remove_via_endpoint(self):
        resp = self.client.post("/api/charge-price-reassign", data={
            "orphan_start_ts": "300", "action": "remove"},
            headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [])

    def test_stale_reassign_forms_refused_once_orphan_reattaches(self):
        # The orphaned session reappears before the stale form arrives.
        with db.connect() as con:
            con.execute(
                "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
                " soc_end, kwh_added, miles_added, charge_type, label)"
                " VALUES (300, 2100, 30, 20.0, 40.0, 10.0, 30.0, 'DC',"
                " 'Test charger')")
        resp = self.client.post("/api/charge-price-reassign", data={
            "orphan_start_ts": "300", "target_start_ts": "100",
            "action": "move"}, headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("attached", resp.get_data(as_text=True).lower())
        resp = self.client.post("/api/charge-price-reassign", data={
            "orphan_start_ts": "300", "action": "remove"},
            headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.prices(), [(300, 0.6)])


class OrphanDisplayTests(unittest.TestCase):
    def test_orphan_note_and_form_render_without_sessions(self):
        html = report._charges_html(
            [], orphans=[(1700000000, 0.48)], ambiguous=())
        self.assertIn("has no matching session any more", html)
        self.assertIn("/api/charge-price-reassign", html)
        self.assertIn("Remove orphan", html)
        self.assertIn("$0.48/kWh", html)

    def test_ambiguous_note_renders(self):
        html = report._charges_html(
            [], orphans=[], ambiguous={1700000000})
        self.assertIn("held back from all of them", html)

    def test_no_note_when_nothing_orphaned(self):
        html = report._charges_html([], orphans=[], ambiguous=())
        self.assertNotIn("charge-price-reassign", html)


if __name__ == "__main__":
    unittest.main()
