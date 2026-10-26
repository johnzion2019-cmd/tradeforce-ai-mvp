"""Billing page upgrade CTAs follow the active subscription."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_db_file = tempfile.NamedTemporaryFile(prefix="tradeforce-billing-test-", suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _db_file.name

from fastapi import FastAPI
from fastapi.testclient import TestClient

import phase5_billing
from phase5_billing import billing_page, router, upgrade_cta_visible


STRIPE_ENV = {
    "STRIPE_SECRET_KEY": "sk_test_billing_cta",
    "STRIPE_PRO_PRICE_ID": "price_pro_test",
    "STRIPE_BUSINESS_PRICE_ID": "price_business_test",
}


def subscription(plan="starter", status="inactive", customer=""):
    return SimpleNamespace(plan=plan, status=status, stripe_customer_id=customer)


def page(sub):
    return billing_page("contractor@example.com", sub)


class StripeEnvMixin(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in STRIPE_ENV}
        os.environ.update(STRIPE_ENV)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class UpgradeVisibilityTests(unittest.TestCase):
    def test_missing_and_starter_show_both(self):
        for sub in (None, subscription(), subscription(plan="starter", status="inactive")):
            self.assertEqual(upgrade_cta_visible(sub), {"pro": True, "business": True})

    def test_inactive_paid_plans_show_both(self):
        for plan in ("pro", "business"):
            for status in ("inactive", "canceled", "past_due", ""):
                self.assertEqual(
                    upgrade_cta_visible(subscription(plan=plan, status=status)),
                    {"pro": True, "business": True},
                )

    def test_active_pro_hides_only_pro(self):
        self.assertEqual(
            upgrade_cta_visible(subscription(plan="Pro", status="Active")),
            {"pro": False, "business": True},
        )

    def test_active_business_hides_both(self):
        self.assertEqual(
            upgrade_cta_visible(subscription(plan=" business ", status="ACTIVE")),
            {"pro": False, "business": False},
        )


class BillingPageTests(StripeEnvMixin):
    def test_starter_shows_both_upgrades_and_no_portal(self):
        html = page(None)
        self.assertIn("<strong>Current plan:</strong> Starter", html)
        self.assertIn("<strong>Status:</strong> Inactive", html)
        self.assertIn('action="/billing/checkout/pro"', html)
        self.assertIn('action="/billing/checkout/business"', html)
        self.assertIn(">Upgrade to Pro</button>", html)
        self.assertIn(">Upgrade to Business</button>", html)
        self.assertNotIn('action="/billing/portal"', html)
        self.assertNotIn("Manage subscription", html)
        self.assertNotIn('type="submit" disabled', html)

    def test_inactive_pro_still_shows_both_upgrades(self):
        html = page(subscription(plan="pro", status="canceled", customer="cus_123"))
        self.assertIn("<strong>Current plan:</strong> Pro", html)
        self.assertIn("<strong>Status:</strong> Canceled", html)
        self.assertIn(">Upgrade to Pro</button>", html)
        self.assertIn(">Upgrade to Business</button>", html)
        self.assertIn(">Manage subscription</button>", html)
        self.assertEqual(html.count('action="/billing/checkout/pro"'), 1)
        self.assertEqual(html.count('action="/billing/checkout/business"'), 1)
        self.assertEqual(html.count('action="/billing/portal"'), 1)

    def test_active_pro_hides_pro_upgrade_and_keeps_business_and_portal(self):
        html = page(subscription(plan="pro", status="active", customer="cus_123"))
        self.assertIn("<strong>Current plan:</strong> Pro", html)
        self.assertIn("<strong>Status:</strong> Active", html)
        self.assertNotIn(">Upgrade to Pro</button>", html)
        self.assertNotIn('action="/billing/checkout/pro"', html)
        self.assertIn(">Upgrade to Business</button>", html)
        self.assertIn('action="/billing/checkout/business"', html)
        self.assertIn(">Manage subscription</button>", html)
        self.assertIn('action="/billing/portal"', html)
        self.assertIn('<p class="plan-state">Current plan</p>', html)
        self.assertNotIn("Included in your plan", html)

    def test_active_business_hides_both_upgrades_and_keeps_portal(self):
        html = page(subscription(plan="business", status="active", customer="cus_999"))
        self.assertIn("<strong>Current plan:</strong> Business", html)
        self.assertIn("<strong>Status:</strong> Active", html)
        self.assertNotIn(">Upgrade to Pro</button>", html)
        self.assertNotIn(">Upgrade to Business</button>", html)
        self.assertNotIn('action="/billing/checkout/pro"', html)
        self.assertNotIn('action="/billing/checkout/business"', html)
        self.assertIn(">Manage subscription</button>", html)
        self.assertEqual(html.count('action="/billing/portal"'), 1)
        self.assertIn("Included in your plan", html)
        self.assertEqual(html.count('<p class="plan-state">Current plan</p>'), 1)

    def test_active_pro_without_customer_has_no_portal_button(self):
        html = page(subscription(plan="pro", status="active", customer=""))
        self.assertNotIn(">Upgrade to Pro</button>", html)
        self.assertIn(">Upgrade to Business</button>", html)
        self.assertNotIn("Manage subscription", html)

    def test_unconfigured_stripe_still_renders_disabled_upgrades_for_starter(self):
        saved = {key: os.environ.pop(key) for key in STRIPE_ENV}
        try:
            html = page(subscription())
        finally:
            os.environ.update(saved)
        self.assertIn('<button type="submit" disabled>Upgrade to Pro</button>', html)
        self.assertIn('<button type="submit" disabled>Upgrade to Business</button>', html)
        self.assertIn("Stripe billing is not fully configured yet.", html)

    def test_email_is_escaped(self):
        html = billing_page('<contractor@example.com>', subscription())
        self.assertIn("&lt;contractor@example.com&gt;", html)
        self.assertNotIn("<contractor@example.com>", html)


class _Query:
    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _DB:
    def __init__(self, sub):
        self._sub = sub

    def query(self, model):
        return _Query(self._sub)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class CheckoutGuardTests(StripeEnvMixin):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def _post(self, plan, sub):
        created = []

        def create(**params):
            created.append(params)
            return SimpleNamespace(url="https://checkout.stripe.test/c/session")

        with (
            patch.object(phase5_billing, "account_id", return_value=7),
            patch.object(phase5_billing, "account_email", return_value="contractor@example.com"),
            patch.object(phase5_billing, "SessionLocal", return_value=_DB(sub)),
            patch.object(phase5_billing.stripe.checkout.Session, "create", side_effect=create),
        ):
            response = self.client.post(f"/billing/checkout/{plan}", follow_redirects=False)
        return response, created

    def test_starter_checkout_still_starts_stripe(self):
        response, created = self._post("pro", None)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "https://checkout.stripe.test/c/session")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["mode"], "subscription")
        self.assertEqual(created[0]["line_items"], [{"price": "price_pro_test", "quantity": 1}])
        self.assertEqual(created[0]["customer_email"], "contractor@example.com")
        self.assertNotIn("customer", created[0])
        self.assertEqual(created[0]["metadata"]["plan"], "pro")
        self.assertTrue(created[0]["success_url"].endswith("/billing?billing=success"))
        self.assertTrue(created[0]["cancel_url"].endswith("/billing?billing=cancelled"))

    def test_active_pro_can_still_checkout_business(self):
        response, created = self._post(
            "business",
            subscription(plan="pro", status="active", customer="cus_123"),
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["customer"], "cus_123")
        self.assertEqual(created[0]["line_items"], [{"price": "price_business_test", "quantity": 1}])
        self.assertEqual(created[0]["metadata"]["plan"], "business")
        self.assertNotIn("customer_email", created[0])

    def test_active_pro_cannot_checkout_pro_again(self):
        response, created = self._post(
            "pro",
            subscription(plan="pro", status="active", customer="cus_123"),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(created, [])
        self.assertIn("Manage subscription", response.json()["detail"])

    def test_active_business_cannot_checkout_either_plan(self):
        sub = subscription(plan="business", status="active", customer="cus_999")
        for plan in ("pro", "business"):
            response, created = self._post(plan, sub)
            self.assertEqual(response.status_code, 409, plan)
            self.assertEqual(created, [])

    def test_canceled_pro_can_checkout_again(self):
        response, created = self._post(
            "pro",
            subscription(plan="pro", status="canceled", customer="cus_123"),
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(created[0]["metadata"]["plan"], "pro")
        self.assertEqual(created[0]["customer"], "cus_123")

    def test_portal_still_opens_for_existing_customer(self):
        opened = []

        def create(**params):
            opened.append(params)
            return SimpleNamespace(url="https://billing.stripe.test/p/session")

        with (
            patch.object(phase5_billing, "account_id", return_value=7),
            patch.object(phase5_billing, "account_email", return_value="contractor@example.com"),
            patch.object(
                phase5_billing,
                "SessionLocal",
                return_value=_DB(subscription(plan="pro", status="active", customer="cus_123")),
            ),
            patch.object(phase5_billing.stripe.billing_portal.Session, "create", side_effect=create),
        ):
            response = self.client.post("/billing/portal", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "https://billing.stripe.test/p/session")
        self.assertEqual(opened[0]["customer"], "cus_123")
        self.assertTrue(opened[0]["return_url"].endswith("/billing"))


if __name__ == "__main__":
    unittest.main()
