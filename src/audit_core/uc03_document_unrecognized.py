"""uc03_document_unrecognized.py — surface documents DI could not classify.

DI's Capture V2 classifier sets a document's state to ``UNKNOWN`` when its
best-confidence guess against the tenant's known document types still falls
below the acceptance threshold -- it genuinely doesn't know what the
document is (as opposed to CLASSIFIED-but-not-yet-extracted, or FAILED).
That state was previously invisible: it never gets an ``evidence`` link (DI
only arms the audit-link webhook for a *successful* classification), so
Audit Core's document-sync pipeline (uc03_confidence_review_policy.py's
_sync_booking_document) never even sees it, and the capture screen's own
card status only distinguishes PROCESSED/FAILED -- everything else, UNKNOWN
included, renders as plain "Uploaded". A PC had no way to tell "still being
classified" from "DI gave up, a human needs to look at this."

This raises one ``DOCUMENT_UNRECOGNIZED`` finding per such document, the
moment a capture-screen read reconciles Delivery's or Booking's live DI
document list (uc03_delivery_capture_v2.py / uc03_document_capture_v2.py --
the same place that state already gets written durably into
document_capture_v2_documents.capture_status). Carries the document's
filename and a direct view link so the PC/TL can open it without hunting
through the capture list themselves. Resolves automatically once the
document is reclassified (a later DI pass changes its mind) or removed.
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_delivery_commands import _machine_flag

_FINDING_TYPE = "DOCUMENT_UNRECOGNIZED"
_RULE_PREFIX = "DOCUMENT_UNRECOGNIZED"

StageCode = Literal["BOOKING", "DELIVERY"]


def _rule_key(stage_code: str, document_id: UUID) -> str:
    return f"{_RULE_PREFIX}:{stage_code}:{document_id}"


def _friendly_filename(raw: Any, document_id: UUID) -> str:
    text_value = str(raw).strip() if raw else ""
    return text_value or f"document {str(document_id)[:8]}"


def sync_document_unrecognized_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    di_documents: list[dict[str, Any]],
    correlation_id: str,
) -> dict[str, Any]:
    """Raise a DOCUMENT_UNRECOGNIZED finding for each currently-UNKNOWN
    document in this read's live DI list; resolve one whose document is no
    longer UNKNOWN (reclassified, or gone).

    Best-effort and idempotent -- safe to call on every capture-screen read.
    Never raises; a failure here must never break the read it's piggybacking on.
    """
    try:
        unrecognized = [
            item for item in di_documents
            if str(item.get("state") or "").upper() == "UNKNOWN"
        ]
    except Exception:  # noqa: BLE001 - producer must never break the caller
        return {"raised": 0, "resolved": 0, "error": True}

    raised = 0
    live_rules: set[str] = set()
    for item in unrecognized:
        try:
            document_id = UUID(str(item["documentId"]))
        except (KeyError, ValueError):
            continue
        live_rules.add(_rule_key(stage_code, document_id))
        filename = _friendly_filename(item.get("originalFilename"), document_id)
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            rule_key=_rule_key(stage_code, document_id),
            finding_type=_FINDING_TYPE,
            severity="LOW",
            title=f"Unrecognized document: {filename}",
            description=(
                f'"{filename}" was uploaded but Document Intelligence could not '
                "confidently identify it as any known document type. Open the "
                "document, confirm what it actually is, and either re-upload it "
                "as the correct type or ask an Admin to register a new document "
                "type if this is a legitimate document DI has never seen before."
            ),
            correlation_id=correlation_id,
            safe_payload={
                "diDocumentId": str(document_id),
                "originalFilename": filename,
                "contentUrl": item.get("contentUrl"),
            },
        )
        raised += 1

    open_findings = connection.execute(
        text(
            """
            SELECT audit_finding_id, rule_key
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND stage_code = :stage_code
              AND finding_type_code = :finding_type
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "finding_type": _FINDING_TYPE,
        },
    ).mappings().all()

    resolved = 0
    for finding in open_findings:
        if finding["rule_key"] in live_rules:
            continue
        updated = connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET finding_status = 'RESOLVED',
                    disposition = 'FIXED',
                    resolved_at_utc = now(),
                    resolved_by_actor_id = NULL,
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id
                  AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "finding_id": finding["audit_finding_id"]},
        )
        if updated.rowcount != 1:
            continue
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code,
                    event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
                ) VALUES (
                    :tenant_id, :finding_id, :journey_id, :stage_code,
                    'RESOLVED', NULL, 'SYSTEM', CAST(:payload AS jsonb), :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "finding_id": finding["audit_finding_id"],
                "journey_id": journey_id,
                "stage_code": stage_code,
                "payload": '{"disposition": "FIXED", "note": "Document reclassified or removed."}',
                "correlation_id": correlation_id,
            },
        )
        resolved += 1

    from audit_core.uc03_delivery_commands import _set_stage_flag_status

    _set_stage_flag_status(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code)
    return {"raised": raised, "resolved": resolved}
