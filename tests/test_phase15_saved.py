"""Phase 15 saved-candidate notes, labels, and workspace. Offline only."""
import os
import re
import tempfile
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text

_db_file = tempfile.NamedTemporaryFile(prefix="tradeforce-phase15-", suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _db_file.name

import main
from app import CSRF_EXEMPT_PATHS, _RATE_BUCKETS, _rate_class, app
from fastapi.testclient import TestClient


_CSRF_RE = re.compile(r"""name=["']csrf_token["'] value=["']([^"']+)["']""")
_NOTE = 'phase15-private <img src=x onerror=phase15alert>'


def _browser():
    return TestClient(app, base_url="https://testserver")


def _token(html: str) -> str:
    match = _CSRF_RE.search(html)
    if not match:
        raise AssertionError("CSRF token missing from HTML")
    return match.group(1)


class Phase15Case(unittest.TestCase):
    def setUp(self):
        _RATE_BUCKETS.clear()
        self.client = _browser()
        self._octet = 10

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

    def _worker(self, email, name, trade, location="Houston, TX", experience="8", desired_pay="$42/hr"):
        headers = self._headers()
        client = self._signup(_browser(), email, "worker", headers)
        page = client.get("/dashboard", headers=headers)
        self.assertEqual(page.status_code, 200, page.text[:300])
        response = client.post(
            "/worker/profile",
            data={
                "name": name,
                "trade": trade,
                "location": location,
                "experience": experience,
                "certifications": "AWS",
                "phone": "555-0100",
                "desired_pay": desired_pay,
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
            worker = db.query(main.Worker).filter(main.Worker.email == email).one()
            return worker.id
        finally:
            db.close()

    def _token_from(self, client, path, headers):
        page = client.get(path, headers=headers)
        self.assertEqual(page.status_code, 200, page.text[:500])
        return _token(page.text), page

    def _favorite(self, client, worker_id, headers):
        token, _page = self._token_from(client, "/talent", headers)
        response = client.post(
            f"/favorite/{worker_id}",
            data={"csrf_token": token},
            headers=headers,
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303), response.text[:300])

    def _set_note(self, client, worker_id, headers, note, label):
        token, _page = self._token_from(client, f"/worker/{worker_id}", headers)
        response = client.post(
            f"/saved/{worker_id}",
            data={"note": note, "label": label, "csrf_token": token},
            headers=headers,
            follow_redirects=False,
        )
        return response

    def _favorite_row(self, contractor_email, worker_id):
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == contractor_email).one()
            row = db.query(main.FavoriteCandidate).filter(
                main.FavoriteCandidate.contractor_user_id == user.id,
                main.FavoriteCandidate.worker_id == worker_id,
            ).one()
            return {"note": row.note, "label": row.label, "id": row.id, "created_at": row.created_at}
        finally:
            db.close()


class SavedWorkspaceTests(Phase15Case):
    def test_save_then_set_note_and_label(self):
        contractor, headers = self._contractor("phase15-owner@example.com")
        _worker_client, _worker_headers, worker_id = self._worker(
            "phase15-ada@example.com", "Phase15 Ada Welder", "Welder", desired_pay="$42/hr"
        )
        profile = contractor.get(f"/worker/{worker_id}", headers=headers)
        self.assertEqual(profile.status_code, 200)
        self.assertNotIn(f"action='/saved/{worker_id}'", profile.text)
        self._favorite(contractor, worker_id, headers)

        saved = self._set_note(contractor, worker_id, headers, _NOTE, "Top pick")
        self.assertEqual(saved.status_code, 303, saved.text[:300])
        self.assertTrue(saved.headers["location"].endswith(f"/worker/{worker_id}"))

        stored = self._favorite_row("phase15-owner@example.com", worker_id)
        self.assertEqual(stored["note"], _NOTE.strip())
        self.assertEqual(stored["label"], "Top pick")

        profile = contractor.get(f"/worker/{worker_id}", headers=headers)
        workspace = contractor.get("/saved", headers=headers)
        self.assertEqual(workspace.status_code, 200)
        for page in (profile, workspace):
            self.assertIn("Phase15 Ada Welder", page.text)
            self.assertIn("Top pick", page.text)
            self.assertIn("&lt;img", page.text)
            self.assertNotIn("<img", page.text)
            self.assertIn('name="csrf_token"', page.text)
        self.assertIn("value='Top pick' selected", profile.text)
        self.assertIn("Private note", profile.text)
        self.assertIn("$42/hr", workspace.text)
        self.assertIn("Houston, TX", workspace.text)
        self.assertIn("Welder", workspace.text)
        self.assertIn("8 yrs experience", workspace.text)
        self.assertIn("Available", workspace.text)
        self.assertIn("View Profile", workspace.text)
        self.assertIn(">Remove<", workspace.text)

        rejected = self._set_note(contractor, worker_id, headers, "should-not-save", "Nope")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self._favorite_row("phase15-owner@example.com", worker_id)["label"], "Top pick")
        self.assertNotIn("should-not-save", self._favorite_row("phase15-owner@example.com", worker_id)["note"])

        capped = ("Z" * 1000) + "TAIL15"
        updated = self._set_note(contractor, worker_id, headers, capped, "Follow up")
        self.assertEqual(updated.status_code, 303)
        stored = self._favorite_row("phase15-owner@example.com", worker_id)
        self.assertEqual(stored["label"], "Follow up")
        self.assertEqual(stored["note"], "Z" * 1000)
        self.assertNotIn("TAIL15", stored["note"])

    def test_contractor_cannot_edit_another_contractors_row(self):
        owner, owner_headers = self._contractor("phase15-private-owner@example.com")
        other, other_headers = self._contractor("phase15-other@example.com")
        worker_client, worker_headers, worker_id = self._worker(
            "phase15-bo@example.com", "Phase15 Bo Electrician", "Electrician"
        )
        self._favorite(owner, worker_id, owner_headers)
        saved = self._set_note(owner, worker_id, owner_headers, "phase15-secret-note", "Maybe")
        self.assertEqual(saved.status_code, 303)

        token, _page = self._token_from(other, "/dashboard", other_headers)
        rejected = other.post(
            f"/saved/{worker_id}",
            data={"note": "hijacked", "label": "Top pick", "csrf_token": token},
            headers=other_headers,
            follow_redirects=False,
        )
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(self._favorite_row("phase15-private-owner@example.com", worker_id)["note"], "phase15-secret-note")

        self._favorite(other, worker_id, other_headers)
        other_profile = other.get(f"/worker/{worker_id}", headers=other_headers)
        other_saved = other.get("/saved", headers=other_headers)
        worker_home = worker_client.get("/dashboard", headers=worker_headers)
        self.assertEqual(worker_client.get(f"/worker/{worker_id}", headers=worker_headers).status_code, 403)
        for page in (other_profile, other_saved, worker_home):
            self.assertNotIn("phase15-secret-note", page.text)
        self.assertEqual(self._favorite_row("phase15-other@example.com", worker_id)["note"], "")
        self.assertEqual(self._favorite_row("phase15-other@example.com", worker_id)["label"], "")

    def test_post_without_csrf_is_rejected(self):
        contractor, headers = self._contractor("phase15-csrf@example.com")
        _worker_client, _worker_headers, worker_id = self._worker(
            "phase15-csrf-worker@example.com", "Phase15 Csrf Worker", "Pipefitter"
        )
        self._favorite(contractor, worker_id, headers)
        rejected = contractor.post(
            f"/saved/{worker_id}",
            data={"note": "missing-token", "label": "Maybe"},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertIn("CSRF validation failed", rejected.text)
        self.assertEqual(self._favorite_row("phase15-csrf@example.com", worker_id)["note"], "")
        self.assertNotIn("/saved/1", CSRF_EXEMPT_PATHS)

    def test_label_and_trade_filters_show_result_count(self):
        contractor, headers = self._contractor("phase15-filter@example.com")
        _ada, _ada_headers, ada_id = self._worker(
            "phase15-filter-ada@example.com", "Phase15 Filter Ada", "Welder", location="Beaumont, TX"
        )
        _bo, _bo_headers, bo_id = self._worker(
            "phase15-filter-bo@example.com", "Phase15 Filter Bo", "Electrician", location="Dallas, TX", experience="3"
        )
        self._favorite(contractor, ada_id, headers)
        self._favorite(contractor, bo_id, headers)
        self.assertEqual(self._set_note(contractor, ada_id, headers, "ada note", "Top pick").status_code, 303)
        self.assertEqual(self._set_note(contractor, bo_id, headers, "bo note", "Maybe").status_code, 303)

        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == "phase15-filter@example.com").one()
            rows = db.query(main.FavoriteCandidate).filter(main.FavoriteCandidate.contractor_user_id == user.id).all()
            by_worker = {row.worker_id: row for row in rows}
            by_worker[ada_id].created_at = "2020-01-01T00:00:00"
            by_worker[bo_id].created_at = "2024-06-01T00:00:00"
            db.commit()
        finally:
            db.close()

        page = contractor.get("/saved", headers=headers)
        self.assertEqual(page.status_code, 200)
        self.assertIn("2 saved", page.text)
        self.assertLess(page.text.find("Phase15 Filter Bo"), page.text.find("Phase15 Filter Ada"))

        labeled = contractor.get("/saved", headers=headers, params={"label": "Top pick"})
        self.assertIn("Phase15 Filter Ada", labeled.text)
        self.assertNotIn("Phase15 Filter Bo", labeled.text)
        self.assertIn("1 saved", labeled.text)
        self.assertIn("Beaumont, TX", labeled.text)

        traded = contractor.get("/saved", headers=headers, params={"trade": "elec"})
        self.assertIn("Phase15 Filter Bo", traded.text)
        self.assertNotIn("Phase15 Filter Ada", traded.text)
        self.assertIn("1 saved", traded.text)
        self.assertIn("3 yrs experience", traded.text)

    def test_workers_and_logged_out_are_blocked(self):
        _contractor, _headers = self._contractor("phase15-blocked-owner@example.com")
        worker, worker_headers, _worker_id = self._worker(
            "phase15-blocked-worker@example.com", "Phase15 Blocked Worker", "Millwright"
        )
        guest = _browser()
        for client, headers in ((worker, worker_headers), (guest, {})):
            saved = client.get("/saved", headers=headers)
            talent = client.get("/talent", headers=headers)
            self.assertEqual(saved.status_code, talent.status_code)
            self.assertIn(saved.status_code, (401, 403))
        self.assertEqual(worker.get("/saved", headers=worker_headers).status_code, 403)
        self.assertEqual(guest.get("/saved").status_code, 401)
        self.assertNotIn("href='/saved'", worker.get("/dashboard", headers=worker_headers).text)

        contractor, contractor_headers = self._contractor("phase15-nav@example.com")
        page = contractor.get("/saved", headers=contractor_headers)
        self.assertEqual(page.status_code, 200)
        self.assertIn("href='/talent'", page.text)
        self.assertIn(">Saved</a>", page.text)
        self.assertIn("have not saved any candidates yet", page.text)
        self.assertIn("0 saved", page.text)
        dashboard = contractor.get("/dashboard", headers=contractor_headers)
        self.assertIn("href='/saved'", dashboard.text)

    def test_migration_adds_note_and_label_without_data_loss(self):
        fd, path = tempfile.mkstemp(prefix="tradeforce-phase15-migrate-", suffix=".db")
        os.close(fd)
        tmp_engine = create_engine("sqlite:///" + path, connect_args={"check_same_thread": False})
        try:
            with tmp_engine.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE favorite_candidates ("
                    "id INTEGER PRIMARY KEY, "
                    "contractor_user_id INTEGER NOT NULL, "
                    "worker_id INTEGER NOT NULL, "
                    "created_at VARCHAR(40))"
                ))
                conn.execute(text(
                    "INSERT INTO favorite_candidates (contractor_user_id, worker_id, created_at) "
                    "VALUES (7, 9, '2019-05-01T00:00:00')"
                ))
            main.migrate_existing_tables(tmp_engine)
            main.migrate_existing_tables(tmp_engine)
            columns = {column["name"] for column in inspect(tmp_engine).get_columns("favorite_candidates")}
            self.assertIn("note", columns)
            self.assertIn("label", columns)
            with tmp_engine.begin() as conn:
                rows = conn.execute(text(
                    "SELECT contractor_user_id, worker_id, created_at, note, label FROM favorite_candidates"
                )).all()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.contractor_user_id, 7)
            self.assertEqual(row.worker_id, 9)
            self.assertEqual(row.created_at, "2019-05-01T00:00:00")
            self.assertIn(row.note, ("", None))
            self.assertIn(row.label, ("", None))
        finally:
            tmp_engine.dispose()
            os.remove(path)

    def test_remove_returns_to_saved_and_rejects_offsite_redirects(self):
        contractor, headers = self._contractor("phase15-remove@example.com")
        _worker_client, _worker_headers, worker_id = self._worker(
            "phase15-remove-worker@example.com", "Phase15 Remove Worker", "Operator"
        )
        self._favorite(contractor, worker_id, headers)
        token, page = self._token_from(contractor, "/saved", headers)
        self.assertIn("Phase15 Remove Worker", page.text)
        removed = contractor.post(
            f"/favorite/{worker_id}",
            data={"csrf_token": token, "return_to": "/saved"},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(removed.status_code, 303)
        self.assertEqual(removed.headers["location"], "/saved")
        empty = contractor.get("/saved", headers=headers)
        self.assertNotIn("Phase15 Remove Worker", empty.text)

        self._favorite(contractor, worker_id, headers)
        token, _page = self._token_from(contractor, "/saved", headers)
        blocked = contractor.post(
            f"/favorite/{worker_id}",
            data={"csrf_token": token, "return_to": "https://evil.example/steal"},
            headers=headers,
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 303)
        self.assertNotIn("evil.example", blocked.headers["location"])
        self.assertTrue(blocked.headers["location"].startswith("/"))

    def test_talent_saved_only_matches_workspace_labels(self):
        contractor, headers = self._contractor("phase15-talent@example.com")
        _worker_client, _worker_headers, worker_id = self._worker(
            "phase15-talent-worker@example.com", "Phase15 Talent Quinn", "Welder", location="Odessa, TX"
        )
        self._favorite(contractor, worker_id, headers)
        self.assertEqual(self._set_note(contractor, worker_id, headers, "call Friday", "Follow up").status_code, 303)
        db = main.SessionLocal()
        try:
            worker = db.query(main.Worker).filter(main.Worker.id == worker_id).one()
            worker.available = False
            db.commit()
        finally:
            db.close()

        saved = contractor.get("/saved", headers=headers)
        saved_only = contractor.get("/talent", headers=headers, params={"favorites": "yes"})
        everyone = contractor.get("/talent", headers=headers)
        missed = contractor.get("/talent", headers=headers, params={"favorites": "yes", "trade": "plumber"})
        self.assertIn("Phase15 Talent Quinn", saved.text)
        self.assertIn("Not available", saved.text)
        self.assertIn("Follow up", saved.text)
        self.assertIn("Phase15 Talent Quinn", saved_only.text)
        self.assertIn("Follow up", saved_only.text)
        self.assertIn("Not available", saved_only.text)
        self.assertNotIn("Phase15 Talent Quinn", everyone.text)
        self.assertNotIn("Phase15 Talent Quinn", missed.text)

    def test_message_requires_recruiting_relationship(self):
        contractor, headers = self._contractor("phase15-message@example.com")
        _worker_client, _worker_headers, worker_id = self._worker(
            "phase15-message-worker@example.com", "Phase15 Message Worker", "Welder"
        )
        self._favorite(contractor, worker_id, headers)
        locked_page = contractor.get("/saved", headers=headers)
        self.assertIn("Invite to unlock messaging", locked_page.text)
        self.assertNotIn("/messages?with_user=", locked_page.text)

        token, _page = self._token_from(contractor, "/dashboard", headers)
        created = contractor.post(
            "/contractor/job",
            data={
                "company": "Phase15 Industrial",
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
        db = main.SessionLocal()
        try:
            user = db.query(main.UserAccount).filter(main.UserAccount.email == "phase15-message@example.com").one()
            job = db.query(main.ManpowerRequest).filter(main.ManpowerRequest.user_id == user.id).one()
            job_id = job.id
            worker = db.query(main.Worker).filter(main.Worker.id == worker_id).one()
            worker_user_id = worker.user_id
        finally:
            db.close()

        listed, _page = self._token_from(contractor, "/saved", headers)
        self.assertIn("Choose job to invite", contractor.get("/saved", headers=headers).text)
        invited = contractor.post(
            f"/job/{job_id}/invite/{worker_id}",
            data={"csrf_token": listed},
            headers=headers,
            follow_redirects=False,
        )
        self.assertIn(invited.status_code, (302, 303), invited.text[:300])
        unlocked = contractor.get("/saved", headers=headers)
        self.assertIn(f"/messages?with_user={worker_user_id}", unlocked.text)
        self.assertNotIn("Invite to unlock messaging", unlocked.text)

    def test_health_reports_phase_15_saved_workspace(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertEqual(body["phase"], 15)
        self.assertIn("saved-workspace", body["features"])
        self.assertIn("csrf", body["features"])
        self.assertIn("rate-limit", body["features"])
        phase14 = self.client.get("/phase14/health")
        self.assertEqual(phase14.status_code, 200)
        self.assertEqual(phase14.json()["phase"], 14)

    def test_saved_posts_use_the_existing_mutation_limit(self):
        request = SimpleNamespace(url=SimpleNamespace(path="/saved/9"), method="POST")
        self.assertEqual(_rate_class(request), "mutation")
        favorite = SimpleNamespace(url=SimpleNamespace(path="/favorite/9"), method="POST")
        self.assertEqual(_rate_class(favorite), "mutation")


if __name__ == "__main__":
    unittest.main()
