# TradeForce AI — Phase 14

Phase 14 hardens the production app for launch. It does not change Stripe price IDs, billing flows, or Phase 13 notifications.

The production entrypoint remains `phase6_app:app` (which loads `app.py`). That module installs CSRF protection and rate limiting. When Phase 14 shipped, `/health` reported `phase: 14`. Phase 15 advances that shared field to `15` and adds `saved-workspace`. `GET /phase14/health` still reports `phase: 14`.

## CSRF

Cookie-authenticated form posts use a session synchronizer token:

1. A `csrf_token` value is stored in the signed session cookie.
2. HTML responses get a matching `<input type="hidden" name="csrf_token">` inside every `method="post"` form, including billing pages rendered outside Jinja.
3. `POST`, `PUT`, `PATCH`, and `DELETE` must send that value as the `csrf_token` form field or the `X-CSRF-Token` header.

A missing or mismatched token returns **403** with `CSRF validation failed. Refresh the page and submit the form again.`

These paths skip CSRF because they are not browser form posts:

- `POST /stripe/webhook` — authenticated by the Stripe signature
- `POST /phase5/admin/reset-subscription` — authenticated by the `X-Admin-Token` header

`GET` and other safe methods are not checked. Offer accept and decline are POST-only so a cross-site link cannot change hire state.

## Rate limiting

Limits are enforced in process memory, per client IP (`X-Forwarded-For` when present, otherwise the direct client). One Render web instance is covered. **More than one instance needs a shared store such as Redis** before these limits hold across processes.

Sensitive routes:

| Route | Limit |
| --- | --- |
| `POST /login` | 10 / 5 minutes |
| `POST /signup` (registration) | 5 / hour |
| `POST /forgot-password` | 5 / 15 minutes |
| `POST /reset-password` | 5 / 15 minutes |
| `POST /billing/checkout/{plan}` | 10 / 10 minutes |
| `POST /billing/portal` | 10 / 10 minutes |

Other state-changing routes keep a higher per-process fallback. `POST /stripe/webhook` is not rate limited. A limited client receives **429**, a `Retry-After` header, and `Too many requests. Please wait N seconds and try again.`

## Object storage

Document upload already uses S3-compatible storage when it is configured, and keeps the database blob fallback when it is not. No credentials belong in git or in Render config committed to this repo. Set them in the Render dashboard:

| Variable | Required | Purpose |
| --- | --- | --- |
| `S3_BUCKET` | yes | Bucket name |
| `S3_ACCESS_KEY_ID` | yes | Access key |
| `S3_SECRET_ACCESS_KEY` | yes | Secret key |
| `S3_ENDPOINT_URL` | no | Custom endpoint. Set this for Cloudflare R2 (`https://<accountid>.r2.cloudflarestorage.com`). Leave unset for AWS S3. |
| `S3_REGION` | no | Region. Defaults to `auto` (use `auto` for R2). |

When all three required variables are set, `/health` and `/phase14/health` report `document_storage: "object-storage"`. When any one is missing, they report `document_storage: "database-fallback"`. Those endpoints do not return secret values or call the bucket.

## Health

`GET /phase14/health` (no database check) reports `phase: 14` plus:

- `document_storage`: `object-storage` or `database-fallback`
- `csrf`: `enabled` when the production app is loaded
- `rate_limit`: `in-memory` when the production app is loaded
- `rate_limit_scope`: `per-process`

`GET /health` (database check) keeps those storage, CSRF, and rate-limit fields. Phase 15 sets its `phase` to `15` and adds `saved-workspace` to `features`. `/health` still includes `csrf` and `rate-limit` when the production app is loaded.

## Verify locally

```bash
pip install -r requirements.txt pytest httpx
pytest tests/test_phase14_hardening.py tests/test_phase5_billing.py
```

Session cookies are marked Secure, so browser login against plain HTTP will not keep the CSRF session. Use the tests above, or an HTTPS deployment, to confirm a form post. Unauthenticated checks still work over HTTP:

```bash
uvicorn phase6_app:app --host 0.0.0.0 --port 8000
curl -s localhost:8000/health
curl -s localhost:8000/phase14/health
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/login -d 'email=a@example.com&password=secret'
# 403 — CSRF rejected
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/stripe/webhook -H 'content-type: application/json' -d '{}'
# 503 or 400 — webhook is not rejected as CSRF
```
