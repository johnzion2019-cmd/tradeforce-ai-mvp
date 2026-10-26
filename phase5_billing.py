"""TradeForce AI Phase 5 billing module.

Register with the FastAPI application from app.py. Secrets stay in environment variables.
"""
import base64
import html
import os

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tradeforce.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **kwargs)
SessionLocal = sessionmaker(bind=engine)
BillingBase = declarative_base()


class Subscription(BillingBase):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    plan = Column(String(40), default="starter")
    status = Column(String(40), default="inactive", index=True)
    stripe_customer_id = Column(String(120), default="", index=True)
    stripe_subscription_id = Column(String(120), default="", index=True)
    current_period_end = Column(String(40), default="")


BillingBase.metadata.create_all(bind=engine)


def _hydrate_stripe_secrets_from_chunks():
    # Render env-size/truncation workaround: full secrets are stored as short
    # chunks and must replace any truncated STRIPE_SECRET_KEY /
    # STRIPE_WEBHOOK_SECRET. Remove once those secrets are set normally.
    for target, prefix in (
        ("STRIPE_SECRET_KEY", "STRIPE_SK_CHUNK"),
        ("STRIPE_WEBHOOK_SECRET", "STRIPE_WH_CHUNK"),
    ):
        raw_count = os.environ.get(f"{prefix}_COUNT")
        if raw_count is None or raw_count.strip() == "":
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        if count <= 0:
            continue
        parts = []
        missing = False
        for index in range(count):
            chunk_name = f"{prefix}_{index:02d}"
            if chunk_name not in os.environ:
                missing = True
                break
            parts.append(os.environ[chunk_name])
        if missing:
            continue
        os.environ[target] = "".join(parts)
    _hydrate_stripe_secrets_from_b64()


def _hydrate_stripe_secrets_from_b64():
    # Base64 chunks win over raw chunks and any truncated secret. Every part
    # must be present. Do not log secret material.
    for target, prefix in (
        ("STRIPE_SECRET_KEY", "STRIPE_SK_B64"),
        ("STRIPE_WEBHOOK_SECRET", "STRIPE_WH_B64"),
    ):
        raw_count = os.environ.get(f"{prefix}_COUNT")
        if raw_count is None or raw_count.strip() == "":
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        if count <= 0:
            continue
        parts = []
        missing = False
        for index in range(count):
            chunk_name = f"{prefix}_{index}"
            if chunk_name not in os.environ:
                missing = True
                break
            parts.append(os.environ[chunk_name])
        if missing:
            continue
        try:
            decoded = base64.b64decode("".join(parts)).decode()
        except ValueError:
            continue
        if not decoded:
            continue
        os.environ[target] = decoded


_hydrate_stripe_secrets_from_chunks()


def stripe_ready():
    return bool(
        os.getenv("STRIPE_SECRET_KEY")
        and os.getenv("STRIPE_PRO_PRICE_ID")
        and os.getenv("STRIPE_BUSINESS_PRICE_ID")
    )


def base_url(request):
    return (os.getenv("APP_BASE_URL") or str(request.base_url)).rstrip("/")


