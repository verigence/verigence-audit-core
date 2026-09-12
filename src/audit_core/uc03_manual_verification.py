"""uc03_manual_verification.py — the "manual verification required" worklist.

When a booking / delivery document is extracted and one or more of its
machine-read values are below the 90% confidence bar, this raises **one
``MANUAL_VERIFICATION`` finding per (stage, document)** carrying the field list.
The PC works it straight from the Review Queue — confirm or correct each value
against the boxed document evidence — instead of reopening the journey and
navigating to the review screen.

  GET  /v1/tenants/{t}/journeys/{j}/manual-verification
  POST /v1/tenants/{t}/journeys/{j}/manual-verification/{findingId}/resolve

The per-field decisions are written to ``auditcore.journey_document_extracted_fields``
(``effective_value`` / ``reviewed_at_utc`` / ``is_modified``) — the same store the
review screen writes, so the existing submit / canonical-materialization path
(gated on ``reviewed_at_utc IS NULL``) picks them up unchanged.

``sync_manual_verification_findings`` is the producer: it raises a finding for
every document that still has unreviewed low-confidence fields and resolves the
finding for every document that no longer does.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import NotFoundError, ValidationError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_authorized_work_items import _authorize_workspace
from audit_core.uc03_delivery_commands import _machine_flag

router = APIRouter(tags=["uc03-manual-verification"])

_RULE_PREFIX = "MANUAL_VERIFICATION"
_FINDING_TYPE = "MANUAL_VERIFICATION"
_CONFIDENCE_THRESHOLD = 0.90  # journey_document_extracted_fields.confidence_score is 0..1

StageCode = Literal["BOOKING", "DELIVERY"]


def _rule_key(stage_code: str, document_id: UUID) -> str:
    return f"{_RULE_PREFIX}:{stage_code}:{document_id}"


# ── low-confidence field query ─────────────────────────────────────────────────
_LOW_CONFIDENCE_SQL = """
    SELECT f.extracted_field_id,
           f.di_document_id,
           f.field_key,
           f.source_canonical_field_id,
           f.source_document_type_key,
           f.source_fact_ref,
           f.source_fact_version,
           f.extracted_value,
           f.effective_value,
           f.confidence_score,
           COALESCE(e.document_type_key, f.source_document_type_key) AS document_label
    FROM auditcore.journey_document_extracted_fields f
    LEFT JOIN auditcore.evidence e
      ON e.tenant_id = f.tenant_id AND e.di_document_id = f.di_document_id
    WHERE f.tenant_id = :tenant_id
      AND f.journey_id = :journey_id
      AND f.stage_code = :stage_code
      AND f.reviewed_at_utc IS NULL
      AND f.extracted_value IS NOT NULL
      AND f.extracted_value <> 'null'::jsonb
      AND (f.confidence_score IS NULL OR f.confidence_score < :threshold)
    ORDER BY f.di_document_id, f.field_key
