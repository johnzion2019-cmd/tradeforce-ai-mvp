"""TradeForce AI Phase 5 billing module.

Register with the FastAPI application from app.py. Secrets stay in environment variables.
"""
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


def billing_page(email, sub):
    plan = sub.plan if sub else "starter"
    status = sub.status if sub else "inactive"
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
<section class="card"><h2>TradeForce Pro</h2><div class="price">$99 <span>/ month</span></div><ul><li>Talent discovery and search</li><li>Candidate favorites and recruiting tools</li><li>Messaging tied to recruiting relationships</li></ul><form method="post" action="/billing/checkout/pro"><button type="submit"{disabled}>Upgrade to Pro</button></form></section>
<section class="card"><h2>TradeForce Business</h2><div class="price">$249 <span>/ month</span></div><ul><li>Everything in Pro</li><li>Advanced contractor recruiting workflow</li><li>Built for higher-volume hiring teams</li></ul><form method="post" action="/billing/checkout/business"><button type="submit"{disabled}>Upgrade to Business</button></form></section>
</div>
<a class="back" href="/dashboard">← Back to contractor dashboard</a>
</main>
</body>
</html>"""


@router.get("/phase5/health")
def phase5_health():
    return {
        "phase": 5,
        "billing_module": "ready",
        "stripe_configured": stripe_ready(),
        "webhook_configured": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
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
