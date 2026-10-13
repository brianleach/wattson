import asyncio
import calendar
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import poller
import report
import riv

T = 1790000000
SESSION_ID = "synthetic-txn-0001"


def api_session(**overrides):
    session = {
        "chargerType": "DCFC", "currencyCode": "USD", "paidTotal": 33.72,
        "startInstant": "2026-01-15T18:20:12.000Z",
        "endInstant": "2026-01-15T18:48:13.000Z",
        "totalEnergyKwh": 60.2, "rangeAddedKm": 250.0,
        "city": "Exampleville", "transactionId": SESSION_ID,
        "vendor": "Example Network", "isPublic": True,
        "isHomeCharger": False,
    }
    session.update(overrides)
    return session


def stored(**overrides):
    row = {"transaction_id": SESSION_ID, "start_ts": T, "end_ts": T + 1680,
           "kwh": 60.2, "paid_total": 33.72, "currency": "USD",
           "vendor": "Example Network", "city": "Exampleville",
           "range_added_km": 250.0, "is_home": False, "is_public": True}
    row.update(overrides)
    return row


def charge(start_ts=T + 100, end_ts=T + 1700, kwh_added=51.77,
           charge_type="DC fast"):
    return {"start_ts": start_ts, "end_ts": end_ts,
            "minutes": (end_ts - start_ts) / 60, "soc_start": 20.0,
            "soc_end": 80.0, "kwh_added": kwh_added, "miles_added": 150.0,
            "charge_type": charge_type}


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                    Path(self.tmp.name) / "test.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()

    def derive(self, *charges):
        db.rebuild_derived([], [], list(charges))

    def set_cost(self, start_ts, cost):
        with db.connect() as con:
            con.execute("UPDATE charges SET user_cost_per_kwh = ?"
                        " WHERE start_ts = ?", (cost, start_ts))

    def costs(self):
        with db.connect() as con:
            return [r["user_cost_per_kwh"] for r in con.execute(
                "SELECT user_cost_per_kwh FROM charges ORDER BY start_ts")]


class ParseTests(unittest.TestCase):
    def test_parses_billed_session(self):
        response = {"data": {"getCompletedSessionSummaries": [api_session()]}}
        rows = riv.parse_rivian_sessions(
            response["data"]["getCompletedSessionSummaries"])
        self.assertEqual(rows, [{
            "transaction_id": SESSION_ID,
            "start_ts": calendar.timegm((2026, 1, 15, 18, 20, 12)),
            "end_ts": calendar.timegm((2026, 1, 15, 18, 48, 13)),
            "kwh": 60.2, "paid_total": 33.72, "currency": "USD",
            "vendor": "Example Network", "city": "Exampleville",
            "range_added_km": 250.0, "is_home": False, "is_public": True}])

    def test_keeps_home_session_without_price(self):
        home = api_session(paidTotal=None, currencyCode=None, vendor=None,
                           isPublic=None, isHomeCharger=True)
        [row] = riv.parse_rivian_sessions([home])
        self.assertIsNone(row["paid_total"])
        self.assertTrue(row["is_home"])

    def test_session_without_transaction_keyed_by_start(self):
        [row] = riv.parse_rivian_sessions([api_session(transactionId=None)])
        self.assertEqual(row["transaction_id"],
                         "start-2026-01-15T18:20:12.000Z")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FetchTests(unittest.TestCase):
    def fake_client(self, result):
        class FakeRivian:
            def __init__(self, session):
                pass

            async def create_csrf_token(self):
                pass

            async def _Rivian__graphql_query(self, headers, url, body):
                if isinstance(result, Exception):
                    raise result
                return result

        return mock.patch.object(riv, "Rivian", FakeRivian)

    def test_returns_session_summaries(self):
        payload = {"data": {"getCompletedSessionSummaries": [api_session()]}}
        with self.fake_client(FakeResponse(payload)):
            sessions = asyncio.run(riv._fetch_charge_sessions("sess"))
        self.assertEqual(sessions, [api_session()])

    def test_unauthenticated_raises_needs_reauth(self):
        with self.fake_client(RuntimeError("UNAUTHENTICATED")):
            with self.assertRaises(riv.NeedsReauth):
                asyncio.run(riv._fetch_charge_sessions("sess"))

    def test_other_errors_propagate(self):
        with self.fake_client(RuntimeError("timeout")):
            with self.assertRaises(RuntimeError):
                asyncio.run(riv._fetch_charge_sessions("sess"))


