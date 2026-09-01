import os
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from audit_core import role_mappings
from audit_core.audit_review import router as audit_review_router
from audit_core.bookings import router as booking_router
from audit_core.commercials import router as commercials_router
from audit_core.config import load_settings
from audit_core.contract_guards import install_contract_guards
from audit_core.crm_api import router as crm_router
from audit_core.customers import router as customer_router
from audit_core.daily_operations_api import router as daily_operations_router
from audit_core.dealers import router as dealer_router
from audit_core.dependencies import (
    _check_db_rtt,
    clear_di_client,
    clear_security_admin_client,
    clear_security_oauth_client,
    set_di_client,
    set_security_admin_client,
    set_security_oauth_client,
)
from audit_core.di_client import DiClient
from audit_core.di_project_master_proxy import router as di_project_master_proxy_router
from audit_core.errors import install_error_handlers
from audit_core.escalations_api import router as escalation_router
from audit_core.evidence import router as evidence_router
from audit_core.evidence_read import router as evidence_read_router
from audit_core.findings import router as findings_router
from audit_core.insurance_tradein import router as insurance_tradein_router
from audit_core.journeys import router as journey_router
from audit_core.logging_config import configure_logging
from audit_core.observability import install_observability
from audit_core.payments_finance import router as payments_finance_router
from audit_core.project_activation import router as project_activation_router
from audit_core.project_master_imports import router as project_master_import_router
from audit_core.project_masters import router as project_master_router
from audit_core.project_provisioning import router as project_provisioning_router
from audit_core.projects import router as project_router
from audit_core.readiness import router as readiness_router
from audit_core.reference_data import router as reference_data_router
from audit_core.role_mapping_policy import install_role_mapping_policy
from audit_core.security_integration import SecurityAdminClient, SecurityOAuthClient
from audit_core.tasks_api import router as task_router
from audit_core.vehicle_delivery import router as vehicle_delivery_router

logger = structlog.get_logger(__name__)

install_role_mapping_policy(role_mappings)
role_mapping_router = role_mappings.router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start-up: create shared HTTP clients, verify DB RTT.
    Shut-down: drain and close all clients so Railway SIGTERM completes cleanly.
    """
    security_base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    security_client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    security_client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    di_base_url = os.environ.get("DI_BASE_URL", "").strip()

    if not security_base_url:
        raise RuntimeError("SECURITY_BASE_URL is required")
    if not security_client_id or not security_client_secret:
        raise RuntimeError("SECURITY_CLIENT_ID and SECURITY_CLIENT_SECRET are required")
    if not di_base_url:
        raise RuntimeError("DI_BASE_URL is required")

    # One async admin client — reused across all admin-proxy requests
    admin_client = SecurityAdminClient(base_url=security_base_url)
    set_security_admin_client(admin_client)

    # One async OAuth client — reused across all evidence uploads (token mint)
    oauth_client = SecurityOAuthClient(
        base_url=security_base_url,
        client_id=security_client_id,
        client_secret=security_client_secret,
    )
    set_security_oauth_client(oauth_client)

    # One sync DI client — reused across all evidence uploads.
    # Plain def handlers run in FastAPI's threadpool; a sync client is correct here.
    di_client = DiClient(base_url=di_base_url)
    set_di_client(di_client)

    # Verify DB round-trip time — warn if > 5 ms (cross-region Neon)
    _check_db_rtt()

    logger.info(
        "audit_core_startup_complete",
        security_base_url=security_base_url,
        di_base_url=di_base_url,
    )

    yield

    # Graceful shutdown — close all HTTP client pools before SIGTERM kills the process
    await admin_client.aclose()
    await oauth_client.aclose()
    di_client.close()
    clear_security_admin_client()
    clear_security_oauth_client()
    clear_di_client()
    logger.info("audit_core_shutdown_complete")


def create_app() -> FastAPI:
    settings = load_settings()
    configure_logging(settings)
    application = FastAPI(
        title=settings.service_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    install_error_handlers(application)
    install_observability(application)
    install_contract_guards(application)
    application.include_router(project_provisioning_router)
    application.include_router(project_router)
    application.include_router(readiness_router)
    application.include_router(project_activation_router)
    # Literal DI-owned Project Master and generic import routes must be registered
    # before the older owner-module dynamic routes so DI requests cannot fall into
    # the Audit-Core-only rejection branches.
    application.include_router(di_project_master_proxy_router)
    application.include_router(project_master_router)
    application.include_router(project_master_import_router)
    application.include_router(dealer_router)
    application.include_router(role_mapping_router)
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
