import asyncio
import calendar
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import poller
import riv

T = 1790000000
RECEIPT_ID = "synthetic-txn-0001"


def api_session(**overrides):
    session = {
        "chargerType": "DCFC", "currencyCode": "USD", "paidTotal": 33.72,
        "startInstant": "2026-01-15T18:20:12.000Z",
        "endInstant": "2026-01-15T18:48:13.000Z",
        "totalEnergyKwh": 60.2, "rangeAddedKm": 250.0,
        "city": "Exampleville", "transactionId": RECEIPT_ID,
        "vendor": "Example Network", "isPublic": True,
        "isHomeCharger": False,
    }
    session.update(overrides)
    return session


def receipt(**overrides):
    row = {"transaction_id": RECEIPT_ID, "start_ts": T, "end_ts": T + 1680,
           "kwh": 60.2, "paid_total": 33.72, "currency": "USD",
           "vendor": "Example Network", "city": "Exampleville"}
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
        rows = riv.parse_charge_receipts(
            response["data"]["getCompletedSessionSummaries"])
        self.assertEqual(rows, [{
            "transaction_id": RECEIPT_ID,
            "start_ts": calendar.timegm((2026, 1, 15, 18, 20, 12)),
            "end_ts": calendar.timegm((2026, 1, 15, 18, 48, 13)),
            "kwh": 60.2, "paid_total": 33.72, "currency": "USD",
            "vendor": "Example Network", "city": "Exampleville"}])

    def test_home_session_without_price_skipped(self):
        home = api_session(paidTotal=None, currencyCode=None, vendor=None,
                           isPublic=None, isHomeCharger=True)
        self.assertEqual(riv.parse_charge_receipts([home]), [])

    def test_session_without_transaction_skipped(self):
        self.assertEqual(
            riv.parse_charge_receipts([api_session(transactionId=None)]), [])


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
        db.upsert_charge_receipts([receipt()])
        self.assertAlmostEqual(self.costs()[0], 33.72 / 51.77)

    def test_manual_value_not_overwritten(self):
        self.derive(charge())
        self.set_cost(T + 100, 0.5)
        db.upsert_charge_receipts([receipt()])
        self.assertEqual(self.costs(), [0.5])

    def test_ac_charge_not_matched(self):
        self.derive(charge(charge_type="AC"))
        db.upsert_charge_receipts([receipt()])
        self.assertEqual(self.costs(), [None])

    def test_non_usd_receipt_skipped(self):
        self.derive(charge())
        db.upsert_charge_receipts([receipt(currency="CAD")])
        self.assertEqual(self.costs(), [None])

    def test_charge_without_kwh_skipped(self):
        self.derive(charge(kwh_added=0))
        db.upsert_charge_receipts([receipt()])
        self.assertEqual(self.costs(), [None])

    def test_adjacent_charge_without_overlap_not_matched(self):
        self.derive(charge(start_ts=T + 1680, end_ts=T + 3000))
        db.upsert_charge_receipts([receipt()])
        self.assertEqual(self.costs(), [None])

    def test_receipt_prices_only_best_overlap(self):
        self.derive(charge(start_ts=T - 600, end_ts=T + 60),
                    charge(start_ts=T + 100, end_ts=T + 1700))
        db.upsert_charge_receipts([receipt()])
        early, best = self.costs()
        self.assertIsNone(early)
        self.assertAlmostEqual(best, 33.72 / 51.77)

    def test_receipt_of_manually_priced_charge_does_not_spill(self):
        self.derive(charge(start_ts=T - 600, end_ts=T + 60),
                    charge(start_ts=T + 100, end_ts=T + 1700))
        self.set_cost(T + 100, 0.5)
        db.upsert_charge_receipts([receipt()])
        self.assertEqual(self.costs(), [None, 0.5])

    def test_receipt_survives_rederive_with_shifted_start(self):
        self.derive(charge())
        db.upsert_charge_receipts([receipt()])
        self.derive(charge(start_ts=T + 220))
        self.assertAlmostEqual(self.costs()[0], 33.72 / 51.77)


class UnpricedDcTests(DbTestCase):
    def test_unpriced_dc_charge(self):
        self.derive(charge())
        self.assertTrue(db.has_unpriced_dc_charge())

    def test_priced_dc_charge(self):
        self.derive(charge())
        self.set_cost(T + 100, 0.5)
        self.assertFalse(db.has_unpriced_dc_charge())

    def test_dc_charge_without_kwh(self):
        self.derive(charge(kwh_added=0))
        self.assertFalse(db.has_unpriced_dc_charge())

    def test_ac_charge(self):
        self.derive(charge(charge_type="AC"))
        self.assertFalse(db.has_unpriced_dc_charge())


class SyncTests(DbTestCase):
    NOW = T + 7200

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

    def sync(self, state, fetch):
        with mock.patch.object(poller, "fetch_charge_receipts", fetch):
            poller.sync_charge_receipts(state, self.NOW)

    def saved_state(self):
        return json.loads(self.state_path.read_text())

    def test_sync_prices_charge_and_records_attempt(self):
        state = {}
        self.sync(state, mock.Mock(return_value=[receipt()]))
        self.assertAlmostEqual(self.costs()[0], 33.72 / 51.77)
        self.assertEqual(state["last_receipt_sync"], self.NOW)
        self.assertEqual(self.saved_state()["last_receipt_sync"], self.NOW)

    def test_throttled_within_window(self):
        fetch = mock.Mock(return_value=[])
        self.sync({"last_receipt_sync": self.NOW - 60}, fetch)
        fetch.assert_not_called()

    def test_skipped_without_unpriced_dc_charge(self):
        self.set_cost(T + 100, 0.5)
        fetch = mock.Mock(return_value=[])
        self.sync({}, fetch)
        fetch.assert_not_called()

    def test_needs_reauth_sets_flag(self):
        with mock.patch("builtins.print"):
            self.sync({}, mock.Mock(side_effect=riv.NeedsReauth()))
        self.assertTrue(self.flag.exists())

    def test_fetch_error_is_swallowed_and_throttled(self):
        with mock.patch.object(poller.traceback, "print_exc"):
            self.sync({}, mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(self.saved_state()["last_receipt_sync"], self.NOW)


if __name__ == "__main__":
    unittest.main()
