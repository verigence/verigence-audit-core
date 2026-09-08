"""UC03 confidence-gated DI persistence and Booking review policy.

Business contract:
- every confirmed DI fact is copied into Audit Core immediately;
- only populated facts with confidence below 90% (or unavailable confidence) need PC review;
- Booking may be submitted while DI extraction is still running;
- if extraction finishes before submit, outstanding low-confidence facts block submit;
- if extraction finishes after submit, low-confidence facts raise a non-blocking PC review flag;
- a PC correction after Booking submit raises an INFO finding visible to TL;
- source mismatches remain visible evidence but never create review work by themselves.
"""
from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import Depends, Header, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import Connection, Engine, text

from audit_core import uc03_booking_capture as booking_capture
from audit_core import uc03_booking_review_decisions as booking_review
from audit_core import uc03_document_review_v2 as review_v2
from audit_core import uc03_pc_booking_documents as pc_documents
from audit_core import uc03_review_effective_values as effective_values
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from audit_core.evidence import (
    _external_context_ref,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError
from audit_core.uc03_booking_commands import _aggregate_lock, _parse_if_match
from audit_core.uc03_di_core_persistence import persist_reviewed_di_fields
from audit_core.uc03_finding_classification import resolve_classification
from audit_core.uc03_v2_review_materialization import (
    materialize_reviewed_di_business_values,
    reviewed_field_core_owner,
)

logger = structlog.get_logger(__name__)

REVIEW_THRESHOLD_PERCENT = 90.0
_DI_AUDIENCE = "di"
_REVIEW_FLAG_RULE = "UC03_DI_LOW_CONFIDENCE_POST_SUBMIT"
_TL_CORRECTION_RULE = "UC03_POST_SUBMIT_DI_CORRECTION"


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def requires_pc_review(confidence_score: float | None) -> bool:
    """Return True only when a DI fact cannot satisfy the 90% trust threshold."""

    return confidence_score is None or float(confidence_score) < REVIEW_THRESHOLD_PERCENT


def _field_review_state(
    *,
    value: Any,
    confidence_score: float | None,
) -> str:
    del value
    return "NEEDS_REVIEW" if requires_pc_review(confidence_score) else "READY"


def _strict_confidence_attributes(original: Any):
    def wrapped(*args: Any, **kwargs: Any):
        attributes, unmapped = original(*args, **kwargs)
        for attribute in attributes:
            populated = [source for source in attribute.sources if _has_value(source.value)]
            if not populated:
                continue
            attribute.reviewState = (
                "NEEDS_REVIEW"
                if any(requires_pc_review(source.confidenceScore) for source in populated)
                else "READY"
            )
        return attributes, unmapped

    return wrapped


def _build_raw_review_item(
    review_key: str,
    sources: list[review_v2.ReviewV2UnmappedField],
):
    populated = [source for source in sources if _has_value(source.value)]
    if not populated:
        return None
    selected = min(
        populated,
        key=lambda source: (
            -(source.confidenceScore if source.confidenceScore is not None else -1.0),
            source.documentLabel.casefold(),
            str(source.documentId),
            source.canonicalFieldId,
            source.sourceFactVersion,
        ),
    )
    return booking_review._ReviewItem(
        review_key=review_key,
        review_kind="RAW_FIELD",
        decision_required=any(
            requires_pc_review(source.confidenceScore) for source in populated
        ),
        source_set_ref=booking_review._source_set_ref(sources),
        source_document_id=selected.documentId,
        source_canonical_field_id=selected.canonicalFieldId,
        source_field_key=selected.fieldKey,
        source_fact_version=selected.sourceFactVersion,
    )


def _booking_review_state(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> tuple[bool, str, int]:
    row = connection.execute(
        text(
            """
            SELECT capture_completed_at_utc, pc_verification_status, version_no
            FROM auditcore.journey_stage_states
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return False, "PENDING", 0
    return (
        row["capture_completed_at_utc"] is not None,
        str(row["pc_verification_status"] or "PENDING"),
        int(row["version_no"]),
    )


def _unreviewed_low_confidence_count(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID | None = None,
    stage_code: str = "BOOKING",
) -> int:
    document_clause = "AND di_document_id=:document_id" if document_id is not None else ""
    params: dict[str, Any] = {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code}
    if document_id is not None:
        params["document_id"] = document_id
    return int(
        connection.execute(
            text(
                f"""
                SELECT count(*)
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND stage_code=:stage_code
                  {document_clause}
                  AND reviewed_at_utc IS NULL
                  AND extracted_value IS NOT NULL
                  AND extracted_value <> 'null'::jsonb
                  AND (
                      confidence_score IS NULL
                      OR CASE
                          WHEN confidence_scale='UNIT_INTERVAL' THEN confidence_score * 100
                          ELSE confidence_score
                      END < {REVIEW_THRESHOLD_PERCENT}
                  )
                """
            ),
            params,
        ).scalar_one()
    )


def _pending_proposal_count(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> int:
    return int(
        connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.journey_capture_proposals
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING' AND proposal_status='PENDING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )


def _completion_summary(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    """Business submit waits for low-confidence review, never for DI processing."""

    documents = booking_capture._document_views(connection, tenant_id, journey_id)
    blockers: list[dict[str, str]] = []
    for item in documents:
        if item["applicabilityState"] == "UNRESOLVED":
            blockers.append(
                {
                    "code": "DOCUMENT_APPLICABILITY_PENDING",
                    "label": f"Resolve applicability for {item['requirementKey']}",
                }
            )
            continue
        if (
            item["applicabilityState"] == "APPLICABLE"
            and item["requirementLevel"] in {"REQUIRED", "CONDITIONAL"}
            and not item["evidenceId"]
            and item["requirementStatus"] not in {"SATISFIED", "WAIVED"}
        ):
            blockers.append(
                {
                    "code": "DOCUMENT_REQUIREMENT_PENDING",
                    "label": f"Address {item['requirementKey']}",
                }
            )

    low_confidence = _unreviewed_low_confidence_count(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if low_confidence:
        blockers.append(
            {
                "code": "DI_LOW_CONFIDENCE_REVIEW_REQUIRED",
                "label": (
                    f"Review {low_confidence} DI field"
                    f"{'s' if low_confidence != 1 else ''} below 90% confidence"
                ),
            }
        )

    blocking_flags = int(
        connection.execute(
            text(
                """
                SELECT count(*)
                FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                  AND finding_status IN ('OPEN','ACKNOWLEDGED')
                  AND blocking_completion=true
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )
    if blocking_flags:
        blockers.append(
            {
                "code": "REVIEW_REQUIRED",
                "label": f"{blocking_flags} blocking flag(s) require review",
            }
        )

    pending_proposals = _pending_proposal_count(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    return {
        "ready": not blockers,
        "blockers": blockers,
        "documentCount": len(documents),
        "addressedDocumentCount": sum(
            1
            for item in documents
            if item["applicabilityState"] == "NOT_APPLICABLE"
            or item["evidenceId"]
            or item["requirementStatus"] in {"SATISFIED", "WAIVED", "NOT_APPLICABLE"}
        ),
        # Kept for existing clients as an informational count only.
        "pendingProposalCount": pending_proposals,
        "blockingFlagCount": blocking_flags,
        "lowConfidenceReviewCount": low_confidence,
    }


def _machine_upsert_fact(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    document_id: UUID,
    document_type_key: str | None,
    fact: Any,
    stage_code: str = "BOOKING",
) -> bool:
    canonical = str(fact.canonical_field_id or "").strip()
    if not canonical:
        raise ValueError("DI fact is missing canonical_field_id")
    existing = connection.execute(
        text(
            """
            SELECT extracted_value, confidence_score, confidence_scale
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code AND di_document_id=:document_id
              AND source_canonical_field_id=:canonical
              AND source_fact_version=:fact_version
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "document_id": document_id,
            "canonical": canonical,
            "fact_version": int(fact.version_no),
        },
    ).mappings().one_or_none()
    changed = (
        existing is None
        or existing["extracted_value"] != fact.value
        or (
            float(existing["confidence_score"])
            if existing["confidence_score"] is not None
            else None
        )
        != (float(fact.confidence_score) if fact.confidence_score is not None else None)
        or (existing["confidence_scale"] or None)
        != ("PERCENT" if fact.confidence_score is not None else None)
    )
    payload = json.dumps(fact.value, default=str)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, modified_value, effective_value,
                confidence_score, confidence_scale, is_modified,
                modified_by_actor_id, modified_at_utc,
                reviewed_by_actor_id, reviewed_at_utc
            ) VALUES (
                :tenant_id, :journey_id, :evidence_id, :document_id,
                NULL, :fact_version, :stage_code,
                :document_type_key, :canonical, :field_key,
                CAST(:value AS jsonb), NULL, CAST(:value AS jsonb),
                :confidence, :confidence_scale, false,
                NULL, NULL, NULL, NULL
            )
            ON CONFLICT (
                tenant_id, journey_id, stage_code, di_document_id,
                source_canonical_field_id, source_fact_version
            ) WHERE source_canonical_field_id IS NOT NULL
            DO UPDATE SET
                evidence_id=EXCLUDED.evidence_id,
                source_document_type_key=EXCLUDED.source_document_type_key,
                field_key=EXCLUDED.field_key,
                extracted_value=EXCLUDED.extracted_value,
                effective_value=CASE
                    WHEN auditcore.journey_document_extracted_fields.reviewed_at_utc IS NULL
                    THEN EXCLUDED.effective_value
                    ELSE auditcore.journey_document_extracted_fields.effective_value
                END,
                confidence_score=EXCLUDED.confidence_score,
                confidence_scale=EXCLUDED.confidence_scale,
                updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "evidence_id": evidence_id,
            "document_id": document_id,
            "fact_version": int(fact.version_no),
            "document_type_key": document_type_key,
            "canonical": canonical,
            "field_key": str(fact.field_key),
            "value": payload,
            "confidence": fact.confidence_score,
            "confidence_scale": "PERCENT" if fact.confidence_score is not None else None,
        },
    )
    return changed


def _find_review_flag(
    connection: Connection,
    *,
    tenant_id: str,
    evidence_id: UUID,
) -> UUID | None:
    return connection.execute(
        text(
            """
            SELECT f.audit_finding_id
            FROM auditcore.audit_findings f
            JOIN auditcore.finding_evidence fe
              ON fe.tenant_id=f.tenant_id
             AND fe.audit_finding_id=f.audit_finding_id
            WHERE f.tenant_id=:tenant_id
              AND fe.evidence_id=:evidence_id
              AND f.rule_key=:rule_key
              AND f.finding_status IN ('OPEN','ACKNOWLEDGED')
            ORDER BY f.created_at_utc DESC
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "evidence_id": evidence_id,
            "rule_key": _REVIEW_FLAG_RULE,
        },
    ).scalar_one_or_none()


def _ensure_post_submit_review_flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    document_id: UUID,
    service_id: str,
    low_confidence_count: int,
) -> bool:
    submitted, _, _ = _booking_review_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if not submitted or low_confidence_count <= 0:
        return False
    if _find_review_flag(connection, tenant_id=tenant_id, evidence_id=evidence_id):
        return False

    routing = resolve_classification(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_key=_REVIEW_FLAG_RULE,
        finding_type_code="DOCUMENT_EXCEPTION",
        severity="INFO",
    )
    finding_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity,
                finding_status, title, description,
                created_by_actor_id, stage_code, origin_kind,
                origin_actor_id, origin_role_snapshot, rule_key,
                blocking_completion, finding_class, owner_role_code, sla_due_at_utc
            ) VALUES (
                :tenant_id, :journey_id, 'DOCUMENT_EXCEPTION', 'INFO',
                'OPEN', 'DI extraction requires PC review', :description,
                :service_id, 'BOOKING', 'RULE',
                :service_id, 'SYSTEM', :rule_key, false,
                :finding_class, :owner_role_code, :sla_due_at_utc
            )
            RETURNING audit_finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "description": (
                f"{low_confidence_count} populated DI field"
                f"{'s' if low_confidence_count != 1 else ''} from document {document_id} "
                "are below the 90% confidence threshold and require PC review."
            ),
            "service_id": service_id,
            "rule_key": _REVIEW_FLAG_RULE,
            **routing,
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.finding_evidence (
                tenant_id, audit_finding_id, evidence_id, linkage_purpose
            ) VALUES (:tenant_id, :finding_id, :evidence_id, 'LOW_CONFIDENCE_DI')
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "evidence_id": evidence_id,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, 'BOOKING',
                'RAISED', :service_id, 'SYSTEM', CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "service_id": service_id,
            "payload": json.dumps(
                {
                    "documentId": str(document_id),
                    "lowConfidenceFieldCount": low_confidence_count,
                    "thresholdPercent": REVIEW_THRESHOLD_PERCENT,
                }
            ),
        },
    )
    return True


