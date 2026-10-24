"""Transaction-integrity tests for db.connect() and db.rebuild_derived().

Step 1 (Oct 2026): a failed block must never commit partial work, and
a failed rebuild must leave the previous derived tables untouched.
Step 2: manually entered charging prices live in charge_prices, which
rebuilds never read or write, so they survive every rebuild outcome.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db


def trip(start_ts=10, **over):
    row = {"start_ts": start_ts, "end_ts": start_ts + 1000, "miles": 5.0,
           "kwh_used": 1.0, "wh_per_mi": 200.0}
    row.update(over)
    return row


def charge(start_ts=50, cost=None, **over):
    row = {"start_ts": start_ts, "end_ts": start_ts + 1800, "minutes": 30,
           "soc_start": 20.0, "soc_end": 40.0, "kwh_added": 10.0,
           "miles_added": 30.0, "charge_type": "DC"}
    row.update(over)
    if cost is not None:
        row["_cost"] = cost
    return row


def drain(sleep_ts=100, **over):
    row = {"sleep_ts": sleep_ts, "wake_ts": sleep_ts + 36000, "hours": 10.0,
           "soc_start": 60.0, "soc_end": 59.0, "kwh_lost": 0.88,
           "watts_avg": 44.0}
    row.update(over)
    return row


def errand(day="2026-10-20", **over):
    row = {"day": day, "drives": 2, "miles": 12.0, "kwh_used": 3.0}
    row.update(over)
    return row


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        patcher = mock.patch("db.DB_PATH", root / "tracker.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()

    def rows(self, table, order="rowid"):
        # Strip the auto id: rebuilds reassign ids, so compare content.
        with db.connect() as con:
            return [tuple(r)[1:] for r in con.execute(
                f"SELECT * FROM {table} ORDER BY {order}")]

    def seed(self):
        with db.connect() as con:
            con.execute(
                "INSERT INTO trips (start_ts, end_ts, miles, kwh_used,"
                " wh_per_mi) VALUES (1, 2, 10.0, 3.0, 300.0)")
            con.execute(
                "INSERT INTO drains (sleep_ts, wake_ts, hours, soc_start,"
                " soc_end, kwh_lost, watts_avg) VALUES (3, 4, 5.0, 70.0,"
                " 69.0, 0.88, 44.0)")
            con.execute(
                "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
                " soc_end, kwh_added, miles_added, charge_type,"
                " user_cost_per_kwh) VALUES (5, 6, 30, 20.0, 40.0, 10.0,"
                " 30.0, 'DC', 0.5)")
            con.execute(
                "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
                " updated_ts) VALUES (5, 0.5, 1700000000)")
            con.execute(
                "INSERT INTO errands (day, drives, miles, kwh_used)"
                " VALUES ('2026-10-19', 1, 8.0, 2.0)")

    def test_successful_transaction_commits(self):
        with db.connect() as con:
            con.execute(
                "INSERT INTO places (lat_r, lon_r, label, ts)"
                " VALUES (1.0, 2.0, 'Home', 100)")
        self.assertEqual(len(self.rows("places")), 1)

    def test_failed_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with db.connect() as con:
                con.execute(
                    "INSERT INTO places (lat_r, lon_r, label, ts)"
                    " VALUES (1.0, 2.0, 'Home', 100)")
                raise RuntimeError("boom")
        self.assertEqual(len(self.rows("places")), 0)

    def test_failed_rebuild_leaves_originals_untouched(self):
        self.seed()
        before = {t: self.rows(t) for t in
                  ("trips", "drains", "charges", "errands")}
        # The charges entry is missing "miles_added", a required key.
        # Its list comprehension is evaluated after the DELETEs and
        # after the trips/drains inserts, so this fails midway.
        bad = charge()
        del bad["miles_added"]
        with self.assertRaises(KeyError):
            db.rebuild_derived([trip()], [drain()], [bad], [errand()])
        after = {t: self.rows(t) for t in
                 ("trips", "drains", "charges", "errands")}
        self.assertEqual(after, before)

    def test_rebuild_leaves_annotations_untouched(self):
        # Step 2 replaces the old cost-read test: rebuilds never read
        # or write the annotation table, so an annotation survives both
        # a successful rebuild and a failed one.
        self.seed()
        db.rebuild_derived([trip()], [drain()], [charge(start_ts=5)],
                           [errand()])
        with db.connect() as con:
            costs = [(r[0], r[1]) for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charge_prices")]
        self.assertEqual(costs, [(5, 0.5)])
        bad = charge()
        del bad["miles_added"]
        with self.assertRaises(KeyError):
            db.rebuild_derived([trip()], [drain()], [bad], [errand()])
        with db.connect() as con:
            costs = [(r[0], r[1]) for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charge_prices")]
        self.assertEqual(costs, [(5, 0.5)])

    def test_successful_rebuild_preserves_manual_costs(self):
        self.seed()
        # The re-derived charge shares the seeded row's start_ts. The
        # price now survives in the annotation table, not on the
        # derived row (whose cost column rebuilds leave NULL).
        db.rebuild_derived([trip()], [drain()], [charge(start_ts=5)],
                           [errand()])
        with db.connect() as con:
            costs = [(r[0], r[1]) for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charge_prices")]
            row_costs = [r[0] for r in con.execute(
                "SELECT user_cost_per_kwh FROM charges")]
        self.assertEqual(costs, [(5, 0.5)])
        self.assertEqual(row_costs, [None])

    def test_rebuild_twice_gives_same_tables(self):
        first = ([trip(), trip(100)], [drain()], [charge()],
                 [errand(), errand("2026-10-21")])
        db.rebuild_derived(*first)
        snapshot = {t: self.rows(t) for t in
                    ("trips", "drains", "charges", "errands")}
        db.rebuild_derived(*first)
        again = {t: self.rows(t) for t in
                 ("trips", "drains", "charges", "errands")}
        self.assertEqual(again, snapshot)


if __name__ == "__main__":
    unittest.main()
