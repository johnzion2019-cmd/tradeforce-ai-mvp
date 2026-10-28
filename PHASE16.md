# TradeForce AI — Phase 16

Phase 16 gates contractor premium recruiting features behind an active paid subscription. Starter and inactive contractors keep the core marketplace. Workers are not gated.

The production entrypoint remains `phase6_app:app` (which loads `app.py`). Phase 14 CSRF protection and rate limits still apply. Phase 15 saved-workspace behavior is unchanged for contractors with an active Pro or Business subscription. `/health` reports `phase: 16` and includes `pro-gating` in `features`.

No new Stripe Price IDs, placement fees, S3/R2 changes, or Redis. Entitlement is read from the Phase 5 `subscriptions` row (`plan`, `status`). Page loads do not call Stripe.

## Who is entitled

`has_active_paid_subscription()` in `phase5_billing.py` is the rule, shared with the billing-page upgrade buttons:

- `plan` is `pro` or `business` (trimmed, case-insensitive)
- `status` is `active` (trimmed, case-insensitive)

A missing row is treated as Starter / inactive. Canceled, past_due, unpaid, incomplete, trialing, and any other non-active status are not entitled, even when the plan name is Pro or Business. `current_period_end` is not re-checked; the webhook-updated status is the source of truth.

HTTP Basic admin routes (`/admin`, `/admin/operations`, and the token-gated subscription reset) are not subscription-gated. A session whose role is `admin` is also treated as entitled so an admin account is not locked out of contractor tools. Signup still only creates worker and contractor accounts.

## What Starter can do

Starter, inactive, and canceled contractors can:

- post and manage jobs
- search available talent (trade, location, experience)
- open worker profiles
- invite workers and run the hiring pipeline
- keep company profile, billing, payroll history, and hires

They cannot open the saved workspace, save a new candidate, edit notes or labels, use Talent Search **Saved only**, or open the contractor messaging inbox.

Removing an existing save still works. The favorite toggle deletes a row the contractor already has, including after a downgrade, and does not require Pro. Creating a new save does.

## What Pro and Business unlock

An active Pro or Business subscription keeps Phase 15 behavior:

- `GET /saved` and `POST /saved/{worker_id}` (notes and labels)
- `POST /favorite/{worker_id}` when the worker is not already saved
- Talent Search **Saved only**, saved labels, and save buttons
- `GET /messages` and `POST /messages/send/{recipient_id}`

`can_message()` is unchanged. A contractor still needs an application or invitation before a send is accepted. Pro does not open messaging to strangers.

## Workers

Workers are not checked against a contractor plan. If `can_message()` already allows the pair, the worker can list and send messages during an application, invitation, or hire. An unpaid contractor does not see that inbox until they subscribe. The worker is not blocked mid-hire.

## Upgrade UX

Unpaid contractors get an HTML upgrade page, not a bare 403:

- `GET /saved`, `GET /talent?favorites=yes`, and `GET /messages` render the upgrade page (200)
- `POST /favorite/{worker_id}` for a new save, `POST /saved/{worker_id}`, and `POST /messages/send/{recipient_id}` redirect (303) to `/upgrade?feature=saved` or `/upgrade?feature=messages`
- the page links to `/billing`
- Talent Search, worker profiles, and the contractor dashboard show Upgrade links in place of save and message actions

Logged-out visitors still get 401. Workers hitting contractor-only routes still get 403. A missing or wrong CSRF token on those POSTs is still 403, before the plan check. Admin routes are unchanged.

An entitled contractor who opens `/upgrade` is sent on to `/saved` or `/messages`.

## Health

`GET /health` includes:

- `phase`: `16`
- `features`: the existing list, plus `saved-workspace` and `pro-gating`, and `csrf` / `rate-limit` when the production app is loaded

`GET /phase14/health` still reports `phase: 14`.

## Verify locally

```bash
pip install -r requirements.txt pytest httpx
pytest tests/test_phase16_pro_gating.py tests/test_phase15_saved.py tests/test_phase14_hardening.py tests/test_phase5_billing.py
```

Session cookies are marked Secure, so a plain HTTP browser will not keep a login. Use the tests above, or an HTTPS deployment, to confirm a signed-in pass. Unauthenticated checks still work over HTTP:

```bash
uvicorn phase6_app:app --host 0.0.0.0 --port 8000
curl -s localhost:8000/health
```

The JSON `phase` field is `16` and `features` includes `pro-gating`.
