"""TradeForce AI application entrypoint with Phase 6 hiring/onboarding enabled."""
import html
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import main
from account_security import _send_email, router as account_security_router
from fastapi import Request
from fastapi.responses import PlainTextResponse
from legal_routes import router as legal_router
from main import app
from phase5_billing import router as billing_router
from phase6_onboarding import init_phase6, onboarding_progress, hire_for_application, router as phase6_router


MAX_UPLOAD_REQUEST_BYTES = 12 * 1024 * 1024
ALLOWED_UPLOADS = {
    ".pdf": {"application/pdf"},
    ".doc": {"application/msword", "application/octet-stream"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip", "application/octet-stream"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
}

# Lightweight per-instance rate limiting for the MVP. This protects high-risk
# endpoints from basic abuse without introducing another paid dependency.
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
RATE_LIMITS = {
    "login": (10, 300),       # 10 attempts / 5 minutes / client
    "signup": (5, 3600),      # 5 account creations / hour / client
    "message": (30, 60),      # 30 message writes / minute / client
    "job": (20, 60),          # 20 job mutations / minute / client
    "apply": (30, 60),        # 30 application writes / minute / client
    "mutation": (120, 60),    # fallback for other state-changing requests
}


def quarantine_legacy_unowned_jobs() -> None:
    """Keep legacy jobs without a contractor owner out of worker-facing flows.

    Older MVP records could have a null requests.user_id. Those records cannot
    safely accept applications because applications require a real contractor
    account. Mark any still-open orphaned records as paused during startup so
    they no longer appear as open opportunities or accept applications.
    """
    db = main.SessionLocal()
    try:
        orphaned = (
            db.query(main.ManpowerRequest)
            .filter(
                main.ManpowerRequest.user_id.is_(None),
                main.ManpowerRequest.status == "open",
            )
            .all()
        )
        if orphaned:
            for job in orphaned:
                job.status = "paused"
            db.commit()
    finally:
        db.close()


def _looks_like_expected_file(extension: str, data: bytes) -> bool:
    """Perform inexpensive signature checks in addition to extension/MIME validation."""
    if extension == ".pdf":
        return b"%PDF-" in data[:1024]
    if extension in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".doc":
        return data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if extension == ".docx":
        return data.startswith(b"PK\x03\x04")
    return False


def validate_worker_uploads(content_type: str, body: bytes) -> str | None:
    """Return an error message when a worker-profile upload is unsafe or unsupported."""
    if len(body) > MAX_UPLOAD_REQUEST_BYTES:
        return "Upload request is too large. Each document must be 5 MB or smaller."

    boundary_match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type, re.I)
    if not boundary_match:
        return "Invalid upload form."
    boundary = (boundary_match.group(1) or boundary_match.group(2)).strip().encode()

    for part in body.split(b"--" + boundary):
        if b"filename=" not in part:
            continue
        header_blob, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            return "Invalid uploaded document."
        data = data.rsplit(b"\r\n", 1)[0]

        disposition = re.search(br'filename="([^"]*)"', header_blob, re.I)
        if not disposition or not disposition.group(1):
            continue
        filename = disposition.group(1).decode("utf-8", "ignore")
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_UPLOADS:
            return "Unsupported file type. Upload PDF, DOC, DOCX, JPG, JPEG, or PNG files only."
        if len(data) > 5 * 1024 * 1024:
            return "Each document must be 5 MB or smaller."

        mime_match = re.search(br"Content-Type:\s*([^\r\n;]+)", header_blob, re.I)
        mime = mime_match.group(1).decode("ascii", "ignore").strip().lower() if mime_match else "application/octet-stream"
        if mime not in ALLOWED_UPLOADS[extension]:
            return "The uploaded document type does not match its file extension."
        if not _looks_like_expected_file(extension, data):
            return "The uploaded document does not appear to be a valid file of that type."

    return None


