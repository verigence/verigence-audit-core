from fastapi import FastAPI

from audit_core.audit_review import router as audit_review_router
from audit_core.bookings import router as booking_router
from audit_core.commercials import router as commercials_router
from audit_core.config import load_settings
from audit_core.customers import router as customer_router
from audit_core.dealers import router as dealer_router
from audit_core.errors import install_error_handlers
from audit_core.evidence import router as evidence_router
from audit_core.evidence_read import router as evidence_read_router
from audit_core.findings import router as findings_router
from audit_core.insurance_tradein import router as insurance_tradein_router
from audit_core.journeys import router as journey_router
from audit_core.observability import install_observability
from audit_core.payments_finance import router as payments_finance_router
from audit_core.projects import router as project_router
from audit_core.tasks_api import router as task_router
from audit_core.vehicle_delivery import router as vehicle_delivery_router

settings = load_settings()

app = FastAPI(
    title=settings.service_name,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
install_error_handlers(app)
install_observability(app)
app.include_router(project_router)
app.include_router(dealer_router)
app.include_router(customer_router)
app.include_router(journey_router)
app.include_router(evidence_router)
app.include_router(evidence_read_router)
app.include_router(booking_router)
app.include_router(commercials_router)
app.include_router(payments_finance_router)
app.include_router(insurance_tradein_router)
app.include_router(vehicle_delivery_router)
app.include_router(findings_router)
app.include_router(audit_review_router)
app.include_router(task_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
