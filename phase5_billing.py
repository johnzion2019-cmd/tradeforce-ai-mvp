"""TradeForce AI Phase 5 billing module.

Register with the FastAPI application from main.py. Secrets stay in environment variables.
"""
import os
import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine
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
    user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, unique=True, index=True)
    plan = Column(String(40), default="starter")
    status = Column(String(40), default="inactive", index=True)
    stripe_customer_id = Column(String(120), default="", index=True)
    stripe_subscription_id = Column(String(120), default="", index=True)
    current_period_end = Column(String(40), default="")

BillingBase.metadata.create_all(bind=engine)

def stripe_ready():
    return bool(os.getenv("STRIPE_SECRET_KEY") and os.getenv("STRIPE_PRO_PRICE_ID") and os.getenv("STRIPE_BUSINESS_PRICE_ID"))

def base_url(request):
    return (os.getenv("APP_BASE_URL") or str(request.base_url)).rstrip("/")

def account_id(request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return int(uid)

def account_email(db, uid):
    row = db.execute(__import__('sqlalchemy').text("SELECT email, role FROM account_users WHERE id=:id"), {"id": uid}).first()
    if not row or row.role != "contractor":
        raise HTTPException(status_code=403, detail="Contractor account required.")
    return row.email

@router.get("/billing")
def billing(request: Request):
    uid = account_id(request)
    with SessionLocal() as db:
        email = account_email(db, uid)
        sub = db.query(Subscription).filter(Subscription.user_id == uid).first()
        return {"phase": 5, "email": email, "plan": sub.plan if sub else "starter", "status": sub.status if sub else "inactive", "stripe_configured": stripe_ready()}

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
        params = {"mode":"subscription","line_items":[{"price":price,"quantity":1}],"success_url":base_url(request)+"/dashboard?billing=success","cancel_url":base_url(request)+"/dashboard?billing=cancelled","client_reference_id":str(uid),"metadata":{"tradeforce_user_id":str(uid),"plan":plan}}
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
        session = stripe.billing_portal.Session.create(customer=sub.stripe_customer_id, return_url=base_url(request)+"/dashboard")
        return RedirectResponse(session.url, status_code=303)

@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook not configured")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook")
    obj = event["data"]["object"]
    with SessionLocal() as db:
        if event["type"] == "checkout.session.completed" and obj.get("mode") == "subscription":
            uid = int(obj.get("client_reference_id") or obj.get("metadata", {}).get("tradeforce_user_id") or 0)
            if uid:
                sub = db.query(Subscription).filter(Subscription.user_id == uid).first() or Subscription(user_id=uid)
                sub.plan = obj.get("metadata", {}).get("plan", "pro"); sub.status = "active"; sub.stripe_customer_id = obj.get("customer") or ""; sub.stripe_subscription_id = obj.get("subscription") or ""; db.add(sub); db.commit()
        elif event["type"] in {"customer.subscription.updated", "customer.subscription.deleted"}:
            sid = obj.get("id")
            sub = db.query(Subscription).filter(Subscription.stripe_subscription_id == sid).first()
            if sub:
                sub.status = obj.get("status", "canceled"); sub.current_period_end = str(obj.get("current_period_end") or ""); db.commit()
    return {"received": True}