def _client_key(request: Request) -> str:
    """Return a best-effort client identifier behind Render's reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:80]
    return (request.client.host if request.client else "unknown")[:80]


def _rate_class(request: Request) -> str:
    path = request.url.path
    if path == "/login":
        return "login"
    if path == "/signup":
        return "signup"
    if path.startswith("/messages") or path.startswith("/message"):
        return "message"
    if path.startswith("/contractor/job"):
        return "job"
    if path.startswith("/job/") and path.endswith("/apply"):
        return "apply"
    return "mutation"


def _rate_limited(request: Request) -> tuple[bool, int]:
    """Return (blocked, retry_after_seconds) for state-changing requests."""
    rate_class = _rate_class(request)
    limit, window = RATE_LIMITS[rate_class]
    now = time.monotonic()
    bucket_key = f"{rate_class}:{_client_key(request)}"
    bucket = _RATE_BUCKETS[bucket_key]
    cutoff = now - window
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        retry_after = max(1, int(window - (now - bucket[0])))
        return True, retry_after
    bucket.append(now)
    return False, 0


def _job_display(job_id: int) -> str:
    """Return a readable company/trade label for worker application cards."""
    db = main.SessionLocal()
    try:
        job = db.query(main.ManpowerRequest).filter(main.ManpowerRequest.id == job_id).first()
        if job:
            return f"{job.company} — {job.trade}"
        return f"Job #{job_id}"
    finally:
        db.close()


def _contractor_display(user_id: int) -> str:
    """Return the contractor's company name, falling back to their latest job."""
    db = main.SessionLocal()
    try:
        profile = db.query(main.CompanyProfile).filter(main.CompanyProfile.user_id == user_id).first()
        if profile and profile.company_name.strip():
            return profile.company_name.strip()
        job = (
            db.query(main.ManpowerRequest)
            .filter(main.ManpowerRequest.user_id == user_id)
            .order_by(main.ManpowerRequest.id.desc())
            .first()
        )
        if job and job.company.strip():
            return job.company.strip()
        account = db.query(main.UserAccount).filter(main.UserAccount.id == user_id).first()
        return account.email if account else "Contractor"
    finally:
        db.close()


def _company_public(user_id: int) -> dict | None:
    """Return worker-safe company profile details for opportunity cards."""
    if not user_id:
        return None
    db = main.SessionLocal()
    try:
        profile = db.query(main.CompanyProfile).filter(main.CompanyProfile.user_id == user_id).first()
        if not profile:
            return None
        company_name = (profile.company_name or "").strip()
        location = (profile.location or "").strip()
        website = (profile.website or "").strip()
        bio = (profile.bio or "").strip()
        if not any((company_name, location, website, bio)):
            return None
        website_url = website
        if website_url and not website_url.lower().startswith(("http://", "https://")):
            website_url = "https://" + website_url
        return {
            "company_name": company_name or _contractor_display(user_id),
            "location": location,
            "website": website,
            "website_url": website_url,
            "bio": bio,
            "verified": bool(profile.verified),
        }
    finally:
        db.close()


def _email_latest_manpower_request(user_id: int) -> None:
    """Email the contractor a confirmation after a manpower request posts successfully."""
    db = main.SessionLocal()
    try:
        account = db.query(main.UserAccount).filter(main.UserAccount.id == user_id).first()
        job = (
            db.query(main.ManpowerRequest)
            .filter(main.ManpowerRequest.user_id == user_id)
            .order_by(main.ManpowerRequest.id.desc())
            .first()
        )
        if not account or not job:
            return
        dashboard_url = (os.getenv("APP_BASE_URL") or "https://tradeforce-ai.com").rstrip("/") + "/dashboard"
        company = html.escape(job.company or "Your company")
        trade = html.escape(job.trade or "Skilled trade")
        location = html.escape(job.location or "Not specified")
        pay_range = html.escape(job.pay_range or "Not specified")
        start_date = html.escape(job.start_date or "Not specified")
        duration = html.escape(job.duration or "Not specified")
        notes = html.escape(job.notes or "None")
        _send_email(
            account.email,
            f"Manpower request posted — {job.trade}",
            (
                "<h2>Your manpower request is live</h2>"
                f"<p><strong>{company}</strong> has posted a request for <strong>{job.workers_needed} {trade}</strong> worker(s).</p>"
                f"<p><strong>Location:</strong> {location}<br>"
                f"<strong>Pay:</strong> {pay_range}<br>"
                f"<strong>Start date:</strong> {start_date}<br>"
                f"<strong>Duration:</strong> {duration}</p>"
                f"<p><strong>Requirements / notes:</strong> {notes}</p>"
                f"<p><a href=\"{html.escape(dashboard_url, quote=True)}\">View your TradeForce AI dashboard</a></p>"
            ),
        )
    finally:
        db.close()


main.templates.env.globals["job_display"] = _job_display
main.templates.env.globals["contractor_display"] = _contractor_display
main.templates.env.globals["company_public"] = _company_public
main.templates.env.globals["hire_for_application"] = hire_for_application
main.templates.env.globals["onboarding_progress"] = onboarding_progress


quarantine_legacy_unowned_jobs()
init_phase6()
app.include_router(billing_router)
app.include_router(legal_router)
app.include_router(account_security_router)
app.include_router(phase6_router)


