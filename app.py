"""TradeForce AI application entrypoint with Phase 5 billing enabled."""
from main import app
from phase5_billing import router as billing_router

app.include_router(billing_router)
