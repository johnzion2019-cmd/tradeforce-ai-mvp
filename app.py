"""TradeForce AI application entrypoint with Phase 5 billing enabled."""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import main
from fastapi import Request
from fastapi.responses import PlainTextResponse
from main import app
from phase5_billing import router as billing_router


MAX_UPLOAD_REQUEST_BYTES = 12 * 1024 * 1024
ALLOWED_UPLOADS = {
    ".pdf": {"application/pdf"},
    ".doc": {"application/msword", "application/octet-stream"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip", "application/octet-stream"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
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


quarantine_legacy_unowned_jobs()
app.include_router(billing_router)


@app.middleware("http")
async def production_security(request: Request, call_next):
    """Apply browser security controls and validate worker document uploads."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/stripe/webhook":
        expected_host = urlparse(os.getenv("APP_BASE_URL", "")).netloc or request.url.netloc
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source_host = urlparse(origin).netloc if origin else (urlparse(referer).netloc if referer else "")
        if source_host and source_host != expected_host:
            return PlainTextResponse("Cross-site request blocked.", status_code=403)

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
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response
