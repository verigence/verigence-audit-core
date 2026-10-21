"""uc03_p2_document_health — one health state per uploaded page, and the
journey's document defects.

Decision 2026-10-01. A page that passed quality and was classified must,
within eight hours, hold values in Audit Core or carry a reason and an
action a person can take. This module computes that state from the facts
both sides hold (the page queue, DI's last reported state, the evidence
lineage) -- never from a timer alone -- and is read by the Upload / Edit
Documents screen, the file status tasks, the journey Re-sync and the
nightly sweep, so all four agree.

States and the one action each offers:

  READ          values are in Audit Core                       -
  WAITING       in hand (classifying, reading, copying), < 8h  -
  STUCK         in hand for 8h or more                          READ_AGAIN
  NOT_READ      classified, settled without being read          READ_AGAIN
  NOTHING_READ  read, but no values came back                   READ_AGAIN
  REJECTED      quality or unreadable: the file is at fault     UPLOAD_AGAIN
  FAILED        a technical failure after every attempt         RETRY
  UNCLASSIFIED  DI could not tell what the page is              SET_TYPE
  OTHERS        the PC kept it as Others                        -
  SUPERSEDED    a newer copy replaced it                        RESTORE
  HIDDEN        merged into a group, or cancelled               -
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

STUCK_AFTER_SECONDS = 8 * 3600
TL_AFTER_SECONDS = 24 * 3600

_IN_HAND = frozenset({
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING",
    "CLASSIFYING", "RETRY_WAIT", "EXTRACTING", "SYNCING_TO_AUDIT_CORE",
})
_QUALITY_PREFIX = "DI_QUALITY_"
_UNREADABLE_CODES = frozenset({"FILE_EMPTY", "INVALID_FILE_CONTENT", "CORRUPT", "UPLOAD_FAILED"})
# Pages whose state counts as a defect of the pipeline, not of the file.
DEFECT_STATES = frozenset({"STUCK", "NOT_READ"})


def page_health(
    unit: Mapping[str, Any], *, capture_status: str | None, now: datetime | None = None,
) -> dict[str, Any]:
    """The health of one queue unit. ``unit`` is a p2_document_queue row
    (queue_status, template_key, type_overridden_by_actor_id or typeSetByPc,
    last_error, di_submitted_at_utc, created_at_utc, di_document_id);
    ``capture_status`` is the matching document_capture_v2_documents row's
    status, when there is one."""
    now = now or datetime.now(UTC)
    status = str(unit.get("queue_status") or "")
    template_key = unit.get("template_key")
    set_by_pc = bool(unit.get("typeSetByPc") or unit.get("type_overridden_by_actor_id"))
    since = unit.get("di_submitted_at_utc") or unit.get("created_at_utc")
    age = (now - since).total_seconds() if isinstance(since, datetime) else 0.0
    since_iso = since.isoformat() if isinstance(since, datetime) else None

    def result(state: str, action: str | None) -> dict[str, Any]:
        return {"state": state, "action": action, "since": since_iso, "ageSeconds": int(age)}

    if status in ("MERGED", "CANCELLED"):
        return result("HIDDEN", None)
    if capture_status == "SUPERSEDED":
        return result("SUPERSEDED", "RESTORE")
    if status == "READY":
        return result("READ", None)
    if status in ("FAILED", "DEAD_LETTER"):
        code = str(unit.get("last_error") or "").upper()
        if code.startswith(_QUALITY_PREFIX) or code in _UNREADABLE_CODES:
            return result("REJECTED", "UPLOAD_AGAIN")
        return result("FAILED", "RETRY")
    if status == "NEEDS_REVIEW":
        return result("NOTHING_READ", "READ_AGAIN")
    if status == "SUPPORTING":
        if template_key in (None, "", "supporting_document"):
            return result("OTHERS" if set_by_pc else "UNCLASSIFIED", None if set_by_pc else "SET_TYPE")
        return result("NOT_READ", "READ_AGAIN")
    if status in _IN_HAND:
        if age >= STUCK_AFTER_SECONDS:
            return result("STUCK", "READ_AGAIN" if unit.get("di_document_id") else "RETRY")
        return result("WAITING", None)
    return result("WAITING", None)


def summarize(healths: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "read": 0, "waiting": 0, "stuck": 0, "notRead": 0, "nothingRead": 0, "rejected": 0,
        "failed": 0, "unclassified": 0, "others": 0, "superseded": 0, "defects": 0,
    }
    key_by_state = {
        "READ": "read", "WAITING": "waiting", "STUCK": "stuck", "NOT_READ": "notRead",
        "NOTHING_READ": "nothingRead", "REJECTED": "rejected", "FAILED": "failed",
        "UNCLASSIFIED": "unclassified", "OTHERS": "others", "SUPERSEDED": "superseded",
    }
    for health in healths:
        key = key_by_state.get(str(health.get("state")))
        if key:
            counts[key] += 1
        if health.get("state") in DEFECT_STATES:
            counts["defects"] += 1
    return counts


def journey_document_health(
    connection: Connection, *, tenant_id: str, journey_id: UUID, now: datetime | None = None,
) -> dict[str, Any]:
    """Every live unit's health for one journey, the summary, and the
    defects (stuck or classified-but-unread pages) with what names them."""
    now = now or datetime.now(UTC)
    rows = connection.execute(
        text(
            """
            SELECT q.queue_id, q.batch_id, q.di_document_id, q.queue_status, q.template_key,
                   q.type_overridden_by_actor_id, q.last_error, q.di_submitted_at_utc, q.created_at_utc,
                   q.page_number, q.page_numbers, b.original_filename,
                   d.capture_status
            FROM auditcore.p2_document_queue q
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
            LEFT JOIN auditcore.document_capture_v2_documents d
              ON d.tenant_id=q.tenant_id AND d.journey_id=q.journey_id AND d.di_document_id=q.di_document_id
            WHERE q.tenant_id=:t AND q.journey_id=:j
              AND q.queue_status NOT IN ('MERGED', 'CANCELLED')
              AND b.batch_status <> 'CANCELLED'
            ORDER BY b.created_at_utc, q.page_number
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    units: dict[str, dict[str, Any]] = {}
    defects: list[dict[str, Any]] = []
    for row in rows:
        health = page_health(row, capture_status=row["capture_status"], now=now)
        units[str(row["queue_id"])] = health
        if health["state"] in DEFECT_STATES:
            defects.append({
                "queueId": str(row["queue_id"]),
                "batchId": str(row["batch_id"]),
                "filename": row["original_filename"],
                "pageNumbers": list(row["page_numbers"] or [row["page_number"]]),
                "templateKey": row["template_key"],
                "state": health["state"],
                "since": health["since"],
            })
    return {"units": units, "summary": summarize(list(units.values())), "defects": defects}


