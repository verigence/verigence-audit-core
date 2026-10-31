"""FILE_IDENTITY_SCAN: whose each uploaded file is, for every booking of one Project (2026-10-31).

The journey recompute decides each file's identity by itself
(uc03_p2_file_identity); this one-shot job reports the decision for every
booking at once, so the product owner sees what enforcement would do before
enforcement is turned on, and -- with "-commit" -- records it and asks for a
recompute of every booking it changes (the recompute then applies it where
enforcement is on). The job never turns enforcement on, "-commit" included:
that is the p2_file_identity_enforcement table (per journey or Project) or
P2_FILE_IDENTITY_ENFORCE. It never deletes anything.

- the tenant comes from the tag, ``<tenant_id>:<label>``;
- ``reports`` may list journey references to scan only those;
- for review, one ``file_identity_scan_review`` line per FOREIGN or UNKNOWN
  file: journey reference, PC, file name, the person it names, masked PAN,
  why, and what enforcement would do -- the documents and receipts it would
  take out, the customer's own copies it would give back (worked out read
  only, uc03_p2_file_identity.plan_enforcement); one
  ``file_identity_scan_restores`` line per booking naming the documents given
  back (type and whose); one ``file_identity_scan_item`` line per other file;
- one ``file_identity_scan_check`` line per check reported (two individual
  PANs in one kept file, more than one car in a booking, one car in two
  customers' bookings) and a ``file_identity_scan_finished`` summary.

Requested as a row of ``p2_worker_jobs``::

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, requested_by)
    VALUES ('FILE_IDENTITY_SCAN', '<tenant_id>:scan-2026-10-31', '<who>');
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_file_identity import (
    _record,
    classify,
    enforce_enabled,
    load_files,
    plan_enforcement,
    same_serial,
    vehicles_of,
)

logger = structlog.get_logger(__name__)


def parse_tag(tag: str) -> tuple[str, bool]:
    """(tenant_id, commit) from ``<tenant_id>:<label>``; commit when the tag
    ends with "-commit". Raises ValueError without a tenant."""
    tenant_id, sep, label = (tag or "").strip().partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError("FILE_IDENTITY_SCAN needs the tag '<tenant_id>:<label>' (label ending -commit to record)")
    return tenant_id.strip(), label.strip().endswith("-commit")


def _journeys(engine: Engine, tenant_id: str, references: list[str]) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if connection.execute(text("SELECT 1 FROM auditcore.projects WHERE tenant_id=:t LIMIT 1"),
                              {"t": tenant_id}).scalar_one_or_none() is None:
            raise ValueError(f"No Project for tenant {tenant_id!r}")
        rows = connection.execute(
            text(
                """
                SELECT j.journey_id, j.journey_reference, j.created_by_display_name AS pc_name
                FROM auditcore.journeys j
                WHERE j.tenant_id=:t
                  AND EXISTS (SELECT 1 FROM auditcore.p2_upload_batches b
                              WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id)
                  AND (cardinality(CAST(:refs AS text[])) = 0 OR j.journey_reference = ANY(CAST(:refs AS text[])))
                ORDER BY j.journey_reference, j.journey_id
                """
            ),
            {"t": tenant_id, "refs": references},
        ).mappings().all()
    return [dict(r) for r in rows]


def scan_journey(connection, *, tenant_id: str, journey_id: UUID, reference: str | None, tag: str,
                 commit: bool, pc_name: str | None = None) -> dict[str, Any]:
    """One booking: its files' decisions logged; with ``commit`` recorded and a
    recompute requested when anything changed or a foreign file still waits
    for enforcement.

    For review, each FOREIGN or UNKNOWN file is one ``file_identity_scan_review``
    line (journey, PC, file, person, masked PAN, reason, and what enforcement
    would take out and give back); each other file one ``file_identity_scan_item``
    line; a booking where enforcement would give documents back one
    ``file_identity_scan_restores`` line naming them."""
    files, recorded = load_files(connection, tenant_id=tenant_id, journey_id=journey_id)
    decision = classify(files) if files else None
    item: dict[str, Any] = {"files": len(files), "foreign": 0, "second_party": 0, "unknown": 0, "no_identity": 0,
                            "changed": 0, "checks": [], "vehicles": [], "customer": None,
                            "recompute_requested": False, "documents_out": 0, "receipts_out": 0, "restores": 0}
    if decision is None:
        return item
    item["customer"] = decision.customer_name
    by_id = {f.batch_id: f for f in files}
    # Whether the recompute would act on it: enforcement comes from the
    # p2_file_identity_enforcement table or P2_FILE_IDENTITY_ENFORCE only --
    # never from this job, "-commit" included.
    enforced = enforce_enabled(connection, tenant_id=tenant_id, journey_id=journey_id)
    item["enforced"] = enforced
    plan = plan_enforcement(connection, tenant_id=tenant_id, journey_id=journey_id, foreign=decision.foreign())
    for batch_id, d in decision.files.items():
        before = (recorded.get(batch_id) or {}).get("identity_status")
        item["changed"] += int(before != d.status)
        item["foreign"] += int(d.status == "FOREIGN")
        item["unknown"] += int(d.status == "UNKNOWN")
        item["second_party"] += int(d.status == "BELONGS" and d.reason_code == "SECOND_PARTY")
        item["no_identity"] += int(d.reason_code == "NO_IDENTITY")
        common = {"tag": tag, "tenant_id": tenant_id, "commit": commit, "journey_reference": reference,
                  "journey_id": str(journey_id), "pc": pc_name, "batch_id": batch_id,
                  "file_name": by_id[batch_id].file_name, "status": d.status, "reason_code": d.reason_code,
                  "person": d.person_name, "masked_pan": d.masked_pan, "reason": d.reason,
                  "customer": decision.customer_name, "previous_status": before, "enforced": enforced}
        if d.status == "BELONGS":
            logger.info("file_identity_scan_item", **common)
            continue
        effect = plan["files"].get(batch_id) or {"documents": 0, "active": 0, "receipts": 0, "receipts_amount": 0,
                                                 "restores": 0}
        item["documents_out"] += effect["documents"]
        item["receipts_out"] += effect["receipts"]
        item["restores"] += effect["restores"]
        logger.info("file_identity_scan_review", **common,
                    would_take_out_documents=effect["documents"], of_which_active=effect["active"],
                    would_stop_receipts=effect["receipts"], receipts_amount=round(effect["receipts_amount"], 2),
                    would_restore_copies=effect["restores"])
    if plan["restored"]:
        logger.info("file_identity_scan_restores", tag=tag, tenant_id=tenant_id, commit=commit,
                    journey_reference=reference, journey_id=str(journey_id), pc=pc_name,
                    customer=decision.customer_name, enforced=enforced,
                    restored=[f"{r['document']}: {r['whose']}" for r in plan["restored"]])
    item["checks"] = decision.checks
    for check in decision.checks:
        logger.info("file_identity_scan_check", tag=tag, tenant_id=tenant_id, journey_reference=reference,
                    journey_id=str(journey_id), customer=decision.customer_name, **check)
    item["vehicles"] = vehicles_of(files, decision.files)
    if not commit:
        return item
    _record(connection, tenant_id=tenant_id, journey_id=journey_id, decision=decision, recorded=recorded)
    # A booking where enforcement is on and a foreign file still waits for it.
    waiting = enforced and any(d.status == "FOREIGN" and not (recorded.get(b) or {}).get("identity_enforced_at_utc")
                               for b, d in decision.files.items())
    if item["changed"] or waiting:
        from audit_core.uc03_p2_runtime import note_facts_changed

        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="FILE_IDENTITY_SCAN",
                           correlation_id=f"file-identity-scan:{tag}"[:128])
        item["recompute_requested"] = True
    return item


def run_file_identity_scan(engine: Engine, tag: str, reports: list[str] | None = None) -> dict[str, Any]:
    tenant_id, commit = parse_tag(tag)
    references = [r.strip() for r in (reports or []) if r and r.strip()]
    journeys = _journeys(engine, tenant_id, references)
    totals: dict[str, Any] = {"journeys": 0, "files": 0, "foreign_journeys": 0, "foreign_files": 0,
                              "second_party_files": 0, "unknown_files": 0, "unknown_journeys": 0,
                              "no_identity_files": 0, "changed_files": 0, "recomputes_requested": 0,
                              "documents_out": 0, "receipts_out": 0, "copies_restored": 0,
                              "checks": {}, "failed": 0}
    logger.info("file_identity_scan_started", tag=tag, tenant_id=tenant_id, commit=commit, journeys=len(journeys))
    cars: list[tuple[str, str | None, str | None, str]] = []
    for row in journeys:
        journey_id, reference = UUID(str(row["journey_id"])), row["journey_reference"]
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                item = scan_journey(connection, tenant_id=tenant_id, journey_id=journey_id, reference=reference,
                                    tag=tag, commit=commit, pc_name=row.get("pc_name"))
        except Exception:
            totals["failed"] += 1
            logger.warning("file_identity_scan_item_failed", tag=tag, tenant_id=tenant_id,
                           journey_reference=reference, journey_id=str(journey_id), exc_info=True)
            continue
        totals["journeys"] += 1
        totals["files"] += item["files"]
        totals["foreign_files"] += item["foreign"]
        totals["foreign_journeys"] += int(item["foreign"] > 0)
        totals["second_party_files"] += item["second_party"]
        totals["unknown_files"] += item["unknown"]
        totals["unknown_journeys"] += int(item["unknown"] > 0)
        totals["no_identity_files"] += item["no_identity"]
        totals["changed_files"] += item["changed"]
        totals["recomputes_requested"] += int(item["recompute_requested"])
        totals["documents_out"] += item["documents_out"]
        totals["receipts_out"] += item["receipts_out"]
        totals["copies_restored"] += item["restores"]
        for check in item["checks"]:
            totals["checks"][check["check"]] = totals["checks"].get(check["check"], 0) + 1
        for car in item["vehicles"]:
            cars.append((car, reference, item["customer"], str(journey_id)))
    # One car in two customers' bookings: reported, never acted on.
    seen: list[tuple[str, str | None, str | None, str]] = []
    for car in cars:
        for other in seen:
            if other[3] != car[3] and same_serial(car[0], other[0]) and (car[2] or "") != (other[2] or ""):
                totals["checks"]["SAME_VEHICLE_OTHER_BOOKING"] = totals["checks"].get(
                    "SAME_VEHICLE_OTHER_BOOKING", 0) + 1
                logger.info("file_identity_scan_check", tag=tag, tenant_id=tenant_id, check="SAME_VEHICLE_OTHER_BOOKING",
                            chassis=f"...{car[0][-5:]}", journey_reference=car[1], customer=car[2],
                            other_journey_reference=other[1], other_customer=other[2])
        seen.append(car)
    logger.info("file_identity_scan_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
    return totals
