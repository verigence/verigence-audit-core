"""STATEMENT_COPY_RESTORE: the NDC and cost-sheet pages a newer page hid (2026-11-02).

Until 2026-11-02 the NDC (customer_ledger) and cost-sheet requirements kept
one document: each new page replaced the last one filed. A page of a
two-page NDC, the NDC beside the Tally customer ledger (both read as DI
customer_ledger), an offer letter beside the final statement -- each hid the
other (``SUPERSEDED`` / ``REPLACED_BY_NEWER_UPLOAD``). The requirements now
keep every copy and a new reading replaces only a reading of the same page
(uc03_pc_booking_documents._same_page_copy); this job brings back what the old
rule hid, by the same rule, for the one tenant the tag names:

* a superseded copy is brought back when it is a page of its own: no active
  copy of that type covers its page (the same upload's same page, or the same
  page image);
* of several superseded readings of one page, the newest is the one brought
  back;
* a copy whose reading is identical to an active copy's (the same page on
  file twice) stays as it is -- it adds nothing.

Each booking touched is re-materialised and recomputed, so the statement sync
reads every copy (uc03_account_statement.combine_statements). Tag
``<tenant_id>:<label>``: a DRY RUN that lists what would come back unless the
label ends with ``-commit``. Idempotent; never deletes.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

TYPES = ("customer_ledger", "cost_sheet")
_ITEMS_REPORTED = 400


def _documents(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT e.journey_id, j.journey_reference, e.evidence_id, e.di_document_id, e.document_type_key,
                   e.association_status, e.process_area, e.linked_at_utc,
                   ARRAY(SELECT DISTINCT q.batch_id::text || ':' || p
                         FROM auditcore.p2_document_queue q
                         CROSS JOIN LATERAL unnest(COALESCE(q.page_numbers, ARRAY[q.page_number])) p
                         WHERE q.tenant_id=e.tenant_id AND q.di_document_id=e.di_document_id) AS pages,
                   ARRAY(SELECT DISTINCT q.page_sha256 FROM auditcore.p2_document_queue q
                         WHERE q.tenant_id=e.tenant_id AND q.di_document_id=e.di_document_id
                           AND q.page_sha256 IS NOT NULL) AS images,
                   (SELECT jsonb_object_agg(f.field_key, f.effective_value)
                    FROM auditcore.journey_document_extracted_fields f
                    WHERE f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id
                      AND f.di_document_id=e.di_document_id AND f.effective_value IS NOT NULL) AS fields
            FROM auditcore.evidence e
            JOIN auditcore.journeys j ON j.tenant_id=e.tenant_id AND j.journey_id=e.journey_id
            WHERE e.tenant_id=:t AND e.document_type_key = ANY(:types)
              AND (e.association_status='ACTIVE'
                   OR (e.association_status='SUPERSEDED' AND e.void_reason='REPLACED_BY_NEWER_UPLOAD'))
            ORDER BY e.journey_id, e.document_type_key, e.linked_at_utc, e.evidence_id
            """
        ),
        {"t": tenant_id, "types": list(TYPES)},
    ).mappings().all()
    return [dict(r) for r in rows]


def _reading(document: dict[str, Any]) -> str:
    fields = document.get("fields") or {}
    if isinstance(fields, str):
        fields = json.loads(fields)
    return json.dumps(fields, sort_keys=True, default=str)


def _kind(document: dict[str, Any]) -> str:
    if document["document_type_key"] != "customer_ledger":
        return "COST_SHEET"
    from audit_core.uc03_account_statement import line_rows, statement_kind

    fields = document.get("fields") or {}
    if isinstance(fields, str):
        fields = json.loads(fields)
    return statement_kind(line_rows(fields.get("line_items")))


def to_restore(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The superseded copies that are a page of their own (module docstring)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for d in documents:
        groups.setdefault((str(d["journey_id"]), d["document_type_key"]), []).append(d)
    out: list[dict[str, Any]] = []
    for members in groups.values():
        active = [d for d in members if d["association_status"] == "ACTIVE"]
        covered = {p for d in active for p in (d["pages"] or [])}
        images = {i for d in active for i in (d["images"] or [])}
        readings = {_reading(d) for d in active}
        # newest first, so the newest reading of a page is the one considered
        hidden = sorted((d for d in members if d["association_status"] == "SUPERSEDED"),
                        key=lambda d: (str(d["linked_at_utc"]), str(d["evidence_id"])), reverse=True)
        for d in hidden:
            pages = set(d["pages"] or [])
            if not pages or pages & covered or set(d["images"] or []) & images:
                continue  # a reading of a page already on file, or a page not known
            covered |= pages
            images |= set(d["images"] or [])
            reading = _reading(d)
            if reading in readings:
                continue  # the same reading on file already: nothing to add
            readings.add(reading)
            out.append(d)
    return out


def _restore(connection: Connection, tenant_id: str, document: dict[str, Any]) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET association_status='ACTIVE', void_reason=NULL, voided_by_actor_id=NULL, voided_at_utc=NULL
            WHERE tenant_id=:t AND evidence_id=:e AND association_status='SUPERSEDED'
              AND void_reason='REPLACED_BY_NEWER_UPLOAD'
            """
        ),
        {"t": tenant_id, "e": document["evidence_id"]},
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_documents
            SET capture_status='CLASSIFIED', updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND capture_status='SUPERSEDED'
            """
        ),
        {"t": tenant_id, "j": document["journey_id"], "d": document["di_document_id"]},
    )


def run_statement_copy_restore(engine: Engine, tag: str, reports: list[str] | None = None) -> dict[str, Any]:
    tenant_id, _, label = (tag or "").partition(":")
    if not tenant_id or not label:
        raise ValueError("the tag must be '<tenant_id>:<label>'")
    commit = label.endswith("-commit")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        restore = to_restore(_documents(connection, tenant_id))
    by_journey: dict[UUID, list[dict[str, Any]]] = {}
    for d in restore:
        by_journey.setdefault(UUID(str(d["journey_id"])), []).append(d)
    summary: dict[str, Any] = {"commit": commit, "copies": len(restore), "bookings": len(by_journey),
                               "byKind": {}, "failed": [], "items": []}
    for d in restore:
        kind = f"{d['document_type_key']}:{_kind(d)}"
        summary["byKind"][kind] = summary["byKind"].get(kind, 0) + 1
        if len(summary["items"]) < _ITEMS_REPORTED:
            summary["items"].append({"journey": d["journey_reference"], "type": d["document_type_key"],
                                     "kind": _kind(d), "documentId": str(d["di_document_id"]),
                                     "pages": sorted(p.rsplit(":", 1)[-1] for p in (d["pages"] or []))})
    if not commit:
        return summary
    from audit_core.uc03_p2_runtime import note_facts_changed
    from audit_core.uc03_p2_worker import _rematerialize_stage

    for journey_id, documents in by_journey.items():
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                for document in documents:
                    _restore(connection, tenant_id, document)
                for stage in sorted({str(d["process_area"] or "DELIVERY").upper() for d in documents}):
                    _rematerialize_stage(connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage)
                note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                   reason="STATEMENT_COPY_RESTORE", correlation_id=f"statement-copy-restore:{tag}")
        except Exception:  # one booking's failure is reported, the rest go on
            logger.warning("statement_copy_restore_failed", tag=tag, journey_id=str(journey_id), exc_info=True)
            summary["failed"].append(str(journey_id))
            continue
        logger.info("statement_copy_restore_item", tag=tag, journey_id=str(journey_id), restored=len(documents))
    return summary
