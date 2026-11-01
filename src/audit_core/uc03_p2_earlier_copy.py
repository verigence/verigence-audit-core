"""A document comes back when the copy that replaced it never became a document (found live 2026-11-01).

A single-slot document (PAN, Aadhaar, GST certificate, booking form, invoice,
insurance cover ...) is replaced the moment a newer page of that type is
classified (uc03_pc_booking_documents, decision 2026-10-01): the earlier copy
becomes SUPERSEDED. The page is then grouped with the pages around it into
one document, which is read again; the page itself is voided as merged into
it (P2_MERGED_INTO_DOCUMENT). When that document is never read -- 23 to 26
October, "Not read within 30 minutes. Please upload this page again." --
nothing of the newer upload is left on the booking, and the earlier copy,
which was read, stays superseded: the booking shows the document missing
(ADI-SJ-261026-003's booking form and customer ledger, ADI-SU-261019-005's
Aadhaar).

So on every journey recompute (uc03_p2_worker.settle_journey), for a
single-slot requirement with nothing active: the newest superseded copy comes
back when every copy that replaced it is a page merged into a document that
was never read (its unit CANCELLED or DEAD_LETTER). It is the earlier upload
as it was read -- never a page of that same unread document (a page merged
into it stays as it is: the document was not read as a whole). When the
document is read later (the PC's Retry, or a new upload), it replaces the copy
again the usual way, and nothing here brings the copy back while it stands.

Never brought back: a copy whose replacement was read (the newer document,
whatever became of it since), a copy the PC replaced and then removed
(P2_DOCUMENT_VOIDED is the PC's own decision; the document card's Restore is
one click away), and a copy replaced by another customer's file
(uc03_p2_file_identity gives those back, and takes them away again should the
file be found to belong).

``run_earlier_copy_back`` (p2_worker_jobs kind EARLIER_COPY_BACK) lists, per
booking, every copy that comes back this way or through the file identity --
a dry run unless the tag ends with "-commit", when each booking listed is
recomputed (which is where the copies come back).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

_ACTOR = "p2-worker"
EVENT = "EARLIER_COPY_BACK"
# The reasons a copy is superseded by a newer upload (uc03_p2_file_identity._RESTORABLE).
_REPLACED = ("REPLACED_BY_NEWER_UPLOAD", "REPLACED_BY_P2_REUPLOAD", "EARLIER_COPY_RESTORED")
# A unit in one of these states will not be read unless someone sends it again.
_NEVER_READ = ("CANCELLED", "DEAD_LETTER")
_NAME_FIELDS = ("pan_name", "aadhaar_name", "customer_name", "buyer_name", "insured_name",
                "organisation_legal_name", "finance_applicant_name")


def _repeatable(requirement_key: str | None) -> bool:
    from audit_core.uc03_pc_booking_documents import _is_repeatable_requirement

    return _is_repeatable_requirement(requirement_key)


def copies_to_bring_back(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Read only: per single-slot requirement with nothing active, the newest
    superseded copy whose every replacement is a page of a document never
    read -- {evidence_id, di_document_id, document_type_key, process_area,
    replaced_by: [the pages' document ids], reason, whose}."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (e.journey_document_requirement_id)
                   e.evidence_id, e.di_document_id, e.document_type_key, e.process_area, e.void_reason,
                   r.requirement_key,
                   -- the copy itself is a page merged into a document: not a document of its own
                   EXISTS (SELECT 1 FROM auditcore.p2_document_queue oq
                           WHERE oq.tenant_id=e.tenant_id AND oq.journey_id=e.journey_id
                             AND oq.di_document_id=e.di_document_id
                             AND oq.unit_kind='PAGE' AND oq.queue_status='MERGED') AS merged_page
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_requirements r
              ON r.tenant_id=e.tenant_id AND r.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='SUPERSEDED'
              AND NOT EXISTS (SELECT 1 FROM auditcore.evidence a
                              WHERE a.tenant_id=e.tenant_id AND a.journey_id=e.journey_id
                                AND a.journey_document_requirement_id=e.journey_document_requirement_id
                                AND a.association_status='ACTIVE')
            ORDER BY e.journey_document_requirement_id, e.linked_at_utc DESC, e.evidence_id DESC
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    picked: list[dict[str, Any]] = []
    for row in rows:
        if row["void_reason"] not in _REPLACED or row["merged_page"] or _repeatable(row["requirement_key"]):
            continue
        replacements = connection.execute(
            text(
                """
                SELECT s.di_document_id, s.association_status, s.void_reason,
                       (SELECT g.queue_status
                          FROM auditcore.p2_document_queue pq
                          JOIN auditcore.p2_document_queue g
                            ON g.tenant_id=pq.tenant_id AND g.queue_id=pq.merged_into_queue_id
                         WHERE pq.tenant_id=s.tenant_id AND pq.journey_id=s.journey_id
                           AND pq.di_document_id=s.di_document_id
                         ORDER BY g.updated_at_utc DESC LIMIT 1) AS merged_into_status
                FROM auditcore.evidence s
                WHERE s.tenant_id=:t AND s.supersedes_evidence_id=:e
                """
            ),
            {"t": tenant_id, "e": row["evidence_id"]},
        ).mappings().all()
        if not replacements or not all(
            s["association_status"] == "VOIDED" and s["void_reason"] == "P2_MERGED_INTO_DOCUMENT"
            and s["merged_into_status"] in _NEVER_READ for s in replacements
        ):
            continue
        picked.append({
            "evidence_id": row["evidence_id"], "di_document_id": row["di_document_id"],
            "document_type_key": row["document_type_key"], "process_area": row["process_area"],
            "replaced_by": [str(s["di_document_id"]) for s in replacements],
            "reason": "The copy that replaced it was never read.",
        })
    if picked:
        names: dict[str, str] = {}
        for name in connection.execute(
            text(
                """
                SELECT f.di_document_id, f.effective_value #>> '{}' AS value
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id=:t AND f.journey_id=:j AND f.di_document_id = ANY(:d) AND f.field_key = ANY(:k)
                  AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
                ORDER BY f.di_document_id, f.field_key
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": [p["di_document_id"] for p in picked], "k": list(_NAME_FIELDS)},
        ).mappings().all():
            if name["value"] and str(name["value"]).strip():
                names.setdefault(str(name["di_document_id"]), " ".join(str(name["value"]).split()))
        for p in picked:
            p["whose"] = names.get(str(p["di_document_id"]))
    return picked