def account_id(request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return int(uid)


def account_email(db, uid):
    row = db.execute(text("SELECT email, role FROM account_users WHERE id=:id"), {"id": uid}).first()
    if not row or row.role != "contractor":
        raise HTTPException(status_code=403, detail="Contractor account required.")
    return row.email


def subscription_plan_status(sub):
    plan = (sub.plan if sub and sub.plan else "starter").strip().lower()
    status = (sub.status if sub and sub.status else "inactive").strip().lower()
    return plan, status


def upgrade_cta_visible(sub):
    """Which checkout CTAs the billing page should offer.

    An active Pro subscription can still move up to Business. An active
    Business subscription is the top plan, so neither checkout CTA is shown.
    Starter and any non-active status keep both upgrades.
    """
    plan, status = subscription_plan_status(sub)
    on_active_paid_plan = status == "active" and plan in {"pro", "business"}
    return {
        "pro": not on_active_paid_plan,
        "business": not (on_active_paid_plan and plan == "business"),
    }


def _checkout_form(plan_slug, label, disabled):
    return (
        f'<form method="post" action="/billing/checkout/{plan_slug}">'
        f'<button type="submit"{disabled}>{label}</button></form>'
    )


def _plan_state_note(message):
    return f'<p class="plan-state">{html.escape(message)}</p>'


def billing_page(email, sub):
    plan = (sub.plan if sub and sub.plan else "starter").strip() or "starter"
    status = (sub.status if sub and sub.status else "inactive").strip() or "inactive"
    has_customer = bool(sub and sub.stripe_customer_id)
    configured = stripe_ready()
    safe_email = html.escape(email)
    safe_plan = html.escape(plan.title())
    safe_status = html.escape(status.title())
    disabled = "" if configured else " disabled"
    manage = ""
    if has_customer:
        manage = '<form method="post" action="/billing/portal"><button class="secondary" type="submit">Manage subscription</button></form>'
    notice = "" if configured else '<div class="notice">Stripe billing is not fully configured yet.</div>'
    visible = upgrade_cta_visible(sub)
    current_plan, _current_status = subscription_plan_status(sub)
    pro_action = (
        _checkout_form("pro", "Upgrade to Pro", disabled)
        if visible["pro"]
        else _plan_state_note("Current plan" if current_plan == "pro" else "Included in your plan")
    )
    business_action = (
        _checkout_form("business", "Upgrade to Business", disabled)
        if visible["business"]
        else _plan_state_note("Current plan")
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeForce AI Billing</title>
<style>
:root{{--bg:#071019;--panel:#101c27;--line:#2a3947;--text:#f5f7fa;--muted:#aeb9c4;--orange:#ff7719;}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:Arial,Helvetica,sans-serif}}
header{{padding:28px 22px;border-bottom:1px solid var(--line);font-weight:800;font-size:28px}} .orange{{color:var(--orange)}}
main{{max-width:980px;margin:auto;padding:34px 20px 60px}} .eyebrow{{color:var(--orange);font-weight:800;letter-spacing:2px;font-size:14px}}
h1{{font-size:44px;line-height:1.05;margin:12px 0}} .sub{{color:var(--muted);font-size:18px;line-height:1.6;max-width:720px}}
.current{{margin:24px 0;padding:18px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}}
.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin-top:26px}} .card{{border:1px solid var(--line);background:var(--panel);border-radius:18px;padding:25px}}
.card h2{{font-size:28px;margin:0 0 8px}} .price{{font-size:42px;font-weight:800;margin:16px 0}} .price span{{font-size:16px;color:var(--muted);font-weight:400}}
ul{{padding-left:20px;color:var(--muted);line-height:1.8}} button{{width:100%;border:0;border-radius:10px;padding:15px 18px;font-size:17px;font-weight:800;background:var(--orange);color:#101010;cursor:pointer}}
button.secondary{{background:transparent;color:var(--text);border:1px solid var(--line);margin-top:12px}} button:disabled{{opacity:.45;cursor:not-allowed}}
.plan-state{{margin:0;padding:15px 18px;border:1px solid var(--line);border-radius:10px;text-align:center;font-size:17px;font-weight:800;color:var(--muted)}}
.back{{display:inline-block;color:var(--orange);text-decoration:none;margin-top:24px;font-weight:700}} .notice{{padding:12px 14px;background:#30200f;border:1px solid #6d4319;border-radius:10px;margin:20px 0}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}h1{{font-size:38px}}header{{font-size:24px}}}}
</style>
</head>
<body>
<header>TRADE<span class="orange">FORCE</span> AI</header>
<main>
<div class="eyebrow">CONTRACTOR BILLING · PHASE 5</div>
<h1>Choose the plan that fits your hiring needs.</h1>
<p class="sub">Upgrade your TradeForce AI contractor account with secure Stripe subscription billing.</p>
{notice}
<div class="current"><strong>Current plan:</strong> {safe_plan} &nbsp; · &nbsp; <strong>Status:</strong> {safe_status}<br><span style="color:var(--muted)">{safe_email}</span>{manage}</div>
<div class="grid">
<section class="card"><h2>TradeForce Pro</h2><div class="price">$99 <span>/ month</span></div><ul><li>Talent discovery and search</li><li>Candidate favorites and recruiting tools</li><li>Messaging tied to recruiting relationships</li></ul>{pro_action}</section>
<section class="card"><h2>TradeForce Business</h2><div class="price">$249 <span>/ month</span></div><ul><li>Everything in Pro</li><li>Advanced contractor recruiting workflow</li><li>Built for higher-volume hiring teams</li></ul>{business_action}</section>
</div>
<a class="back" href="/dashboard">← Back to contractor dashboard</a>
</main>
</body>
</html>"""


def _stripe_health_diagnostics():
    secret = os.getenv("STRIPE_SECRET_KEY") or ""
    webhook = os.getenv("STRIPE_WEBHOOK_SECRET") or ""
    chunk_count = os.getenv("STRIPE_SK_CHUNK_COUNT")
    chunks_present = 0
    try:
        count = int(chunk_count) if chunk_count is not None else 0
    except ValueError:
        count = 0
    if count > 0:
        chunks_present = sum(
            1 for index in range(count) if f"STRIPE_SK_CHUNK_{index:02d}" in os.environ
        )
    return {
        "stripe_secret_len": len(secret),
        "stripe_webhook_len": len(webhook),
        "stripe_sk_chunk_count": chunk_count,
        "stripe_sk_chunks_present": chunks_present,
        "stripe_secret_prefix": secret[:7] if secret else "",
    }


@router.get("/phase5/health")
def phase5_health():
    return {
        "phase": 5,
        "billing_module": "ready",
        "stripe_configured": stripe_ready(),
        "webhook_configured": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
        **_stripe_health_diagnostics(),
    }


@router.get("/billing", response_class=HTMLResponse)
def billing(request: Request):
    uid = account_id(request)
    with SessionLocal() as db:
        email = account_email(db, uid)
        sub = db.query(Subscription).filter(Subscription.user_id == uid).first()
        return HTMLResponse(billing_page(email, sub))


@router.post("/billing/checkout/{plan}")
def checkout(plan: str, request: Request):
    if plan not in {"pro", "business"}:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if not stripe_ready():
        raise HTTPException(status_code=503, detail="Billing is not configured yet.")
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    uid = account_id(request)
    with SessionLocal() as db:
        email = account_email(db, uid)
        sub = db.query(Subscription).filter(Subscription.user_id == uid).first()
        if not upgrade_cta_visible(sub).get(plan):
            raise HTTPException(
                status_code=409,
                detail="That plan is already included in your active subscription. Use Manage subscription to make billing changes.",
            )
        price = os.environ["STRIPE_PRO_PRICE_ID"] if plan == "pro" else os.environ["STRIPE_BUSINESS_PRICE_ID"]
        params = {
            "mode": "subscription",
            "line_items": [{"price": price, "quantity": 1}],
            "success_url": base_url(request) + "/billing?billing=success",
            "cancel_url": base_url(request) + "/billing?billing=cancelled",
            "client_reference_id": str(uid),
            "metadata": {"tradeforce_user_id": str(uid), "plan": plan},
        }
        if sub and sub.stripe_customer_id:
            params["customer"] = sub.stripe_customer_id
        else:
            params["customer_email"] = email
        session = stripe.checkout.Session.create(**params)
        return RedirectResponse(session.url, status_code=303)


@router.post("/billing/portal")
def portal(request: Request):
    if not os.getenv("STRIPE_SECRET_KEY"):
        raise HTTPException(status_code=503, detail="Billing is not configured yet.")
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    uid = account_id(request)
    with SessionLocal() as db:
        account_email(db, uid)
        sub = db.query(Subscription).filter(Subscription.user_id == uid).first()
        if not sub or not sub.stripe_customer_id:
            raise HTTPException(status_code=400, detail="No billing account found.")
        session = stripe.billing_portal.Session.create(
            customer=sub.stripe_customer_id,
            return_url=base_url(request) + "/billing",
        )
        return RedirectResponse(session.url, status_code=303)


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook not configured")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload,
            request.headers.get("stripe-signature", ""),
            secret,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook")
    obj = event["data"]["object"]
    with SessionLocal() as db:
        if event["type"] == "checkout.session.completed" and obj.get("mode") == "subscription":
            uid = int(obj.get("client_reference_id") or obj.get("metadata", {}).get("tradeforce_user_id") or 0)
            if uid:
                sub = db.query(Subscription).filter(Subscription.user_id == uid).first() or Subscription(user_id=uid)
                sub.plan = obj.get("metadata", {}).get("plan", "pro")
                sub.status = "active"
                sub.stripe_customer_id = obj.get("customer") or ""
                sub.stripe_subscription_id = obj.get("subscription") or ""
                db.add(sub)
                db.commit()
        elif event["type"] in {"customer.subscription.updated", "customer.subscription.deleted"}:
            sid = obj.get("id")
            sub = db.query(Subscription).filter(Subscription.stripe_subscription_id == sid).first()
            if sub:
                sub.status = obj.get("status", "canceled")
                sub.current_period_end = str(obj.get("current_period_end") or "")
                db.commit()
    return {"received": True}
