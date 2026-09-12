"""TradeForce AI production entrypoint with Phase 6 hiring/onboarding enabled."""
import main
from app import app
from phase6_onboarding import init_phase6, onboarding_progress, hire_for_application, router as phase6_router

init_phase6()
main.templates.env.globals["hire_for_application"] = hire_for_application
main.templates.env.globals["onboarding_progress"] = onboarding_progress
app.include_router(phase6_router)