"""


def _unreviewed_low_confidence(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(_LOW_CONFIDENCE_SQL),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "threshold": _CONFIDENCE_THRESHOLD,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _by_document(rows: list[dict[str, Any]]) -> dict[UUID, list[dict[str, Any]]]:
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["di_document_id"], []).append(row)
    return grouped


def _extracted_field_count(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str
) -> int:
    """How many extracted fields exist at all for this stage, regardless of
    confidence or review status -- distinguishes "nothing extracted for this
    stage yet" (SKIPPED) from "checked, nothing currently below threshold"
    (PASS) for the Execution Log, since _LOW_CONFIDENCE_SQL only ever
    returns the rows that already have a problem."""
    return connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND stage_code = :stage_code
              AND extracted_value IS NOT NULL AND extracted_value <> 'null'::jsonb
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).scalar_one()


# ── producer ──────────────────────────────────────────────────────────────────
def sync_manual_verification_findings(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: StageCode,
    correlation_id: str,
) -> dict[str, Any]:
    """Raise a MANUAL_VERIFICATION finding for each document that still has
    unreviewed low-confidence fields; resolve it for each that no longer does.

    Best-effort and idempotent — safe to call after every extraction, review or
    correction. Never raises.
    """
    try:
        pending = _by_document(
            _unreviewed_low_confidence(
                connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code
            )
        )
    except Exception:  # noqa: BLE001 - producer must never break the caller
        return {"raised": 0, "resolved": 0, "error": True}

    raised = 0
    for document_id, fields in pending.items():
        label = _friendly_label(fields[0].get("document_label"), document_id)
        _machine_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            rule_key=_rule_key(stage_code, document_id),
            finding_type=_FINDING_TYPE,
            severity="LOW",
            title=f"Verify {len(fields)} value{'s' if len(fields) != 1 else ''} on {label}",
            description=(
                f"{len(fields)} machine-read value"
                f"{'s' if len(fields) != 1 else ''} on {label} are below the 90% "
                "confidence threshold — confirm or correct each against the document."
            ),
            correlation_id=correlation_id,
            safe_payload={
                "diDocumentId": str(document_id),
                "documentLabel": label,
                "fieldKeys": [row["field_key"] for row in fields],
            },
        )
        raised += 1

    # resolve findings whose document is now clear
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

    live_rules = {_rule_key(stage_code, doc_id) for doc_id in pending}
    resolved = 0
    for finding in open_findings:
        if finding["rule_key"] in live_rules:
            continue
        _resolve_finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            finding_id=finding["audit_finding_id"],
            actor_id=None,
            correlation_id=correlation_id,
            note="All flagged values verified.",
        )
        resolved += 1

    _refresh_stage_flag_status(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code)
    examined = _extracted_field_count(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code
    )
    return {"raised": raised, "resolved": resolved, "examined": examined}


def _friendly_label(raw: Any, document_id: UUID) -> str:
    text_value = str(raw).strip() if raw else ""
    if not text_value:
        return f"document {str(document_id)[:8]}"
    return text_value.replace("_", " ").title()


def _resolve_finding(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    finding_id: UUID,
    actor_id: str | None,
    correlation_id: str,
    note: str,
) -> None:
    updated = connection.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET finding_status = 'RESOLVED',
                disposition = 'FIXED',
                resolved_at_utc = now(),
                resolved_by_actor_id = :actor_id,
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "finding_id": finding_id, "actor_id": actor_id},
    )
    if updated.rowcount != 1:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, :stage_code,
                'RESOLVED', :actor_id, :role, CAST(:payload AS jsonb), :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "actor_id": actor_id,
            "role": "PC" if actor_id else "SYSTEM",
            "payload": json.dumps({"disposition": "FIXED", "note": note}),
            "correlation_id": correlation_id,
        },
    )


def _refresh_stage_flag_status(
    connection: Connection, *, tenant_id: str, journey_id: UUID, stage_code: str
) -> None:
    from audit_core.uc03_delivery_commands import _set_stage_flag_status

    _set_stage_flag_status(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code
    )


# ── read ──────────────────────────────────────────────────────────────────────
class ManualVerificationField(BaseModel):
    extractedFieldId: UUID
    fieldKey: str
    canonicalFieldId: str | None
    extractedValue: Any
    effectiveValue: Any
    confidence: float | None
    sourceFactRef: UUID | None
    sourceFactVersion: int


class ManualVerificationItem(BaseModel):
    findingId: UUID
    stageCode: str
    diDocumentId: UUID
    documentLabel: str
    severity: str
    ownerRoleCode: str | None
    slaDueAtUtc: str | None
    createdAtUtc: str
    fields: list[ManualVerificationField]


class ManualVerificationView(BaseModel):
    journeyId: UUID
    items: list[ManualVerificationItem]


@router.get(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/manual-verification",
    response_model=ManualVerificationView,
)
def get_manual_verification(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ManualVerificationView:
    _authorize_workspace(
        authorization_client, human_principal=human_principal, tenant_id=tenant_id
    )
    set_tenant_context(connection, tenant_id)

    # self-heal on read: raise findings for newly-extracted low-confidence fields,
    # resolve any whose document is now clear.
    for stage in ("BOOKING", "DELIVERY"):
        sync_manual_verification_findings(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage,  # type: ignore[arg-type]
            correlation_id="",
        )

    findings = connection.execute(
        text(
            """
            SELECT audit_finding_id, stage_code, rule_key, severity,
                   owner_role_code, sla_due_at_utc, created_at_utc
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND finding_type_code = :finding_type
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            ORDER BY stage_code, created_at_utc
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "finding_type": _FINDING_TYPE},
    ).mappings().all()
    if not findings:
        return ManualVerificationView(journeyId=journey_id, items=[])

    by_stage: dict[str, dict[UUID, list[dict[str, Any]]]] = {}
    for stage in {row["stage_code"] for row in findings}:
        by_stage[stage] = _by_document(
            _unreviewed_low_confidence(
                connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage
            )
        )

    items: list[ManualVerificationItem] = []
    for finding in findings:
        stage = finding["stage_code"]
        document_id = _document_from_rule(finding["rule_key"])
        fields = by_stage.get(stage, {}).get(document_id, [])
        if not fields:
            continue
        items.append(
            ManualVerificationItem(
                findingId=finding["audit_finding_id"],
                stageCode=stage,
                diDocumentId=document_id,
                documentLabel=_friendly_label(fields[0].get("document_label"), document_id),
                severity=finding["severity"],
                ownerRoleCode=finding["owner_role_code"],
                slaDueAtUtc=finding["sla_due_at_utc"].isoformat() if finding["sla_due_at_utc"] else None,
                createdAtUtc=finding["created_at_utc"].isoformat(),
                fields=[
                    ManualVerificationField(
                        extractedFieldId=row["extracted_field_id"],
                        fieldKey=row["field_key"],
                        canonicalFieldId=row["source_canonical_field_id"],
                        extractedValue=row["extracted_value"],
                        effectiveValue=row["effective_value"],
                        confidence=float(row["confidence_score"]) if row["confidence_score"] is not None else None,
                        sourceFactRef=row["source_fact_ref"],
                        sourceFactVersion=row["source_fact_version"],
                    )
                    for row in fields
                ],
            )
        )
    return ManualVerificationView(journeyId=journey_id, items=items)


