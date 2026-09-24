"""uc03_requirement_satisfaction.py — the one canonical answer to "is this
document requirement satisfied, and if not, why."

Confirmed live (2026-09-24): this exact fact was independently computed in
at least five places (the capture-v2 checklist, _delivery_audit_gaps'
now-dead journey_document_assessments read, Journey Overview's own dedup
heuristic, and two frontend derivations on top of that), while the column
everyone assumed was the real source of truth
(journey_document_requirements.requirement_status) has at least 7
uncoordinated writers and is never actually transitioned to SATISFIED for
Booking anywhere in the codebase.

This module is not a new source of truth -- it is capture-v2's own,
already-correct satisfaction logic (uc03_document_capture_v2.
_build_capture_response / uc03_delivery_capture_v2.
_build_delivery_capture_response), extracted so every other consumer reads
it instead of recomputing its own version. Computed live, on every call,
directly from document_capture_v2_documents -- no cached column, so there
is nothing for a concurrent upload batch to race on or leave stale.

stage_code is a plain parameter here, never a fork: requirements_for_journey
and linked_documents_for_journey are the same query verigence-audit-core
already ran twice (once per stage, byte-identical except the stage
literal) -- this module runs it once, parametrized, matching the pattern
uc03_document_review_v2.py already proves works for BOOKING, DELIVERY, and
both at once.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.telemetry import record_metric

logger = structlog.get_logger(__name__)

SatisfactionReason = Literal["SATISFIED", "NOT_APPLICABLE", "NOT_YET_CLASSIFIED", "NO_DOCUMENT"]


@dataclass(frozen=True)
class RequirementSatisfaction:
    requirement_key: str
    stage_code: str
    requirement_level: str
    display_label: str
    document_type_key: str
    condition_key: str | None
    satisfied: bool
    reason: SatisfactionReason
    active_document_id: UUID | None
    active_document: dict[str, Any] | None


def requirements_for_journey(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> list[dict[str, Any]]:
    """Requirement catalog rows for one stage. Byte-identical query shape to
    the pre-unification uc03_document_capture_v2._base_requirements /
    uc03_delivery_capture_v2._delivery_requirements -- stage_code is now a
    bound parameter instead of a literal duplicated in two files.
    """
    rows = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id AS requirement_ref,
                   jdr.requirement_key, jdr.document_type_key,
                   jdr.requirement_level, jdr.requirement_status,
                   COALESCE(p.display_label, jdr.requirement_key) AS display_label,
                   COALESCE(p.condition_key, jdr.condition_snapshot->>'conditionKey') AS condition_key,
                   COALESCE(p.sort_order, dri.sort_order, 999999) AS sort_order
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=jdr.tenant_id
             AND dri.document_requirement_item_id=jdr.document_requirement_item_id
            LEFT JOIN auditcore.document_capture_v2_requirement_policy p
              ON p.requirement_key=jdr.requirement_key
             AND p.process_area=:stage_code
             AND p.is_active=true
            WHERE jdr.tenant_id=:tenant_id
              AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)=:stage_code
            ORDER BY COALESCE(p.sort_order, dri.sort_order, 999999), jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().all()
    requirements = [dict(row) for row in rows]

    extensions = connection.execute(
        text(
            """
            SELECT NULL::uuid AS requirement_ref,
                   requirement_key,
                   extension_document_type_key AS document_type_key,
                   extension_requirement_level AS requirement_level,
                   'PENDING' AS requirement_status,
                   display_label, condition_key, sort_order
            FROM auditcore.document_capture_v2_requirement_policy
            WHERE process_area=:stage_code AND is_active=true AND is_extension=true
            ORDER BY sort_order, requirement_key
            """
        ),
        {"stage_code": stage_code},
    ).mappings().all()
    existing = {row["requirement_key"] for row in requirements}
    requirements.extend(dict(row) for row in extensions if row["requirement_key"] not in existing)
    requirements.sort(key=lambda row: (int(row.get("sort_order") or 999999), row["requirement_key"]))
    return requirements


def linked_documents_for_journey(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
) -> list[dict[str, Any]]:
    """document_capture_v2_documents rows for one stage. Byte-identical to
    the pre-unification uc03_document_capture_v2._linked_documents /
    uc03_delivery_capture_v2._linked_delivery_documents.
    """
    rows = connection.execute(
        text(
            """
            SELECT di_document_id, client_upload_id, requirement_key,
                   classified_document_type_key, capture_status,
                   original_filename, content_type, created_at_utc
            FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code=:stage_code AND capture_status <> 'SUPERSEDED'
            ORDER BY created_at_utc, di_document_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "stage_code": stage_code},
    ).mappings().all()
    return [dict(row) for row in rows]


