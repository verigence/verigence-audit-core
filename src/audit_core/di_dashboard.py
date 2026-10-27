"""Document Intelligence dashboard for one Project: what was uploaded, how it was
read, how long it took and why anything failed (product owner, 2026-10-27).
Executive and Superadmin only; the route checks the caller.

Everything is grouped by the IST day of the upload. A "document" is one
unit DI reads: a single page, or several pages grouped into one document
(the pages merged into a group are counted once, through the group).

Outcome of each document, in this order:
- DUPLICATE: the same page was already on the booking; dropped on arrival.
- BAD_SCAN: refused by DI's scan-quality check (faded, blurred, too small,
  unreadable). Never sent to Gemini.
- NOT_READ_IN_TIME: not read within 30 minutes of the upload.
- READ_FIRST: read, with no retry of any kind.
- READ_RETRIED: read after at least one retry (automatic or asked for).
- NEEDS_REVIEW / EXTRA_COPY: read; kept for review / as an extra copy.
- FAILED_OTHER: failed for another reason (the reason is listed).
- REMOVED: taken off the booking by a person (re-scan, superseded).
- IN_PROGRESS: still being read.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Any

from sqlalchemy import Connection, text

from audit_core.task_manager import TaskManagerError, known_names
from audit_core.uc03_p2_object_keys import IST

MAX_DAYS = 31
FAILED_LIST_LIMIT = 500
LINK_SECONDS = 1800

OUTCOMES = ("READ_FIRST", "READ_RETRIED", "NEEDS_REVIEW", "EXTRA_COPY", "BAD_SCAN", "NOT_READ_IN_TIME",
            "FAILED_OTHER", "DUPLICATE", "REMOVED", "IN_PROGRESS")
FAILED_OUTCOMES = ("BAD_SCAN", "NOT_READ_IN_TIME", "FAILED_OTHER")

_OUTCOME_SQL = """
    CASE
      WHEN q.last_error = 'DUPLICATE_PAGE' THEN 'DUPLICATE'
      WHEN q.last_error LIKE 'DI_QUALITY_%' THEN 'BAD_SCAN'
      WHEN q.last_error = 'NOT_READ_IN_30_MIN' THEN 'NOT_READ_IN_TIME'
      WHEN q.queue_status = 'READY' AND NOT retried THEN 'READ_FIRST'
      WHEN q.queue_status = 'READY' THEN 'READ_RETRIED'
      WHEN q.queue_status = 'NEEDS_REVIEW' THEN 'NEEDS_REVIEW'
      WHEN q.queue_status = 'SUPPORTING' THEN 'EXTRA_COPY'
      WHEN q.queue_status IN ('FAILED', 'DEAD_LETTER') THEN 'FAILED_OTHER'
      WHEN q.queue_status = 'CANCELLED' AND q.last_error IS NOT NULL
           AND q.last_error NOT IN ('PAGE_REMOVED', 'SUPERSEDED') THEN 'FAILED_OTHER'
      WHEN q.queue_status = 'CANCELLED' THEN 'REMOVED'
      ELSE 'IN_PROGRESS'
    END
"""

_UNITS_SQL = f"""
    WITH units AS (
      SELECT q.*, b.created_at_utc AS uploaded_at, b.uploaded_by_actor_id, b.original_filename,
             b.size_bytes AS file_bytes, b.page_count AS file_pages, b.journey_id AS batch_journey_id,
             (q.attempt_count > 0
              OR EXISTS (SELECT 1 FROM auditcore.p2_activity_events e
                         WHERE e.tenant_id = q.tenant_id AND e.journey_id = q.journey_id
                           AND e.subject_id = q.queue_id::text
                           AND (e.event_type IN ('PAGE_RETRY_REQUESTED', 'PAGE_REREAD_REQUESTED')
                                OR (e.event_type = 'DOCUMENT_STATUS_CHANGED'
                                    AND e.details->>'status' = 'RETRY_WAIT')))
              OR EXISTS (SELECT 1 FROM auditcore.p2_work_queue w
                         WHERE w.tenant_id = q.tenant_id AND w.work_type = 'DOCUMENT_INGEST'
                           AND w.work_key = q.queue_id::text AND w.attempt_count > 1)) AS retried
      FROM auditcore.p2_document_queue q
      JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
      WHERE q.tenant_id = :t AND b.created_at_utc >= :s AND b.created_at_utc < :e
        AND q.queue_status <> 'MERGED'
    )
    SELECT q.queue_id, q.batch_id, q.journey_id, q.unit_kind, q.queue_status, q.last_error, q.status_reason,
           q.classified_document_type, q.template_key, q.page_object_key, q.uploaded_at, q.updated_at_utc,
           q.uploaded_by_actor_id, q.original_filename, q.retried,
           COALESCE(cardinality(q.page_numbers), 1) AS pages,
           COALESCE(q.page_numbers, ARRAY[q.page_number]) AS page_numbers,
           ((q.uploaded_at AT TIME ZONE 'Asia/Kolkata')::date) AS day,
           EXTRACT(HOUR FROM (q.uploaded_at AT TIME ZONE 'Asia/Kolkata'))::int AS hour,
           {_OUTCOME_SQL} AS outcome
    FROM units q
