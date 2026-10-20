"""The Phase 1 capture endpoints are closed (2026-10-20).

Found live 19 Oct (journey 9d1c307c): a PC reached the old Web Overview,
pressed "Capture new booking", and the old flow created the Journey
(POST /v1/tenants/{t}/uc03/bookings) and uploaded a 5-page PDF through the
old whole-file upload (POST /v2/.../uc03/documents/upload-intents). Document
Intelligence read the whole file as ONE booking form: it was never split into
pages, so the PAN and Aadhaar inside it were never read and the customer was
never named.

Booking & Delivery (the P2 endpoints: POST /p2/.../journeys and
uploads:init) is the only way a booking starts or a document is uploaded: it
splits every PDF into pages and groups them into documents. Web no longer
offers the old screens; these two endpoints refuse every call as well, so an
old bookmark, an old Android build or any other client cannot bring the
whole-file path back. Everything else on the old routers (reads, resync,
delete) is untouched -- the P2 screens still use the unified resync.
"""
from __future__ import annotations

from audit_core.errors import AuditCoreError

LEGACY_CAPTURE_ERROR_CODE = "VAC-UC03-LEGACY-CAPTURE-CLOSED"


class LegacyCaptureClosedError(AuditCoreError):
    def __init__(self, *, detail: str) -> None:
        super().__init__(LEGACY_CAPTURE_ERROR_CODE, 410, "This capture screen is retired", detail)


def refuse_legacy_booking_create() -> None:
    """FastAPI dependency on the old create-booking route."""
    raise LegacyCaptureClosedError(
        detail=(
            "New bookings start on Booking & Delivery: open Booking & Delivery and press "
            "Capture new booking. The old capture screen is retired."
        )
    )


def refuse_legacy_whole_file_upload() -> None:
    """FastAPI dependency on the old whole-file upload route."""
    raise LegacyCaptureClosedError(
        detail=(
            "Documents are uploaded on the journey's Booking & Delivery screen, which sorts a "
            "PDF of many documents page by page. The old upload screen is retired."
        )
    )
