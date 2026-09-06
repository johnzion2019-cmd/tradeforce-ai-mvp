"""Account recovery and email security for TradeForce AI."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib import error as urlerror
from urllib import request as urlrequest

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Session

import main

router = APIRouter()


class AccountToken(main.Base):
    __tablename__ = "account_tokens"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    purpose = Column(String(40), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(String(50), nullable=False)
    used_at = Column(String(50), default="")
    created_at = Column(String(50), nullable=False)


main.Base.metadata.create_all(bind=main.engine)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _new_token(db: Session, user_id: int, purpose: str, ttl_minutes: int) -> str:
    raw = secrets.token_urlsafe(32)
    row = AccountToken(
        user_id=user_id,
        purpose=purpose,
        token_hash=_hash_token(raw),
        expires_at=(_now() + timedelta(minutes=ttl_minutes)).isoformat(),
        created_at=_now().isoformat(),
        used_at="",
    )
    db.add(row)
    db.commit()
    return raw


def _valid_token(db: Session, raw_token: str, purpose: str):
    row = (
        db.query(AccountToken)
        .filter(
            AccountToken.token_hash == _hash_token(raw_token),
            AccountToken.purpose == purpose,
            AccountToken.used_at == "",
        )
        .first()
    )
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row.expires_at)
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        return None
    return row


def email_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY") and os.getenv("EMAIL_FROM"))


def _send_email(to_email: str, subject: str, html: str) -> bool:
    """Send transactional email through Resend when configured."""
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("EMAIL_FROM")
    if not api_key or not from_email:
        return False
    payload = json.dumps({"from": from_email, "to": [to_email], "subject": subject, "html": html}).encode()
    req = urlrequest.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TradeForceAI/1.0",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as response:
            return 200 <= response.status < 300
    except (urlerror.URLError, TimeoutError):
        return False


def _base_url(request: Request) -> str:
    return (os.getenv("APP_BASE_URL") or str(request.base_url).rstrip("/")).rstrip("/")


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request):
    return main.templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "sent": False, "email_configured": email_configured()},
    )


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password(request: Request, email: str = Form(...), db: Session = Depends(main.get_db)):
    normalized = main.normalize(email)
    user = db.query(main.UserAccount).filter(main.UserAccount.email == normalized).first()
    if user and email_configured():
        raw = _new_token(db, user.id, "password_reset", 30)
        reset_url = f"{_base_url(request)}/reset-password?token={raw}"
        _send_email(
            user.email,
            "Reset your TradeForce AI password",
            (
                "<h2>Reset your TradeForce AI password</h2>"
                "<p>This link expires in 30 minutes and can only be used once.</p>"
                f"<p><a href=\"{reset_url}\">Reset password</a></p>"
                "<p>If you did not request this, you can ignore this email.</p>"
            ),
        )
    # Always return the same response to avoid revealing whether an account exists.
    return main.templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "sent": True, "email_configured": email_configured()},
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(request: Request, token: str = "", db: Session = Depends(main.get_db)):
    valid = bool(token and _valid_token(db, token, "password_reset"))
    return main.templates.TemplateResponse(
        "reset_password.html",
        {"request": request, "token": token, "valid": valid, "error": ""},
        status_code=200 if valid else 400,
    )


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(main.get_db),
):
    row = _valid_token(db, token, "password_reset")
    if not row:
        return main.templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": token, "valid": False, "error": "This reset link is invalid or has expired."},
            status_code=400,
        )
    if len(password) < 10:
        return main.templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": token, "valid": True, "error": "Use a password with at least 10 characters."},
            status_code=400,
        )
    if password != confirm_password:
        return main.templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": token, "valid": True, "error": "Passwords do not match."},
            status_code=400,
        )
    user = db.query(main.UserAccount).filter(main.UserAccount.id == row.user_id).first()
    if not user:
        return RedirectResponse("/login", status_code=303)
    user.password_hash = main.hash_password(password)
    row.used_at = _now().isoformat()
    # Invalidate any other outstanding reset tokens for the account.
    for other in db.query(AccountToken).filter(AccountToken.user_id == user.id, AccountToken.purpose == "password_reset", AccountToken.used_at == "").all():
        other.used_at = _now().isoformat()
    db.commit()
    request.session.clear()
    return RedirectResponse("/login?reset=success", status_code=303)


@router.get("/account-security/health")
def account_security_health():
    return {
        "password_recovery": "ready",
        "email_provider": "resend",
        "email_configured": email_configured(),
        "reset_token_ttl_minutes": 30,
    }