def _resolve_review_flags(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_ids: set[UUID],
    actor_id: str,
    actor_role: str,
) -> None:
    for evidence_id in evidence_ids:
        if _unreviewed_low_confidence_count(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=connection.execute(
                text(
                    "SELECT di_document_id FROM auditcore.evidence WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id"
                ),
                {"tenant_id": tenant_id, "evidence_id": evidence_id},
            ).scalar_one(),
        ):
            continue
        finding_id = _find_review_flag(
            connection,
            tenant_id=tenant_id,
            evidence_id=evidence_id,
        )
        if finding_id is None:
            continue
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET finding_status='RESOLVED',
                    resolution_reason='PC reviewed low-confidence DI extraction',
                    updated_at_utc=now(), version_no=version_no+1
                WHERE tenant_id=:tenant_id AND audit_finding_id=:finding_id
                """
            ),
            {"tenant_id": tenant_id, "finding_id": finding_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code,
                    event_type, actor_id, actor_role_snapshot, safe_payload
                ) VALUES (
                    :tenant_id, :finding_id, :journey_id, 'BOOKING',
                    'RESOLVED', :actor_id, :actor_role, '{}'::jsonb
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "finding_id": finding_id,
                "journey_id": journey_id,
                "actor_id": actor_id,
                "actor_role": actor_role,
            },
        )


def _raise_tl_correction_info(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
    actor_role: str,
    corrections: list[effective_values.ReviewFieldCorrection],
    evidence_by_document: dict[UUID, UUID | None],
) -> None:
    if not corrections:
        return
    field_keys = sorted({correction.fieldKey for correction in corrections})
    routing = resolve_classification(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        rule_key=_TL_CORRECTION_RULE,
        finding_type_code="DOCUMENT_EXCEPTION",
        severity="INFO",
    )
    finding_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity,
                finding_status, title, description,
                created_by_actor_id, stage_code, origin_kind,
                origin_actor_id, origin_role_snapshot, rule_key,
                blocking_completion, finding_class, owner_role_code, sla_due_at_utc
            ) VALUES (
                :tenant_id, :journey_id, 'DOCUMENT_EXCEPTION', 'INFO',
                'OPEN', 'PC corrected DI data after Booking submission', :description,
                :actor_id, 'BOOKING', 'HUMAN',
                :actor_id, :actor_role, :rule_key, false,
                :finding_class, :owner_role_code, :sla_due_at_utc
            )
            RETURNING audit_finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "description": (
                f"PC corrected {len(corrections)} DI field"
                f"{'s' if len(corrections) != 1 else ''} after Booking submission. "
                f"Fields: {', '.join(field_keys)}. Original DI values remain preserved in Audit Core."
            ),
            "actor_id": actor_id,
            "actor_role": actor_role,
            "rule_key": _TL_CORRECTION_RULE,
            **routing,
        },
    ).scalar_one()
    for evidence_id in sorted(
        {
            evidence_by_document.get(correction.documentId)
            for correction in corrections
            if evidence_by_document.get(correction.documentId) is not None
        },
        key=str,
    ):
        connection.execute(
            text(
                """
                INSERT INTO auditcore.finding_evidence (
                    tenant_id, audit_finding_id, evidence_id, linkage_purpose
                ) VALUES (:tenant_id, :finding_id, :evidence_id, 'POST_SUBMIT_PC_CORRECTION')
                """
            ),
            {
                "tenant_id": tenant_id,
                "finding_id": finding_id,
                "evidence_id": evidence_id,
            },
        )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload
            ) VALUES (
                :tenant_id, :finding_id, :journey_id, 'BOOKING',
                'RAISED', :actor_id, :actor_role, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "journey_id": journey_id,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "payload": json.dumps(
                {
                    "correctedFieldCount": len(corrections),
                    "fieldKeys": field_keys,
                    "severity": "INFO",
                }
            ),
        },
    )


def _refresh_stage_audit_status(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> None:
    open_findings = int(
        connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                  AND finding_status IN ('OPEN','ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET audit_status=:audit_status,
                audit_state=CASE
                    WHEN business_status='BOOKING_CLOSED' AND :open_findings=0 THEN 'COMPLETE'
                    WHEN :open_findings>0 THEN 'IN_PROGRESS'
                    ELSE audit_state
                END,
                latest_activity_at_utc=now(), updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "open_findings": open_findings,
            "audit_status": "FLAGS_RAISED" if open_findings else "NO_FLAGS",
        },
    )


def _sync_booking_document(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    service_id: str,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
    bump_version: bool,
    stage_code: str = "BOOKING",
) -> int:
    """The one per-document sync pipeline: DI status -> DOCUMENT_MISSING ->
    durable fact copy -> MANUAL_VERIFICATION -> stage-specific rules/triggers
    (SKU resolution is Booking-only; payment reconciliation and canonical
    materialization apply to both). ``stage_code`` is read off the calling
    requirement's process_area, never assumed -- Booking and Delivery run the
    identical pipeline; the only branches are ones a document type genuinely
    doesn't apply to (a Delivery document has no vehicle model to resolve a
    SKU against)."""
    link = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.document_type_key, e.association_status,
                   j.customer_id
            FROM auditcore.evidence e
            JOIN auditcore.journeys j
              ON j.tenant_id=e.tenant_id AND j.journey_id=e.journey_id
            WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
              AND e.di_document_id=:document_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
        },
    ).mappings().one_or_none()
    if link is None or str(link["association_status"]) != "ACTIVE":
        return 0

    try:
        token = security_client.get_service_token(audience=_DI_AUDIENCE)
        context_ref = _external_context_ref(
            journey_id=journey_id,
            customer_id=link["customer_id"],
        )
        document = di_client.get_audit_document(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
    except (SecurityTokenError, DiClientError) as exc:
        raise DependencyUnavailableError(
            detail="Document extraction synchronization is temporarily unavailable."
        ) from exc

    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET processing_status_cache=:processing,
                verification_status_cache=:verification,
                confirmation_status_cache=:confirmation,
                cache_updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "evidence_id": link["evidence_id"],
            "processing": document.processing_status,
            "verification": document.verification_state,
            "confirmation": document.confirmation_status,
        },
    )

    from audit_core.uc03_async_sync_tasks import sync_document_confirmation_status
    from audit_core.uc03_manual_verification import _friendly_label

    sync_document_confirmation_status(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        document_id=document_id,
        confirmation_status=document.confirmation_status,
        document_label=_friendly_label(
            document.document_type_key or link["document_type_key"], document_id
        ),
        correlation_id="",
    )

    if str(document.confirmation_status or "").upper() != "CONFIRMED":
        return 0

    try:
        facts = di_client.get_audit_document_facts(
            token=token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(document_id),
        )
    except DiClientError as exc:
        raise DependencyUnavailableError(
            detail="Document extraction facts are temporarily unavailable."
        ) from exc

    document_type_key = (
        str(document.document_type_key).strip().lower()
        if document.document_type_key
        else str(link["document_type_key"] or "").strip().lower() or None
    )
    changed = False
    for fact in facts:
        changed = _machine_upsert_fact(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=link["evidence_id"],
            document_id=document_id,
            document_type_key=document_type_key,
            fact=fact,
            stage_code=stage_code,
        ) or changed

    low_count = _unreviewed_low_confidence_count(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        document_id=document_id,
        stage_code=stage_code,
    )
    # The post-submit "DI extraction requires PC review" INFO flag is a
    # Booking-only mechanism today (_ensure_post_submit_review_flag reads
    # Booking's own review-state table); Delivery's equivalent doesn't exist
    # yet, so this stays gated rather than silently mis-checking Booking's
    # submission state against a Delivery document.
    finding_created = False
    if stage_code == "BOOKING":
        finding_created = _ensure_post_submit_review_flag(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=link["evidence_id"],
            document_id=document_id,
            service_id=service_id,
            low_confidence_count=low_count,
        )

    if changed or finding_created:
        status = "PENDING" if low_count else None
        connection.execute(
            text(
                f"""
                UPDATE auditcore.journey_stage_states
                SET pc_verification_status=COALESCE(:verification_status, pc_verification_status),
                    audit_state=CASE
                        WHEN :low_count > 0 THEN 'IN_PROGRESS'
                        ELSE audit_state
                    END,
                    audit_status=CASE
                        WHEN :low_count > 0 THEN 'FLAGS_RAISED'
                        ELSE audit_status
                    END,
                    latest_activity_at_utc=now(), updated_at_utc=now()
                    {', version_no=version_no+1' if bump_version else ''}
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code=:stage_code
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "stage_code": stage_code,
                "verification_status": status,
                "low_count": low_count,
            },
        )

    # Surface the low-confidence fields as a per-document MANUAL_VERIFICATION
    # finding the PC can work straight from the Review Queue (never raises).
    from audit_core.uc03_manual_verification import sync_manual_verification_findings

    sync_manual_verification_findings(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        correlation_id="",
    )

    from audit_core.uc03_async_sync_tasks import reconcile_payments_with_escalation

    if stage_code == "BOOKING":
        # Resolve the booking's SKU against the OEM price masters, or raise a
        # MODEL_NOT_IDENTIFIED finding for the PC (never raises). Booking-only
        # -- a Delivery document has no vehicle model to resolve a SKU against.
        # A repeated internal failure (as opposed to the expected 0/many-match
        # outcome, which MODEL_NOT_IDENTIFIED already covers) escalates too.
        from audit_core.uc03_async_sync_tasks import (
            sync_model_resolution_with_escalation,
        )

        sync_model_resolution_with_escalation(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            correlation_id="",
        )

    # A receipt or bank statement just confirming is exactly when a fresh
    # reconciliation pass has something new to match -- run it here instead
    # of waiting for PC Verify/Submit or the next Overview read. Scoped to
    # these document types so unrelated documents (PAN, RTO, insurance...)
    # don't pay for a no-op reconciliation pass. Booking's receipt document
    # type is dealer_receipt; Delivery's is payment_receipt (0017/0022).
    if document_type_key in ("dealer_receipt", "payment_receipt", "bank_statement_extract"):
        reconcile_payments_with_escalation(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=stage_code,
            correlation_id="",
        )

    if stage_code == "DELIVERY" and changed:
        from audit_core.uc03_delivery_post_extraction_materialization import (
            materialize_delivery_documents_from_durable_store,
        )

        materialize_delivery_documents_from_durable_store(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )

    return len(facts)


