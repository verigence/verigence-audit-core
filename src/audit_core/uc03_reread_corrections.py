"""A person's corrections survive a page being read again (2026-11-10).

Reading a page again -- a PC's or Reviewer's "Set the document type", or a job
doing the same (uc03_page_retype, uc03_cost_sheet_statements) -- retires the
document the page was and reads the stored page anew. Every value a person had
corrected on the retired document used to be lost with it. Live 2026-11-10,
UTK-SU-261020-005: the Reviewer set the struck-out accessories Rs 7,000 to 0
on the cost sheet; the page was read again as the NDC and Rs 7,000 came back.

When a page is sent again (uc03_p2_api._send_page_again), ``record`` keeps the
corrections the retired document carries. Once the new reading is ready,
``settle`` (the worker, every few minutes):

* applies again each correction of a field the new reading also has (a page
  read again as the same type), as the system, naming who made it;
* lists the rest -- a field the new reading does not have (a cost sheet read
  again as the NDC has rows, not four figures) -- in ONE task for the journey's
  PC to re-check on the new reading (CORRECTIONS_RECHECK), closed with a comment;
* settles with nothing to do when the page was not read again (failed or
  replaced), keeping the list on the row.

A correction is the value the field now holds when a person changed it: the
field is marked modified, and its latest CORRECTION in the field change log
says who, in which role, when and why.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

logger = structlog.get_logger(__name__)

ACTOR = "SYSTEM:REREAD_CORRECTIONS"
_BATCH = 50


def _parsed(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def corrections_of(connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: Any) -> list[dict[str, Any]]:
    """The values a person corrected on a document, one per field: the value
    it holds now and who set it."""
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (f.field_key)
                   f.field_key, f.effective_value, f.extracted_value, f.updated_at_utc,
                   l.actor_id, l.actor_role_code, l.remarks, l.changed_at_utc
            FROM auditcore.journey_document_extracted_fields f
            LEFT JOIN LATERAL (
                SELECT c.actor_id, c.actor_role_code, c.remarks, c.changed_at_utc
                FROM auditcore.journey_field_change_log c
                WHERE c.tenant_id = f.tenant_id AND c.journey_id = f.journey_id
                  AND c.di_document_id = f.di_document_id AND c.field_key = f.field_key
                  AND c.change_reason = 'CORRECTION'
                ORDER BY c.changed_at_utc DESC LIMIT 1
            ) l ON true
            WHERE f.tenant_id = :t AND f.journey_id = :j AND f.di_document_id = CAST(:d AS uuid)
              AND f.is_modified
            ORDER BY f.field_key, f.source_fact_version DESC, f.updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": str(document_id)},
    ).mappings().all()
    return [
        {
            "fieldKey": r["field_key"],
            "value": _parsed(r["effective_value"]),
            "readAs": _parsed(r["extracted_value"]),
            "actorId": r["actor_id"],
            "role": (r["actor_role_code"] or "").upper() or None,
            "remarks": r["remarks"],
            "at": (r["changed_at_utc"] or r["updated_at_utc"]).isoformat()
            if (r["changed_at_utc"] or r["updated_at_utc"]) else None,
        }
        for r in rows
        # A value the system set is no correction, except one it carried over
        # from a person's (its remark names who): read again twice -- a cost
        # sheet first as a cost sheet, then as the NDC -- it is kept again.
        if not str(r["actor_id"] or "").upper().startswith("SYSTEM") or r["actor_id"] == ACTOR
    ]


def record(connection: Connection, *, tenant_id: str, journey_id: UUID, from_unit: dict[str, Any],
           to_queue_id: UUID) -> int:
    """Keep the corrections of the document a page sent again leaves behind."""
    document_id = from_unit.get("di_document_id")
    if not document_id:
        return 0
    items = corrections_of(connection, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id)
    if not items:
        return 0
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_reread_corrections
                (tenant_id, to_queue_id, from_queue_id, journey_id, from_document_id, from_document_type, corrections)
            VALUES (:t, :to, :frm, :j, CAST(:d AS uuid), :dt, CAST(:c AS jsonb))
            ON CONFLICT (tenant_id, to_queue_id) DO NOTHING
            """
        ),
        {"t": tenant_id, "to": to_queue_id, "frm": from_unit["queue_id"], "j": journey_id, "d": str(document_id),
         "dt": from_unit.get("template_key") or from_unit.get("classified_document_type"),
         "c": json.dumps(items, default=str)},
    )
    return len(items)


