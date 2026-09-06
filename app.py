"""TradeForce AI application entrypoint with Phase 5 billing enabled."""
import os
from urllib.parse import urlparse

import main
from fastapi import Request
from fastapi.responses import PlainTextResponse
from main import app
from phase5_billing import router as billing_router


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


quarantine_legacy_unowned_jobs()
app.include_router(billing_router)


@app.middleware("http")
async def production_security(request: Request, call_next):
    """Apply lightweight browser security controls without breaking Stripe webhooks."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/stripe/webhook":
        expected_host = urlparse(os.getenv("APP_BASE_URL", "")).netloc or request.url.netloc
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source_host = urlparse(origin).netloc if origin else (urlparse(referer).netloc if referer else "")
        if source_host and source_host != expected_host:
            return PlainTextResponse("Cross-site request blocked.", status_code=403)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response
