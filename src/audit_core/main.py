from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from audit_core import mahindra_masters, role_mappings
from audit_core.attendance_context import router as attendance_context_router
from audit_core.audit_review import router as audit_review_router
from audit_core.bookings import router as booking_router
from audit_core.commercials import router as commercials_router
from audit_core.config import load_settings
from audit_core.contract_guards import install_contract_guards
from audit_core.crm_api import router as crm_router
from audit_core.customers import router as customer_router
from audit_core.daily_operations_api import router as daily_operations_router
from audit_core.dealers import router as dealer_router
from audit_core.di_project_master_proxy import router as di_project_master_proxy_router
from audit_core.errors import install_error_handlers
from audit_core.escalations_api import router as escalation_router
from audit_core.evidence import router as evidence_router
from audit_core.evidence_read import router as evidence_read_router
from audit_core.feedback import router as feedback_router
from audit_core.findings import router as findings_router
from audit_core.insurance_tradein import router as insurance_tradein_router
from audit_core.journey_housekeeping import router as journey_housekeeping_router
from audit_core.journeys import router as journey_router
from audit_core.logging_config import configure_logging
from audit_core.mahindra_dealer_policy_scope import install_dealer_policy_scope
from audit_core.mahindra_native_effective_date import install_native_effective_date
from audit_core.mahindra_native_workbooks import install_native_workbook_parser
from audit_core.mahindra_upload_state import router as mahindra_upload_state_router
from audit_core.observability import install_observability
from audit_core.otel import configure_otlp
from audit_core.payments_finance import router as payments_finance_router
from audit_core.pricing import router as pricing_router
from audit_core.project_activation import router as project_activation_router
from audit_core.project_master_admin import router as project_master_admin_router
from audit_core.project_master_forms import router as project_master_form_router
from audit_core.project_master_imports import router as project_master_import_router
from audit_core.project_masters import router as project_master_router
from audit_core.project_provisioning import router as project_provisioning_router
from audit_core.project_reference_data import router as project_reference_data_router
from audit_core.projects import router as project_router
from audit_core.readiness import router as readiness_router
from audit_core.reference_data import router as reference_data_router
from audit_core.role_mapping_policy import install_role_mapping_policy
from audit_core.runtime_warmup import warm_runtime_dependencies
from audit_core.tasks_api import router as task_router
from audit_core.uc02_project_admin_stabilization import (
    router as uc02_project_admin_stabilization_router,
)
from audit_core.uc03_audit_flags import router as uc03_audit_router
from audit_core.uc03_authorized_work_items import router as uc03_work_items_router
from audit_core.uc03_booking_capture import router as uc03_booking_capture_router
from audit_core.uc03_booking_commands import router as uc03_booking_router
from audit_core.uc03_booking_details import router as uc03_booking_details_router
from audit_core.uc03_booking_evidence import router as uc03_booking_evidence_router
from audit_core.uc03_booking_evidence_details import (
    router as uc03_booking_evidence_details_router,
)
from audit_core.uc03_booking_exchange import router as uc03_booking_exchange_router
from audit_core.uc03_booking_integrations import (
    router as uc03_booking_integrations_router,
)
from audit_core.uc03_booking_part1 import router as uc03_booking_part1_router
from audit_core.uc03_booking_receipt_capture import install_uc03_booking_receipt_capture
from audit_core.uc03_booking_review import router as uc03_booking_review_router
from audit_core.uc03_booking_v2 import router as uc03_booking_v2_router
from audit_core.uc03_create_booking import router as uc03_create_booking_router
from audit_core.uc03_customer_mobile_pii import install_uc03_customer_mobile_pii
from audit_core.uc03_delivery_capture_v2 import (
    router as uc03_delivery_capture_v2_router,
)
from audit_core.uc03_delivery_commands import router as uc03_delivery_router
from audit_core.uc03_delivery_documents import router as uc03_delivery_documents_router
from audit_core.uc03_di_core_persistence import install_uc03_di_core_persistence
from audit_core.uc03_document_assessments import (
    router as uc03_document_assessments_router,
)
from audit_core.uc03_document_capture_v2 import (
    router as uc03_document_capture_v2_router,
)
from audit_core.uc03_document_capture_v2_rules import (
    install_uc03_v2_capture_business_rules,
)
from audit_core.uc03_document_review_v2 import router as uc03_document_review_v2_router
from audit_core.uc03_fast_work_items import router as uc03_fast_work_items_router
from audit_core.uc03_identity_business_date import install_uc03_identity_business_date
from audit_core.uc03_identity_business_date import (
    router as uc03_identity_business_date_router,
)
from audit_core.uc03_journey_search import router as uc03_journey_search_router
from audit_core.uc03_pc_booking_documents import (
    router as uc03_pc_booking_documents_router,
)
from audit_core.uc03_pc_direct_review import router as uc03_pc_direct_review_router
from audit_core.uc03_pc_generic_review import router as uc03_pc_generic_review_router
from audit_core.uc03_pc_verification import router as uc03_pc_verification_router
from audit_core.uc03_project_context import router as uc03_project_context_router
from audit_core.uc03_sku_candidates import router as uc03_sku_candidates_router
from audit_core.uc03_tl_scope_alignment import install_tl_scope_alignment
from audit_core.uc03_tl_supervisory import router as uc03_tl_supervisory_router
from audit_core.vehicle_delivery import router as vehicle_delivery_router

