"""Make a superseded copy of a single-slot document the active one again
(decision 2026-10-01: a newer upload supersedes automatically, so the earlier
copy is one click away). The copy that was active becomes superseded in turn;
the stage is rebuilt from the documents on file and its recompute requested.

The one implementation of the PC's "Restore" action
(uc03_p2_api.restore_document_copy) and of OWN_COPY_RESTORE
(uc03_own_copy_restore), moved here unchanged on 2026-11-01.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_p2_runtime import record_activity


class CopyNotFound(LookupError):
    """No document of this journey has that evidence id."""


class NotSuperseded(ValueError):
    """Only a superseded copy can be restored."""


def restore_superseded_copy(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    actor_id: str,
    correlation_id: str | None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = connection.execute(
        text(
            """
            SELECT evidence_id, journey_document_requirement_id, di_document_id, association_status, process_area
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND evidence_id=:evidence_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "evidence_id": evidence_id},
    ).mappings().one_or_none()
    if target is None:
        raise CopyNotFound(str(evidence_id))
    if target["association_status"] != "SUPERSEDED":
        raise NotSuperseded(str(evidence_id))
    current = connection.execute(
        text(
            """
            SELECT evidence_id, di_document_id FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND journey_document_requirement_id=:requirement_ref
              AND association_status='ACTIVE'
            ORDER BY linked_at_utc DESC LIMIT 1
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id,
         "requirement_ref": target["journey_document_requirement_id"]},
    ).mappings().one_or_none()
    if current is not None:
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence
                SET association_status='SUPERSEDED', void_reason='EARLIER_COPY_RESTORED',
                    voided_by_actor_id=:actor, voided_at_utc=now()
                WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                """
            ),
            {"tenant_id": tenant_id, "evidence_id": current["evidence_id"], "actor": actor_id},
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.document_capture_v2_documents
                SET capture_status='SUPERSEDED', updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND di_document_id=:document_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": current["di_document_id"]},
        )
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='ACTIVE', void_reason=NULL, voided_by_actor_id=NULL, voided_at_utc=NULL,
                supersedes_evidence_id=:supersedes
            WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
            """
        ),
        {"tenant_id": tenant_id, "evidence_id": evidence_id,
         "supersedes": current["evidence_id"] if current is not None else None},
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents
            SET capture_status='CLASSIFIED', updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND di_document_id=:document_id
              AND capture_status='SUPERSEDED'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": target["di_document_id"]},
    )
    from audit_core.uc03_p2_worker import _rematerialize_stage

    _rematerialize_stage(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        stage_code=str(target["process_area"] or "BOOKING").upper(),
    )
    # The one STAGE_RECOMPUTE row of the Journey ('booking:<journey>'), with the
    # fact version bumped (2026-10-29): a second key never merged with it.
    from audit_core.uc03_p2_runtime import note_facts_changed

    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                       reason="DOCUMENT_COPY_RESTORED", correlation_id=correlation_id)
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="DOCUMENT_COPY_RESTORED",
        subject_type="DOCUMENT", subject_id=str(target["di_document_id"]),
        details={"evidenceId": str(evidence_id),
                 "supersededEvidenceId": str(current["evidence_id"]) if current is not None else None,
                 "by": actor_id, **(details or {})},
        correlation_id=correlation_id,
    )
    return {"evidenceId": str(evidence_id), "documentId": str(target["di_document_id"]), "status": "ACTIVE",
            "supersededEvidenceId": str(current["evidence_id"]) if current is not None else None}