class ApplyTests(DbTestCase):
    def test_prices_overlapping_dc_charge_from_battery_kwh(self):
        self.derive(charge())
        db.upsert_rivian_sessions([stored()])
        self.assertAlmostEqual(self.costs()[0], 33.72 / 51.77)

    def test_manual_value_not_overwritten(self):
        self.derive(charge())
        self.set_cost(T + 100, 0.5)
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.costs(), [0.5])

    def test_ac_charge_not_matched(self):
        self.derive(charge(charge_type="AC"))
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.costs(), [None])

    def test_non_usd_session_skipped(self):
        self.derive(charge())
        db.upsert_rivian_sessions([stored(currency="CAD")])
        self.assertEqual(self.costs(), [None])

    def test_unpaid_session_does_not_price(self):
        self.derive(charge())
        db.upsert_rivian_sessions([stored(paid_total=None)])
        self.assertEqual(self.costs(), [None])

    def test_charge_without_kwh_skipped(self):
        self.derive(charge(kwh_added=0))
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.costs(), [None])

    def test_adjacent_charge_without_overlap_not_matched(self):
        self.derive(charge(start_ts=T + 1680, end_ts=T + 3000))
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.costs(), [None])

    def test_session_prices_only_best_overlap(self):
        self.derive(charge(start_ts=T - 600, end_ts=T + 60),
                    charge(start_ts=T + 100, end_ts=T + 1700))
        db.upsert_rivian_sessions([stored()])
        early, best = self.costs()
        self.assertIsNone(early)
        self.assertAlmostEqual(best, 33.72 / 51.77)

    def test_session_of_manually_priced_charge_does_not_spill(self):
        self.derive(charge(start_ts=T - 600, end_ts=T + 60),
                    charge(start_ts=T + 100, end_ts=T + 1700))
        self.set_cost(T + 100, 0.5)
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.costs(), [None, 0.5])

    def test_session_survives_rederive_with_shifted_start(self):
        self.derive(charge())
        db.upsert_rivian_sessions([stored()])
        self.derive(charge(start_ts=T + 220))
        self.assertAlmostEqual(self.costs()[0], 33.72 / 51.77)


class UnmatchedSessionTests(DbTestCase):
    def ids(self):
        return [r["transaction_id"] for r in db.unmatched_rivian_sessions()]

    def test_session_overlapping_no_charge(self):
        self.derive(charge(start_ts=T + 5000, end_ts=T + 6000))
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.ids(), [SESSION_ID])

    def test_session_overlapping_ac_charge_is_matched(self):
        self.derive(charge(charge_type="AC"))
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.ids(), [])

    def test_touching_charge_does_not_match(self):
        self.derive(charge(start_ts=T + 1680, end_ts=T + 3000))
        db.upsert_rivian_sessions([stored()])
        self.assertEqual(self.ids(), [SESSION_ID])


def session_row(**overrides):
    row = stored()
    row.update(overrides)
    return row


class BackfillCostTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(report, "HOME_KWH_RATE", 0.10)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_session_costs_what_was_paid(self):
        b = report._backfill_cost(session_row(), 3.0)
        self.assertEqual(b["ev_cost"], 33.72)
        self.assertTrue(b["is_dc"])
        self.assertAlmostEqual(b["miles"], 250.0 * report.MI_PER_KM)

    def test_home_charger_costs_home_rate(self):
        b = report._backfill_cost(session_row(
            kwh=10.0, paid_total=None, currency=None, is_home=True,
            is_public=None), 3.0)
        self.assertAlmostEqual(b["ev_cost"], 1.0)
        self.assertFalse(b["is_dc"])

    def test_unpaid_private_session_counts_as_home(self):
        b = report._backfill_cost(session_row(
            kwh=10.0, paid_total=None, currency=None, is_home=None,
            is_public=None), 3.0)
        self.assertAlmostEqual(b["ev_cost"], 1.0)

    def test_home_session_without_kwh_skipped(self):
        self.assertIsNone(report._backfill_cost(session_row(
            kwh=0, paid_total=None, is_home=True), 3.0))

    def test_unpaid_public_session_skipped(self):
        self.assertIsNone(report._backfill_cost(session_row(
            paid_total=None, currency=None), 3.0))

    def test_non_usd_public_session_skipped(self):
        self.assertIsNone(report._backfill_cost(session_row(
            currency="CAD"), 3.0))

    def test_miles_fall_back_to_efficiency_without_range(self):
        b = report._backfill_cost(session_row(range_added_km=0), 3.0)
        self.assertAlmostEqual(b["miles"], 60.2 * 3.0)

    def test_log_row_marks_session_and_names_home(self):
        b = report._backfill_cost(session_row(
            kwh=10.0, paid_total=None, currency=None, vendor=None,
            city=None, is_home=True), 3.0)
        html = report._backfill_row_html(
            {**session_row(vendor=None, city=None), **b,
             "gas_price": 4.0, "save": 3.8})
        self.assertIn("from Rivian", html)
        self.assertIn('<div class="trip-route">Home</div>', html)


    def test_log_row_for_unnamed_public_session(self):
        row = session_row(vendor=None, city=None)
        html = report._backfill_row_html(
            {**row, **report._backfill_cost(row, 3.0),
             "gas_price": 4.0, "save": -8.0})
        self.assertIn('<div class="trip-route">Unknown location</div>', html)


class ReportSavingsTests(DbTestCase):
    def setUp(self):
        super().setUp()
        tmp = Path(self.tmp.name)
        self.report_path = tmp / "dashboard.html"
        for name, value in (("REPORT_PATH", self.report_path),
                            ("TOKENS_PATH", tmp / "missing.enc"),
                            ("HOME_KWH_RATE", 0.10),
                            ("GAS_COMPARISON_MPG", 25.0)):
            patcher = mock.patch.object(report, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"WATTSON_GAS_PRICE": "4.00"})
        env.start()
        self.addCleanup(env.stop)

    def test_backfilled_sessions_join_total_without_double_counting(self):
        # Tracked DC charge priced by its overlapping Rivian session:
        # gas 150 mi / 25 * $4 = $24, paid $33.72, saved -$9.72.
        self.derive(charge())
        db.upsert_rivian_sessions([
            stored(),
            # Untracked home session: 10 kWh * $0.10 = $1, gas on
            # 10 kWh * 3 mi/kWh = 30 mi = $4.80, saved $3.80.
            stored(transaction_id="synthetic-txn-0002", start_ts=T - 90000,
                   end_ts=T - 80000, kwh=10.0, paid_total=None,
                   currency=None, vendor=None, city=None,
                   range_added_km=0, is_home=True, is_public=None),
            # Untracked public session: paid $2, gas on 50 km = $4.97.
            stored(transaction_id="synthetic-txn-0003", start_ts=T - 50000,
                   end_ts=T - 49000, kwh=5.0, paid_total=2.0,
                   range_added_km=50.0),
            # Untracked public session Rivian didn't price: left out.
            stored(transaction_id="synthetic-txn-0004", start_ts=T - 30000,
                   end_ts=T - 29000, paid_total=None, currency=None),
        ])
        report.generate()
        html = self.report_path.read_text()
        public_save = 50.0 * report.MI_PER_KM / 25 * 4 - 2.0
        total = -9.72 + 3.80 + public_save
        hero = re.search(r'<div class="v green">(.*?)</div>', html).group(1)
        self.assertEqual(hero, report._fmt_money(int(round(total))))
        self.assertEqual(html.count('<span class="dim">from Rivian</span>'),
                         2)


