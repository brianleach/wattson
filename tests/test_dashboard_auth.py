import os
import unittest
from unittest import mock

import api
from dashboard_auth import (MAX_FAILURES, SESSION_COOKIE, SESSION_MS,
                            THROTTLE_MS, DashboardAuth, token_from_env)

TOKEN = "synthetic-dashboard-token-0123456789"
START = 1700000000


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self):
        return self.t


def cookie_value(set_cookie):
    return set_cookie.split(";")[0].split("=", 1)[1]


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.original_auth = api.auth
        self.client = api.app.test_client(use_cookies=False)
        self.configure(TOKEN)

    def tearDown(self):
        api.auth = self.original_auth

    def configure(self, token):
        api.auth = DashboardAuth(token, self.clock)

    def check(self, headers):
        return self.client.get("/api/auth/check", headers=headers).status_code

    def post_login(self, password, ip="198.51.100.7", headers=None):
        return self.client.post(
            "/login", data={"password": password},
            headers={"X-Forwarded-For": ip, **(headers or {})})

    def login_cookie(self):
        return cookie_value(self.post_login(TOKEN).headers["Set-Cookie"])

    def with_cookie(self, value):
        return {"Cookie": f"{SESSION_COOKIE}={value}"}


class CheckTests(RouteTestCase):
    def test_unconfigured_check_passes(self):
        self.configure(None)
        self.assertEqual(self.check({}), 200)

    def test_configured_check_denies_without_credentials(self):
        self.assertEqual(self.check({}), 401)

    def test_bearer_token_passes(self):
        self.assertEqual(
            self.check({"Authorization": f"Bearer {TOKEN}"}), 200)

    def test_cookie_passes(self):
        self.assertEqual(self.check(self.with_cookie(self.login_cookie())),
                         200)

    def test_tampered_signature_fails(self):
        value = self.login_cookie()
        flipped = "0" if value[-1] != "0" else "1"
        self.assertEqual(self.check(self.with_cookie(value[:-1] + flipped)),
                         401)

    def test_expired_cookie_fails(self):
        value = self.login_cookie()
        self.clock.t += SESSION_MS // 1000 + 1
        self.assertEqual(self.check(self.with_cookie(value)), 401)

    def test_token_rotation_invalidates_cookie(self):
        value = self.login_cookie()
        self.configure("synthetic-rotated-token-9876543210")
        self.assertEqual(self.check(self.with_cookie(value)), 401)


class LoginTests(RouteTestCase):
    def test_bad_password_returns_401_and_counts(self):
        resp = self.post_login("wrong")
        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Wrong password", resp.data)
        self.assertEqual(api.auth.failures["198.51.100.7"]["count"], 1)

    def test_fifth_failure_trips_lockout(self):
        for _ in range(MAX_FAILURES):
            self.assertEqual(self.post_login("wrong").status_code, 401)
        self.assertEqual(self.post_login("wrong").status_code, 429)

    def test_lockout_blocks_correct_password(self):
        for _ in range(MAX_FAILURES):
            self.post_login("wrong")
        resp = self.post_login(TOKEN)
        self.assertEqual(resp.status_code, 429)
        self.assertNotIn("Set-Cookie", resp.headers)

    def test_lockout_expires_after_window(self):
        for _ in range(MAX_FAILURES):
            self.post_login("wrong")
        self.clock.t += THROTTLE_MS // 1000
        self.assertEqual(self.post_login(TOKEN).status_code, 303)

    def test_unconfigured_login_redirects_without_cookie(self):
        self.configure(None)
        resp = self.post_login("anything")
        self.assertEqual(resp.status_code, 303)
        self.assertNotIn("Set-Cookie", resp.headers)

    def test_good_login_sets_cookie_with_303(self):
        resp = self.post_login(TOKEN)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["Location"], "/")
        cookie = resp.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(f"{SESSION_COOKIE}="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_secure_flag_only_with_forwarded_https(self):
        plain = self.post_login(TOKEN).headers["Set-Cookie"]
        https = self.post_login(
            TOKEN, headers={"X-Forwarded-Proto": "https"}
        ).headers["Set-Cookie"]
        self.assertNotIn("Secure", plain)
        self.assertIn("Secure", https)

    def test_logout_clears_cookie(self):
        resp = self.client.post("/logout")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["Location"], "/login")
        cookie = resp.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(f"{SESSION_COOKIE}=;"))
        self.assertIn("Max-Age=0", cookie)


class TokenFromEnvTests(unittest.TestCase):
    def test_empty_token_is_unset(self):
        with mock.patch.dict(os.environ, {"WATTSON_DASHBOARD_TOKEN": "  "}):
            self.assertIsNone(token_from_env())


if __name__ == "__main__":
    unittest.main()
