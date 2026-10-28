# TradeForce AI — Phase 15

Phase 15 finishes the saved-candidates workspace for contractors. It does not change Stripe billing, S3/R2 document storage, or Redis. Phase 16 now gates this workspace behind an active Pro or Business subscription. See PHASE16.md.

The production entrypoint remains `phase6_app:app` (which loads `app.py`). Phase 14 CSRF protection and rate limits still apply to every new form post. `/health` reports `phase: 16`.

## Saved workspace

Signed-in contractors open `GET /saved` from the **Saved** nav link (contractors only). The page lists that contractor's saved workers, newest first:

- name, trade, years of experience, location, desired pay
- available or not available
- label, when one is set
- a short preview of the private note

The page shows how many rows match the current filters. Label and trade filters use the `label` and `trade` query parameters. Trade matching is a case-insensitive partial match.

An empty list links to `/talent`. Workers and logged-out visitors get the same response `/talent` already returns (403 for the wrong role, 401 when signed out).

Each card can:

- open the worker profile
- remove the save through the existing `POST /favorite/{worker_id}` toggle, returning to `/saved`
- invite the worker to one of the contractor's open jobs, using the same control as Talent Search
- message the worker when `can_message()` finds a recruiting relationship, otherwise show “Invite to unlock messaging”

Removing a save deletes that favorite row, including its note and label.

## Private notes and labels

`favorite_candidates` gains `note` and `label`. Existing rows are kept. `migrate_existing_tables()` adds the columns when they are missing.

`POST /saved/{worker_id}` updates the signed-in contractor's own row. Anyone else, including another contractor who saved the same worker, gets **404**. A contractor who has not saved that worker gets **404** as well. The route does not create a new save.

Labels are limited to:

- Top pick
- Follow up
- Maybe
- Not now

An empty label clears it. Any other value is rejected with **400**.

Notes are stored as plain text, stripped, and capped at 1,000 characters. Pages render them with HTML escaping, so a note cannot inject markup. Notes and labels are loaded only for the contractor who wrote them. They are not shown to workers or to other contractors.

The worker profile shows and edits the note and label when that contractor has the worker saved. Talent Search shows the label badge on a saved card when a label is set. **Saved only** uses the same saved rows as `/saved`, including people who are no longer available, and still honors the trade, location, and experience filters.

## Security

New post forms rely on the Phase 14 session CSRF token. `POST /saved/{worker_id}` and the remove form are covered by the existing per-process mutation rate limit. A missing or wrong token is rejected with **403**.

`return_to` on the favorite toggle accepts only same-app relative paths. Talent Search and the dashboard still fall back to the referer when that field is omitted.

## Health

`GET /health` includes:

- `phase`: `16` (Phase 16 moved this shared field and added `pro-gating`)
- `features`: the existing list, plus `saved-workspace` and `pro-gating`, and `csrf` / `rate-limit` when the production app is loaded

`GET /phase14/health` is unchanged and still reports `phase: 14`.

## Verify locally

```bash
pip install -r requirements.txt pytest httpx
pytest tests/test_phase15_saved.py tests/test_phase14_hardening.py tests/test_phase5_billing.py
```

Session cookies are marked Secure, so a plain HTTP browser will not keep a login. Use the tests above, or an HTTPS deployment, to confirm a signed-in pass. Unauthenticated checks still work over HTTP:

```bash
uvicorn phase6_app:app --host 0.0.0.0 --port 8000
curl -s localhost:8000/health
```

The JSON `phase` field is `16` and `features` includes `saved-workspace` and `pro-gating`.