def _field_now(connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: Any,
               field_key: str) -> dict[str, Any] | None:
    return connection.execute(
        text(
            """
            SELECT evidence_id, source_document_type_key, source_canonical_field_id, source_fact_version,
                   confidence_score, extracted_value, effective_value
            FROM auditcore.journey_document_extracted_fields
            WHERE tenant_id = :t AND journey_id = :j AND di_document_id = CAST(:d AS uuid) AND field_key = :k
            ORDER BY source_fact_version DESC, updated_at_utc DESC LIMIT 1
            """
        ),
        {"t": tenant_id, "j": journey_id, "d": str(document_id), "k": field_key},
    ).mappings().one_or_none()


def set_value(connection: Connection, *, tenant_id: str, journey_id: UUID, document_id: Any, field_key: str,
              value: Any, remarks: str, actor_id: str = ACTOR) -> bool:
    """Write ``value`` to a field of a document as a correction by the system,
    with the field change trail; False when the document has no such field."""
    from audit_core.uc03_document_field_corrections import _apply_field_value
    from audit_core.uc03_p2_reviewer import log_field_change, page_stage_code

    field = _field_now(connection, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
                       field_key=field_key)
    if field is None:
        return False
    current = _parsed(field["effective_value"] if field["effective_value"] is not None else field["extracted_value"])
    if current == value:
        return True
    stage = page_stage_code(connection, tenant_id=tenant_id, journey_id=journey_id,
                            di_document_id=UUID(str(document_id))) or "DELIVERY"
    confidence = float(field["confidence_score"]) if field["confidence_score"] is not None else None
    _apply_field_value(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage, document_id=UUID(str(document_id)),
        evidence_id=field["evidence_id"], document_type_key=field["source_document_type_key"], field_key=field_key,
        canonical_field_id=field["source_canonical_field_id"], source_fact_version=int(field["source_fact_version"]),
        confidence_score=confidence, original_value=field["extracted_value"], new_value=value, actor_id=actor_id,
    )
    log_field_change(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage, document_id=UUID(str(document_id)),
        field_key=field_key, canonical_field_id=field["source_canonical_field_id"],
        source_fact_version=int(field["source_fact_version"]), previous_value=json.dumps(current),
        new_value=json.dumps(value), confidence_score=confidence, change_reason="CORRECTION",
        actor_id=actor_id, actor_role_code="SYSTEM", remarks=remarks[:2000],
    )
    return True


def _who(item: dict[str, Any]) -> str:
    role = {"REVIEWER": "the Reviewer", "TL": "the Team Lead", "PC": "the PC"}.get(item.get("role") or "", "a person")
    day = (item.get("at") or "")[:10]
    return f"{role}{' on ' + day if day else ''}"


def _shown(value: Any) -> str:
    if value is None or value == "":
        return "(blank)"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _recheck_task(connection: Connection, *, tenant_id: str, journey_id: UUID, to_queue_id: UUID,
                  from_type: str | None, new_type: str | None, items: list[dict[str, Any]]) -> UUID | None:
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh
    from audit_core.uc03_p2_tasks import CORRECTIONS_RECHECK_TASK

    listed = "; ".join(
        f"{i['fieldKey']}: {_shown(i['readAs'])} → {_shown(i['value'])} by {_who(i)}"
        + (f" ({i['remarks']})" if i.get("remarks") else "")
        for i in items
    )
    count = len(items)
    task_id, _ = raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=f"reread-corrections:{to_queue_id}",
        task_type=CORRECTIONS_RECHECK_TASK, source_type="DOCUMENT_FIELD", source_code=CORRECTIONS_RECHECK_TASK,
        title=f"Re-check {count} correction{'s' if count != 1 else ''} on the page read again",
        description=(
            f"This page was read again as {new_type or 'another document'} (it was {from_type or 'a document'}), "
            f"and the new reading does not carry {'these corrections' if count != 1 else 'this correction'} "
            f"made before: {listed}. Check the same figures on the new reading, correct them there if they are "
            "still wrong, then close this task with a comment."
        ),
        reference={"sourceCode": CORRECTIONS_RECHECK_TASK, "toQueueId": str(to_queue_id),
                   "fields": [i["fieldKey"] for i in items], "corrections": items},
        severity="MEDIUM", registry=get_registry(),
    )
    return task_id


