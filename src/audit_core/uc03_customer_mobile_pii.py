from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core.customers import normalize_mobile_number
from audit_core.errors import AuditCoreError

_WriteTypedCapture = Callable[..., tuple[str, str]]
_previous_write_typed_capture: _WriteTypedCapture | None = None
_installed = False


def _mobile_aware_write_typed_capture(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    field_key: str,
    value: Any,
    source_evidence_id: UUID | None,
) -> tuple[str, str]:
    key = field_key.strip().upper()
    if key != "CUSTOMER_NUMBER":
        if _previous_write_typed_capture is None:
            raise RuntimeError("UC03 customer mobile installer is not initialized")
        return _previous_write_typed_capture(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            field_key=field_key,
            value=value,
            source_evidence_id=source_evidence_id,
        )

    uc03_booking_capture._validate_evidence_for_journey(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=source_evidence_id,
    )
    customer_id = uc03_booking_capture._journey_customer_id(
        connection,
        tenant_id,
        journey_id,
    )
    raw_value = uc03_booking_capture._as_text(value, key)
    try:
        mobile_number, mobile_last4 = normalize_mobile_number(raw_value)
    except ValueError as exc:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail=str(exc),
        ) from exc

    connection.execute(
        text(
            """
            UPDATE auditcore.customers
            SET mobile_number=:mobile_number,
                mobile_last4=:mobile_last4,
                updated_at_utc=now(),
                version_no=version_no+1
            WHERE tenant_id=:tenant_id AND customer_id=:customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "mobile_number": mobile_number,
            "mobile_last4": mobile_last4,
        },
    )
    return "CUSTOMER", str(customer_id)


def install_uc03_customer_mobile_pii() -> None:
    """Publish full customer mobile persistence onto the existing UC03 capture API.

    UC03 already uses small installer layers for reconciled behavior. Capture and DI
    proposal acceptance both resolve through ``_write_typed_capture``; wrapping that
    one publication boundary therefore updates both paths without adding an API.
    """

    global _installed, _previous_write_typed_capture
    if _installed:
        return
    _previous_write_typed_capture = uc03_booking_capture._write_typed_capture
    uc03_booking_capture._write_typed_capture = _mobile_aware_write_typed_capture
    _installed = True
