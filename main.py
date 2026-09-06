import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, Column, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

app = FastAPI(title="TradeForce AI")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()

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


class Worker(Base):
    __tablename__ = "workers"
    id = Column(Integer, primary_key=True, index=True)
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


class ManpowerRequest(Base):
    __tablename__ = "requests"
    id = Column(Integer, primary_key=True, index=True)
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


Base.metadata.create_all(bind=engine)


def migrate_existing_tables():
    inspector = inspect(engine)
    dialect = engine.dialect.name
    migrations = {
        "workers": {
            "desired_pay": "TEXT DEFAULT ''",
            "willing_to_travel": "BOOLEAN DEFAULT TRUE" if dialect != "sqlite" else "INTEGER DEFAULT 1",
        },
        "requests": {
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


def normalize(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def match_score(worker: Worker, job: ManpowerRequest) -> int:
    score = 0
    worker_trade, job_trade = normalize(worker.trade), normalize(job.trade)
    if worker_trade == job_trade:
        score += 55
    elif worker_trade in job_trade or job_trade in worker_trade:
        score += 35
    wloc, jloc = normalize(worker.location), normalize(job.location)
    if wloc and jloc:
        if wloc == jloc:
            score += 20
        elif wloc in jloc or jloc in wloc:
            score += 12
    if worker.willing_to_travel:
        score += 8
    if worker.available:
        score += 7
    required, experience = job.required_experience or 0, worker.experience or 0
    if experience >= required:
        score += min(10, max(2, experience // 2))
    else:
        score -= min(15, (required - experience) * 3)
    return max(0, min(100, score))


def ranked_matches(workers, job, limit=5):
    ranked = [{"worker": w, "score": match_score(w, job)} for w in workers if w.available and normalize(w.trade) and normalize(job.trade)]
    ranked.sort(key=lambda x: (x["score"], x["worker"].experience or 0), reverse=True)
    return [m for m in ranked if m["score"] >= 35][:limit]


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/worker", response_class=HTMLResponse)
def worker_form(request: Request):
    return templates.TemplateResponse("worker.html", {"request": request})


@app.post("/worker")
def create_worker(name: str = Form(...), trade: str = Form(...), location: str = Form(""), experience: int = Form(0), certifications: str = Form(""), phone: str = Form(""), email: str = Form(""), desired_pay: str = Form(""), willing_to_travel: Optional[str] = Form(None), db: Session = Depends(get_db)):
    db.add(Worker(name=name.strip(), trade=trade.strip(), location=location.strip(), experience=max(0, experience), certifications=certifications.strip(), phone=phone.strip(), email=email.strip(), desired_pay=desired_pay.strip(), willing_to_travel=willing_to_travel == "yes", available=True))
    db.commit()
    return RedirectResponse("/thanks?type=worker", status_code=303)


@app.get("/contractor", response_class=HTMLResponse)
def contractor_form(request: Request):
    return templates.TemplateResponse("contractor.html", {"request": request})


@app.post("/contractor")
def create_request(company: str = Form(...), contact: str = Form(""), trade: str = Form(...), workers_needed: int = Form(1), location: str = Form(""), start_date: str = Form(""), duration: str = Form(""), pay_range: str = Form(""), notes: str = Form(""), required_experience: int = Form(0), db: Session = Depends(get_db)):
    db.add(ManpowerRequest(company=company.strip(), contact=contact.strip(), trade=trade.strip(), workers_needed=max(1, workers_needed), location=location.strip(), start_date=start_date.strip(), duration=duration.strip(), pay_range=pay_range.strip(), notes=notes.strip(), required_experience=max(0, required_experience), status="open"))
    db.commit()
    return RedirectResponse("/thanks?type=contractor", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    workers = db.query(Worker).order_by(Worker.id.desc()).all()
    jobs = db.query(ManpowerRequest).order_by(ManpowerRequest.id.desc()).all()
    matches = {job.id: ranked_matches(workers, job) for job in jobs}
    stats = {"workers": len(workers), "available": sum(1 for w in workers if w.available), "open_jobs": sum(1 for j in jobs if j.status == "open"), "requests": len(jobs)}
    return templates.TemplateResponse("admin.html", {"request": request, "workers": workers, "requests": jobs, "matches": matches, "stats": stats})


@app.get("/thanks", response_class=HTMLResponse)
def thanks(request: Request, type: str = "worker"):
    return templates.TemplateResponse("thanks.html", {"request": request, "type": type})


@app.get("/health")
def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = "postgresql" if engine.dialect.name == "postgresql" else "sqlite"
        return {"status": "ok", "service": "TradeForce AI", "database": database}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")