@app.middleware("http")
async def production_security(request: Request, call_next):
    """Apply browser security, abuse controls, upload validation, and post-job email confirmation."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/stripe/webhook":
        expected_host = urlparse(os.getenv("APP_BASE_URL", "")).netloc or request.url.netloc
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source_host = urlparse(origin).netloc if origin else (urlparse(referer).netloc if referer else "")
        if source_host and source_host != expected_host:
            return PlainTextResponse("Cross-site request blocked.", status_code=403)

        blocked, retry_after = _rate_limited(request)
        if blocked:
            return PlainTextResponse(
                "Too many requests. Please try again shortly.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

    if request.method == "POST" and request.url.path == "/worker/profile":
        content_type = request.headers.get("content-type", "")
        if content_type.lower().startswith("multipart/form-data"):
            body = await request.body()
            error = validate_worker_uploads(content_type, body)
            if error:
                return PlainTextResponse(error, status_code=400)

            sent = False

            async def replay_receive():
                nonlocal sent
                if sent:
                    return {"type": "http.request", "body": b"", "more_body": False}
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = replay_receive

    response = await call_next(request)

    if request.method == "POST" and request.url.path == "/contractor/job" and response.status_code in {302, 303}:
        try:
            user_id = request.session.get("user_id")
            if user_id:
                _email_latest_manpower_request(int(user_id))
        except Exception:
            # Email confirmation must never turn a successful job posting into an error.
            pass

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.post("/job/{job_id}/reopen")
def reopen_job(job_id: int, request: main.Request, db: main.Session = main.Depends(main.get_db)):
    """Reopen a contractor-owned closed manpower request without touching its history."""
    user = main.require_user(request, db, "contractor")
    job = (
        db.query(main.ManpowerRequest)
        .filter(main.ManpowerRequest.id == job_id, main.ManpowerRequest.user_id == user.id)
        .first()
    )
    if not job:
        raise main.HTTPException(status_code=404, detail="Job not found")
    if job.status == "closed":
        job.status = "open"
        main.notify(db, user.id, "Manpower request reopened", f"Your {job.trade} request is open again.")
        db.commit()
    return main.RedirectResponse("/dashboard", status_code=303)


@app.post("/application/{application_id}/offer/accept")
def accept_offer(application_id: int, request: main.Request, db: main.Session = main.Depends(main.get_db)):
    """Allow the worker who owns an application to accept a contractor offer."""
    user = main.require_user(request, db, "worker")
    app_row = (
        db.query(main.Application)
        .filter(
            main.Application.id == application_id,
            main.Application.worker_user_id == user.id,
        )
        .first()
    )
    if not app_row:
        raise main.HTTPException(status_code=404, detail="Application not found")
    if app_row.status != "offered":
        raise main.HTTPException(status_code=400, detail="This application does not have an active offer.")
    app_row.status = "offer_accepted"
    app_row.updated_at = main.now_iso()
    job = main.job_for_application(db, app_row)
    worker = db.query(main.Worker).filter(main.Worker.id == app_row.worker_id).first()
    worker_name = worker.name if worker else "Candidate"
    main.notify(
        db,
        app_row.contractor_user_id,
        "Offer accepted",
        f"{worker_name} accepted your offer for {job.trade if job else 'the position'}. You can now mark the candidate as hired.",
    )
    main.notify(
        db,
        user.id,
        "Offer accepted",
        f"You accepted the offer from {job.company if job else 'the contractor'}.",
    )
    db.commit()
    return main.RedirectResponse("/dashboard", status_code=303)


@app.post("/application/{application_id}/offer/decline")
def decline_offer(application_id: int, request: main.Request, db: main.Session = main.Depends(main.get_db)):
    """Allow the worker who owns an application to decline a contractor offer."""
    user = main.require_user(request, db, "worker")
    app_row = (
        db.query(main.Application)
        .filter(
            main.Application.id == application_id,
            main.Application.worker_user_id == user.id,
        )
        .first()
    )
    if not app_row:
        raise main.HTTPException(status_code=404, detail="Application not found")
    if app_row.status != "offered":
        raise main.HTTPException(status_code=400, detail="This application does not have an active offer.")
    app_row.status = "offer_declined"
    app_row.updated_at = main.now_iso()
    job = main.job_for_application(db, app_row)
    worker = db.query(main.Worker).filter(main.Worker.id == app_row.worker_id).first()
    worker_name = worker.name if worker else "Candidate"
    main.notify(
        db,
        app_row.contractor_user_id,
        "Offer declined",
        f"{worker_name} declined your offer for {job.trade if job else 'the position'}.",
    )
    main.notify(
        db,
        user.id,
        "Offer declined",
        f"You declined the offer from {job.company if job else 'the contractor'}.",
    )
    db.commit()
    return main.RedirectResponse("/dashboard", status_code=303)