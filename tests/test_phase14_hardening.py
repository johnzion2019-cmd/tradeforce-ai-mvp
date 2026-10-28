"""Phase 14 CSRF, rate limiting, and storage health flags. Offline only."""
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs

_db_file = tempfile.NamedTemporaryFile(prefix="tradeforce-phase14-", suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _db_file.name

import app as phase14
from fastapi.testclient import TestClient

from app import (
    CSRF_EXEMPT_PATHS,
    RATE_LIMITS,
    _RATE_BUCKETS,
    _csrf_token_from_body,
    _inject_csrf_into_html,
    _rate_class,
    app,
)


_CSRF_RE = re.compile(r"""name=["']csrf_token["'] value=["']([^"']+)["']""")
_S3_KEYS = ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION")
_STRIPE_KEYS = ("STRIPE_SECRET_KEY", "STRIPE_PRO_PRICE_ID", "STRIPE_BUSINESS_PRICE_ID", "STRIPE_WEBHOOK_SECRET")


def _browser():
    return TestClient(app, base_url="https://testserver")


def _token(html: str) -> str:
    match = _CSRF_RE.search(html)
    if not match:
        raise AssertionError("CSRF token missing from HTML")
    return match.group(1)


def _ip(address: str) -> dict:
    return {"x-forwarded-for": address}


class Phase14Case(unittest.TestCase):
    def setUp(self):
        _RATE_BUCKETS.clear()
        self.client = _browser()

    def _page_token(self, path: str, address: str) -> str:
        response = self.client.get(path, headers=_ip(address))
        self.assertEqual(response.status_code, 200, response.text[:300])
        return _token(response.text)


class CsrfTests(Phase14Case):
    def test_login_post_without_token_is_rejected(self):
        response = self.client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "not-the-password"},
            headers=_ip("203.0.113.10"),
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF validation failed", response.text)

    def test_login_post_with_session_token_reaches_the_handler(self):
        token = self._page_token("/login", "203.0.113.11")
        response = self.client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "not-the-password", "csrf_token": token},
            headers=_ip("203.0.113.11"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid email or password", response.text)
        self.assertIn("csrf_token", response.text)

    def test_header_token_is_accepted(self):
        token = self._page_token("/login", "203.0.113.12")
        response = self.client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "not-the-password"},
            headers={**_ip("203.0.113.12"), "x-csrf-token": token},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid email or password", response.text)

    def test_signup_with_token_creates_the_account(self):
        token = self._page_token("/signup", "203.0.113.13")
        response = self.client.post(
            "/signup",
            data={
                "email": "phase14-worker@example.com",
                "password": "password12",
                "role": "worker",
                "csrf_token": token,
            },
            headers=_ip("203.0.113.13"),
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")

    def test_multipart_form_token_is_accepted(self):
        token = self._page_token("/login", "203.0.113.14")
        response = self.client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "not-the-password", "csrf_token": token},
            files={"unused": ("note.txt", b"hello", "text/plain")},
            headers=_ip("203.0.113.14"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid email or password", response.text)

    def test_stripe_webhook_and_admin_reset_skip_csrf(self):
        saved_webhook = os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        saved_admin = os.environ.get("ADMIN_RESET_TOKEN")
        os.environ["ADMIN_RESET_TOKEN"] = "phase14-admin-token"
        try:
            webhook = self.client.post(
                "/stripe/webhook",
                content=b"{}",
                headers={"content-type": "application/json", **_ip("203.0.113.15")},
            )
            self.assertNotEqual(webhook.status_code, 403)
            self.assertEqual(webhook.status_code, 503)

            admin = self.client.post(
                "/phase5/admin/reset-subscription",
                json={"email": "missing-phase14@example.com"},
                headers=_ip("203.0.113.16"),
            )
            self.assertNotEqual(admin.status_code, 403)
            self.assertEqual(admin.status_code, 401)
        finally:
            if saved_webhook is None:
                os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
            else:
                os.environ["STRIPE_WEBHOOK_SECRET"] = saved_webhook
            if saved_admin is None:
                os.environ.pop("ADMIN_RESET_TOKEN", None)
            else:
                os.environ["ADMIN_RESET_TOKEN"] = saved_admin

    def test_billing_checkout_requires_token_and_ignores_stripe_when_absent(self):
        token = self._page_token("/signup", "203.0.113.17")
        created = self.client.post(
            "/signup",
            data={
                "email": "phase14-contractor@example.com",
                "password": "password12",
                "role": "contractor",
                "csrf_token": token,
            },
            headers=_ip("203.0.113.17"),
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)

        billing = self.client.get("/billing", headers=_ip("203.0.113.17"))
        self.assertEqual(billing.status_code, 200)
        self.assertIn('action="/billing/checkout/pro"', billing.text)
        self.assertGreaterEqual(billing.text.count("csrf_token"), 2)
        billing_token = _token(billing.text)

        denied = self.client.post("/billing/checkout/pro", headers=_ip("203.0.113.17"))
        self.assertEqual(denied.status_code, 403)

        saved = {key: os.environ.pop(key, None) for key in _STRIPE_KEYS}
        try:
            allowed = self.client.post(
                "/billing/checkout/pro",
                data={"csrf_token": billing_token},
                headers=_ip("203.0.113.17"),
            )
            portal = self.client.post(
                "/billing/portal",
                data={"csrf_token": billing_token},
                headers=_ip("203.0.113.17"),
            )
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(allowed.status_code, 503)
        self.assertNotEqual(portal.status_code, 403)

    def test_get_offer_accept_is_not_a_state_change(self):
        response = self.client.get("/application/1/offer/accept", headers=_ip("203.0.113.18"))
        self.assertEqual(response.status_code, 405)

    def test_token_parser_and_html_injection(self):
        encoded = b"email=a%40example.com&csrf_token=abc123"
        self.assertEqual(_csrf_token_from_body("application/x-www-form-urlencoded", encoded), "abc123")
        multipart = (
            b"------bound\r\n"
            b'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
            b"multipart-token\r\n"
            b"------bound--\r\n"
        )
        self.assertEqual(
            _csrf_token_from_body("multipart/form-data; boundary=----bound", multipart),
            "multipart-token",
        )
        self.assertEqual(_csrf_token_from_body("application/json", b'{"csrf_token":"nope"}'), "")

        page = "<form method='get' action='/search'></form><form method=\"post\" action='/login'></form>"
        once = _inject_csrf_into_html(page, "t" * 32)
        twice = _inject_csrf_into_html(once, "t" * 32)
        self.assertEqual(once.count("csrf_token"), 1)
        self.assertEqual(twice.count("csrf_token"), 1)
        self.assertIn("action='/login'", once)
        self.assertNotIn("action='/search'><input", once)
        self.assertEqual(parse_qs("csrf_token=" + _token(once))["csrf_token"], ["t" * 32])


class RateLimitTests(Phase14Case):
    def test_sensitive_routes_have_their_own_buckets(self):
        def request_for(path):
            return SimpleNamespace(url=SimpleNamespace(path=path))

        self.assertEqual(_rate_class(request_for("/login")), "login")
        self.assertEqual(_rate_class(request_for("/signup")), "signup")
        self.assertEqual(_rate_class(request_for("/forgot-password")), "forgot")
        self.assertEqual(_rate_class(request_for("/reset-password")), "reset")
        self.assertEqual(_rate_class(request_for("/billing/checkout/pro")), "checkout")
        self.assertEqual(_rate_class(request_for("/billing/portal")), "portal")
        self.assertIn("/stripe/webhook", CSRF_EXEMPT_PATHS)
        self.assertIn("/phase5/admin/reset-subscription", CSRF_EXEMPT_PATHS)
        self.assertLessEqual(RATE_LIMITS["login"][0], 10)
        self.assertLessEqual(RATE_LIMITS["forgot"][0], 5)

    def test_login_returns_429_after_the_limit(self):
        address = "203.0.113.21"
        token = self._page_token("/login", address)
        limit = RATE_LIMITS["login"][0]
        for _ in range(limit):
            response = self.client.post(
                "/login",
                data={"email": "nobody@example.com", "password": "not-the-password", "csrf_token": token},
                headers=_ip(address),
            )
            self.assertEqual(response.status_code, 400, response.text[:200])
        blocked = self.client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "not-the-password", "csrf_token": token},
            headers=_ip(address),
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Too many requests", blocked.text)
        self.assertIn("Retry-After", blocked.headers)

    def test_forgot_password_returns_429_after_the_limit(self):
        address = "203.0.113.22"
        token = self._page_token("/forgot-password", address)
        limit = RATE_LIMITS["forgot"][0]
        for _ in range(limit):
            response = self.client.post(
                "/forgot-password",
                data={"email": "nobody@example.com", "csrf_token": token},
                headers=_ip(address),
            )
            self.assertEqual(response.status_code, 200, response.text[:240])
            self.assertIn("If that email is registered", response.text)
        blocked = self.client.post(
            "/forgot-password",
            data={"email": "nobody@example.com", "csrf_token": token},
            headers=_ip(address),
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Too many requests", blocked.text)
        self.assertTrue(blocked.headers["retry-after"].isdigit())


class HealthTests(Phase14Case):
    def test_health_reports_phase_14_without_secrets(self):
        saved = {key: os.environ.get(key) for key in _S3_KEYS}
        for key in _S3_KEYS:
            os.environ.pop(key, None)
        try:
            health = self.client.get("/health")
            phase = self.client.get("/phase14/health")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertEqual(body["phase"], 16)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["document_storage"], "database-fallback")
        self.assertEqual(body["csrf"], "enabled")
        self.assertEqual(body["rate_limit"], "in-memory")
        for feature in ("messaging", "notifications", "talent-search", "favorites", "company-profiles", "opportunity-search", "saved-workspace", "pro-gating", "csrf", "rate-limit"):
            self.assertIn(feature, body["features"])

        self.assertEqual(phase.status_code, 200)
        flags = phase.json()
        self.assertEqual(flags["phase"], 14)
        self.assertEqual(flags["csrf"], "enabled")
        self.assertEqual(flags["rate_limit"], "in-memory")
        self.assertEqual(flags["rate_limit_scope"], "per-process")
        self.assertEqual(flags["document_storage"], "database-fallback")
        self.assertEqual(phase14.main.CSRF_PROTECTION, "enabled")

    def test_object_storage_mode_is_reported_without_leaking_secrets(self):
        saved = {key: os.environ.get(key) for key in _S3_KEYS}
        secret = "phase14-s3-secret-do-not-leak"
        os.environ["S3_BUCKET"] = "tradeforce-docs"
        os.environ["S3_ACCESS_KEY_ID"] = "phase14-access-key"
        os.environ["S3_SECRET_ACCESS_KEY"] = secret
        os.environ["S3_ENDPOINT_URL"] = "https://example.r2.cloudflarestorage.com"
        os.environ["S3_REGION"] = "auto"
        try:
            response = self.client.get("/phase14/health")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["document_storage"], "object-storage")
        self.assertNotIn(secret, response.text)
        self.assertNotIn("phase14-access-key", response.text)


if __name__ == "__main__":
    unittest.main()
