"""Phase 16 Pro/Business gating for saved candidates and contractor messaging."""
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import text

_db_file = tempfile.NamedTemporaryFile(prefix="tradeforce-phase16-", suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _db_file.name

import main
from app import CSRF_EXEMPT_PATHS, _RATE_BUCKETS, app
from fastapi.testclient import TestClient
from phase5_billing import has_active_paid_subscription


_CSRF_RE = re.compile(r"""name=["']csrf_token["'] value=["']([^"']+)["']""")
_NOTE = "phase16-private-note"


def _browser():
    return TestClient(app, base_url="https://testserver")


def _token(html: str) -> str:
    match = _CSRF_RE.search(html)
    if not match:
        raise AssertionError("CSRF token missing from HTML")
    return match.group(1)


class Phase16Case(unittest.TestCase):
    def setUp(self):
        _RATE_BUCKETS.clear()
        self.client = _browser()
        self._octet = 20

    def _headers(self):
        headers = {"x-forwarded-for": f"198.51.100.{self._octet}"}
        self._octet += 1
        return headers

    def _signup(self, client, email, role, headers):
        page = client.get("/signup?role=" + role, headers=headers)
        self.assertEqual(page.status_code, 200, page.text[:300])
        response = client.post(
            "/signup",
            data={"email": email, "password": "password12", "role": role, "csrf_token": _token(page.text)},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text[:300])
        return client

    def _contractor(self, email):
        headers = self._headers()
        return self._signup(_browser(), email, "contractor", headers), headers

    def _worker(self, email, name, trade="Welder"):
        headers = self._headers()
        client = self._signup(_browser(), email, "worker", headers)
        page = client.get("/dashboard", headers=headers)
        self.assertEqual(page.status_code, 200, page.text[:300])
        response = client.post(
            "/worker/profile",
            data={
                "name": name,
                "trade": trade,
                "location": "Houston, TX",
                "experience": "8",
                "certifications": "AWS",
                "phone": "555-0100",
                "desired_pay": "$42/hr",
                "willing_to_travel": "yes",
                "available": "yes",
                "csrf_token": _token(page.text),
            },
            headers=headers,
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303), response.text[:400])
        return client, headers, self._worker_id(email)

    def _worker_id(self, email):
        db = main.SessionLocal()
        try:
            return db.query(main.Worker).filter(main.Worker.email == email).one().id
        finally:
            db.close()

    def _user_id(self, email):
        db = main.SessionLocal()
        try:
            return db.query(main.UserAccount).filter(main.UserAccount.email == email).one().id
        finally:
            db.close()

    def _set_plan(self, email, plan, status):
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == email).one()
            db.execute(text("DELETE FROM subscriptions WHERE user_id = :user_id"), {"user_id": user.id})
            db.execute(
                text(
                    "INSERT INTO subscriptions (user_id, plan, status, stripe_customer_id, stripe_subscription_id, current_period_end) "
                    "VALUES (:user_id, :plan, :status, '', '', '')"
                ),
                {"user_id": user.id, "plan": plan, "status": status},
            )
            db.commit()
        finally:
            db.close()

    def _token_from(self, client, path, headers):
        page = client.get(path, headers=headers)
        self.assertEqual(page.status_code, 200, page.text[:500])
        return _token(page.text), page

    def _post_job(self, client, headers):
        token, _page = self._token_from(client, "/dashboard", headers)
        created = client.post(
            "/contractor/job",
            data={
                "company": "Phase16 Industrial",
                "contact": "Casey",
                "trade": "Welder",
                "workers_needed": "1",
                "location": "Houston, TX",
                "start_date": "",
                "duration": "3 months",
                "pay_range": "$40/hr",
                "notes": "",
                "required_experience": "0",
                "csrf_token": token,
            },
            headers=headers,
            follow_redirects=False,
        )
        self.assertIn(created.status_code, (302, 303), created.text[:300])
        return created

    def _favorite_count(self, email, worker_id):
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == email).one()
            return db.query(main.FavoriteCandidate).filter(
                main.FavoriteCandidate.contractor_user_id == user.id,
                main.FavoriteCandidate.worker_id == worker_id,
            ).count()
        finally:
            db.close()

    def _seed_favorite(self, email, worker_id, note="", label=""):
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == email).one()
            row = main.FavoriteCandidate(
                contractor_user_id=user.id,
                worker_id=worker_id,
                note=note,
                label=label,
            )
            db.add(row)
            db.commit()
        finally:
            db.close()


