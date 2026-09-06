"""Public Privacy Policy and Terms of Service routes for TradeForce AI."""

import main
from fastapi import APIRouter, Request

router = APIRouter()


def _viewer(request: Request):
    db = main.SessionLocal()
    try:
        return main.current_user(request, db)
    finally:
        db.close()


@router.get("/privacy")
def privacy_policy(request: Request):
    return main.templates.TemplateResponse(
        "privacy.html",
        {"request": request, "user": _viewer(request)},
    )


@router.get("/terms")
def terms_of_service(request: Request):
    return main.templates.TemplateResponse(
        "terms.html",
        {"request": request, "user": _viewer(request)},
    )