def acknowledge_booking_document_link_with_auto_sync(
    payload: pc_documents.BookingDocumentLinkCommand,
    service_principal: Annotated[
        Any,
        Depends(pc_documents.require_audit_service_principal),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> pc_documents.BookingDocumentLinkResponse:
    response = pc_documents.acknowledge_booking_document_link(
        payload=payload,
        service_principal=service_principal,
        connection=connection,
    )
    discovered = pc_documents._discover_requirement_for_callback(
        connection,
        service_id=service_principal.subject,
        requirement_ref=payload.requirementRef,
    )
    # One pipeline, stage as data: this callback accepts Booking and Delivery
    # requirements alike (the requirement row says which -- see migration
    # 0068), and _sync_booking_document runs the identical sync for either --
    # DI status -> DOCUMENT_MISSING -> durable fact copy -> MANUAL_VERIFICATION
    # -> stage-appropriate rules (SKU resolution is Booking-only; payment
    # reconciliation and canonical materialization run for both).
    _sync_booking_document(
        connection,
        tenant_id=str(discovered["tenant_id"]),
        journey_id=discovered["journey_id"],
        document_id=payload.documentId,
        service_id=service_principal.subject,
        security_client=security_client,
        di_client=di_client,
        bump_version=True,
        stage_code=str(discovered["process_area"]).upper(),
    )
    return response


def get_booking_review_v2_confidence_policy(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ],
) -> review_v2.BookingReviewV2Response:
    booking_review._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    submitted, verification_status, aggregate_version = _booking_review_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    requirements, documents, attributes, unmapped = review_v2._booking_review_data(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )
    items = booking_review._current_review_items(attributes, unmapped)
    needs_review = sum(1 for item in items.values() if item.decision_required)

    # Existing Web clients historically turned MISMATCH into mandatory work. Keep
    # mismatch evidence in Audit/source-comparison, but do not expose it as a
    # Booking-review gate when every source is at/above the confidence threshold.
    for attribute in attributes:
        item = items.get(f"attribute:{attribute.attributeKey}")
        if (
            item is not None
            and not item.decision_required
            and attribute.comparisonState == "MISMATCH"
        ):
            attribute.comparisonState = "SINGLE_SOURCE"

    actual_pending = any(document.extractionState == "PENDING" for document in documents)
    return review_v2.BookingReviewV2Response(
        journeyId=journey_id,
        captureSubmitted=submitted,
        pcVerificationStatus=verification_status,
        aggregateVersion=aggregate_version,
        # A current low-confidence exception must be reviewable even while other
        # documents continue processing in the background.
        processingPending=actual_pending and needs_review == 0,
        needsReviewCount=needs_review,
        attributes=attributes,
        unmappedFields=unmapped,
        documents=documents,
        missingDeclarations=review_v2._missing_declarations(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirements=requirements,
        ),
    )


def set_booking_review_decision_confidence_policy(
    tenant_id: str,
    journey_id: UUID,
    payload: booking_review.BookingReviewDecisionCommand,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[review_v2.DiClient, Depends(get_di_client)],
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ],
) -> booking_review.BookingReviewDecision:
    booking_review._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _, verification_status, _ = _booking_review_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    if verification_status != "PENDING":
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Booking Review is not pending",
            detail="Review decisions can be recorded only while a low-confidence exception is pending.",
        )

    _, _, attributes, unmapped = review_v2._booking_review_data(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )
    item = booking_review._current_review_items(attributes, unmapped).get(payload.reviewKey)
    if item is None:
        raise NotFoundError(
            error_code="VAC-NOTFOUND-001",
            title="Review item not found",
            detail="The extracted review item is no longer available. Refresh Review.",
        )
    if not item.decision_required:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Review decision is not required",
            detail="This DI value is at or above 90% confidence and needs no PC decision.",
        )
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_attribute_review_decisions (
                tenant_id, journey_id, stage_code, review_key, review_kind,
                decision, source_set_ref, source_di_document_id,
                source_canonical_field_id, source_field_key, source_fact_version,
                decided_by_actor_id, decided_at_utc, updated_at_utc
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING', :review_key, :review_kind,
                :decision, :source_set_ref, :source_di_document_id,
                :source_canonical_field_id, :source_field_key, :source_fact_version,
                :actor_id, now(), now()
            )
            ON CONFLICT (tenant_id, journey_id, stage_code, review_key)
            DO UPDATE SET
                review_kind=EXCLUDED.review_kind,
                decision=EXCLUDED.decision,
                source_set_ref=EXCLUDED.source_set_ref,
                source_di_document_id=EXCLUDED.source_di_document_id,
                source_canonical_field_id=EXCLUDED.source_canonical_field_id,
                source_field_key=EXCLUDED.source_field_key,
                source_fact_version=EXCLUDED.source_fact_version,
                decided_by_actor_id=EXCLUDED.decided_by_actor_id,
                decided_at_utc=now(), updated_at_utc=now()
            RETURNING review_key, review_kind, decision, source_set_ref,
                      source_di_document_id, source_canonical_field_id,
                      source_field_key, source_fact_version, decided_by_actor_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "review_key": item.review_key,
            "review_kind": item.review_kind,
            "decision": payload.decision,
            "source_set_ref": item.source_set_ref,
            "source_di_document_id": item.source_document_id,
            "source_canonical_field_id": item.source_canonical_field_id,
            "source_field_key": item.source_field_key,
            "source_fact_version": item.source_fact_version,
            "actor_id": human_principal.subject,
        },
    ).mappings().one()
    return booking_review._decision_model(dict(row))


