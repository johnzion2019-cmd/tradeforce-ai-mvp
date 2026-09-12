"""Phase 6: hire details, onboarding checklist, and active hires dashboard."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Text

import main

router = APIRouter()


class HireRecord(main.Base):
    __tablename__ = "hire_records"
    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False, unique=True, index=True)
    job_id = Column(Integer, ForeignKey("requests.id"), nullable=False, index=True)
    worker_id = Column(Integer, ForeignKey("workers.id"), nullable=False, index=True)
    worker_user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    contractor_user_id = Column(Integer, ForeignKey("account_users.id"), nullable=False, index=True)
    agreed_pay = Column(String(120), default="")
    start_date = Column(String(40), default="")
    job_location = Column(String(220), default="")
    shift = Column(String(120), default="")
    duration = Column(String(120), default="")
    notes = Column(Text, default="")
    worker_confirmed = Column(Boolean, default=False)
    documents_verified = Column(Boolean, default=False)
    orientation_complete = Column(Boolean, default=False)
    cleared_to_start = Column(Boolean, default=False)
    created_at = Column(String(40), default=main.now_iso)
    updated_at = Column(String(40), default=main.now_iso)


def init_phase6():
    main.Base.metadata.create_all(bind=main.engine)


def _application(db, application_id: int):
    return db.query(main.Application).filter(main.Application.id == application_id).first()


def _hire(db, application_id: int):
    return db.query(HireRecord).filter(HireRecord.application_id == application_id).first()


def _allowed_relationship(user, app_row):
    return app_row and user.id in {app_row.worker_user_id, app_row.contractor_user_id}


def _onboarding_progress(worker, hire):
    checks = [
        bool(worker and worker.resume_name),
        bool(worker and ((worker.cert_name or "").strip() or (worker.certifications or "").strip())),
        bool(hire and hire.worker_confirmed),
        bool(hire and hire.documents_verified),
        bool(hire and hire.orientation_complete),
        bool(hire and hire.cleared_to_start),
    ]
    return int(round(sum(checks) / len(checks) * 100))


def hire_for_application(application_id: int):
    db = main.SessionLocal()
    try:
        return _hire(db, application_id)
    finally:
        db.close()


def onboarding_progress(application_id: int):
    db = main.SessionLocal()
    try:
        app_row = _application(db, application_id)
        if not app_row:
            return 0
        worker = db.query(main.Worker).filter(main.Worker.id == app_row.worker_id).first()
        return _onboarding_progress(worker, _hire(db, application_id))
    finally:
        db.close()


@router.get("/hires", response_class=HTMLResponse)
def hires_page(request: Request, db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db)
    query = db.query(main.Application).filter(main.Application.status.in_(["offer_accepted", "hired"]))
    if user.role == "contractor":
        apps = query.filter(main.Application.contractor_user_id == user.id).order_by(main.Application.id.desc()).all()
    else:
        apps = query.filter(main.Application.worker_user_id == user.id).order_by(main.Application.id.desc()).all()
    rows = []
    for a in apps:
        job = db.query(main.ManpowerRequest).filter(main.ManpowerRequest.id == a.job_id).first()
        worker = db.query(main.Worker).filter(main.Worker.id == a.worker_id).first()
        hire = _hire(db, a.id)
        rows.append({"application": a, "job": job, "worker": worker, "hire": hire, "progress": _onboarding_progress(worker, hire)})
    return main.templates.TemplateResponse("hires.html", {"request": request, "user": user, "rows": rows})


@router.get("/hire/{application_id}", response_class=HTMLResponse)
def hire_detail_page(application_id: int, request: Request, db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db)
    app_row = _application(db, application_id)
    if not _allowed_relationship(user, app_row):
        raise HTTPException(status_code=404, detail="Hire not found")
    if app_row.status not in {"offer_accepted", "hired"}:
        raise HTTPException(status_code=400, detail="Hire details open after an offer is accepted.")
    job = db.query(main.ManpowerRequest).filter(main.ManpowerRequest.id == app_row.job_id).first()
    worker = db.query(main.Worker).filter(main.Worker.id == app_row.worker_id).first()
    hire = _hire(db, application_id)
    return main.templates.TemplateResponse("hire_detail.html", {"request": request, "user": user, "application": app_row, "job": job, "worker": worker, "hire": hire, "progress": _onboarding_progress(worker, hire)})


@router.post("/hire/{application_id}/details")
def save_hire_details(
    application_id: int,
    request: Request,
    agreed_pay: str = Form(""),
    start_date: str = Form(""),
    job_location: str = Form(""),
    shift: str = Form(""),
    duration: str = Form(""),
    notes: str = Form(""),
    db: main.Session = Depends(main.get_db),
):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.contractor_user_id == user.id).first()
    if not app_row or app_row.status not in {"offer_accepted", "hired"}:
        raise HTTPException(status_code=404, detail="Accepted hire not found")
    job = db.query(main.ManpowerRequest).filter(main.ManpowerRequest.id == app_row.job_id).first()
    hire = _hire(db, application_id)
    if not hire:
        hire = HireRecord(application_id=app_row.id, job_id=app_row.job_id, worker_id=app_row.worker_id, worker_user_id=app_row.worker_user_id, contractor_user_id=app_row.contractor_user_id)
        db.add(hire)
    hire.agreed_pay = agreed_pay.strip()[:120]
    hire.start_date = start_date.strip()[:40]
    hire.job_location = job_location.strip()[:220]
    hire.shift = shift.strip()[:120]
    hire.duration = duration.strip()[:120]
    hire.notes = notes.strip()[:4000]
    hire.updated_at = main.now_iso()
    main.notify(db, app_row.worker_user_id, "Hire details updated", f"{job.company if job else 'Your contractor'} updated your hire details. Please review and confirm them.")
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/confirm")
def confirm_hire_details(application_id: int, request: Request, db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db, "worker")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.worker_user_id == user.id).first()
    hire = _hire(db, application_id)
    if not app_row or not hire or app_row.status not in {"offer_accepted", "hired"}:
        raise HTTPException(status_code=404, detail="Hire details not found")
    hire.worker_confirmed = True
    hire.updated_at = main.now_iso()
    worker = db.query(main.Worker).filter(main.Worker.id == app_row.worker_id).first()
    main.notify(db, app_row.contractor_user_id, "Hire details confirmed", f"{worker.name if worker else 'Worker'} confirmed the start/pay/location details.")
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/onboarding")
def update_onboarding(
    application_id: int,
    request: Request,
    documents_verified: str = Form("no"),
    orientation_complete: str = Form("no"),
    cleared_to_start: str = Form("no"),
    db: main.Session = Depends(main.get_db),
):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.contractor_user_id == user.id).first()
    if not app_row or app_row.status not in {"offer_accepted", "hired"}:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = _hire(db, application_id)
    if not hire:
        hire = HireRecord(application_id=app_row.id, job_id=app_row.job_id, worker_id=app_row.worker_id, worker_user_id=app_row.worker_user_id, contractor_user_id=app_row.contractor_user_id)
        db.add(hire)
    hire.documents_verified = documents_verified == "yes"
    hire.orientation_complete = orientation_complete == "yes"
    hire.cleared_to_start = cleared_to_start == "yes"
    hire.updated_at = main.now_iso()
    if hire.cleared_to_start:
        main.notify(db, app_row.worker_user_id, "Cleared to start", "Your contractor marked your onboarding as cleared to start.")
    else:
        main.notify(db, app_row.worker_user_id, "Onboarding updated", "Your contractor updated your onboarding checklist.")
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)