class EntitlementHelperTests(unittest.TestCase):
    def test_only_active_pro_or_business_is_entitled(self):
        def row(plan, status):
            return SimpleNamespace(plan=plan, status=status)

        self.assertTrue(has_active_paid_subscription(row("pro", "active")))
        self.assertTrue(has_active_paid_subscription(row(" Pro ", " Active ")))
        self.assertTrue(has_active_paid_subscription(row("BUSINESS", "active")))
        for plan, status in (
            ("starter", "inactive"),
            ("starter", "active"),
            ("pro", "inactive"),
            ("pro", "canceled"),
            ("pro", "past_due"),
            ("pro", "trialing"),
            ("pro", "unpaid"),
            ("pro", "incomplete"),
            ("business", "canceled"),
            ("enterprise", "active"),
        ):
            self.assertFalse(has_active_paid_subscription(row(plan, status)), (plan, status))
        self.assertFalse(has_active_paid_subscription(None))


class SavedAndFavoriteGateTests(Phase16Case):
    def test_starter_and_inactive_get_an_upgrade_page(self):
        contractor, headers = self._contractor("phase16-starter@example.com")
        _worker, _worker_headers, worker_id = self._worker("phase16-ada@example.com", "Phase16 Ada Welder")
        self._seed_favorite("phase16-starter@example.com", worker_id, note=_NOTE, label="Top pick")

        def assert_upgrade(page, feature_title):
            self.assertEqual(page.status_code, 200, page.text[:400])
            self.assertNotEqual(page.status_code, 403)
            self.assertIn(feature_title, page.text)
            self.assertIn("href='/billing'", page.text)
            self.assertIn("Upgrade on the billing page", page.text)
            self.assertNotIn(_NOTE, page.text)
            self.assertNotIn("0 saved", page.text)

        assert_upgrade(contractor.get("/saved", headers=headers), "Saved candidates are included with Pro")
        assert_upgrade(
            contractor.get("/talent", headers=headers, params={"favorites": "yes"}),
            "Saved candidates are included with Pro",
        )
        assert_upgrade(contractor.get("/upgrade", headers=headers), "Saved candidates are included with Pro")
        messages = contractor.get("/messages", headers=headers)
        assert_upgrade(messages, "Contractor messaging is included with Pro")
        self.assertNotIn("Messaging Center", messages.text)

        basic = contractor.get("/talent", headers=headers)
        self.assertEqual(basic.status_code, 200)
        self.assertIn("Phase16 Ada Welder", basic.text)
        self.assertIn("Upgrade to save", basic.text)
        self.assertIn("Upgrade to filter saved candidates", basic.text)
        self.assertNotIn("name='favorites'", basic.text)
        self.assertNotIn("Top pick", basic.text)
        self.assertNotIn(_NOTE, basic.text)

        profile = contractor.get(f"/worker/{worker_id}", headers=headers)
        self.assertEqual(profile.status_code, 200)
        self.assertIn("Phase16 Ada Welder", profile.text)
        self.assertNotIn(_NOTE, profile.text)
        self.assertIn("Upgrade on the billing page", profile.text)
        self.assertIn("Remove Saved", profile.text)
        self.assertNotIn(f"action='/saved/{worker_id}'", profile.text)

        self._set_plan("phase16-starter@example.com", "pro", "canceled")
        canceled = contractor.get("/saved", headers=headers)
        assert_upgrade(canceled, "Saved candidates are included with Pro")
        self._set_plan("phase16-starter@example.com", "pro", "past_due")
        self.assertIn("Saved candidates are included with Pro", contractor.get("/saved", headers=headers).text)

    def test_new_save_and_label_edit_are_blocked_without_csrf_bypass(self):
        contractor, headers = self._contractor("phase16-save@example.com")
        _worker, _headers, worker_id = self._worker("phase16-bo@example.com", "Phase16 Bo Electrician", trade="Electrician")
        self._seed_favorite("phase16-save@example.com", worker_id, note=_NOTE, label="Maybe")

        missing = contractor.post(
            f"/favorite/{worker_id}",
            data={"return_to": "/talent"},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(missing.status_code, 403)
        self.assertIn("CSRF validation failed", missing.text)
        self.assertEqual(self._favorite_count("phase16-save@example.com", worker_id), 1)

        token, _page = self._token_from(contractor, "/dashboard", headers)
        edited = contractor.post(
            f"/saved/{worker_id}",
            data={"note": "rewritten", "label": "Top pick", "csrf_token": token},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(edited.status_code, 303)
        self.assertEqual(edited.headers["location"], "/upgrade?feature=saved")
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == "phase16-save@example.com").one()
            row = db.query(main.FavoriteCandidate).filter(
                main.FavoriteCandidate.contractor_user_id == user.id,
                main.FavoriteCandidate.worker_id == worker_id,
            ).one()
            self.assertEqual(row.note, _NOTE)
            self.assertEqual(row.label, "Maybe")
        finally:
            db.close()

        bare = contractor.post(
            f"/saved/{worker_id}",
            data={"note": "no-token", "label": "Top pick"},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(bare.status_code, 403)
        self.assertIn("CSRF validation failed", bare.text)
        self.assertNotIn("/saved/", str(CSRF_EXEMPT_PATHS))

        removed = contractor.post(
            f"/favorite/{worker_id}",
            data={"csrf_token": token, "return_to": "/talent"},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        self.assertEqual(removed.headers["location"], "/talent")
        self.assertEqual(self._favorite_count("phase16-save@example.com", worker_id), 0)

        token, _page = self._token_from(contractor, "/dashboard", headers)
        blocked = contractor.post(
            f"/favorite/{worker_id}",
            data={"csrf_token": token},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 303)
        self.assertEqual(blocked.headers["location"], "/upgrade?feature=saved")
        self.assertEqual(self._favorite_count("phase16-save@example.com", worker_id), 0)

        with patch("phase5_billing.stripe.Subscription.retrieve", side_effect=AssertionError("stripe")):
            page = contractor.get("/saved", headers=headers)
        self.assertEqual(page.status_code, 200)
        self.assertIn("href='/billing'", page.text)

    def test_active_pro_and_business_keep_the_saved_workspace(self):
        for plan, email in (("pro", "phase16-pro@example.com"), ("Business", "phase16-business@example.com")):
            contractor, headers = self._contractor(email)
            self._set_plan(email, plan, "active")
            _worker, _headers, worker_id = self._worker(email.replace("@", "-worker@"), f"Phase16 {plan} Worker")
            token, _page = self._token_from(contractor, "/talent", headers)
            saved = contractor.post(
                f"/favorite/{worker_id}",
                data={"csrf_token": token},
                headers=headers,
                follow_redirects=False,
            )
            self.assertIn(saved.status_code, (302, 303), saved.text[:300])
            self.assertNotIn("/upgrade", saved.headers["location"])
            token, profile = self._token_from(contractor, f"/worker/{worker_id}", headers)
            self.assertIn("Save Note", profile.text)
            updated = contractor.post(
                f"/saved/{worker_id}",
                data={"note": _NOTE, "label": "Follow up", "csrf_token": token},
                headers=headers,
                follow_redirects=False,
            )
            self.assertEqual(updated.status_code, 303)
            self.assertTrue(updated.headers["location"].endswith(f"/worker/{worker_id}"))
            workspace = contractor.get("/saved", headers=headers)
            saved_only = contractor.get("/talent", headers=headers, params={"favorites": "yes"})
            self.assertEqual(workspace.status_code, 200)
            self.assertIn(_NOTE, workspace.text)
            self.assertIn("Follow up", workspace.text)
            self.assertIn("1 saved", workspace.text)
            self.assertIn("Follow up", saved_only.text)
            self.assertIn(f"Phase16 {plan} Worker", saved_only.text)
            self.assertEqual(contractor.get("/upgrade?feature=saved", headers=headers, follow_redirects=False).headers["location"], "/saved")

    def test_starter_can_post_jobs_search_talent_and_invite(self):
        contractor, headers = self._contractor("phase16-jobs@example.com")
        _worker, _worker_headers, worker_id = self._worker("phase16-invitee@example.com", "Phase16 Invitee")
        dashboard = contractor.get("/dashboard", headers=headers)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Post Job", dashboard.text)
        self.assertIn("Upgrade saved candidates", dashboard.text)
        self.assertIn("Upgrade messaging", dashboard.text)
        self._post_job(contractor, headers)
        talent = contractor.get("/talent", headers=headers)
        self.assertIn("Phase16 Invitee", talent.text)
        self.assertIn(">Invite</button>", talent.text)
        token = _token(talent.text)
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == "phase16-jobs@example.com").one()
            job_id = db.query(main.ManpowerRequest).filter(main.ManpowerRequest.user_id == user.id).one().id
        finally:
            db.close()
        invited = contractor.post(
            f"/job/{job_id}/invite/{worker_id}",
            data={"csrf_token": token},
            headers=headers,
            follow_redirects=False,
        )
        self.assertIn(invited.status_code, (302, 303), invited.text[:300])
        self.assertNotIn("/upgrade", invited.headers.get("location", ""))


class MessagingGateTests(Phase16Case):
    def test_workers_can_message_while_unpaid_contractors_cannot(self):
        contractor, headers = self._contractor("phase16-inbox@example.com")
        worker, worker_headers, worker_id = self._worker("phase16-messenger@example.com", "Phase16 Messenger")
        self._post_job(contractor, headers)
        token, _page = self._token_from(contractor, "/talent", headers)
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == "phase16-inbox@example.com").one()
            job_id = db.query(main.ManpowerRequest).filter(main.ManpowerRequest.user_id == user.id).one().id
            contractor_id = user.id
        finally:
            db.close()
        invited = contractor.post(
            f"/job/{job_id}/invite/{worker_id}",
            data={"csrf_token": token},
            headers=headers,
            follow_redirects=False,
        )
        self.assertIn(invited.status_code, (302, 303), invited.text[:300])

        inbox = worker.get(f"/messages?with_user={contractor_id}", headers=worker_headers)
        self.assertEqual(inbox.status_code, 200)
        self.assertIn("Messaging Center", inbox.text)
        sent = worker.post(
            f"/messages/send/{contractor_id}",
            data={"body": "phase16-worker-ping", "csrf_token": _token(inbox.text)},
            headers=worker_headers,
            follow_redirects=False,
        )
        self.assertIn(sent.status_code, (302, 303), sent.text[:300])
        self.assertNotIn("/upgrade", sent.headers["location"])

        blocked_page = contractor.get("/messages", headers=headers)
        self.assertEqual(blocked_page.status_code, 200)
        self.assertIn("Contractor messaging is included with Pro", blocked_page.text)
        self.assertNotIn("phase16-worker-ping", blocked_page.text)
        self.assertNotIn("Messaging Center", blocked_page.text)

        denied = contractor.post(
            f"/messages/send/{self._user_id('phase16-messenger@example.com')}",
            data={"body": "phase16-should-not-send"},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIn("CSRF validation failed", denied.text)

        token, _page = self._token_from(contractor, "/dashboard", headers)
        redirected = contractor.post(
            f"/messages/send/{self._user_id('phase16-messenger@example.com')}",
            data={"body": "phase16-contractor-reply", "csrf_token": token},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(redirected.status_code, 303)
        self.assertEqual(redirected.headers["location"], "/upgrade?feature=messages")

        db = main.SessionLocal()
        try:
            bodies = [row.body for row in db.query(main.Message).all()]
        finally:
            db.close()
        self.assertIn("phase16-worker-ping", bodies)
        self.assertNotIn("phase16-contractor-reply", bodies)

        self._set_plan("phase16-inbox@example.com", "pro", "active")
        opened = contractor.get("/messages", headers=headers)
        self.assertEqual(opened.status_code, 200)
        self.assertIn("Messaging Center", opened.text)
        self.assertIn("phase16-worker-ping", opened.text)
        replied = contractor.post(
            f"/messages/send/{self._user_id('phase16-messenger@example.com')}",
            data={"body": "phase16-contractor-reply", "csrf_token": _token(opened.text)},
            headers=headers,
            follow_redirects=False,
        )
        self.assertIn(replied.status_code, (302, 303), replied.text[:300])
        self.assertNotIn("/upgrade", replied.headers["location"])
        seen = worker.get(f"/messages?with_user={contractor_id}", headers=worker_headers)
        self.assertIn("phase16-contractor-reply", seen.text)


class AdminAndHealthTests(Phase16Case):
    def test_admin_routes_stay_open_without_a_subscription(self):
        saved_user = os.environ.get("ADMIN_USERNAME")
        saved_password = os.environ.get("ADMIN_PASSWORD")
        os.environ["ADMIN_USERNAME"] = "phase16-admin"
        os.environ["ADMIN_PASSWORD"] = "phase16-admin-password"
        try:
            denied = self.client.get("/admin", headers=self._headers())
            self.assertEqual(denied.status_code, 401)
            allowed = self.client.get(
                "/admin",
                auth=("phase16-admin", "phase16-admin-password"),
                headers=self._headers(),
            )
            self.assertEqual(allowed.status_code, 200)
            self.assertIn("TradeForce AI Admin", allowed.text)
            ops = self.client.get(
                "/admin/operations",
                auth=("phase16-admin", "phase16-admin-password"),
                headers=self._headers(),
            )
            self.assertEqual(ops.status_code, 200)
            self.assertIn("Platform Operations", ops.text)
        finally:
            if saved_user is None:
                os.environ.pop("ADMIN_USERNAME", None)
            else:
                os.environ["ADMIN_USERNAME"] = saved_user
            if saved_password is None:
                os.environ.pop("ADMIN_PASSWORD", None)
            else:
                os.environ["ADMIN_PASSWORD"] = saved_password

    def test_health_reports_phase_16_and_pro_gating(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertEqual(body["phase"], 16)
        self.assertIn("pro-gating", body["features"])
        self.assertIn("saved-workspace", body["features"])
        self.assertIn("csrf", body["features"])
        self.assertIn("rate-limit", body["features"])
        phase14 = self.client.get("/phase14/health")
        self.assertEqual(phase14.json()["phase"], 14)

    def test_workers_and_guests_keep_existing_saved_errors(self):
        worker, worker_headers, _worker_id = self._worker("phase16-guest-worker@example.com", "Phase16 Guest Worker")
        self.assertEqual(worker.get("/saved", headers=worker_headers).status_code, 403)
        self.assertEqual(_browser().get("/saved").status_code, 401)
        self.assertEqual(worker.get("/messages", headers=worker_headers).status_code, 200)
