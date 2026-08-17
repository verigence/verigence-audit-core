from fastapi import FastAPI

from audit_core.audit_review import router as audit_review_router
from audit_core.bookings import router as booking_router
from audit_core.commercials import router as commercials_router
from audit_core.config import load_settings
from audit_core.contract_guards import install_contract_guards
from audit_core.crm_api import router as crm_router
from audit_core.customers import router as customer_router
from audit_core.daily_operations_api import router as daily_operations_router
from audit_core.dealers import router as dealer_router
from audit_core.errors import install_error_handlers
from audit_core.escalations_api import router as escalation_router
from audit_core.evidence import router as evidence_router
from audit_core.evidence_read import router as evidence_read_router
from audit_core.findings import router as findings_router
from audit_core.insurance_tradein import router as insurance_tradein_router
from audit_core.journeys import router as journey_router
from audit_core.observability import install_observability
from audit_core.payments_finance import router as payments_finance_router
from audit_core.projects import router as project_router
from audit_core.reference_data import router as reference_data_router
from audit_core.tasks_api import router as task_router
from audit_core.vehicle_delivery import router as vehicle_delivery_router


def create_app() -> FastAPI:
    settings = load_settings()
    application = FastAPI(
        title=settings.service_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(application)
    install_observability(application)
    install_contract_guards(application)
    application.include_router(project_router)
    application.include_router(dealer_router)
    application.include_router(customer_router)
    application.include_router(journey_router)
    application.include_router(evidence_router)
    application.include_router(evidence_read_router)
    application.include_router(booking_router)
    application.include_router(commercials_router)
    application.include_router(payments_finance_router)
    application.include_router(insurance_tradein_router)
    application.include_router(vehicle_delivery_router)
    application.include_router(findings_router)
    application.include_router(audit_review_router)
    application.include_router(task_router)
    application.include_router(daily_operations_router)
    application.include_router(crm_router)
    application.include_router(escalation_router)
    application.include_router(reference_data_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
