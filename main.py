import html
import hashlib
import os
import secrets
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlencode, urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, Column, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, inspect, or_, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

import phase5_billing
from phase5_billing import has_active_paid_subscription

app = FastAPI(title="TradeForce AI")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()

SESSION_SECRET = os.getenv("SESSION_SECRET") or os.getenv("ADMIN_PASSWORD") or "tradeforce-dev-secret"
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tradeforce.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def now_iso():
    return datetime.utcnow().isoformat()


class UserAccount(Base):
    __tablename__ = "account_users"
    id = Column(Integer, primary_key=True)
    email = Column(String(220), nullable=False, unique=True, index=True)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(30), nullable=False, index=True)
    created_at = Column(String(40), default=now_iso)


class Worker(Base):
    __tablename__ = "workers"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("account_users.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False)
    trade = Column(String(100), nullable=False, index=True)
    location = Column(String(200), default="")
    experience = Column(Integer, default=0)
    certifications = Column(Text, default="")
    phone = Column(String(80), default="")
    email = Column(String(200), default="")
    available = Column(Boolean, default=True)
    desired_pay = Column(String(100), default="")
    willing_to_travel = Column(Boolean, default=True)
    resume_name = Column(String(255), default="")
    resume_type = Column(String(120), default="")
    resume_data = Column(LargeBinary, nullable=True)
    resume_key = Column(String(500), default="")
    cert_name = Column(String(255), default="")
    cert_type = Column(String(120), default="")
    cert_data = Column(LargeBinary, nullable=True)
    cert_key = Column(String(500), default="")


class ManpowerRequest(Base):
    __tablename__ = "requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("account_users.id"), nullable=True, index=True)
    company = Column(String(200), nullable=False)
    contact = Column(String(250), default="")
    trade = Column(String(100), nullable=False, index=True)
    workers_needed = Column(Integer, default=1)
    location = Column(String(200), default="")
    start_date = Column(String(40), default="")
    duration = Column(String(100), default="")
    pay_range = Column(String(100), default="")
    notes = Column(Text, default="")
    required_experience = Column(Integer, default=0)
    status = Column(String(40), default="open")


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    message = Column(Text, default="")
    link = Column(String(500), default="")
    is_read = Column(Boolean, default=False)
    created_at = Column(String(40), default=now_iso)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", "worker_id", name="uq_job_worker_application"),)
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("requests.id"), nullable=False, index=True)
    worker_id = Column(Integer, ForeignKey("workers.id"), nullable=False, index=True)
    worker_user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    contractor_user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    source = Column(String(30), default="application")
    status = Column(String(40), default="applied", index=True)
    message = Column(Text, default="")
    created_at = Column(String(40), default=now_iso)
    updated_at = Column(String(40), default=now_iso)


class CompanyProfile(Base):
    __tablename__ = "company_profiles"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, unique=True, index=True)
    company_name = Column(String(220), default="")
    contact_name = Column(String(180), default="")
    phone = Column(String(80), default="")
    location = Column(String(200), default="")
    website = Column(String(300), default="")
    bio = Column(Text, default="")
    verified = Column(Boolean, default=False)
    created_at = Column(String(40), default=now_iso)
    updated_at = Column(String(40), default=now_iso)


class FavoriteCandidate(Base):
    __tablename__ = "favorite_candidates"
    __table_args__ = (UniqueConstraint("contractor_user_id", "worker_id", name="uq_favorite_candidate"),)
    id = Column(Integer, primary_key=True)
    contractor_user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    worker_id = Column(Integer, ForeignKey("workers.id"), nullable=False, index=True)
    created_at = Column(String(40), default=now_iso)
    note = Column(Text, default="")
    label = Column(String(40), default="")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    sender_user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    recipient_user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=True, index=True)
    body = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(String(40), default=now_iso)


Base.metadata.create_all(bind=engine)


def migrate_existing_tables(bind=None):
    target = bind or engine
    inspector = inspect(target)
    dialect = target.dialect.name
    binary = "BYTEA" if dialect == "postgresql" else "BLOB"
    bool_true = "BOOLEAN DEFAULT TRUE" if dialect != "sqlite" else "INTEGER DEFAULT 1"
    migrations = {
        "workers": {
            "user_id": "INTEGER",
            "desired_pay": "TEXT DEFAULT ''",
            "willing_to_travel": bool_true,
            "resume_name": "TEXT DEFAULT ''",
            "resume_type": "TEXT DEFAULT ''",
            "resume_data": binary,
            "resume_key": "TEXT DEFAULT ''",
            "cert_name": "TEXT DEFAULT ''",
            "cert_type": "TEXT DEFAULT ''",
            "cert_data": binary,
            "cert_key": "TEXT DEFAULT ''",
        },
        "requests": {
            "user_id": "INTEGER",
            "required_experience": "INTEGER DEFAULT 0",
            "status": "TEXT DEFAULT 'open'",
        },
        "notifications": {
            "link": "TEXT DEFAULT ''",
        },
        "favorite_candidates": {
            "note": "TEXT DEFAULT ''",
            "label": "TEXT DEFAULT ''",
        },
    }
    with target.begin() as conn:
        for table_name, columns in migrations.items():
            if table_name not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table_name)}
            for column_name, ddl in columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))


migrate_existing_tables()
# Same subscriptions table Phase 5 webhooks write. Bind it to this engine so
# entitlement checks read the app database rather than calling Stripe.
phase5_billing.BillingBase.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD")
    if not expected_password:
        raise HTTPException(status_code=503, detail="Admin credentials are not configured.")
    user_ok = secrets.compare_digest(credentials.username.encode(), expected_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), expected_password.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240000).hex()
    return secrets.compare_digest(digest, expected)


def current_user(request: Request, db: Session) -> Optional[UserAccount]:
    user_id = request.session.get("user_id")
    return db.query(UserAccount).filter(UserAccount.id == user_id).first() if user_id else None