"""

# Reasons in plain words, by code.
_REASONS = {
    "DI_QUALITY_IMAGE_CONTRAST": ("Bad scan", "Faded or low contrast: the writing is too light to read."),
    "DI_QUALITY_IMAGE_BLUR_SCORE": ("Bad scan", "Blurred: the photo is out of focus or shaken."),
    "DI_QUALITY_IMAGE_MIN_DIMENSIONS": ("Bad scan", "Too small: the image has too few pixels to read."),
    "DI_QUALITY_FILE_NOT_EMPTY": ("Bad scan", "Empty file."),
    "NOT_READ_IN_30_MIN": ("Processing delay",
                           "Not read within 30 minutes of the upload (the system was too slow, not the scan)."),
    "DUPLICATE_PAGE": ("Duplicate", "The same page was already on the booking."),
}


def reason_of(code: str | None, status_reason: str | None) -> tuple[str, str]:
    value = str(code or "").strip()
    if value in _REASONS:
        return _REASONS[value]
    if value.startswith("DI_QUALITY_"):
        return "Bad scan", f"Refused by the scan-quality check ({value.removeprefix('DI_QUALITY_').lower()})."
    if value.startswith("FACTS_COPY_FAILED"):
        return "System error", "Read by DI but not copied into the booking."
    if value:
        return "System error", (status_reason or value)[:300]
    return "Other", (status_reason or "No reason recorded.")[:300]


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return (datetime.combine(start, time(0), tzinfo=IST).astimezone(UTC),
            datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST).astimezone(UTC))


def _signed(object_key: str | None) -> str | None:
    if not object_key:
        return None
    from audit_core.uc03_p2_storage import (
        P2DocumentStorageError,
        get_p2_document_storage,
    )

    try:
        return get_p2_document_storage().presign_get(object_key, expires_seconds=LINK_SECONDS)
    except (RuntimeError, P2DocumentStorageError):
        return None


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"min": None, "avg": None, "median": None, "max": None, "count": 0}
    return {"min": round(min(values), 1), "avg": round(sum(values) / len(values), 1),
            "median": round(median(values), 1), "max": round(max(values), 1), "count": len(values)}


def _empty_day(day: str) -> dict[str, Any]:
    return {
        "day": day, "files": 0, "pages": 0, "duplicateFiles": 0, "notUploadedFiles": 0,
        "documents": 0, "outcomes": dict.fromkeys(OUTCOMES, 0), "pagesByOutcome": dict.fromkeys(OUTCOMES, 0),
        "badScanReasons": {}, "_minutes": [], "_bytes": [], "filesInProgress": 0,
    }


def build_di_dashboard(connection: Connection, *, tenant_id: str, start_day: date, end_day: date,
                       include_links: bool = True) -> dict[str, Any]:
    if end_day < start_day:
        raise TaskManagerError("The end date is before the start date.")
    if (end_day - start_day).days + 1 > MAX_DAYS:
        raise TaskManagerError(f"Choose at most {MAX_DAYS} days.")
    start, end = _bounds(start_day, end_day)
    params = {"t": tenant_id, "s": start, "e": end}
    names = known_names(connection, tenant_id=tenant_id)
    days = {(start_day + timedelta(days=i)).isoformat(): _empty_day((start_day + timedelta(days=i)).isoformat())
            for i in range((end_day - start_day).days + 1)}

    files = connection.execute(text("""
        SELECT b.batch_id, b.batch_status, b.page_count, b.size_bytes, b.sha256, b.failure_reason,
               b.created_at_utc, b.uploaded_by_actor_id,
               ((b.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date) AS day
        FROM auditcore.p2_upload_batches b
        WHERE b.tenant_id = :t AND b.created_at_utc >= :s AND b.created_at_utc < :e
    """), params).mappings().all()
    units = connection.execute(text(_UNITS_SQL), params).mappings().all()

    units_by_file: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        units_by_file[unit["batch_id"]].append(dict(unit))

    for f in files:
        d = days[f["day"].isoformat()]
        if f["batch_status"] == "CANCELLED" and not (f["page_count"] or 0) and f["sha256"]:
            d["duplicateFiles"] += 1
            continue
        if f["batch_status"] == "FAILED" and not (f["page_count"] or 0):
            d["notUploadedFiles"] += 1
            continue
        d["files"] += 1
        d["pages"] += int(f["page_count"] or 0)
        if f["size_bytes"]:
            d["_bytes"].append(float(f["size_bytes"]))
        mine = units_by_file.get(f["batch_id"], [])
        if not mine or any(u["outcome"] == "IN_PROGRESS" for u in mine):
            d["filesInProgress"] += 1
        else:
            finished = max(u["updated_at_utc"] for u in mine)
            d["_minutes"].append(max(0.0, (finished - f["created_at_utc"]).total_seconds() / 60))

    by_pc: dict[str, dict[str, int]] = defaultdict(lambda: {"documents": 0, "pages": 0, "badScans": 0,
                                                              "notReadInTime": 0, "failedOther": 0,
                                                              "readFirst": 0})
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"documents": 0, "readFirst": 0, "retried": 0,
                                                                "failed": 0})
    by_hour = [{"hour": h, "documents": 0, "failed": 0, "notReadInTime": 0} for h in range(24)]
    reasons: dict[tuple[str, str], dict[str, Any]] = {}
    for u in units:
        d = days[u["day"].isoformat()]
        outcome = u["outcome"]
        pages = int(u["pages"] or 1)
        d["documents"] += 1
        d["outcomes"][outcome] += 1
        d["pagesByOutcome"][outcome] += pages
        if outcome == "BAD_SCAN":
            label = reason_of(u["last_error"], u["status_reason"])[1]
            d["badScanReasons"][label] = d["badScanReasons"].get(label, 0) + 1
        pc = by_pc[str(u["uploaded_by_actor_id"] or "")]
        pc["documents"] += 1
        pc["pages"] += pages
        pc["badScans"] += outcome == "BAD_SCAN"
        pc["notReadInTime"] += outcome == "NOT_READ_IN_TIME"
        pc["failedOther"] += outcome == "FAILED_OTHER"
        pc["readFirst"] += outcome == "READ_FIRST"
        if outcome not in ("DUPLICATE", "REMOVED", "IN_PROGRESS"):
            kind = by_type[str(u["template_key"] or u["classified_document_type"] or "not identified")]
            kind["documents"] += 1
            kind["readFirst"] += outcome == "READ_FIRST"
            kind["retried"] += outcome == "READ_RETRIED"
            kind["failed"] += outcome in FAILED_OUTCOMES
        h = by_hour[int(u["hour"])]
        h["documents"] += 1
        h["failed"] += outcome in FAILED_OUTCOMES
        h["notReadInTime"] += outcome == "NOT_READ_IN_TIME"
        if outcome in FAILED_OUTCOMES:
            group, words = reason_of(u["last_error"], u["status_reason"])
            key = (group, words)
            entry = reasons.setdefault(key, {"group": group, "reason": words, "documents": 0, "pages": 0})
            entry["documents"] += 1
            entry["pages"] += pages

    def finish_day(d: dict[str, Any]) -> dict[str, Any]:
        o = d["outcomes"]
        read = o["READ_FIRST"] + o["READ_RETRIED"] + o["NEEDS_REVIEW"] + o["EXTRA_COPY"]
        settled = read + o["BAD_SCAN"] + o["NOT_READ_IN_TIME"] + o["FAILED_OTHER"]
        out = {k: v for k, v in d.items() if not k.startswith("_")}
        out["avgPagesPerFile"] = round(d["pages"] / d["files"], 1) if d["files"] else None
        sizes = d["_bytes"]
        out["fileSizeMb"] = {"avg": round(sum(sizes) / len(sizes) / 1_048_576, 2) if sizes else None,
                             "max": round(max(sizes) / 1_048_576, 2) if sizes else None}
        out["processingMinutes"] = _stats(d["_minutes"])
        out["readFirstPct"] = round(100 * o["READ_FIRST"] / settled, 1) if settled else None
        out["failedPct"] = (round(100 * (o["BAD_SCAN"] + o["NOT_READ_IN_TIME"] + o["FAILED_OTHER"]) / settled, 1)
                            if settled else None)
        return out

    day_rows = [finish_day(d) for d in days.values()]
    total = _empty_day("total")
    for d in days.values():
        for key in ("files", "pages", "duplicateFiles", "notUploadedFiles", "documents", "filesInProgress"):
            total[key] += d[key]
        for k in OUTCOMES:
            total["outcomes"][k] += d["outcomes"][k]
            total["pagesByOutcome"][k] += d["pagesByOutcome"][k]
        for label, n in d["badScanReasons"].items():
            total["badScanReasons"][label] = total["badScanReasons"].get(label, 0) + n
        total["_minutes"] += d["_minutes"]
        total["_bytes"] += d["_bytes"]

    failed = []
    for u in sorted((u for u in units if u["outcome"] in FAILED_OUTCOMES),
                    key=lambda x: x["uploaded_at"], reverse=True)[:FAILED_LIST_LIMIT]:
        group, words = reason_of(u["last_error"], u["status_reason"])
        failed.append({
            "queueId": str(u["queue_id"]), "journeyId": str(u["journey_id"]), "batchId": str(u["batch_id"]),
            "filename": u["original_filename"], "pageNumbers": [int(p) for p in (u["page_numbers"] or [])],
            "documentType": u["template_key"] or u["classified_document_type"],
            "uploadedAtUtc": u["uploaded_at"].isoformat(), "failedAtUtc": u["updated_at_utc"].isoformat(),
            "uploadedBy": u["uploaded_by_actor_id"], "uploadedByName": names.get(str(u["uploaded_by_actor_id"] or "")),
            "outcome": u["outcome"], "group": group, "reason": words, "code": u["last_error"],
            "retried": bool(u["retried"]),
            "openUrl": _signed(u["page_object_key"]) if include_links else None,
        })
    journeys = {r["journey_id"]: r for r in connection.execute(text("""
        SELECT j.journey_id::text AS journey_id, j.journey_reference, COALESCE(c.legal_name, c.display_name) AS customer,
               o.outlet_name
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = j.tenant_id AND o.outlet_id = j.outlet_id
        WHERE j.tenant_id = :t AND j.journey_id = ANY(CAST(:ids AS uuid[]))
    """), {"t": tenant_id, "ids": list({f["journeyId"] for f in failed})}).mappings().all()} if failed else {}
    for f in failed:
        j = journeys.get(f["journeyId"]) or {}
        f.update({"journeyReference": j.get("journey_reference"), "customerName": j.get("customer"),
                  "outletName": j.get("outlet_name")})

    pcs = []
    for actor, v in by_pc.items():
        if not actor:
            continue
        pcs.append({"actorId": actor, "name": names.get(actor), **v,
                    "badScanPct": round(100 * v["badScans"] / v["documents"], 1) if v["documents"] else None,
                    "readFirstPct": round(100 * v["readFirst"] / v["documents"], 1) if v["documents"] else None})
    pcs.sort(key=lambda p: (-(p["badScanPct"] or 0), -p["documents"]))
    types = [{"documentType": k, **v,
              "readFirstPct": round(100 * v["readFirst"] / v["documents"], 1) if v["documents"] else None}
             for k, v in by_type.items()]
    types.sort(key=lambda t: -t["documents"])
    return {
        "from": start_day.isoformat(), "to": end_day.isoformat(),
        "totals": finish_day(total), "days": day_rows,
        "failureReasons": sorted(reasons.values(), key=lambda r: -r["documents"]),
        "byPc": pcs, "byDocumentType": types, "byHour": by_hour,
        "failed": failed, "failedTruncated": sum(1 for u in units if u["outcome"] in FAILED_OUTCOMES) > len(failed),
        "linkExpiresInSeconds": LINK_SECONDS,
        "notes": [
            "Grouped by the day of the upload (IST). A document is one page, or several pages DI reads together.",
            ("Read first time: read with no retry of any kind. Read after retries: an automatic retry, a re-read or "
             "a retry asked for by a person came first."),
            ("Bad scan: refused by DI's scan-quality check before any reading (faded, blurred, too small, "
             "unreadable). The PC has to scan again."),
            ("Not read in time: the document was not read within 30 minutes of the upload. This is the system "
             "being too slow, not the scan or Gemini."),
            ("Processing time: from the upload to the last document of the file settling (read, refused or "
             "failed). Files still being read are not counted."),
            "Links to failed pages open the page as uploaded and are valid 30 minutes from when the page was loaded.",
        ],
    }
