"""Phase 6: hire details, onboarding checklist, and post-hire assignment lifecycle."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Text, inspect, text

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
    assignment_status = Column(String(40), default="ready_to_start")
    actual_start_date = Column(String(40), default="")
    completed_date = Column(String(40), default="")
    supervisor = Column(String(160), default="")
    site_contact = Column(String(160), default="")
    expected_end_date = Column(String(40), default="")
    assignment_notes = Column(Text, default="")
    hours_worked = Column(String(40), default="")
    worker_hourly_rate = Column(String(40), default="")
    overtime_multiplier = Column(String(40), default="1.5")
    contractor_bill_rate = Column(String(40), default="")
    created_at = Column(String(40), default=main.now_iso)
    updated_at = Column(String(40), default=main.now_iso)


class PayrollInvoiceRecord(main.Base):
    __tablename__ = "payroll_invoice_records"
    id = Column(Integer, primary_key=True)
    hire_id = Column(Integer, ForeignKey("hire_records.id"), nullable=False, index=True)
    regular_hours = Column(String(40), default="0")
    overtime_hours = Column(String(40), default="0")
    worker_gross_pay = Column(String(40), default="0")
    contractor_billing = Column(String(40), default="0")
    payroll_status = Column(String(40), default="pending")
    invoice_status = Column(String(40), default="pending")
    created_at = Column(String(40), default=main.now_iso)
    updated_at = Column(String(40), default=main.now_iso)


class Timesheet(main.Base):
    __tablename__ = "timesheets"
    id = Column(Integer, primary_key=True)
    hire_id = Column(Integer, ForeignKey("hire_records.id"), nullable=False, index=True)
    week_start = Column(String(40), nullable=False)
    regular_hours = Column(String(40), default="0")
    overtime_hours = Column(String(40), default="0")
    notes = Column(Text, default="")
    status = Column(String(40), default="submitted")
    contractor_notes = Column(Text, default="")
    payroll_invoice_id = Column(Integer, ForeignKey("payroll_invoice_records.id"), nullable=True, index=True)
    created_at = Column(String(40), default=main.now_iso)
    updated_at = Column(String(40), default=main.now_iso)


def init_phase6():
    main.Base.metadata.create_all(bind=main.engine)
    # create_all does not add new columns to an existing table, so keep this
    # small migration safe for both SQLite and Postgres deployments.
    existing = {c["name"] for c in inspect(main.engine).get_columns("hire_records")}
    timesheet_existing = {c["name"] for c in inspect(main.engine).get_columns("timesheets")}
    additions = {
        "assignment_status": "VARCHAR(40) DEFAULT 'ready_to_start'",
        "actual_start_date": "VARCHAR(40) DEFAULT ''",
        "completed_date": "VARCHAR(40) DEFAULT ''",
        "supervisor": "VARCHAR(160) DEFAULT ''",
        "site_contact": "VARCHAR(160) DEFAULT ''",
        "expected_end_date": "VARCHAR(40) DEFAULT ''",
        "assignment_notes": "TEXT DEFAULT ''",
        "hours_worked": "VARCHAR(40) DEFAULT ''",
        "worker_hourly_rate": "VARCHAR(40) DEFAULT ''",
        "overtime_multiplier": "VARCHAR(40) DEFAULT '1.5'",
        "contractor_bill_rate": "VARCHAR(40) DEFAULT ''",
    }
    with main.engine.begin() as conn:
        for name, sql_type in additions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE hire_records ADD COLUMN {name} {sql_type}"))
        conn.execute(text("UPDATE hire_records SET assignment_status=\'ready_to_start\' WHERE assignment_status IS NULL OR assignment_status=\'\'"))
        if "contractor_notes" not in timesheet_existing:
            conn.execute(text("ALTER TABLE timesheets ADD COLUMN contractor_notes TEXT DEFAULT ''"))
        if "payroll_invoice_id" not in timesheet_existing:
            conn.execute(text("ALTER TABLE timesheets ADD COLUMN payroll_invoice_id INTEGER"))


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


def _details_complete(hire):
    if not hire:
        return False
    return all((value or "").strip() for value in [hire.agreed_pay, hire.start_date, hire.job_location, hire.shift, hire.duration])


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
        rows.append({
            "application": a,
            "job": job,
            "worker": worker,
            "hire": hire,
            "progress": _onboarding_progress(worker, hire),
        })
    return main.templates.TemplateResponse("hires.html", {"request": request, "user": user, "rows": rows})


@router.get("/payroll-history", response_class=HTMLResponse)
def payroll_history_page(request: Request, db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db, "contractor")
    records = (
        db.query(PayrollInvoiceRecord, HireRecord, main.Worker, main.ManpowerRequest)
        .join(HireRecord, PayrollInvoiceRecord.hire_id == HireRecord.id)
        .join(main.Worker, HireRecord.worker_id == main.Worker.id)
        .join(main.ManpowerRequest, HireRecord.job_id == main.ManpowerRequest.id)
        .filter(HireRecord.contractor_user_id == user.id)
        .order_by(PayrollInvoiceRecord.id.desc())
        .all()
    )
    rows = []
    for record, hire, worker, job in records:
        rows.append({"record": record, "hire": hire, "worker": worker, "job": job})
    total_billing = sum(float(r.contractor_billing or 0) for r, _, _, _ in records)
    paid_billing = sum(float(r.contractor_billing or 0) for r, _, _, _ in records if r.invoice_status == "paid")
    outstanding_billing = total_billing - paid_billing
    total_payroll = sum(float(r.worker_gross_pay or 0) for r, _, _, _ in records)
    paid_payroll = sum(float(r.worker_gross_pay or 0) for r, _, _, _ in records if r.payroll_status == "paid")
    return main.templates.TemplateResponse("payroll_history.html", {
        "request": request, "user": user, "rows": rows,
        "total_billing": total_billing, "paid_billing": paid_billing,
        "outstanding_billing": outstanding_billing, "total_payroll": total_payroll,
        "paid_payroll": paid_payroll, "outstanding_payroll": total_payroll - paid_payroll,
    })


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
    return main.templates.TemplateResponse("hire_detail.html", {
        "request": request,
        "user": user,
        "application": app_row,
        "job": job,
        "worker": worker,
        "hire": hire,
        "progress": _onboarding_progress(worker, hire),
        "details_complete": _details_complete(hire),
        "timesheets": db.query(Timesheet).filter(Timesheet.hire_id == hire.id).order_by(Timesheet.week_start.desc()).all() if hire else [],
        "approved_regular": sum(float(t.regular_hours or 0) for t in db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved").all()) if hire else 0,
        "approved_overtime": sum(float(t.overtime_hours or 0) for t in db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved").all()) if hire else 0,
        "worker_pay_estimate": ((sum(float(t.regular_hours or 0) for t in db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved").all()) * float(hire.worker_hourly_rate or 0)) + (sum(float(t.overtime_hours or 0) for t in db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved").all()) * float(hire.worker_hourly_rate or 0) * float(hire.overtime_multiplier or 1.5))) if hire else 0,
        "payroll_records": db.query(PayrollInvoiceRecord).filter(PayrollInvoiceRecord.hire_id == hire.id).order_by(PayrollInvoiceRecord.id.desc()).all() if hire else [],
        "unbilled_approved_count": db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved", Timesheet.payroll_invoice_id.is_(None)).count() if hire else 0,
        "contractor_bill_estimate": ((sum(float(t.regular_hours or 0) for t in db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved").all()) + sum(float(t.overtime_hours or 0) for t in db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved").all()) * float(hire.overtime_multiplier or 1.5)) * float(hire.contractor_bill_rate or 0)) if hire else 0,
    })


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
        hire = HireRecord(
            application_id=app_row.id,
            job_id=app_row.job_id,
            worker_id=app_row.worker_id,
            worker_user_id=app_row.worker_user_id,
            contractor_user_id=app_row.contractor_user_id,
        )
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
    if not _details_complete(hire):
        raise HTTPException(status_code=400, detail="All agreed hire details must be completed before confirmation.")
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
        hire = HireRecord(
            application_id=app_row.id,
            job_id=app_row.job_id,
            worker_id=app_row.worker_id,
            worker_user_id=app_row.worker_user_id,
            contractor_user_id=app_row.contractor_user_id,
        )
        db.add(hire)
    worker = db.query(main.Worker).filter(main.Worker.id == app_row.worker_id).first()
    hire.documents_verified = documents_verified == "yes"
    hire.orientation_complete = orientation_complete == "yes"
    requested_clearance = cleared_to_start == "yes"
    prerequisites = [
        bool(worker and worker.resume_name),
        bool(worker and ((worker.cert_name or "").strip() or (worker.certifications or "").strip())),
        bool(hire.worker_confirmed),
        bool(hire.documents_verified),
        bool(hire.orientation_complete),
    ]
    if requested_clearance and not all(prerequisites):
        raise HTTPException(status_code=400, detail="Complete résumé, certifications, worker confirmation, document review, and orientation before clearing the worker to start.")
    hire.cleared_to_start = requested_clearance
    if hire.cleared_to_start and (not hire.assignment_status or hire.assignment_status == "pending"):
        hire.assignment_status = "ready_to_start"
    hire.updated_at = main.now_iso()
    if hire.cleared_to_start:
        main.notify(db, app_row.worker_user_id, "Cleared to start", "Your contractor marked your onboarding as cleared to start.")
    else:
        main.notify(db, app_row.worker_user_id, "Onboarding updated", "Your contractor updated your onboarding checklist.")
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/timesheet")
def submit_timesheet(application_id: int, request: Request, week_start: str = Form(...), regular_hours: str = Form("0"), overtime_hours: str = Form("0"), notes: str = Form(""), db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db, "worker")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.worker_user_id == user.id, main.Application.status == "hired").first()
    hire = _hire(db, application_id)
    if not app_row or not hire or hire.assignment_status != "active":
        raise HTTPException(status_code=404, detail="Active assignment not found")
    try:
        regular = float(regular_hours or 0)
        overtime = float(overtime_hours or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="Hours must be valid numbers.")
    if regular < 0 or overtime < 0 or regular + overtime > 168:
        raise HTTPException(status_code=400, detail="Weekly hours must be between 0 and 168.")
    week = week_start.strip()[:40]
    existing_sheet = db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.week_start == week).first()
    if existing_sheet and existing_sheet.status != "rejected":
        raise HTTPException(status_code=400, detail="A timesheet already exists for this week.")
    if existing_sheet:
        row = existing_sheet
        row.regular_hours = str(regular)
        row.overtime_hours = str(overtime)
        row.notes = notes.strip()[:2000]
        row.status = "submitted"
        row.contractor_notes = ""
        row.updated_at = main.now_iso()
    else:
        row = Timesheet(hire_id=hire.id, week_start=week, regular_hours=str(regular), overtime_hours=str(overtime), notes=notes.strip()[:2000], status="submitted")
    db.add(row)
    main.notify(db, app_row.contractor_user_id, "Timesheet submitted", "A weekly timesheet is ready for review.")
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/timesheet/{timesheet_id}")
def review_timesheet(application_id: int, timesheet_id: int, request: Request, action: str = Form(...), contractor_notes: str = Form(""), db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.contractor_user_id == user.id, main.Application.status == "hired").first()
    hire = _hire(db, application_id)
    if not app_row or not hire:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    row = db.query(Timesheet).filter(Timesheet.id == timesheet_id, Timesheet.hire_id == hire.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Unknown timesheet action")
    if row.status != "submitted":
        raise HTTPException(status_code=400, detail="Only submitted timesheets can be reviewed.")
    row.status = "approved" if action == "approve" else "rejected"
    row.contractor_notes = contractor_notes.strip()[:2000]
    row.updated_at = main.now_iso()
    main.notify(db, app_row.worker_user_id, "Timesheet reviewed", f"Your timesheet was {row.status}.")
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/billing-rates")
def save_billing_rates(application_id: int, request: Request, worker_hourly_rate: str = Form(""), overtime_multiplier: str = Form("1.5"), contractor_bill_rate: str = Form(""), db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.contractor_user_id == user.id, main.Application.status == "hired").first()
    hire = _hire(db, application_id)
    if not app_row or not hire:
        raise HTTPException(status_code=404, detail="Assignment not found")
    try:
        worker_rate = float(worker_hourly_rate or 0)
        ot_multiplier = float(overtime_multiplier or 1.5)
        bill_rate = float(contractor_bill_rate or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="Rates must be valid numbers.")
    if worker_rate < 0 or bill_rate < 0 or ot_multiplier < 1 or ot_multiplier > 3:
        raise HTTPException(status_code=400, detail="Enter valid rates and an OT multiplier from 1 to 3.")
    hire.worker_hourly_rate = str(worker_rate)
    hire.overtime_multiplier = str(ot_multiplier)
    hire.contractor_bill_rate = str(bill_rate)
    hire.updated_at = main.now_iso()
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/payroll-invoice")
def create_payroll_invoice(application_id: int, request: Request, db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.contractor_user_id == user.id, main.Application.status == "hired").first()
    hire = _hire(db, application_id)
    if not app_row or not hire:
        raise HTTPException(status_code=404, detail="Assignment not found")
    sheets = db.query(Timesheet).filter(Timesheet.hire_id == hire.id, Timesheet.status == "approved", Timesheet.payroll_invoice_id.is_(None)).all()
    if not sheets:
        raise HTTPException(status_code=400, detail="No new approved timesheets are available.")
    try:
        worker_rate = float(hire.worker_hourly_rate or 0)
        multiplier = float(hire.overtime_multiplier or 1.5)
        bill_rate = float(hire.contractor_bill_rate or 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="Save valid payroll and billing rates first.")
    if worker_rate <= 0 or bill_rate <= 0:
        raise HTTPException(status_code=400, detail="Save worker and contractor rates before creating records.")
    regular = sum(float(s.regular_hours or 0) for s in sheets)
    overtime = sum(float(s.overtime_hours or 0) for s in sheets)
    record = PayrollInvoiceRecord(hire_id=hire.id, regular_hours=str(regular), overtime_hours=str(overtime), worker_gross_pay=str(regular * worker_rate + overtime * worker_rate * multiplier), contractor_billing=str((regular + overtime * multiplier) * bill_rate))
    db.add(record)
    db.flush()
    for sheet in sheets:
        sheet.payroll_invoice_id = record.id
        sheet.updated_at = main.now_iso()
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/payroll-invoice/{record_id}")
def update_payroll_invoice(application_id: int, record_id: int, request: Request, action: str = Form(...), db: main.Session = Depends(main.get_db)):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(main.Application.id == application_id, main.Application.contractor_user_id == user.id).first()
    hire = _hire(db, application_id)
    if not app_row or not hire:
        raise HTTPException(status_code=404, detail="Record not found")
    record = db.query(PayrollInvoiceRecord).filter(PayrollInvoiceRecord.id == record_id, PayrollInvoiceRecord.hire_id == hire.id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    if action == "process_payroll":
        record.payroll_status = "processed"
    elif action == "pay_payroll":
        record.payroll_status = "paid"
    elif action == "send_invoice":
        record.invoice_status = "processed"
    elif action == "pay_invoice":
        record.invoice_status = "paid"
    else:
        raise HTTPException(status_code=400, detail="Unknown record action")
    record.updated_at = main.now_iso()
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/active-details")
def save_active_assignment_details(
    application_id: int,
    request: Request,
    supervisor: str = Form(""),
    site_contact: str = Form(""),
    expected_end_date: str = Form(""),
    assignment_notes: str = Form(""),
    hours_worked: str = Form(""),
    db: main.Session = Depends(main.get_db),
):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(
        main.Application.id == application_id,
        main.Application.contractor_user_id == user.id,
        main.Application.status == "hired",
    ).first()
    hire = _hire(db, application_id)
    if not app_row or not hire:
        raise HTTPException(status_code=404, detail="Active assignment not found")
    if hire.assignment_status != "active":
        raise HTTPException(status_code=400, detail="Assignment management is available only while Active.")
    hire.supervisor = supervisor.strip()[:160]
    hire.site_contact = site_contact.strip()[:160]
    hire.expected_end_date = expected_end_date.strip()[:40]
    hire.assignment_notes = assignment_notes.strip()[:4000]
    hire.hours_worked = hours_worked.strip()[:40]
    hire.updated_at = main.now_iso()
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)


@router.post("/hire/{application_id}/assignment")
def update_assignment(
    application_id: int,
    request: Request,
    action: str = Form(...),
    db: main.Session = Depends(main.get_db),
):
    user = main.require_user(request, db, "contractor")
    app_row = db.query(main.Application).filter(
        main.Application.id == application_id,
        main.Application.contractor_user_id == user.id,
        main.Application.status == "hired",
    ).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="Hired worker not found")
    hire = _hire(db, application_id)
    worker = db.query(main.Worker).filter(main.Worker.id == app_row.worker_id).first()
    if not hire:
        raise HTTPException(status_code=400, detail="Complete hire details and onboarding first.")
    progress = _onboarding_progress(worker, hire)

    if action == "start":
        if progress < 100 or not hire.cleared_to_start:
            raise HTTPException(status_code=400, detail="Worker must be 100% onboarded and cleared to start before the assignment can begin.")
        if hire.assignment_status == "completed":
            raise HTTPException(status_code=400, detail="Completed assignments cannot be started again.")
        hire.assignment_status = "active"
        if not hire.actual_start_date:
            hire.actual_start_date = main.now_iso()[:10]
        main.notify(db, app_row.worker_user_id, "Assignment started", "Your contractor marked your assignment as Active.")
    elif action == "complete":
        if hire.assignment_status != "active":
            raise HTTPException(status_code=400, detail="Only an active assignment can be completed.")
        hire.assignment_status = "completed"
        hire.completed_date = main.now_iso()[:10]
        main.notify(db, app_row.worker_user_id, "Assignment completed", "Your contractor marked your assignment as completed.")
    else:
        raise HTTPException(status_code=400, detail="Unknown assignment action")

    hire.updated_at = main.now_iso()
    db.commit()
    return RedirectResponse(f"/hire/{application_id}", status_code=303)