def first_linked_document_ids(
    documents: list[dict[str, Any]],
    *,
    is_repeatable: Callable[[str], bool],
) -> dict[str, UUID]:
    """Which document (by upload order, i.e. created_at_utc -- callers
    already sort their query this way) currently occupies each
    non-repeatable requirement's slot, regardless of classification status.

    Deliberately distinct from resolve_requirement_satisfaction's own
    "active document" concept: satisfaction requires the document to be
    CLASSIFIED, but this is a plain occupancy rule -- a single not-yet-
    classified upload still owns its slot, it just isn't satisfied yet.
    This is exactly the "first physical upload wins" rule
    uc03_journey_overview_projection.py's own dedup nulling already
    implements; extracted here so it isn't re-derived a second way.

    is_repeatable is injected rather than imported directly so this module
    stays free of uc03_pc_booking_documents.py's much heavier dependency
    chain -- every current caller already imports it for its own use.
    """
    first_seen: dict[str, UUID] = {}
    for doc in documents:
        key = doc.get("requirement_key") or doc.get("requirementKey")
        if not key or is_repeatable(str(key)):
            continue
        key = str(key)
        if key in first_seen:
            continue
        doc_id = doc.get("di_document_id") or doc.get("documentId")
        if doc_id is not None:
            first_seen[key] = UUID(str(doc_id))
    return first_seen


def resolve_requirement_satisfaction(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    requirements: list[dict[str, Any]] | None = None,
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, RequirementSatisfaction]:
    """The one canonical fact this whole module exists for. Reuses the exact
    "first classified document by created_at_utc wins the requirement slot"
    rule already proven in uc03_document_capture_v2._build_capture_response
    / uc03_delivery_capture_v2._build_delivery_capture_response (each built
    it via an ordered query + dict.setdefault; this reimplements that
    identical behavior explicitly, in one place, so every caller gets the
    same answer instead of a second, differently-reasoned heuristic).

    requirements/documents are optional pre-fetched inputs so a caller that
    already queried them (the capture-v2 checklist itself) doesn't pay for
    a second round trip; a caller with neither fetches both itself.
    """
    started = time.perf_counter()
    try:
        if requirements is None:
            requirements = requirements_for_journey(
                connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code,
            )
        if documents is None:
            documents = linked_documents_for_journey(
                connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage_code,
            )

        active_by_requirement: dict[str, dict[str, Any]] = {}
        any_document_for_key: set[str] = set()
        for doc in documents:
            key = doc.get("requirement_key")
            if not key:
                continue
            key = str(key)
            any_document_for_key.add(key)
            if str(doc.get("capture_status") or "").upper() != "CLASSIFIED":
                continue
            active_by_requirement.setdefault(key, doc)

        result: dict[str, RequirementSatisfaction] = {}
        for requirement in requirements:
            key = str(requirement["requirement_key"])
            status = str(requirement.get("requirement_status") or "PENDING").upper()
            active = active_by_requirement.get(key)
            reason: SatisfactionReason
            if status == "NOT_APPLICABLE":
                satisfied, reason = True, "NOT_APPLICABLE"
            elif active is not None:
                satisfied, reason = True, "SATISFIED"
            elif key in any_document_for_key:
                satisfied, reason = False, "NOT_YET_CLASSIFIED"
            else:
                satisfied, reason = False, "NO_DOCUMENT"
            result[key] = RequirementSatisfaction(
                requirement_key=key,
                stage_code=stage_code,
                requirement_level=str(requirement["requirement_level"]),
                display_label=str(requirement["display_label"]),
                document_type_key=str(requirement["document_type_key"]),
                condition_key=(
                    str(requirement["condition_key"]) if requirement.get("condition_key") else None
                ),
                satisfied=satisfied,
                reason=reason,
                active_document_id=(
                    UUID(str(active["di_document_id"])) if active is not None else None
                ),
                active_document=active,
            )
        return result
    except Exception:
        logger.warning(
            "uc03_resolve_requirement_satisfaction_failed",
            tenant_id=tenant_id,
            journey_id=str(journey_id),
            stage_code=stage_code,
            exc_info=True,
        )
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000.0
        record_metric(
            "audit_core.uc03_resolve_requirement_satisfaction.duration_ms",
            duration_ms,
            kind="histogram",
            labels={"stage_code": stage_code},
        )
