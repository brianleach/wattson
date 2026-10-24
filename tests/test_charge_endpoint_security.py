"""Step 3 (Oct 2026) tests: authorization and origin protection for the
charging-price POST endpoints, plus an endpoint-level concurrency test.

Covers unauthorized, bearer, session-cookie (valid, invalid,
expired), cross-origin (Origin and Referer), missing-origin,
no-token local behavior, existing cost/orphan functionality under
auth, and the endpoint's check-and-write under a concurrent rebuild.
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
from dashboard_auth import SESSION_COOKIE, SESSION_MS, DashboardAuth

TOKEN = "step3-test-token"
START = 1700000000
ORIGIN = "http://localhost"


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self):
        return self.t


def cookie_value(set_cookie):
    return set_cookie.split(";")[0].split("=", 1)[1]


class EndpointSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "tracker.db"
        self.clock = Clock()
        self.original_auth = api.auth
        for target, value in [
            ("api.DB_PATH", self.database),
            ("db.DB_PATH", self.database),
            ("report.REPORT_PATH", root / "dashboard.html"),
            ("report.TOKENS_PATH", root / "missing-tokens"),
            ("gasprice.get_gas_price", lambda day: 4.5),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        api.auth = DashboardAuth(TOKEN, self.clock)
        self.addCleanup(setattr, api, "auth", self.original_auth)
        self.client = api.app.test_client(use_cookies=False)
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

    def tearDown(self):
        api.auth = self.original_auth

    def prices(self):
        with db.connect() as con:
            return [(r[0], r[1]) for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charge_prices"
                " ORDER BY start_ts")]

    def login_cookie(self):
        resp = self.client.post(
            "/login", data={"password": TOKEN},
            headers={"X-Forwarded-For": "198.51.100.7"})
        self.assertEqual(resp.status_code, 303)
        return cookie_value(resp.headers["Set-Cookie"])

    def post_cost(self, headers):
        return self.client.post(
            "/api/charge-cost-form",
            data={"charge_id": "1", "start_ts": "100",
                  "cost_per_kwh": "0.5"},
            headers=headers)

    def post_reassign(self, headers, action="move"):
        data = {"orphan_start_ts": "300", "action": action}
        if action == "move":
            data["target_start_ts"] = "100"
        return self.client.post(
            "/api/charge-price-reassign", data=data, headers=headers)

    def test_unauthenticated_posts_rejected_without_writes(self):
        for post in (self.post_cost, self.post_reassign):
            resp = post({"Origin": ORIGIN})
            self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_bearer_token_allows_cost_post(self):
        resp = self.post_cost({"Origin": ORIGIN,
                               "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [(100, 0.5), (300, 0.6)])

    def test_session_cookie_allows_reassign_and_clear(self):
        cookie = self.login_cookie()
        headers = {"Origin": ORIGIN,
                   "Cookie": f"{SESSION_COOKIE}={cookie}"}
        resp = self.post_reassign(headers)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [(100, 0.6)])
        resp = self.client.post(
            "/api/charge-cost-form",
            data={"charge_id": "1", "start_ts": "100",
                  "cost_per_kwh": ""},
            headers=headers)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [])

    def test_cross_origin_origin_header_rejected(self):
        resp = self.post_cost({"Origin": "https://evil.example",
                               "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_cross_origin_referer_fallback_rejected(self):
        resp = self.post_cost({"Referer": "https://evil.example/page",
                               "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_same_origin_referer_without_origin_allows(self):
        # Older browsers may send no Origin on a form POST; the
        # Referer fallback keeps their legitimate submissions working.
        resp = self.post_cost({"Referer": ORIGIN + "/#charging",
                               "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [(100, 0.5), (300, 0.6)])

    def test_missing_origin_and_referer_rejected(self):
        resp = self.post_cost({"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_invalid_session_cookie_rejected(self):
        resp = self.post_cost({"Origin": ORIGIN,
                               "Cookie": f"{SESSION_COOKIE}=garbage"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_expired_session_cookie_rejected(self):
        cookie = self.login_cookie()
        self.clock.t += SESSION_MS // 1000 + 1
        resp = self.post_cost(
            {"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}={cookie}"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.prices(), [(300, 0.6)])

    def test_no_token_preserves_local_behavior_but_not_cross_origin(self):
        api.auth = DashboardAuth(None, self.clock)
        resp = self.post_cost({"Origin": ORIGIN})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [(100, 0.5), (300, 0.6)])
        resp = self.post_cost({"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)

    def test_orphan_remove_under_auth(self):
        resp = self.post_reassign(
            {"Origin": ORIGIN, "Authorization": f"Bearer {TOKEN}"},
            action="remove")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.prices(), [])


class EndpointConcurrencyTests(unittest.TestCase):
    """Step 2's check-and-write, exercised through the real endpoint.

    Holds a rebuild-style write transaction open while the cost form
    POST runs in another thread: the POST must wait for the lock, see
    the re-inserted session, and land the price (never 503, never
    lost).
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.database = root / "tracker.db"
        for target, value in [
            ("api.DB_PATH", self.database),
            ("db.DB_PATH", self.database),
            ("api.auth", DashboardAuth(None, lambda: START)),
            ("report.REPORT_PATH", root / "dashboard.html"),
            ("report.TOKENS_PATH", root / "missing-tokens"),
            ("gasprice.get_gas_price", lambda day: 4.5),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init()
        with db.connect() as con:
            con.execute(
                "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
                " soc_end, kwh_added, miles_added, charge_type)"
                " VALUES (100, 1900, 30, 20.0, 40.0, 10.0, 30.0, 'DC')")

    def prices(self):
        with db.connect() as con:
            return [(r[0], r[1]) for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charge_prices"
                " ORDER BY start_ts")]

    def test_endpoint_post_during_open_rebuild_lands(self):
        holder = sqlite3.connect(self.database, timeout=30)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("DELETE FROM charges")
        results = []
        errors = []

        def post_price():
            try:
                client = api.app.test_client(use_cookies=False)
                resp = client.post(
                    "/api/charge-cost-form",
                    data={"charge_id": "1", "start_ts": "100",
                          "cost_per_kwh": "0.5"},
                    headers={"Origin": ORIGIN})
                results.append(resp.status_code)
            except Exception as e:  # noqa: BLE001 - captured for assert
                errors.append(e)

        t = threading.Thread(target=post_price)
        t.start()
        time.sleep(0.5)  # let the POST block on the held lock
        holder.execute(
            "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
            " soc_end, kwh_added, miles_added, charge_type)"
            " VALUES (100, 1900, 30, 20.0, 40.0, 10.0, 30.0, 'DC')")
        holder.commit()
        holder.close()
        t.join(timeout=30)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [303])
        self.assertEqual(self.prices(), [(100, 0.5)])


if __name__ == "__main__":
    unittest.main()
