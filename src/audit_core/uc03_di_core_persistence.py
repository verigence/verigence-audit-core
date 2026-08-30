from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core import uc03_booking_review_decisions as review_decisions
from audit_core.uc03_v2_review_materialization import (
    materialize_reviewed_di_business_values,
)

_installed = False


def _current_actor_id(connection: Connection) -> str:
    actor_id = connection.execute(
        text("SELECT NULLIF(current_setting('app.security_actor_id', true), '')")
    ).scalar_one_or_none()
    if actor_id is None:
        raise RuntimeError("UC03 reviewed DI persistence requires an authenticated actor context")
    return str(actor_id)


def _materialize_all_reviewed_business_values(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    documents: list[Any],
    rejected_review_keys: set[str],
) -> dict[str, int]:
    """Compatibility wrapper for the existing Review-confirm transaction.

    The Review Decisions route already owns transactionality, idempotency, rejection
    semantics and aggregate locking. Replacing only its materialization callback keeps
    those guarantees while expanding persistence from Dealer Receipts to Booking Form,
    PAN, Aadhaar and all Receipt fields.
    """

    result = materialize_reviewed_di_business_values(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        documents=documents,
        rejected_review_keys=rejected_review_keys,
        actor_id=_current_actor_id(connection),
    )
    # Keep the legacy result keys consumed by uc03_booking_review_decisions.
    return {
        "created": result["receiptPaymentsCreated"],
        "updated": result["receiptPaymentsUpdated"],
        "unchanged": result["receiptPaymentsUnchanged"],
        "skippedWithoutAmount": result["receiptPaymentsSkippedWithoutAmount"],
        **result,
    }


def install_uc03_di_core_persistence() -> None:
    """Make Review confirmation persist every accepted DI business field in Core."""

    global _installed
    if _installed:
        return
    review_decisions.materialize_reviewed_booking_receipts = (
        _materialize_all_reviewed_business_values
    )
    _installed = True