def _document_from_rule(rule_key: str) -> UUID:
    return UUID(rule_key.rsplit(":", 1)[1])


# ── resolve ───────────────────────────────────────────────────────────────────
class FieldDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extractedFieldId: UUID
    action: Literal["CONFIRM", "CORRECT"]
    effectiveValue: Any = None


class ResolveManualVerificationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[FieldDecision] = Field(min_length=1)


class ResolveManualVerificationResponse(BaseModel):
    findingId: UUID
    findingStatus: str
    fieldsVerified: int
    stageManualVerificationOpen: int


@router.post(
    "/v1/tenants/{tenant_id}/journeys/{journey_id}/manual-verification/{finding_id}/resolve",
    response_model=ResolveManualVerificationResponse,
)
def resolve_manual_verification(
    tenant_id: str,
    journey_id: UUID,
    finding_id: UUID,
    command: ResolveManualVerificationCommand,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ResolveManualVerificationResponse:
    _authorize_workspace(
        authorization_client, human_principal=human_principal, tenant_id=tenant_id
    )
    set_tenant_context(connection, tenant_id)
    correlation_id = get_correlation_id(request)
    actor_id = human_principal.subject

    finding = connection.execute(
        text(
            """
            SELECT stage_code, rule_key, finding_status
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id
              AND journey_id = :journey_id
              AND finding_type_code = :finding_type
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "finding_type": _FINDING_TYPE,
        },
    ).mappings().one_or_none()
    if finding is None:
        raise NotFoundError(
            error_code="VAC-NF-040",
            title="Manual verification item not found",
            detail="No manual-verification finding with that id on this journey.",
        )
    if finding["finding_status"] not in ("OPEN", "ACKNOWLEDGED"):
        raise ValidationError(detail="This manual-verification item is already resolved.")

    stage_code = finding["stage_code"]
    document_id = _document_from_rule(finding["rule_key"])

    field_ids = [decision.extractedFieldId for decision in command.decisions]
    owned = {
        row["extracted_field_id"]
        for row in connection.execute(
            text(
                """
                SELECT extracted_field_id
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                  AND stage_code = :stage_code AND di_document_id = :document_id
                  AND extracted_field_id = ANY(:field_ids)
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": stage_code,
                "document_id": document_id,
                "field_ids": field_ids,
            },
        ).mappings()
    }
    missing = [fid for fid in field_ids if fid not in owned]
    if missing:
        raise ValidationError(
            detail=f"{len(missing)} field(s) do not belong to this manual-verification item."
        )

    verified = 0
    for decision in command.decisions:
        if decision.action == "CORRECT":
            if decision.effectiveValue is None or decision.effectiveValue == "":
                raise ValidationError(detail="A corrected value is required for CORRECT decisions.")
            connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_document_extracted_fields
                    SET effective_value = CAST(:value AS jsonb),
                        modified_value = CAST(:value AS jsonb),
                        is_modified = true,
                        modified_by_actor_id = :actor_id,
                        modified_at_utc = now(),
                        reviewed_by_actor_id = :actor_id,
                        reviewed_at_utc = now(),
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id AND extracted_field_id = :field_id
                    """
                ),
                {
                    "value": json.dumps(decision.effectiveValue),
                    "actor_id": actor_id,
                    "tenant_id": tenant_id,
                    "field_id": decision.extractedFieldId,
                },
            )
        else:  # CONFIRM
            connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_document_extracted_fields
                    SET effective_value = COALESCE(effective_value, extracted_value),
                        reviewed_by_actor_id = :actor_id,
                        reviewed_at_utc = now(),
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id AND extracted_field_id = :field_id
                    """
                ),
                {"actor_id": actor_id, "tenant_id": tenant_id, "field_id": decision.extractedFieldId},
            )
        verified += 1

    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, :stage_code,
                'REMARK', :actor_id, 'PC', CAST(:payload AS jsonb), :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "actor_id": actor_id,
            "payload": json.dumps(
                {
                    "manualVerification": True,
                    "fieldsVerified": verified,
                    "corrections": sum(1 for d in command.decisions if d.action == "CORRECT"),
                }
            ),
            "correlation_id": correlation_id,
        },
    )

    sync_manual_verification_findings(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        correlation_id=correlation_id,
    )

    status_row = connection.execute(
        text(
            "SELECT finding_status FROM auditcore.audit_findings "
            "WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id"
        ),
        {"tenant_id": tenant_id, "finding_id": finding_id},
    ).scalar_one()
    stage_open = int(
        connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                  AND stage_code = :stage_code AND finding_type_code = :finding_type
                  AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": stage_code,
                "finding_type": _FINDING_TYPE,
            },
        ).scalar_one()
    )

    return ResolveManualVerificationResponse(
        findingId=finding_id,
        findingStatus=str(status_row),
        fieldsVerified=verified,
        stageManualVerificationOpen=stage_open,
    )


__all__ = ["router", "sync_manual_verification_findings"]