class LatestUnpricedDcEndTests(DbTestCase):
    def test_unpriced_dc_charge(self):
        self.derive(charge())
        self.assertEqual(db.latest_unpriced_dc_end(T), T + 1700)

    def test_charge_ended_before_window(self):
        self.derive(charge())
        self.assertIsNone(db.latest_unpriced_dc_end(T + 1700))

    def test_priced_dc_charge(self):
        self.derive(charge())
        self.set_cost(T + 100, 0.5)
        self.assertIsNone(db.latest_unpriced_dc_end(T))

    def test_dc_charge_without_kwh(self):
        self.derive(charge(kwh_added=0))
        self.assertIsNone(db.latest_unpriced_dc_end(T))

    def test_ac_charge(self):
        self.derive(charge(charge_type="AC"))
        self.assertIsNone(db.latest_unpriced_dc_end(T))


class SyncTests(DbTestCase):
    CHARGE_END = T + 1700
    NOW = CHARGE_END + 120

    def setUp(self):
        super().setUp()
        tmp = Path(self.tmp.name)
        self.state_path = tmp / "poller_state.json"
        self.flag = tmp / "NEEDS_REAUTH"
        for name, value in (("STATE_PATH", self.state_path),
                            ("FLAG_REAUTH", self.flag)):
            patcher = mock.patch.object(poller, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.derive(charge())

    def sync(self, state, fetch, now=NOW):
        with mock.patch.object(poller, "fetch_rivian_sessions", fetch):
            poller.sync_rivian_sessions(state, now)

    def saved_state(self):
        return json.loads(self.state_path.read_text())

    def test_first_sync_runs_without_unpriced_charge(self):
        self.set_cost(T + 100, 0.5)
        fetch = mock.Mock(return_value=[])
        self.sync({}, fetch)
        fetch.assert_called_once()

    def test_new_charge_synced_right_after_it_ends(self):
        state = {"last_rivian_sync": self.CHARGE_END - 600}
        self.sync(state, mock.Mock(return_value=[stored()]))
        self.assertAlmostEqual(self.costs()[0], 33.72 / 51.77)
        self.assertEqual(state["last_rivian_sync"], self.NOW)
        self.assertEqual(self.saved_state()["last_rivian_sync"], self.NOW)

    def test_retry_waits_an_hour(self):
        fetch = mock.Mock(return_value=[])
        self.sync({"last_rivian_sync": self.NOW}, fetch,
                  now=self.NOW + poller.SESSION_RETRY_SECONDS - 1)
        fetch.assert_not_called()

    def test_retry_after_an_hour(self):
        fetch = mock.Mock(return_value=[])
        self.sync({"last_rivian_sync": self.NOW}, fetch,
                  now=self.NOW + poller.SESSION_RETRY_SECONDS)
        fetch.assert_called_once()

    def test_charge_older_than_window_not_synced(self):
        fetch = mock.Mock(return_value=[])
        now = self.CHARGE_END + poller.SESSION_WINDOW_SECONDS
        self.sync({"last_rivian_sync": self.CHARGE_END - 600}, fetch,
                  now=now)
        fetch.assert_not_called()

    def test_skipped_without_unpriced_dc_charge(self):
        self.set_cost(T + 100, 0.5)
        fetch = mock.Mock(return_value=[])
        self.sync({"last_rivian_sync": self.CHARGE_END - 600}, fetch)
        fetch.assert_not_called()

    def test_needs_reauth_sets_flag(self):
        with mock.patch("builtins.print"):
            self.sync({}, mock.Mock(side_effect=riv.NeedsReauth()))
        self.assertTrue(self.flag.exists())

    def test_fetch_error_is_swallowed_and_throttled(self):
        with mock.patch.object(poller.traceback, "print_exc"):
            self.sync({}, mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(self.saved_state()["last_rivian_sync"], self.NOW)


class DeriveSyncReportTests(unittest.TestCase):
    def test_sync_runs_between_derive_and_report(self):
        calls = mock.Mock()
        calls.derive.return_value = ([], [], [], [])
        with mock.patch("analyze.derive", calls.derive), \
                mock.patch("report.generate", calls.generate), \
                mock.patch.object(poller, "sync_rivian_sessions",
                                  calls.sync):
            poller._derive_sync_report({}, T)
        self.assertEqual([c[0] for c in calls.mock_calls],
                         ["derive", "sync", "generate"])


if __name__ == "__main__":
    unittest.main()
