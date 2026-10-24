"""Optional gate in front of the dashboard.

Set WATTSON_DASHBOARD_TOKEN to require a login. Leave it unset and the
dashboard stays open, which is fine on localhost or a private network.
nginx asks /api/auth/check before serving anything, so this module only
decides yes or no; it never sees the dashboard itself.

There is deliberately no loopback bypass: inside Docker, host-local curl
and a tunnel daemon both arrive from the bridge gateway and cannot be
told apart.
"""
import hashlib
import hmac
import os

SESSION_COOKIE = "wattson_session"
SESSION_MS = 30 * 24 * 60 * 60 * 1000
MAX_FAILURES = 5
THROTTLE_MS = 10 * 60 * 1000


def token_from_env():
    # Compose passes an empty .env value through as "", which must not
    # become a password that matches an empty form field.
    return os.environ.get("WATTSON_DASHBOARD_TOKEN", "").strip() or None


def _equal(a, b):
    # Bytes, because compare_digest raises on non-ASCII str, and an
    # exception here would turn nginx's auth check into a site-wide 500.
    return hmac.compare_digest(a.encode(), b.encode())


def _sign(token, expires_ms):
    # Sign with a key derived from the token under a domain
    # separator, never the raw token itself. Rotating the token
    # still logs everyone out.
    key = hashlib.sha256(
        b"wattson dashboard session v1\0" + token.encode()).digest()
    return hmac.new(key, str(expires_ms).encode(),
                    hashlib.sha256).hexdigest()


def _first_hop(value):
    first = (value or "").split(",")[0].strip()
    return first or None


def client_ip(headers, remote_addr):
    """Who to blame for a failed login. Only ever used for throttling."""
    return ((headers.get("CF-Connecting-IP") or "").strip()
            or _first_hop(headers.get("X-Forwarded-For"))
            or (headers.get("X-Real-IP") or "").strip()
            or remote_addr
            or "unknown")


def is_https(headers):
    return (_first_hop(headers.get("X-Forwarded-Proto")) or "").lower() == "https"


class DashboardAuth:
    def __init__(self, token, now):
        self.token = token
        self.now = now
        self.failures = {}

    def _now_ms(self):
        return int(self.now() * 1000)

    def _cookie_valid(self, value):
        expires, _, signature = value.rpartition(".")
        try:
            expires_ms = int(expires)
        except ValueError:
            return False
        if not _equal(signature, _sign(self.token, expires_ms)):
            return False
        return expires_ms > self._now_ms()

    def bearer_valid(self, authorization):
        """True only for a valid Bearer token. Unlike a cookie, a bearer
        token is not attached automatically by browsers, so a request
        carrying one cannot be a cross-site form POST."""
        return bool(self.token) and bool(authorization) \
            and authorization.startswith("Bearer ") \
            and _equal(authorization[7:], self.token)

    def authorized(self, authorization, cookie):
        if not self.token:
            return True
        if self.bearer_valid(authorization):
            return True
        return bool(cookie) and self._cookie_valid(cookie)

    def login(self, ip, password):
        """Returns 'ok', 'bad', 'throttled' or 'unconfigured'."""
        if not self.token:
            return "unconfigured"
        at = self._now_ms()
        self.failures = {k: v for k, v in self.failures.items()
                         if at - v["first_at"] < THROTTLE_MS}
        entry = self.failures.get(ip)
        if entry and entry["count"] >= MAX_FAILURES:
            return "throttled"
        if not _equal(password, self.token):
            entry = entry or {"count": 0, "first_at": at}
            entry["count"] += 1
            self.failures[ip] = entry
            return "bad"
        self.failures.pop(ip, None)
        return "ok"

    def _attributes(self, https):
        # Lax, not Strict: a link to the dashboard from a chat app should
        # arrive logged in. Lax still keeps the cookie off cross-site POSTs.
        parts = ["Path=/", "HttpOnly", "SameSite=Lax"]
        if https:
            parts.append("Secure")
        return "; ".join(parts)

    def session_cookie(self, https):
        expires_ms = self._now_ms() + SESSION_MS
        value = f"{expires_ms}.{_sign(self.token, expires_ms)}"
        return (f"{SESSION_COOKIE}={value}; {self._attributes(https)}; "
                f"Max-Age={SESSION_MS // 1000}")

    def clear_cookie(self, https):
        return f"{SESSION_COOKIE}=; {self._attributes(https)}; Max-Age=0"