def bring_back_earlier_copies(connection: Connection, *, tenant_id: str, journey_id: UUID) -> int:
    """Make each copy copies_to_bring_back finds active again, with its
    capture row and one EARLIER_COPY_BACK entry in the booking's history.
    The caller rebuilds the booking from its documents afterwards. Returns
    how many came back."""
    back = 0
    for copy in copies_to_bring_back(connection, tenant_id=tenant_id, journey_id=journey_id):
        restored = connection.execute(
            text(
                """
                UPDATE auditcore.evidence e
                SET association_status='ACTIVE', void_reason=NULL, voided_by_actor_id=NULL, voided_at_utc=NULL
                WHERE e.tenant_id=:t AND e.evidence_id=:e AND e.association_status='SUPERSEDED'
                  AND NOT EXISTS (SELECT 1 FROM auditcore.evidence a
                                  WHERE a.tenant_id=e.tenant_id AND a.journey_id=e.journey_id
                                    AND a.journey_document_requirement_id=e.journey_document_requirement_id
                                    AND a.association_status='ACTIVE')
                RETURNING e.evidence_id
                """
            ),
            {"t": tenant_id, "e": copy["evidence_id"]},
        ).scalar_one_or_none()
        if restored is None:
            continue
        connection.execute(
            text(
                """
                UPDATE auditcore.document_capture_v2_documents SET capture_status='CLASSIFIED', updated_at_utc=now()
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND capture_status='SUPERSEDED'
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": copy["di_document_id"]},
        )
        from audit_core.uc03_p2_runtime import record_activity

        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type=EVENT,
            subject_type="DOCUMENT", subject_id=str(copy["di_document_id"]),
            details={"evidenceId": str(copy["evidence_id"]), "documentType": copy["document_type_key"],
                     "reason": copy["reason"], "replacedBy": copy["replaced_by"], "by": _ACTOR},
        )
        logger.info("p2_earlier_copy_back", tenant_id=tenant_id, journey_id=str(journey_id),
                    evidence_id=str(copy["evidence_id"]), document_type=copy["document_type_key"],
                    whose=copy.get("whose"), replaced_by=copy["replaced_by"])
        back += 1
    return back


# ------------------------------------------------------------- one-off job


def _journeys(connection: Connection, tenant_id: str) -> list[UUID]:
    """Bookings that may hold such a copy: a superseded copy replaced by a
    page of a document never read, or a file taken out (file identity)."""
    return [UUID(str(j)) for j in connection.execute(
        text(
            """
            SELECT DISTINCT e.journey_id
            FROM auditcore.evidence e
            JOIN auditcore.evidence s ON s.tenant_id=e.tenant_id AND s.supersedes_evidence_id=e.evidence_id
            JOIN auditcore.p2_document_queue pq
              ON pq.tenant_id=s.tenant_id AND pq.journey_id=s.journey_id AND pq.di_document_id=s.di_document_id
            JOIN auditcore.p2_document_queue g ON g.tenant_id=pq.tenant_id AND g.queue_id=pq.merged_into_queue_id
            WHERE e.tenant_id=:t AND e.association_status='SUPERSEDED'
              AND s.association_status='VOIDED' AND s.void_reason='P2_MERGED_INTO_DOCUMENT'
              AND g.queue_status = ANY(:never)
            UNION
            SELECT DISTINCT b.journey_id FROM auditcore.p2_upload_batches b
            WHERE b.tenant_id=:t AND b.identity_enforced_at_utc IS NOT NULL
            ORDER BY 1
            """
        ),
        {"t": tenant_id, "never": list(_NEVER_READ)},
    ).scalars().all()]


def plan_journey(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """What the next recompute of the booking gives back: the copies whose
    replacement was never read, and the customer's own copies the file
    identity gives back for the files taken out."""
    from audit_core.uc03_p2_file_identity import enforce_enabled, plan_enforcement

    unread = copies_to_bring_back(connection, tenant_id=tenant_id, journey_id=journey_id)
    foreign_file: list[dict[str, Any]] = []
    if enforce_enabled(connection, tenant_id=tenant_id, journey_id=journey_id):
        taken = [str(b) for b in connection.execute(
            text("SELECT batch_id FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j "
                 "AND identity_enforced_at_utc IS NOT NULL ORDER BY batch_id"),
            {"t": tenant_id, "j": journey_id},
        ).scalars().all()]
        if taken:
            foreign_file = plan_enforcement(connection, tenant_id=tenant_id, journey_id=journey_id,
                                            foreign=taken)["restored"]
    return {
        "neverRead": [{"documentType": c["document_type_key"], "whose": c.get("whose") or "no name read",
                       "documentId": str(c["di_document_id"])} for c in unread],
        "foreignFile": [{"documentType": c["documentType"], "whose": c["whose"], "documentId": c["documentId"]}
                        for c in foreign_file],
    }


def run_earlier_copy_back(engine: Engine, tag: str, tenant_ids: list[str]) -> None:
    """One ``earlier_copy_back_item`` log line per booking with a copy to give
    back; with a tag ending in "-commit" each of those bookings is recomputed,
    which gives the copies back (settle_journey). Idempotent: a second run
    finds nothing."""
    commit = tag.endswith("-commit")
    for tenant_id in tenant_ids:
        totals = {"journeys": 0, "never_read": 0, "foreign_file": 0, "recomputed": 0, "failed": 0}
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                journeys = _journeys(connection, tenant_id)
        except Exception:
            logger.warning("earlier_copy_back_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        for journey_id in journeys:
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    plan = plan_journey(connection, tenant_id=tenant_id, journey_id=journey_id)
                    if not plan["neverRead"] and not plan["foreignFile"]:
                        continue
                    reference = connection.execute(
                        text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
                        {"t": tenant_id, "j": journey_id},
                    ).scalar_one_or_none()
                    if commit:
                        from audit_core.uc03_p2_runtime import note_facts_changed

                        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                           reason=EVENT, correlation_id=f"earlier-copy-back:{tag}")
                        totals["recomputed"] += 1
            except Exception:
                totals["failed"] += 1
                logger.warning("earlier_copy_back_item_failed", tag=tag, tenant_id=tenant_id,
                               journey_id=str(journey_id), exc_info=True)
                continue
            totals["journeys"] += 1
            totals["never_read"] += len(plan["neverRead"])
            totals["foreign_file"] += len(plan["foreignFile"])
            logger.info("earlier_copy_back_item", tag=tag, tenant_id=tenant_id, commit=commit,
                        journey_id=str(journey_id), reference=reference, never_read=plan["neverRead"],
                        foreign_file=plan["foreignFile"])
        logger.info("earlier_copy_back_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