def require_user(request: Request, db: Session, role: Optional[str] = None) -> UserAccount:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in.")
    if role and user.role != role:
        raise HTTPException(status_code=403, detail="This account does not have access to that page.")
    return user


def normalize(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def cert_terms(value: str):
    keywords = ["nccer", "osha 10", "osha 30", "twic", "aws", "api", "rigging", "forklift", "journeyman", "welding"]
    normalized = normalize(value)
    return {k for k in keywords if k in normalized}


def match_score(worker: Worker, job: ManpowerRequest) -> int:
    score = 0
    worker_trade, job_trade = normalize(worker.trade), normalize(job.trade)
    if worker_trade == job_trade:
        score += 45
    elif worker_trade in job_trade or job_trade in worker_trade:
        score += 30
    wloc, jloc = normalize(worker.location), normalize(job.location)
    if wloc and jloc:
        if wloc == jloc:
            score += 15
        elif wloc in jloc or jloc in wloc:
            score += 9
    if worker.willing_to_travel:
        score += 10
    if worker.available:
        score += 5
    required, experience = job.required_experience or 0, worker.experience or 0
    if experience >= required:
        score += 15 if required else min(10, max(3, experience // 2))
    else:
        score -= min(20, (required - experience) * 4)
    wanted = cert_terms((job.notes or "") + " " + job.trade)
    owned = cert_terms(worker.certifications or "")
    if wanted:
        score += min(10, len(wanted & owned) * 5)
    return max(0, min(100, score))


def match_reasons(worker: Worker, job: ManpowerRequest):
    reasons = []
    if normalize(worker.trade) == normalize(job.trade):
        reasons.append("Exact trade")
    if normalize(worker.location) == normalize(job.location) and worker.location:
        reasons.append("Same location")
    elif worker.willing_to_travel:
        reasons.append("Will travel")
    if (worker.experience or 0) >= (job.required_experience or 0):
        reasons.append("Experience met")
    if cert_terms(worker.certifications or "") & cert_terms(job.notes or ""):
        reasons.append("Certification fit")
    return reasons[:3]


def ranked_matches(workers, job, limit=5):
    ranked = [{"worker": w, "score": match_score(w, job), "reasons": match_reasons(w, job)} for w in workers if w.available and normalize(w.trade) and normalize(job.trade)]
    ranked.sort(key=lambda x: (x["score"], x["worker"].experience or 0), reverse=True)
    return [m for m in ranked if m["score"] >= 35][:limit]


def worker_job_matches(worker, jobs, limit=12):
    ranked = [{"job": j, "score": match_score(worker, j), "reasons": match_reasons(worker, j)} for j in jobs if j.status == "open"]
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return [m for m in ranked if m["score"] >= 35][:limit]


def safe_notification_link(link: Optional[str]) -> str:
    """Keep notification destinations inside this app. Rejects external and script URLs."""
    if link is None:
        return ""
    value = str(link).strip()
    if not value or len(value) > 500 or any(ch in value for ch in ("\r", "\n", "\x00", "\\")):
        return ""
    lowered = value.lower()
    if "javascript:" in lowered or "data:" in lowered:
        return ""
    if value.startswith("/") and not value.startswith("//"):
        if _is_notification_open_path(value):
            return ""
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    allowed = {"tradeforce-ai.com", "www.tradeforce-ai.com"}
    base = (os.getenv("APP_BASE_URL") or "").strip()
    if base:
        base_host = urlparse(base).netloc.lower()
        if base_host:
            allowed.add(base_host)
    if host not in allowed:
        return ""
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return ""
    query = f"?{parsed.query}" if parsed.query else ""
    destination = path + query
    if _is_notification_open_path(destination):
        return ""
    return destination


def _is_notification_open_path(value: str) -> bool:
    head = value.split("?", 1)[0].rstrip("/")
    parts = head.split("/")
    return len(parts) == 4 and parts[1] == "notifications" and parts[2].isdigit() and parts[3] == "open"


def notify(db: Session, user_id: int, title: str, message: str, link: str = ""):
    db.add(Notification(user_id=user_id, title=title, message=message, link=safe_notification_link(link)))


def notification_list(db: Session, user_id: int):
    return db.query(Notification).filter(Notification.user_id == user_id).order_by(Notification.id.desc()).limit(12).all()


def nav_unread(request: Request, kind: str) -> int:
    """Unread message and notification counts for the signed-in nav. Never raises."""
    counts = getattr(request.state, "nav_unread_counts", None)
    if counts is None:
        counts = {"messages": 0, "notifications": 0}
        user_id = None
        try:
            user_id = request.session.get("user_id")
        except Exception:
            user_id = None
        if user_id:
            db = SessionLocal()
            try:
                counts = {
                    "messages": db.query(Message).filter(Message.recipient_user_id == user_id, Message.is_read.is_(False)).count(),
                    "notifications": db.query(Notification).filter(Notification.user_id == user_id, Notification.is_read.is_(False)).count(),
                }
            except Exception:
                counts = {"messages": 0, "notifications": 0}
            finally:
                db.close()
        try:
            request.state.nav_unread_counts = counts
        except Exception:
            pass
    try:
        return int(counts.get(kind) or 0)
    except Exception:
        return 0


SAVED_LABELS = ("Top pick", "Follow up", "Maybe", "Not now")
SAVED_NOTE_LIMIT = 1000
UPGRADE_COPY = {
    "saved": {
        "title": "Saved candidates are included with Pro",
        "message": "Starter includes job posting and basic talent search. An active Pro or Business plan unlocks the saved-candidates workspace, private notes and labels, and the Saved only filter.",
    },
    "messages": {
        "title": "Contractor messaging is included with Pro",
        "message": "Starter includes job posting, invitations, and the hiring pipeline. An active Pro or Business plan unlocks the contractor messaging inbox. Workers can still message you about an application, invitation, or hire.",
    },
}


def contractor_pro_entitled(db: Session, user: Optional[UserAccount]) -> bool:
    """Active Pro or Business from the Phase 5 subscriptions row.

    Session role "admin" is allowed through so an admin account is not locked
    out of contractor tools. HTTP Basic /admin routes do not use this check.
    Workers are never contractor-entitled. No Stripe request is made.
    """
    if not user:
        return False
    role = (user.role or "").strip().lower()
    if role == "admin":
        return True
    if role != "contractor":
        return False
    row = db.execute(
        text("SELECT plan, status FROM subscriptions WHERE user_id = :user_id"),
        {"user_id": user.id},
    ).first()
    return has_active_paid_subscription(row)


def bind_contractor_pro(request: Request, db: Session, user: UserAccount) -> bool:
    entitled = contractor_pro_entitled(db, user)
    try:
        request.state.contractor_has_pro = entitled
    except Exception:
        pass
    return entitled


def contractor_has_pro(request: Request) -> bool:
    """Template helper. Reuses the value bound during the request when present."""
    cached = getattr(request.state, "contractor_has_pro", None)
    if cached is not None:
        return bool(cached)
    entitled = False
    user_id = request.session.get("user_id")
    if user_id:
        db = SessionLocal()
        try:
            user = db.query(UserAccount).filter(UserAccount.id == user_id).first()
            entitled = contractor_pro_entitled(db, user)
        finally:
            db.close()
    try:
        request.state.contractor_has_pro = entitled
    except Exception:
        pass
    return entitled


def upgrade_feature(feature: Optional[str]) -> str:
    value = (feature or "").strip().lower()
    return value if value in UPGRADE_COPY else "saved"


def render_upgrade(request: Request, user: UserAccount, feature: Optional[str]):
    key = upgrade_feature(feature)
    copy = UPGRADE_COPY[key]
    return templates.TemplateResponse(
        "upgrade.html",
        {
            "request": request,
            "user": user,
            "upgrade_title": copy["title"],
            "upgrade_message": copy["message"],
            "upgrade_feature": key,
        },
    )


def pro_upgrade_redirect(feature: str):
    return RedirectResponse("/upgrade?feature=" + upgrade_feature(feature), status_code=303)


templates.env.globals["nav_unread"] = nav_unread
templates.env.globals["saved_labels"] = SAVED_LABELS
templates.env.globals["contractor_has_pro"] = contractor_has_pro


def notification_return_path(request: Request) -> str:
    """Same-app page to return to when a notification has no destination."""
    referer = request.headers.get("referer") or ""
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return "/notifications"
    path = parsed.path or ""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/notifications"
    if path.rstrip("/").endswith("/open") and "/notifications/" in path:
        return "/notifications"
    query = f"?{parsed.query}" if parsed.query else ""
    return path + query

def email_user(db: Session, user_id: int, subject: str, message: str, path: str = "/dashboard"):
    """Best-effort transactional email; failures never block the primary action."""
    try:
        from account_security import _send_email
        account = db.query(UserAccount).filter(UserAccount.id == user_id).first()
        if not account or not account.email:
            return
        app_url = (os.getenv("APP_BASE_URL") or "https://tradeforce-ai.com").rstrip("/")
        safe_message = html.escape(message)
        link = html.escape(app_url + path, quote=True)
        _send_email(account.email, subject, f"<h2>{html.escape(subject)}</h2><p>{safe_message}</p><p><a href=\"{link}\">Open TradeForce AI</a></p>")
    except Exception:
        pass


def storage_enabled() -> bool:
    return bool(os.getenv("S3_BUCKET") and os.getenv("S3_ACCESS_KEY_ID") and os.getenv("S3_SECRET_ACCESS_KEY"))


def s3_client():
    return boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT_URL") or None, aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"), aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"), region_name=os.getenv("S3_REGION", "auto"))


def store_document(data: bytes, filename: str, content_type: str, user_id: int, kind: str):
    if not storage_enabled():
        return "", data
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")[:120] or f"{kind}.bin"
    key = f"users/{user_id}/{kind}/{uuid.uuid4().hex}-{safe_name}"
    try:
        s3_client().put_object(Bucket=os.environ["S3_BUCKET"], Key=key, Body=data, ContentType=content_type)
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(status_code=503, detail=f"Document storage unavailable: {exc.__class__.__name__}")
    return key, None


def load_document(key: str):
    try:
        obj = s3_client().get_object(Bucket=os.environ["S3_BUCKET"], Key=key)
        return obj["Body"].read()
    except (BotoCoreError, ClientError):
        raise HTTPException(status_code=503, detail="Document storage unavailable")


def application_for(db: Session, job_id: int, worker_id: int):
    return db.query(Application).filter(Application.job_id == job_id, Application.worker_id == worker_id).first()


def can_message(db: Session, user_a: int, user_b: int) -> Optional[Application]:
    return db.query(Application).filter(or_((Application.worker_user_id == user_a) & (Application.contractor_user_id == user_b), (Application.worker_user_id == user_b) & (Application.contractor_user_id == user_a))).order_by(Application.id.desc()).first()


def job_for_application(db: Session, app_row: Application):
    return db.query(ManpowerRequest).filter(ManpowerRequest.id == app_row.job_id).first()


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("index.html", {"request": request, "user": current_user(request, db)})


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, role: str = "worker"):
    role = role if role in {"worker", "contractor"} else "worker"
    return templates.TemplateResponse("signup.html", {"request": request, "role": role, "error": ""})


@app.post("/signup", response_class=HTMLResponse)
def signup(request: Request, email: str = Form(...), password: str = Form(...), role: str = Form(...), db: Session = Depends(get_db)):
    email = normalize(email)
    role = role if role in {"worker", "contractor"} else "worker"
    if len(password) < 8:
        return templates.TemplateResponse("signup.html", {"request": request, "role": role, "error": "Password must be at least 8 characters."}, status_code=400)
    if db.query(UserAccount).filter(UserAccount.email == email).first():
        return templates.TemplateResponse("signup.html", {"request": request, "role": role, "error": "An account with that email already exists."}, status_code=400)
    user = UserAccount(email=email, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(UserAccount).filter(UserAccount.email == normalize(email)).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password."}, status_code=400)
    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/worker")
def worker_entry(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/signup?role=worker", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/contractor")
def contractor_entry(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/signup?role=contractor", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    notifications = notification_list(db, user.id)
    if user.role == "worker":
        worker = db.query(Worker).filter(Worker.user_id == user.id).first()
        jobs = db.query(ManpowerRequest).filter(ManpowerRequest.status == "open").order_by(ManpowerRequest.id.desc()).all()
        matches = worker_job_matches(worker, jobs) if worker else []
        apps = db.query(Application).filter(Application.worker_user_id == user.id).order_by(Application.id.desc()).all()
        app_map = {a.job_id: a for a in apps}
        return templates.TemplateResponse("worker_dashboard.html", {"request": request, "user": user, "worker": worker, "matches": matches, "notifications": notifications, "applications": apps, "app_map": app_map})
    jobs = db.query(ManpowerRequest).filter(ManpowerRequest.user_id == user.id).order_by(ManpowerRequest.id.desc()).all()
    workers = db.query(Worker).filter(Worker.available.is_(True)).all()
    matches = {job.id: ranked_matches(workers, job) for job in jobs}
    apps = db.query(Application).filter(Application.contractor_user_id == user.id).order_by(Application.id.desc()).all()
    applications_by_job = {job.id: [] for job in jobs}
    worker_lookup = {w.id: w for w in db.query(Worker).filter(Worker.id.in_([a.worker_id for a in apps] or [-1])).all()}
    for a in apps:
        applications_by_job.setdefault(a.job_id, []).append({"application": a, "worker": worker_lookup.get(a.worker_id)})
    favorite_ids = {row.worker_id for row in db.query(FavoriteCandidate).filter(FavoriteCandidate.contractor_user_id == user.id).all()}
    company_profile = db.query(CompanyProfile).filter(CompanyProfile.user_id == user.id).first()
    bind_contractor_pro(request, db, user)
    return templates.TemplateResponse("contractor_dashboard.html", {"request": request, "user": user, "jobs": jobs, "matches": matches, "notifications": notifications, "applications_by_job": applications_by_job, "favorite_ids": favorite_ids, "company_profile": company_profile})


@app.post("/worker/profile")
async def save_worker_profile(request: Request, name: str = Form(...), trade: str = Form(...), location: str = Form(""), experience: int = Form(0), certifications: str = Form(""), phone: str = Form(""), desired_pay: str = Form(""), willing_to_travel: Optional[str] = Form(None), available: Optional[str] = Form(None), resume: Optional[UploadFile] = File(None), cert_file: Optional[UploadFile] = File(None), db: Session = Depends(get_db)):
    user = require_user(request, db, "worker")
    worker = db.query(Worker).filter(Worker.user_id == user.id).first()
    if not worker:
        worker = Worker(user_id=user.id, name=name.strip(), trade=trade.strip(), email=user.email)
        db.add(worker)
    worker.name = name.strip(); worker.trade = trade.strip(); worker.location = location.strip(); worker.experience = max(0, experience); worker.certifications = certifications.strip(); worker.phone = phone.strip(); worker.email = user.email; worker.desired_pay = desired_pay.strip(); worker.willing_to_travel = willing_to_travel == "yes"; worker.available = available == "yes"
    for upload, prefix in ((resume, "resume"), (cert_file, "cert")):
        if upload and upload.filename:
            data = await upload.read()
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Each document must be 5 MB or smaller.")
            content_type = (upload.content_type or "application/octet-stream")[:120]
            key, blob = store_document(data, upload.filename, content_type, user.id, prefix)
            setattr(worker, f"{prefix}_name", upload.filename[:255]); setattr(worker, f"{prefix}_type", content_type); setattr(worker, f"{prefix}_key", key); setattr(worker, f"{prefix}_data", blob)
    db.commit()
    strong = worker_job_matches(worker, db.query(ManpowerRequest).filter(ManpowerRequest.status == "open").all(), limit=3)
    if strong:
        notify(db, user.id, "New job matches ready", f"Your updated profile has {len(strong)} strong job match(es).", "/opportunities")
        db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/contractor/job")
def create_job(request: Request, company: str = Form(...), contact: str = Form(""), trade: str = Form(...), workers_needed: int = Form(1), location: str = Form(""), start_date: str = Form(""), duration: str = Form(""), pay_range: str = Form(""), notes: str = Form(""), required_experience: int = Form(0), db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    job = ManpowerRequest(user_id=user.id, company=company.strip(), contact=contact.strip(), trade=trade.strip(), workers_needed=max(1, workers_needed), location=location.strip(), start_date=start_date.strip(), duration=duration.strip(), pay_range=pay_range.strip(), notes=notes.strip(), required_experience=max(0, required_experience), status="open")
    db.add(job); db.commit(); db.refresh(job)
    top = ranked_matches(db.query(Worker).filter(Worker.available.is_(True)).all(), job, limit=10)
    for match in top:
        if match["score"] >= 60 and match["worker"].user_id:
            notify(db, match["worker"].user_id, f"New {job.trade} opportunity", f"{job.company} posted a {job.trade} job in {job.location or 'a new location'} with a {match['score']}% match to your profile.", "/opportunities?trade=" + quote(job.trade or ""))
    notify(db, user.id, "Manpower request posted", f"Your {job.trade} request is live with {len(top)} ranked candidate match(es).", "/dashboard")
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/job/{job_id}/apply")
def apply_to_job(job_id: int, request: Request, message: str = Form(""), db: Session = Depends(get_db)):
    user = require_user(request, db, "worker")
    worker = db.query(Worker).filter(Worker.user_id == user.id).first()
    job = db.query(ManpowerRequest).filter(ManpowerRequest.id == job_id, ManpowerRequest.status == "open").first()
    if not worker or not job:
        raise HTTPException(status_code=404, detail="Profile or job not found")
    app_row = application_for(db, job.id, worker.id)
    if app_row:
        if app_row.source == "invite" and app_row.status == "invited":
            app_row.status = "applied"; app_row.source = "invite_accepted"; app_row.message = message.strip(); app_row.updated_at = now_iso()
        else:
            return RedirectResponse("/dashboard", status_code=303)
    else:
        app_row = Application(job_id=job.id, worker_id=worker.id, worker_user_id=user.id, contractor_user_id=job.user_id, source="application", status="applied", message=message.strip())
        db.add(app_row)
    notify(db, job.user_id, f"New applicant: {worker.name}", f"{worker.name} applied to your {job.trade} request.", f"/worker/{worker.id}")
    notify(db, user.id, "Application sent", f"Your application to {job.company} — {job.trade} was submitted.", "/dashboard")
    db.commit()
    email_user(db, job.user_id, f"New applicant: {worker.name}", f"{worker.name} applied to your {job.trade} request.", f"/worker/{worker.id}")
    email_user(db, user.id, "Application sent", f"Your application to {job.company} — {job.trade} was submitted.", "/dashboard")
    return RedirectResponse(request.headers.get("referer") or "/dashboard", status_code=303)


@app.post("/job/{job_id}/invite/{worker_id}")
def invite_worker(job_id: int, worker_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    job = db.query(ManpowerRequest).filter(ManpowerRequest.id == job_id, ManpowerRequest.user_id == user.id, ManpowerRequest.status == "open").first()
    worker = db.query(Worker).filter(Worker.id == worker_id, Worker.available.is_(True)).first()
    if not job or not worker or not worker.user_id:
        raise HTTPException(status_code=404, detail="Job or worker not found")
    app_row = application_for(db, job.id, worker.id)
    if not app_row:
        db.add(Application(job_id=job.id, worker_id=worker.id, worker_user_id=worker.user_id, contractor_user_id=user.id, source="invite", status="invited"))
    elif app_row.status in {"rejected", "withdrawn"}:
        app_row.status = "invited"; app_row.source = "invite"; app_row.updated_at = now_iso()
    notify(db, worker.user_id, f"Invitation from {job.company}", f"You were invited to apply for {job.trade}.", "/dashboard")
    notify(db, user.id, "Worker invited", f"Invitation sent to {worker.name} for your {job.trade} request.", f"/worker/{worker.id}")
    db.commit()
    return RedirectResponse(request.headers.get("referer") or "/dashboard", status_code=303)


@app.post("/application/{application_id}/status")
def update_application_status(application_id: int, request: Request, new_status: str = Form(...), db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    allowed = {"reviewing", "shortlisted", "interview", "offered", "hired", "rejected"}
    if new_status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid application status")
    app_row = db.query(Application).filter(Application.id == application_id, Application.contractor_user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="Application not found")
    app_row.status = new_status; app_row.updated_at = now_iso()
    job = job_for_application(db, app_row)
    status_link = f"/hire/{application_id}" if new_status == "hired" else "/dashboard"
    notify(db, app_row.worker_user_id, "Application status updated", f"Your {job.trade if job else 'job'} application is now: {new_status}.", status_link)
    if new_status == "hired":
        notify(db, app_row.contractor_user_id, "Candidate hired", f"The candidate has been marked hired for {job.trade if job else 'the position'}.", f"/hire/{application_id}")
    db.commit()
    email_user(db, app_row.worker_user_id, "Application status updated", f"Your {job.trade if job else 'job'} application is now: {new_status}.", status_link)
    if new_status == "hired":
        email_user(db, app_row.worker_user_id, "You have been hired", f"You have been marked hired for {job.trade if job else 'the position'}. Continue to your onboarding details.", f"/hire/{application_id}")
        email_user(db, app_row.contractor_user_id, "Candidate hired", f"The candidate has been marked hired for {job.trade if job else 'the position'}.", f"/hire/{application_id}")
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/application/{application_id}/withdraw")
def withdraw_application(application_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "worker")
    app_row = db.query(Application).filter(Application.id == application_id, Application.worker_user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="Application not found")
    if app_row.status not in {"hired", "rejected"}:
        app_row.status = "withdrawn"; app_row.updated_at = now_iso(); notify(db, app_row.contractor_user_id, "Candidate withdrew", "A candidate withdrew from your manpower request.", "/dashboard"); db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/job/{job_id}/close")
def close_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    job = db.query(ManpowerRequest).filter(ManpowerRequest.id == job_id, ManpowerRequest.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = "closed"; db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request, trade: str = "", location: str = "", db: Session = Depends(get_db)):
    user = require_user(request, db, "worker")
    worker = db.query(Worker).filter(Worker.user_id == user.id).first()
    query = db.query(ManpowerRequest).filter(ManpowerRequest.status == "open")
    if trade.strip(): query = query.filter(ManpowerRequest.trade.ilike(f"%{trade.strip()}%"))
    if location.strip(): query = query.filter(ManpowerRequest.location.ilike(f"%{location.strip()}%"))
    jobs = query.order_by(ManpowerRequest.id.desc()).limit(100).all()
    apps = db.query(Application).filter(Application.worker_user_id == user.id).all(); app_map = {a.job_id: a for a in apps}
    rows = [{"job": job, "score": match_score(worker, job) if worker else 0, "application": app_map.get(job.id)} for job in jobs]
    rows.sort(key=lambda x: (x["score"], x["job"].id), reverse=True)
    return templates.TemplateResponse("opportunities.html", {"request": request, "user": user, "worker": worker, "rows": rows, "trade_filter": trade, "location_filter": location})


def safe_app_path(value: Optional[str]) -> str:
    """Keep form redirects on this app. Rejects off-site and protocol-relative URLs."""
    if value is None:
        return ""
    candidate = str(value).strip()
    if not candidate or len(candidate) > 500 or not candidate.startswith("/") or candidate.startswith("//"):
        return ""
    if any(ch in candidate for ch in ("\r", "\n", "\x00", "\\")) or ".." in candidate:
        return ""
    lowered = candidate.lower()
    if "javascript:" in lowered or "data:" in lowered or "://" in candidate:
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return ""
    return candidate


def normalize_saved_label(label: str) -> str:
    value = (label or "").strip()
    if value and value not in SAVED_LABELS:
        raise HTTPException(status_code=400, detail="Choose a label from the list.")
    return value


def normalize_saved_note(note: str) -> str:
    return (note or "").strip()[:SAVED_NOTE_LIMIT]


def saved_note_preview(note: Optional[str], limit: int = 160) -> str:
    text_value = (note or "").strip()
    if len(text_value) <= limit:
        return text_value
    return text_value[: limit - 3].rstrip() + "..."


def saved_return_path(label: str, trade: str) -> str:
    params = {}
    if label:
        params["label"] = label
    if trade:
        params["trade"] = trade
    if not params:
        return "/saved"
    return "/saved?" + urlencode(params)


def contractor_open_jobs(db: Session, user_id: int):
    return db.query(ManpowerRequest).filter(ManpowerRequest.user_id == user_id, ManpowerRequest.status == "open").order_by(ManpowerRequest.id.desc()).all()


@app.get("/upgrade", response_class=HTMLResponse)
def upgrade_page(request: Request, feature: str = "saved", db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    key = upgrade_feature(feature)
    if bind_contractor_pro(request, db, user):
        destination = "/messages" if key == "messages" else "/saved"
        return RedirectResponse(destination, status_code=303)
    return render_upgrade(request, user, key)


@app.get("/talent", response_class=HTMLResponse)
def talent_search(request: Request, trade: str = "", location: str = "", min_experience: int = 0, favorites: str = "", db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    entitled = bind_contractor_pro(request, db, user)
    if favorites == "yes" and not entitled:
        return render_upgrade(request, user, "saved")
    favorite_rows = db.query(FavoriteCandidate).filter(FavoriteCandidate.contractor_user_id == user.id).all()
    favorite_ids = {row.worker_id for row in favorite_rows}
    favorite_labels = {}
    if entitled:
        favorite_labels = {row.worker_id: row.label for row in favorite_rows if (row.label or "").strip()}
    if favorites == "yes":
        query = db.query(Worker).filter(Worker.id.in_(favorite_ids or [-1]))
    else:
        query = db.query(Worker).filter(Worker.available.is_(True))
    if trade.strip():
        query = query.filter(Worker.trade.ilike(f"%{trade.strip()}%"))
    if location.strip():
        query = query.filter(Worker.location.ilike(f"%{location.strip()}%"))
    if min_experience > 0:
        query = query.filter(Worker.experience >= min_experience)
    workers = query.order_by(Worker.experience.desc(), Worker.id.desc()).limit(200).all()
    open_jobs = contractor_open_jobs(db, user.id)
    return templates.TemplateResponse("talent.html", {"request": request, "user": user, "workers": workers, "favorite_ids": favorite_ids, "favorite_labels": favorite_labels, "jobs": open_jobs, "trade_filter": trade, "location_filter": location, "min_experience": min_experience, "favorites_filter": favorites})


@app.get("/saved", response_class=HTMLResponse)
def saved_candidates(request: Request, label: str = "", trade: str = "", db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    if not bind_contractor_pro(request, db, user):
        return render_upgrade(request, user, "saved")
    label_filter = (label or "").strip()
    trade_filter = (trade or "").strip()
    favorites = db.query(FavoriteCandidate).filter(FavoriteCandidate.contractor_user_id == user.id).order_by(FavoriteCandidate.created_at.desc(), FavoriteCandidate.id.desc()).all()
    workers_by_id = {w.id: w for w in db.query(Worker).filter(Worker.id.in_([row.worker_id for row in favorites] or [-1])).all()}
    needle = trade_filter.lower()
    rows = []
    for favorite in favorites:
        worker = workers_by_id.get(favorite.worker_id)
        if not worker:
            continue
        if label_filter and (favorite.label or "") != label_filter:
            continue
        if needle and needle not in (worker.trade or "").lower():
            continue
        relationship = can_message(db, user.id, worker.user_id) if worker.user_id else None
        rows.append({"worker": worker, "label": favorite.label or "", "note_preview": saved_note_preview(favorite.note), "can_message": relationship is not None})
    return templates.TemplateResponse("saved.html", {"request": request, "user": user, "rows": rows, "jobs": contractor_open_jobs(db, user.id), "label_filter": label_filter, "trade_filter": trade_filter, "has_saved": bool(favorites), "return_path": saved_return_path(label_filter, trade_filter)})


@app.post("/saved/{worker_id}")
def update_saved_candidate(worker_id: int, request: Request, note: str = Form(""), label: str = Form(""), return_to: str = Form(""), db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    if not contractor_pro_entitled(db, user):
        return pro_upgrade_redirect("saved")
    favorite = db.query(FavoriteCandidate).filter(FavoriteCandidate.contractor_user_id == user.id, FavoriteCandidate.worker_id == worker_id).first()
    if not favorite:
        raise HTTPException(status_code=404, detail="Saved candidate not found")
    clean_label = normalize_saved_label(label)
    clean_note = normalize_saved_note(note)
    favorite.label = clean_label
    favorite.note = clean_note
    db.commit()
    destination = safe_app_path(return_to) or f"/worker/{worker_id}"
    return RedirectResponse(destination, status_code=303)


@app.post("/favorite/{worker_id}")
def toggle_favorite(worker_id: int, request: Request, return_to: str = Form(""), db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    favorite = db.query(FavoriteCandidate).filter(FavoriteCandidate.contractor_user_id == user.id, FavoriteCandidate.worker_id == worker_id).first()
    if favorite:
        db.delete(favorite)
    elif not contractor_pro_entitled(db, user):
        return pro_upgrade_redirect("saved")
    else:
        db.add(FavoriteCandidate(contractor_user_id=user.id, worker_id=worker_id))
    db.commit()
    destination = safe_app_path(return_to) or request.headers.get("referer") or "/talent"
    return RedirectResponse(destination, status_code=303)


@app.get("/company", response_class=HTMLResponse)
def company_profile_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    profile = db.query(CompanyProfile).filter(CompanyProfile.user_id == user.id).first()
    return templates.TemplateResponse("company_profile.html", {"request": request, "user": user, "profile": profile})


@app.post("/company")
def save_company_profile(request: Request, company_name: str = Form(...), contact_name: str = Form(""), phone: str = Form(""), location: str = Form(""), website: str = Form(""), bio: str = Form(""), db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    profile = db.query(CompanyProfile).filter(CompanyProfile.user_id == user.id).first()
    if not profile:
        profile = CompanyProfile(user_id=user.id); db.add(profile)
    profile.company_name = company_name.strip(); profile.contact_name = contact_name.strip(); profile.phone = phone.strip(); profile.location = location.strip(); profile.website = website.strip(); profile.bio = bio.strip(); profile.updated_at = now_iso(); db.commit()
    return RedirectResponse("/company", status_code=303)


@app.get("/worker/{worker_id}", response_class=HTMLResponse)
def worker_profile_page(worker_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    worker = db.query(Worker).filter(Worker.id == worker_id).first()
    if not worker: raise HTTPException(status_code=404, detail="Worker not found")
    entitled = bind_contractor_pro(request, db, user)
    favorite = db.query(FavoriteCandidate).filter(FavoriteCandidate.contractor_user_id == user.id, FavoriteCandidate.worker_id == worker_id).first()
    relationship = can_message(db, user.id, worker.user_id) if worker.user_id else None
    return templates.TemplateResponse("worker_profile.html", {"request": request, "user": user, "worker": worker, "favorite": bool(favorite), "saved": favorite if entitled else None, "relationship": relationship})


@app.get("/contractor/worker/{worker_id}/document/{kind}")
def contractor_document(worker_id: int, kind: str, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    relationship = db.query(Application).filter(Application.contractor_user_id == user.id, Application.worker_id == worker_id).first()
    if not relationship:
        raise HTTPException(status_code=403, detail="A recruiting relationship is required to view documents.")
    return document_response(db.query(Worker).filter(Worker.id == worker_id).first(), kind)


@app.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request, with_user: int = 0, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if user.role == "contractor" and not bind_contractor_pro(request, db, user):
        return render_upgrade(request, user, "messages")
    messages = db.query(Message).filter(or_(Message.sender_user_id == user.id, Message.recipient_user_id == user.id)).order_by(Message.id.desc()).limit(200).all()
    partner_ids = []; seen = set()
    for m in messages:
        partner_id = m.recipient_user_id if m.sender_user_id == user.id else m.sender_user_id
        if partner_id not in seen: seen.add(partner_id); partner_ids.append(partner_id)
    relationship_apps = db.query(Application).filter(or_(Application.worker_user_id == user.id, Application.contractor_user_id == user.id)).order_by(Application.id.desc()).all()
    for a in relationship_apps:
        partner_id = a.contractor_user_id if a.worker_user_id == user.id else a.worker_user_id
        if partner_id not in seen: seen.add(partner_id); partner_ids.append(partner_id)
    accounts = {a.id: a for a in db.query(UserAccount).filter(UserAccount.id.in_(partner_ids or [-1])).all()}
    workers_by_user = {w.user_id: w for w in db.query(Worker).filter(Worker.user_id.in_(partner_ids or [-1])).all()}
    companies_by_user = {c.user_id: c for c in db.query(CompanyProfile).filter(CompanyProfile.user_id.in_(partner_ids or [-1])).all()}
    partners = []
    for pid in partner_ids:
        account = accounts.get(pid); display = account.email if account else f"User #{pid}"
        if pid in workers_by_user: display = workers_by_user[pid].name
        elif pid in companies_by_user and companies_by_user[pid].company_name: display = companies_by_user[pid].company_name
        partners.append({"id": pid, "display": display})
    active_id = with_user if with_user in partner_ids else (partner_ids[0] if partner_ids else 0)
    thread = []; active_display = ""
    if active_id:
        thread = [m for m in reversed(messages) if {m.sender_user_id, m.recipient_user_id} == {user.id, active_id}]
        db.query(Message).filter(Message.sender_user_id == active_id, Message.recipient_user_id == user.id, Message.is_read.is_(False)).update({Message.is_read: True}); db.commit()
        active_display = next((p["display"] for p in partners if p["id"] == active_id), "")
    return templates.TemplateResponse("messages.html", {"request": request, "user": user, "partners": partners, "active_id": active_id, "active_display": active_display, "thread": thread})


@app.post("/messages/send/{recipient_id}")
def send_message(recipient_id: int, request: Request, body: str = Form(...), db: Session = Depends(get_db)):
    user = require_user(request, db)
    if user.role == "contractor" and not contractor_pro_entitled(db, user):
        return pro_upgrade_redirect("messages")
    recipient = db.query(UserAccount).filter(UserAccount.id == recipient_id).first()
    if not recipient or recipient.id == user.id: raise HTTPException(status_code=404, detail="Recipient not found")
    relationship = can_message(db, user.id, recipient_id)
    if not relationship: raise HTTPException(status_code=403, detail="Messaging opens after an application or invitation exists.")
    clean = body.strip()
    if not clean: return RedirectResponse(f"/messages?with_user={recipient_id}", status_code=303)
    if len(clean) > 4000: raise HTTPException(status_code=413, detail="Message must be 4,000 characters or fewer.")
    db.add(Message(sender_user_id=user.id, recipient_user_id=recipient_id, application_id=relationship.id, body=clean))
    notify(db, recipient_id, "New message", "You have a new message in TradeForce AI.", f"/messages?with_user={user.id}")
    db.commit()
    # Phase 13 email alert. Email failure must never block in-app messaging.
    try:
        from account_security import _send_email
        app_url = (os.getenv("APP_BASE_URL") or "https://tradeforce-ai.com").rstrip("/")
        sender_label = user.email
        sender_worker = db.query(Worker).filter(Worker.user_id == user.id).first()
        sender_company = db.query(CompanyProfile).filter(CompanyProfile.user_id == user.id).first()
        if sender_worker and sender_worker.name:
            sender_label = sender_worker.name
        elif sender_company and sender_company.company_name:
            sender_label = sender_company.company_name
        safe_sender = html.escape(sender_label)
        safe_preview = html.escape(clean[:240])
        conversation_url = app_url + "/messages?with_user=" + str(user.id)
        _send_email(recipient.email, f"New TradeForce AI message from {sender_label}", f"<h2>You have a new TradeForce AI message</h2><p><strong>{safe_sender}</strong> sent you:</p><p>{safe_preview}</p><p><a href=\"{html.escape(conversation_url, quote=True)}\">Open the conversation</a></p>")
    except Exception:
        pass
    return RedirectResponse(f"/messages?with_user={recipient_id}", status_code=303)


@app.get("/documents/{kind}")
def own_document(kind: str, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "worker")
    return document_response(db.query(Worker).filter(Worker.user_id == user.id).first(), kind)


@app.get("/admin/worker/{worker_id}/document/{kind}")
def admin_document(worker_id: int, kind: str, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    return document_response(db.query(Worker).filter(Worker.id == worker_id).first(), kind)


def document_response(worker: Optional[Worker], kind: str):
    if not worker or kind not in {"resume", "cert"}: raise HTTPException(status_code=404, detail="Document not found")
    key = getattr(worker, f"{kind}_key", "") or ""; data = load_document(key) if key else getattr(worker, f"{kind}_data")
    if not data: raise HTTPException(status_code=404, detail="Document not found")
    filename = getattr(worker, f"{kind}_name") or f"{kind}.bin"; content_type = getattr(worker, f"{kind}_type") or "application/octet-stream"; safe_filename = filename.replace('"', "")
    return Response(content=data, media_type=content_type, headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'})


@app.get("/notifications/{notification_id}/open")
def open_notification(notification_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    item = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Notification not found")
    item.is_read = True
    destination = safe_notification_link(item.link or "")
    if not destination or urlparse(destination).path == request.url.path:
        destination = notification_return_path(request)
    db.commit()
    response = RedirectResponse(destination, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/notifications/read")
def mark_notifications_read(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db); db.query(Notification).filter(Notification.user_id == user.id).update({Notification.is_read: True}); db.commit(); return RedirectResponse("/dashboard", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    workers = db.query(Worker).order_by(Worker.id.desc()).all(); jobs = db.query(ManpowerRequest).order_by(ManpowerRequest.id.desc()).all(); matches = {job.id: ranked_matches(workers, job) for job in jobs}; applications = db.query(Application).order_by(Application.id.desc()).limit(100).all()
    stats = {"workers": len(workers), "available": sum(1 for w in workers if w.available), "open_jobs": sum(1 for j in jobs if j.status == "open"), "requests": len(jobs), "accounts": db.query(UserAccount).count(), "applications": len(applications), "hires": sum(1 for a in applications if a.status == "hired"), "messages": db.query(Message).count(), "favorites": db.query(FavoriteCandidate).count(), "companies": db.query(CompanyProfile).count()}
    return templates.TemplateResponse("admin.html", {"request": request, "workers": workers, "requests": jobs, "matches": matches, "stats": stats, "applications": applications, "storage_mode": "object-storage" if storage_enabled() else "database-fallback"})


@app.post("/admin/job/{job_id}/status")
def admin_job_status(job_id: int, new_status: str = Form(...), _: str = Depends(require_admin), db: Session = Depends(get_db)):
    if new_status not in {"open", "closed", "paused"}: raise HTTPException(status_code=400, detail="Invalid job status")
    job = db.query(ManpowerRequest).filter(ManpowerRequest.id == job_id).first()
    if not job: raise HTTPException(status_code=404, detail="Job not found")
    job.status = new_status; db.commit(); return RedirectResponse("/admin", status_code=303)


# app.py turns these on when the production middleware stack is installed.
# Importing main.py alone leaves them disabled so /health does not claim protections that are not running.
CSRF_PROTECTION = "disabled"
RATE_LIMIT = "disabled"


@app.get("/health")
def health():
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        database = "postgresql" if engine.dialect.name == "postgresql" else "sqlite"
        features = ["messaging", "notifications", "talent-search", "favorites", "company-profiles", "opportunity-search", "saved-workspace", "pro-gating"]
        if CSRF_PROTECTION == "enabled":
            features.append("csrf")
        if RATE_LIMIT != "disabled":
            features.append("rate-limit")
        return {
            "status": "ok",
            "service": "TradeForce AI",
            "database": database,
            "phase": 16,
            "document_storage": "object-storage" if storage_enabled() else "database-fallback",
            "csrf": CSRF_PROTECTION,
            "rate_limit": RATE_LIMIT,
            "features": features,
        }
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")
