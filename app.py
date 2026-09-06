"""TradeForce AI application entrypoint with Phase 5 billing enabled."""
import main
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
