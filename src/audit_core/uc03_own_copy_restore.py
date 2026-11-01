"""A customer's own KYC copy replaced by someone else's (2026-11-01).

A newer upload of a single-slot document supersedes the earlier one
automatically (decision 2026-10-01; the earlier copy is one click away,
uc03_p2_copy_restore). When the newer Aadhaar or PAN is another person's -- a
relative, a co-applicant, another customer's page in the same file -- the
customer's own copy was set aside and the other person's is the one the booking
uses. 13 bookings on 2026-11-01, e.g. UTK-SA-261019-005: Rakesh Kumar
Pattanayak's Aadhaar replaced Suryakant Parida's and the booking took Rakesh's
name; ADI-RD-261026-034: Laxmi Behera's Aadhaar replaced Biswanath Behera's.

This job, for the journey references it is given (``reports``; none, nothing
is done), restores the customer's own copy -- the same "Restore" a PC can press
-- where all of this holds:

- the booking's customer is decided by its files (p2_journey_file_identity)
  and confirmed by a document on the booking: its PAN card, booking docket,
  booking form or customer KYC form names the same person;
- the active Aadhaar (or PAN card) names someone else, read unmasked;
- a superseded copy of that same document names the customer (the newest
  such copy is restored).

Anything else is left as it is and logged with the reason. The other
person's copy stays on the booking, superseded, one click away.

Started as worker job ``OWN_COPY_RESTORE``. A DRY RUN unless the tag ends with
"-commit". Idempotent: once restored, the active copy names the customer.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_names import is_masked, masked_matches, same_person

logger = structlog.get_logger(__name__)

JOB = "OWN_COPY_RESTORE"
ACTOR = "SYSTEM:OWN_COPY_RESTORE"
# The single-slot KYC documents and the field that names their holder.
_KYC = {"aadhaar": "aadhaar_name", "pan_card": "pan_name"}
# The documents that confirm whom the files decided the customer is.
_CONFIRMING = {"pan_card": "pan_name", "booking_docket": "customer_name", "booking_form": "customer_name",
               "customer_kyc": "customer_name"}


def _is(customer: str, name: str | None) -> bool:
    if not name:
        return False
    if is_masked(name):
        return masked_matches(name, customer)
    return same_person(customer, name)


def _named(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Every ACTIVE or SUPERSEDED document of the booking that names a person."""
    fields = {**_CONFIRMING, **_KYC}
    return [dict(r) for r in connection.execute(
        text(
            """
            SELECT DISTINCT ON (e.evidence_id)
                   e.evidence_id, e.di_document_id, e.document_type_key, e.association_status,
                   e.journey_document_requirement_id, e.linked_at_utc, x.effective_value #>> '{}' AS name
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields x
              ON x.tenant_id=e.tenant_id AND x.journey_id=e.journey_id AND x.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status IN ('ACTIVE', 'SUPERSEDED')
              AND e.document_type_key = ANY(:types)
              AND x.field_key = (CAST(:fields AS jsonb) ->> e.document_type_key)
              AND x.effective_value IS NOT NULL AND x.effective_value NOT IN ('null'::jsonb, '""'::jsonb)
            ORDER BY e.evidence_id, x.source_fact_version DESC NULLS LAST, x.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": sorted(fields), "fields": json.dumps(fields)},
    ).mappings().all()]


def plan(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """What restoring would do on one booking: the copies to restore, or why not."""
    customer = connection.execute(
        text("SELECT customer_name FROM auditcore.p2_journey_file_identity WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    if not customer:
        return {"skipped": "No customer decided from the booking's files."}
    documents = _named(connection, tenant_id, journey_id)
    confirmed_by = sorted({d["document_type_key"] for d in documents
                           if d["association_status"] == "ACTIVE" and d["document_type_key"] in _CONFIRMING
                           and _is(customer, d["name"])})
    if not confirmed_by:
        return {"customer": customer, "skipped": "No PAN card, docket, booking form or KYC form on the booking "
                                                 "names the customer."}
    restores: list[dict[str, Any]] = []
    left: list[str] = []
    for doc_type in _KYC:
        typed = [d for d in documents if d["document_type_key"] == doc_type]
        active = [d for d in typed if d["association_status"] == "ACTIVE"]
        if not active or any(_is(customer, d["name"]) for d in active):
            continue
        if any(is_masked(d["name"]) for d in active):
            left.append(f"{doc_type}: the active copy's name is masked")
            continue
        requirement = active[0]["journey_document_requirement_id"]
        own = sorted((d for d in typed if d["association_status"] == "SUPERSEDED"
                      and d["journey_document_requirement_id"] == requirement and not is_masked(d["name"])
                      and _is(customer, d["name"])),
                     key=lambda d: d["linked_at_utc"], reverse=True)
        if not own:
            left.append(f"{doc_type}: no earlier copy names the customer")
            continue
        restores.append({"documentType": doc_type, "evidenceId": str(own[0]["evidence_id"]),
                         "restoredName": own[0]["name"], "replacedName": active[0]["name"],
                         "replacedEvidenceId": str(active[0]["evidence_id"])})
    return {"customer": customer, "confirmedBy": confirmed_by, "restores": restores, "left": left}


def _journeys(connection: Connection, tenant_id: str, references: list[str]) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text("SELECT journey_id, journey_reference FROM auditcore.journeys "
             "WHERE tenant_id=:t AND journey_reference = ANY(:refs) ORDER BY journey_reference"),
        {"t": tenant_id, "refs": list(references)},
    ).mappings().all()]


def restore_tenant(engine: Engine, *, tenant_id: str, tag: str, commit: bool,
                   references: list[str]) -> dict[str, Any]:
    from audit_core.uc03_p2_copy_restore import restore_superseded_copy

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = _journeys(connection, tenant_id, references)
    totals: dict[str, Any] = {"journeys": len(journeys), "toRestore": 0, "restored": 0, "skipped": 0, "failed": 0}
    for journey in journeys:
        journey_id = UUID(str(journey["journey_id"]))
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                outcome = plan(connection, tenant_id=tenant_id, journey_id=journey_id)
                for item in outcome.get("restores", []):
                    if commit:
                        restore_superseded_copy(
                            connection, tenant_id=tenant_id, journey_id=journey_id,
                            evidence_id=UUID(item["evidenceId"]), actor_id=ACTOR,
                            correlation_id=f"{JOB.lower()}:{tag}",
                            details={"job": JOB, "tag": tag, "customer": outcome["customer"],
                                     "restoredName": item["restoredName"], "replacedName": item["replacedName"]},
                        )
                        totals["restored"] += 1
        except Exception:
            totals["failed"] += 1
            logger.warning("own_copy_restore_failed", tag=tag, tenant_id=tenant_id,
                           reference=journey["journey_reference"], exc_info=True)
            continue
        totals["toRestore"] += len(outcome.get("restores", []))
        if not outcome.get("restores"):
            totals["skipped"] += 1
        logger.info("own_copy_restore_item", tag=tag, tenant_id=tenant_id, commit=commit,
                    reference=journey["journey_reference"], **outcome)
    return totals


def run_own_copy_restore(engine: Engine, tag: str, tenant_ids: list[str], references: list[str]) -> None:
    commit = tag.endswith("-commit")
    if not references:
        logger.warning("own_copy_restore_no_references", tag=tag)
        return
    for tenant_id in tenant_ids:
        try:
            totals = restore_tenant(engine, tenant_id=tenant_id, tag=tag, commit=commit, references=references)
        except Exception:
            logger.warning("own_copy_restore_tenant_failed", tag=tag, tenant_id=tenant_id, exc_info=True)
            continue
        logger.info("own_copy_restore_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
