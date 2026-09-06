import hashlib
import os
import secrets
import uuid
from datetime import datetime
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, Column, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

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


class UserAccount(Base):
    __tablename__ = "account_users"
    id = Column(Integer, primary_key=True)
    email = Column(String(220), nullable=False, unique=True, index=True)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(30), nullable=False, index=True)
    created_at = Column(String(40), default=lambda: datetime.utcnow().isoformat())


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
    is_read = Column(Boolean, default=False)
    created_at = Column(String(40), default=lambda: datetime.utcnow().isoformat())


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
    created_at = Column(String(40), default=lambda: datetime.utcnow().isoformat())
    updated_at = Column(String(40), default=lambda: datetime.utcnow().isoformat())


Base.metadata.create_all(bind=engine)


def migrate_existing_tables():
    inspector = inspect(engine)
    dialect = engine.dialect.name
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
    }
    with engine.begin() as conn:
        for table_name, columns in migrations.items():
            if table_name not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table_name)}
            for column_name, ddl in columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))


migrate_existing_tables()


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


def notify(db: Session, user_id: int, title: str, message: str):
    db.add(Notification(user_id=user_id, title=title, message=message))


def notification_list(db: Session, user_id: int):
    return db.query(Notification).filter(Notification.user_id == user_id).order_by(Notification.id.desc()).limit(12).all()


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
    return templates.TemplateResponse("contractor_dashboard.html", {"request": request, "user": user, "jobs": jobs, "matches": matches, "notifications": notifications, "applications_by_job": applications_by_job})


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
        notify(db, user.id, "New job matches ready", f"Your updated profile has {len(strong)} strong job match(es).")
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
            notify(db, match["worker"].user_id, f"New {job.trade} opportunity", f"{job.company} posted a {job.trade} job in {job.location or 'a new location'} with a {match['score']}% match to your profile.")
    notify(db, user.id, "Manpower request posted", f"Your {job.trade} request is live with {len(top)} ranked candidate match(es).")
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
            app_row.status = "applied"; app_row.source = "invite_accepted"; app_row.message = message.strip(); app_row.updated_at = datetime.utcnow().isoformat()
        else:
            return RedirectResponse("/dashboard", status_code=303)
    else:
        db.add(Application(job_id=job.id, worker_id=worker.id, worker_user_id=user.id, contractor_user_id=job.user_id, source="application", status="applied", message=message.strip()))
    notify(db, job.user_id, f"New applicant: {worker.name}", f"{worker.name} applied to your {job.trade} request in {job.location or 'your project location'}.")
    notify(db, user.id, "Application sent", f"Your application to {job.company} — {job.trade} was submitted.")
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


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
        app_row.status = "invited"; app_row.source = "invite"; app_row.updated_at = datetime.utcnow().isoformat()
    notify(db, worker.user_id, f"Invitation from {job.company}", f"You were invited to apply for {job.trade} in {job.location or 'their project location'}.")
    notify(db, user.id, "Worker invited", f"Invitation sent to {worker.name} for your {job.trade} request.")
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/application/{application_id}/status")
def update_application_status(application_id: int, request: Request, new_status: str = Form(...), db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    allowed = {"reviewing", "shortlisted", "interview", "offered", "hired", "rejected"}
    if new_status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid application status")
    app_row = db.query(Application).filter(Application.id == application_id, Application.contractor_user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="Application not found")
    app_row.status = new_status; app_row.updated_at = datetime.utcnow().isoformat()
    job = db.query(ManpowerRequest).filter(ManpowerRequest.id == app_row.job_id).first()
    notify(db, app_row.worker_user_id, "Application status updated", f"Your {job.trade if job else 'job'} application is now: {new_status}.")
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/application/{application_id}/withdraw")
def withdraw_application(application_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "worker")
    app_row = db.query(Application).filter(Application.id == application_id, Application.worker_user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="Application not found")
    if app_row.status not in {"hired", "rejected"}:
        app_row.status = "withdrawn"; app_row.updated_at = datetime.utcnow().isoformat(); notify(db, app_row.contractor_user_id, "Candidate withdrew", "A candidate withdrew from your manpower request."); db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/job/{job_id}/close")
def close_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "contractor")
    job = db.query(ManpowerRequest).filter(ManpowerRequest.id == job_id, ManpowerRequest.user_id == user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = "closed"; db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/documents/{kind}")
def own_document(kind: str, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db, "worker")
    return document_response(db.query(Worker).filter(Worker.user_id == user.id).first(), kind)


@app.get("/admin/worker/{worker_id}/document/{kind}")
def admin_document(worker_id: int, kind: str, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    return document_response(db.query(Worker).filter(Worker.id == worker_id).first(), kind)


def document_response(worker: Optional[Worker], kind: str):
    if not worker or kind not in {"resume", "cert"}:
        raise HTTPException(status_code=404, detail="Document not found")
    key = getattr(worker, f"{kind}_key", "") or ""
    data = load_document(key) if key else getattr(worker, f"{kind}_data")
    if not data:
        raise HTTPException(status_code=404, detail="Document not found")
    filename = getattr(worker, f"{kind}_name") or f"{kind}.bin"
    content_type = getattr(worker, f"{kind}_type") or "application/octet-stream"
    return Response(content=data, media_type=content_type, headers={"Content-Disposition": f'attachment; filename="{filename.replace(chr(34), "")}"'})


@app.post("/notifications/read")
def mark_notifications_read(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    db.query(Notification).filter(Notification.user_id == user.id).update({Notification.is_read: True}); db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    workers = db.query(Worker).order_by(Worker.id.desc()).all()
    jobs = db.query(ManpowerRequest).order_by(ManpowerRequest.id.desc()).all()
    matches = {job.id: ranked_matches(workers, job) for job in jobs}
    applications = db.query(Application).order_by(Application.id.desc()).limit(100).all()
    stats = {"workers": len(workers), "available": sum(1 for w in workers if w.available), "open_jobs": sum(1 for j in jobs if j.status == "open"), "requests": len(jobs), "accounts": db.query(UserAccount).count(), "applications": len(applications), "hires": sum(1 for a in applications if a.status == "hired")}
    return templates.TemplateResponse("admin.html", {"request": request, "workers": workers, "requests": jobs, "matches": matches, "stats": stats, "applications": applications, "storage_mode": "object-storage" if storage_enabled() else "database-fallback"})


@app.post("/admin/job/{job_id}/status")
def admin_job_status(job_id: int, new_status: str = Form(...), _: str = Depends(require_admin), db: Session = Depends(get_db)):
    if new_status not in {"open", "closed", "paused"}:
        raise HTTPException(status_code=400, detail="Invalid job status")
    job = db.query(ManpowerRequest).filter(ManpowerRequest.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.status = new_status; db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = "postgresql" if engine.dialect.name == "postgresql" else "sqlite"
        return {"status": "ok", "service": "TradeForce AI", "database": database, "phase": 3, "document_storage": "object-storage" if storage_enabled() else "database-fallback"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")