install_role_mapping_policy(role_mappings)
install_native_workbook_parser(mahindra_masters)
install_native_effective_date(mahindra_masters)
install_dealer_policy_scope(mahindra_masters)
install_uc03_identity_business_date()
install_uc03_customer_mobile_pii()
install_uc03_booking_receipt_capture()
install_uc03_di_core_persistence()
install_tl_scope_alignment()
install_uc03_v2_capture_business_rules()
role_mapping_router = role_mappings.router
mahindra_master_router = mahindra_masters.router

_CORS_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_CORS_HEADERS = [
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "If-Match",
    "X-Correlation-ID",
    "X-Trace-ID",
]
_CORS_EXPOSE_HEADERS = ["ETag", "X-Correlation-ID", "X-Trace-ID"]


@asynccontextmanager
async def _lifespan(_: FastAPI):
    warm_runtime_dependencies()
    yield


def create_app() -> FastAPI:
    settings = load_settings()
    application = FastAPI(
        title=settings.service_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    configure_otlp(application, settings)
    configure_logging(settings)
    install_error_handlers(application)
    install_observability(application)
    install_contract_guards(application)
    if settings.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=_CORS_METHODS,
            allow_headers=_CORS_HEADERS,
            expose_headers=_CORS_EXPOSE_HEADERS,
        )
    application.include_router(project_reference_data_router)
    application.include_router(project_provisioning_router)
    application.include_router(project_router)
    application.include_router(journey_housekeeping_router)
    application.include_router(uc03_project_context_router)
    application.include_router(attendance_context_router)
    application.include_router(uc03_fast_work_items_router)
    application.include_router(uc03_work_items_router)
    application.include_router(uc03_journey_search_router)
    application.include_router(uc03_tl_supervisory_router)
    application.include_router(uc03_create_booking_router)
    application.include_router(uc03_booking_router)
    application.include_router(uc03_booking_exchange_router)
    application.include_router(uc03_booking_integrations_router)
    application.include_router(uc03_pc_verification_router)
    application.include_router(uc03_booking_capture_router)
    application.include_router(uc03_booking_v2_router)
    application.include_router(uc03_sku_candidates_router)
    application.include_router(uc03_document_capture_v2_router)
    application.include_router(uc03_document_review_v2_router)
    application.include_router(uc03_pc_booking_documents_router)
    application.include_router(uc03_pc_direct_review_router)
    application.include_router(uc03_pc_generic_review_router)
    application.include_router(uc03_booking_part1_router)
    application.include_router(uc03_identity_business_date_router)
    application.include_router(uc03_booking_evidence_router)
    application.include_router(uc03_booking_evidence_details_router)
    application.include_router(uc03_booking_details_router)
    application.include_router(uc03_booking_review_router)
    application.include_router(uc03_delivery_router)
    application.include_router(uc03_delivery_capture_v2_router)
    application.include_router(uc03_delivery_documents_router)
    application.include_router(uc03_document_assessments_router)
    application.include_router(uc03_audit_router)
    application.include_router(readiness_router)
    application.include_router(project_activation_router)
    application.include_router(mahindra_upload_state_router)
    application.include_router(mahindra_master_router)
    application.include_router(di_project_master_proxy_router)
    application.include_router(project_master_admin_router)
    application.include_router(project_master_form_router)
    application.include_router(project_master_router)
    application.include_router(project_master_import_router)
    application.include_router(uc02_project_admin_stabilization_router)
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
    application.include_router(pricing_router)
    application.include_router(feedback_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()