def confirm_booking_review_v2_confidence_policy(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[review_v2.DiClient, Depends(get_di_client)],
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ],
    payload: effective_values.ReviewConfirmCommand | None = None,
) -> booking_review.BookingReviewV2ConfirmWithDecisionsResponse:
    context = booking_review._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    _, documents, attributes, unmapped = review_v2._booking_review_data(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )
    items = booking_review._current_review_items(attributes, unmapped)
    decisions = booking_review._current_decisions(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        items=items,
    )
    required_keys = sorted(
        item.review_key for item in items.values() if item.decision_required
    )
    missing_keys = [key for key in required_keys if key not in decisions]
    if missing_keys:
        raise ConflictError(
            error_code="VAC-CONFLICT-012",
            title="Review decisions are pending",
            detail=(
                f"{len(missing_keys)} low-confidence extracted value"
                f"{'s' if len(missing_keys) != 1 else ''} still require Accept or Reject."
            ),
        )

    rejected_keys = {
        key for key, decision in decisions.items() if decision == "REJECTED"
    }
    command = payload or effective_values.ReviewConfirmCommand()
    corrections = effective_values._correction_map(documents, command.corrections)
    effective_values._validate_mapped_corrections(attributes, corrections)
    corrected_documents = effective_values._corrected_documents(documents, corrections)
    corrected_attributes = effective_values._corrected_attributes(attributes, corrections)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = connection.execute(
            text(
                """
                SELECT capture_completed_at_utc, pc_verification_status, version_no
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        if state is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking has not been started",
                detail="Start Booking before completing DI exception review.",
            )
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since Review was loaded. Refresh Review and try again.",
            )
        if str(state["pc_verification_status"] or "PENDING") != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking Review is not pending",
                detail="There is no pending low-confidence Booking exception to confirm.",
            )
        submitted_before = state["capture_completed_at_utc"] is not None

        stored_field_count = persist_reviewed_di_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            actor_id=human_principal.subject,
            fields=effective_values._reviewed_fields(
                documents,
                corrections,
                rejected_keys=rejected_keys,
            ),
        )

        applied: list[str] = []
        conflicts: list[str] = []
        rejected_attributes: list[str] = []
        resolved_count = 0
        for attribute in corrected_attributes:
            source = attribute.resolvedSource
            if source is None or attribute.resolvedValue is None:
                continue
            review_key = f"attribute:{attribute.attributeKey}"
            if review_key in rejected_keys:
                rejected_attributes.append(attribute.attributeKey)
                continue
            spec = review_v2.spec_for_field(source.fieldKey)
            if spec is None or spec.attribute_key != attribute.attributeKey:
                raise booking_review._missing_core_owner_error(
                    field_key=source.fieldKey,
                    document_type_key=source.documentTypeKey,
                    attribute_key=attribute.attributeKey,
                )
            resolved_count += 1
            application = review_v2.apply_supported_operational_attribute(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                spec=spec,
                value=attribute.resolvedValue,
                actor_id=human_principal.subject,
                source_document_type_key=source.documentTypeKey,
                source_field_key=source.fieldKey,
                source_evidence_id=source.evidenceId,
            )
            owning_domain_key: str | None = None
            owning_record_reference: str | None = None
            if application is None:
                typed_owner = reviewed_field_core_owner(
                    document_type_key=source.documentTypeKey,
                    field_key=source.fieldKey,
                    document_id=source.documentId,
                )
                if typed_owner is not None:
                    owning_domain_key, owning_record_reference = typed_owner
            else:
                owning_domain_key, owning_record_reference, application_status = application
                if application_status == "CONFLICT":
                    conflicts.append(attribute.attributeKey)
            applied.append(attribute.attributeKey)
            if spec.mapping_status == "SUPPORTED":
                review_v2.record_attribute_resolution(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code="BOOKING",
                    spec=spec,
                    source_di_document_id=source.documentId,
                    source_evidence_id=source.evidenceId,
                    source_canonical_field_id=source.canonicalFieldId,
                    source_field_key=source.fieldKey,
                    source_fact_version=source.sourceFactVersion,
                    source_document_type_key=source.documentTypeKey,
                    actor_id=human_principal.subject,
                    owning_domain_key=owning_domain_key,
                    owning_record_reference=owning_record_reference,
                )

        materialization = materialize_reviewed_di_business_values(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=corrected_documents,
            rejected_review_keys=rejected_keys,
            actor_id=human_principal.subject,
        )

        evidence_ids = {
            document.evidenceId
            for document in documents
            if document.evidenceId is not None
        }
        _resolve_review_flags(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_ids=evidence_ids,
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
        )
        if submitted_before and command.corrections:
            _raise_tl_correction_info(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                actor_id=human_principal.subject,
                actor_role=context["operating_role"],
                corrections=command.corrections,
                evidence_by_document={
                    document.documentId: document.evidenceId for document in documents
                },
            )
        _refresh_stage_audit_status(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )

        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET pc_verification_status='VERIFIED',
                    latest_activity_at_utc=now(), updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        booking_review._append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=f"{idempotency_key}:review-confirmed",
            correlation_id=correlation_id,
            safe_payload={
                "thresholdPercent": REVIEW_THRESHOLD_PERCENT,
                "resolvedAttributeCount": resolved_count,
                "storedFieldCount": stored_field_count,
                "modifiedFieldCount": len(command.corrections),
                "appliedAttributeKeys": sorted(applied),
                "conflictAttributeKeys": sorted(conflicts),
                "rejectedReviewKeys": sorted(rejected_keys),
                "bookingAlreadySubmitted": submitted_before,
                **materialization,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "pcVerificationStatus": "VERIFIED",
            "aggregateVersion": next_version,
            "resolvedAttributeCount": resolved_count,
            "appliedAttributes": sorted(applied),
            "conflictAttributes": sorted(conflicts),
            "rejectedAttributes": sorted(rejected_attributes),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.attribute-review.confirm:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "corrections": command.model_dump(mode="json")["corrections"],
        },
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return booking_review.BookingReviewV2ConfirmWithDecisionsResponse.model_validate(body)


def close_booking_ready_confidence_policy(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[DiClient, Depends(get_di_client)],
):
    # Close the extraction-completion race: facts that are already confirmed in DI
    # are copied to Core before the submit gate is evaluated. Pending/unavailable DI
    # remains non-blocking and will be delivered through the durable callback later.
    booking_review._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    document_ids = connection.execute(
        text(
            """
            SELECT e.di_document_id
            FROM auditcore.evidence e
            LEFT JOIN auditcore.journey_document_requirements jdr
              ON jdr.tenant_id=e.tenant_id
             AND jdr.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
              AND e.association_status='ACTIVE'
              AND e.di_document_id IS NOT NULL
              AND (jdr.process_area IS NULL OR upper(jdr.process_area)='BOOKING')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    for document_id in document_ids:
        try:
            _sync_booking_document(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                document_id=document_id,
                service_id="audit-core",
                security_client=security_client,
                di_client=di_client,
                bump_version=False,
            )
        except DependencyUnavailableError:
            logger.warning(
                "uc03_booking_pre_submit_di_sync_unavailable",
                tenant_id=tenant_id,
                journey_id=str(journey_id),
                document_id=str(document_id),
            )
    return booking_capture.close_booking_ready(
        tenant_id=tenant_id,
        journey_id=journey_id,
        request=request,
        response=response,
        idempotency_key=idempotency_key,
        if_match=if_match,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )


def _replace_route(
    router: Any,
    *,
    suffix: str,
    method: str,
    endpoint: Any,
    response_model: Any,
) -> None:
    retained = []
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path.endswith(suffix)
            and method in route.methods
        ):
            continue
        retained.append(route)
    router.routes[:] = retained
    router.add_api_route(
        suffix,
        endpoint,
        methods=[method],
        response_model=response_model,
    )


def install_uc03_confidence_review_policy() -> None:
    """Install the 90% exception-only policy after the existing UC03 Review stack."""

    if getattr(review_v2, "_confidence_review_policy_installed", False):
        return

    review_v2._REVIEW_THRESHOLD = REVIEW_THRESHOLD_PERCENT
    review_v2._field_review_state = _field_review_state  # type: ignore[assignment]
    review_v2._build_attributes = _strict_confidence_attributes(  # type: ignore[assignment]
        review_v2._build_attributes
    )
    booking_review._build_raw_review_item = _build_raw_review_item  # type: ignore[assignment]
    booking_capture._completion_summary = _completion_summary  # type: ignore[assignment]

    _replace_route(
        review_v2.router,
        suffix="/booking/review",
        method="GET",
        endpoint=get_booking_review_v2_confidence_policy,
        response_model=review_v2.BookingReviewV2Response,
    )
    _replace_route(
        review_v2.router,
        suffix="/booking/review/decision",
        method="POST",
        endpoint=set_booking_review_decision_confidence_policy,
        response_model=booking_review.BookingReviewDecision,
    )
    _replace_route(
        review_v2.router,
        suffix="/booking/review/confirm",
        method="POST",
        endpoint=confirm_booking_review_v2_confidence_policy,
        response_model=booking_review.BookingReviewV2ConfirmWithDecisionsResponse,
    )
    _replace_route(
        pc_documents.router,
        suffix="/v1/internal/di/booking-document-links",
        method="POST",
        endpoint=acknowledge_booking_document_link_with_auto_sync,
        response_model=pc_documents.BookingDocumentLinkResponse,
    )
    _replace_route(
        booking_capture.router,
        suffix="/booking/close-ready",
        method="POST",
        endpoint=close_booking_ready_confidence_policy,
        response_model=booking_capture.BookingCommandResponse,
    )
    review_v2._confidence_review_policy_installed = True