def settle_one(connection: Connection, *, tenant_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """Carry or list one page's corrections once its new reading is ready;
    None while it is still being read."""
    from audit_core.uc03_p2_runtime import note_facts_changed

    unit = connection.execute(
        text("SELECT queue_status, di_document_id, template_key, classified_document_type "
             "FROM auditcore.p2_document_queue WHERE tenant_id = :t AND queue_id = :q"),
        {"t": tenant_id, "q": row["to_queue_id"]},
    ).mappings().one_or_none()
    items = list(row["corrections"] or [])
    if unit is None or unit["queue_status"] in ("FAILED", "DEAD_LETTER", "CANCELLED", "MERGED"):
        return {"result": "NOT_READ", "status": unit["queue_status"] if unit else None}
    if unit["queue_status"] != "READY" or not unit["di_document_id"]:
        return None
    journey_id = UUID(str(row["journey_id"]))
    new_type = unit["template_key"] or unit["classified_document_type"]
    carried, listed = [], []
    for item in items:
        remarks = (f"Kept from before the page was read again: set by {_who(item)}"
                   + (f" ({item['remarks']})" if item.get("remarks") else ""))
        if set_value(connection, tenant_id=tenant_id, journey_id=journey_id, document_id=unit["di_document_id"],
                     field_key=item["fieldKey"], value=item["value"], remarks=remarks):
            carried.append(item["fieldKey"])
        else:
            listed.append(item)
    task_id = None
    if listed:
        task_id = _recheck_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                                to_queue_id=UUID(str(row["to_queue_id"])), from_type=row["from_document_type"],
                                new_type=new_type, items=listed)
    if carried:
        note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="FIELD_CORRECTED")
    return {"result": "SETTLED", "carried": carried, "listed": [i["fieldKey"] for i in listed],
            "taskId": str(task_id) if task_id else None, "documentId": str(unit["di_document_id"])}


def settle(engine: Engine, tenant_ids: list[str]) -> dict[str, int]:
    """Every page sent again whose new reading is ready (or will not come):
    corrections carried or listed. Each page in its own transaction."""
    from audit_core.db import set_tenant_context

    totals = {"settled": 0, "carried": 0, "listed": 0, "notRead": 0, "failed": 0}
    for tenant_id in tenant_ids:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            pending = [dict(r) for r in connection.execute(
                text("SELECT * FROM auditcore.p2_reread_corrections WHERE tenant_id = :t AND settled_at_utc IS NULL "
                     "ORDER BY recorded_at_utc LIMIT :n"),
                {"t": tenant_id, "n": _BATCH},
            ).mappings().all()]
        for row in pending:
            try:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    outcome = settle_one(connection, tenant_id=tenant_id, row=row)
                    if outcome is None:
                        continue
                    connection.execute(
                        text("UPDATE auditcore.p2_reread_corrections SET settled_at_utc = now(), "
                             "outcome = CAST(:o AS jsonb) WHERE tenant_id = :t AND to_queue_id = :q"),
                        {"o": json.dumps(outcome), "t": tenant_id, "q": row["to_queue_id"]},
                    )
                totals["settled"] += 1
                totals["carried"] += len(outcome.get("carried") or [])
                totals["listed"] += len(outcome.get("listed") or [])
                totals["notRead"] += outcome.get("result") == "NOT_READ"
            except Exception:
                totals["failed"] += 1
                logger.warning("reread_corrections_settle_failed", tenant_id=tenant_id,
                               to_queue_id=str(row["to_queue_id"]), exc_info=True)
    if totals["settled"] or totals["failed"]:
        logger.info("reread_corrections_settled", **totals)
    return totals


def run_field_set(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    """FIELD_SET: set named fields of named documents as the system, with the
    field change trail and the remark given (2026-11-10: the Reviewer's
    correction a page read again lost, put back on the new reading).

    Tag ``<tenant_id>:<label>``, a DRY RUN unless the label ends with
    ``-commit``. Each report is JSON: {"journey": reference, "documentId",
    "fieldKey", "value", "remarks"}."""
    from audit_core.db import set_tenant_context
    from audit_core.uc03_p2_runtime import note_facts_changed

    tenant_id, _, label = (tag or "").partition(":")
    if not tenant_id or not label:
        raise ValueError("the tag must be '<tenant_id>:<label>'")
    commit = label.endswith("-commit")
    out: dict[str, Any] = {"commit": commit, "set": [], "skipped": []}
    for raw in reports:
        item = json.loads(raw)
        remarks = str(item.get("remarks") or "").strip()
        if not remarks:
            out["skipped"].append({**item, "why": "no remarks"})
            continue
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            journey_id = connection.execute(
                text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id = :t AND journey_reference = :r"),
                {"t": tenant_id, "r": item["journey"]},
            ).scalar_one_or_none()
            field = journey_id and _field_now(connection, tenant_id=tenant_id, journey_id=journey_id,
                                              document_id=item["documentId"], field_key=item["fieldKey"])
            if not field:
                out["skipped"].append({**item, "why": "no such field on that document"})
                continue
            entry = {"journey": item["journey"], "fieldKey": item["fieldKey"],
                     "from": _parsed(field["effective_value"]), "to": item["value"]}
            if not commit:
                out["set"].append(entry)
                continue
            set_value(connection, tenant_id=tenant_id, journey_id=journey_id, document_id=item["documentId"],
                      field_key=item["fieldKey"], value=item["value"], remarks=remarks)
            note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason="FIELD_CORRECTED")
            out["set"].append(entry)
    return out
