"""Automatic retries stop once the PC has closed the page's task.

Product owner, 2026-10-27: "if the task is closed, then automatic retries
shouldn't happen, else keep it continued as normal."

The automatic attempts are the worker's own: the T0 / +10 / +20 minute retries
of a page's document steps (DOCUMENT_INGEST, PAGE_REREAD; uc03_p2_worker._fail)
and the operator's one-off page re-send (uc03_p2_page_resend). A page is
stopped when a person has disposed of it after the last time a person asked
for it to be read (its read clock, ``read_clock_started_at_utc``):

- a document task naming the page was closed by a person: Close task
  (``CLOSED_BY_PC``), Accept as exception (``EXCEPTION_ACCEPTED``), or closed
  because a person removed the page (``removedPage`` in its result);
- the page was removed by a person (PAGE_REMOVED);
- the page was kept as Others (PAGE_KEPT_AS_OTHER).

A task names a page through its reference: ``queueId`` (unclassified page),
``pages[].queueId`` ("Document to resubmit") or ``pageQueues[].queueId``
(image quality).

Deliberately not stopped (the most conservative reading, listed in the PR):
a task the system closed because the condition went away -- the page was read,
sent again or uploaded again. That is the PC asking for the page to be read,
and its retries continue as normal. Nor is a page stopped while any open task
still names it, and a person's later Retry / Read again / Send again restarts
its clock, so its retries run as normal again.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

# Outcomes a person's own action writes when it closes a task.
PERSON_CLOSED_OUTCOMES = ("CLOSED_BY_PC", "EXCEPTION_ACCEPTED")
_OPEN_TASK_EXCLUDED = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")
STOPPED_CODE = "AUTO_RETRY_STOPPED"
STOPPED_EVENT = "AUTOMATIC_RETRY_STOPPED"


def task_names_page_sql(alias: str = "t", queue: str = "CAST(:q AS text)") -> str:
    """SQL condition: the p2_tasks row ``alias`` names the page ``queue``
    (a text expression; by default the ``:q`` parameter)."""
    return (
        f"({alias}.reference->>'queueId' = {queue}"
        f" OR (jsonb_typeof({alias}.reference->'pages') = 'array'"
        f"     AND {alias}.reference->'pages' @> jsonb_build_array(jsonb_build_object('queueId', {queue})))"
        f" OR (jsonb_typeof({alias}.reference->'pageQueues') = 'array'"
        f"     AND {alias}.reference->'pageQueues' @> jsonb_build_array(jsonb_build_object('queueId', {queue})))"
        f" OR {alias}.completion_result->'removedPage'->>'queueId' = {queue})"
    )


def person_closed_page_task_sql(queue_alias: str = "q") -> str:
    """SQL condition on the p2_document_queue row ``queue_alias``: a person
    closed a task naming this page (Close task, Accept as exception) after the
    page was last asked to be read, and no open task names it. The same
    disposal that stops its automatic retries. Placeholders: ``:t``, ``:j``,
    ``:person_outcomes``, ``:closed_statuses`` (bind ``person_closed_params()``)."""
    queue = f"CAST({queue_alias}.queue_id AS text)"
    return (
        "(EXISTS (SELECT 1 FROM auditcore.p2_tasks pt"
        "   WHERE pt.tenant_id=:t AND pt.journey_id=:j AND pt.task_status IN ('VERIFIED_COMPLETE','CANCELLED')"
        f"    AND {task_names_page_sql('pt', queue)}"
        "     AND pt.completion_result->>'outcome' = ANY(:person_outcomes)"
        "     AND COALESCE(pt.verified_at_utc, pt.updated_at_utc)"
        f"        >= COALESCE({queue_alias}.read_clock_started_at_utc, '-infinity'::timestamptz))"
        " AND NOT EXISTS (SELECT 1 FROM auditcore.p2_tasks ot"
        "   WHERE ot.tenant_id=:t AND ot.journey_id=:j AND ot.task_status <> ALL(:closed_statuses)"
        f"    AND {task_names_page_sql('ot', queue)}))"
    )


def person_closed_params() -> dict[str, Any]:
    return {"person_outcomes": list(PERSON_CLOSED_OUTCOMES), "closed_statuses": list(_OPEN_TASK_EXCLUDED)}


def automatic_retry_stop_reason(
    connection: Connection, *, tenant_id: str, journey_id: UUID, queue_id: UUID | str,
) -> str | None:
    """Why the worker must not try this page again by itself, or None when
    its automatic retries continue as normal."""
    row = connection.execute(
        text(
            f"""
            WITH unit AS (
              SELECT COALESCE(read_clock_started_at_utc, '-infinity'::timestamptz) AS clock
              FROM auditcore.p2_document_queue
              WHERE tenant_id=:t AND journey_id=:j AND queue_id=CAST(CAST(:q AS text) AS uuid)
            )
            SELECT
              EXISTS (
                SELECT 1 FROM auditcore.p2_tasks t
                WHERE t.tenant_id=:t AND t.journey_id=:j AND t.task_status <> ALL(:closed)
                  AND {task_names_page_sql("t")}
              ) AS open_task,
              (SELECT t.title FROM auditcore.p2_tasks t, unit
                WHERE t.tenant_id=:t AND t.journey_id=:j AND t.task_status IN ('VERIFIED_COMPLETE','CANCELLED')
                  AND {task_names_page_sql("t")}
                  AND (t.completion_result->>'outcome' = ANY(:outcomes) OR t.completion_result ? 'removedPage')
                  AND COALESCE(t.verified_at_utc, t.updated_at_utc) >= unit.clock
                ORDER BY COALESCE(t.verified_at_utc, t.updated_at_utc) DESC LIMIT 1) AS closed_task,
              (SELECT e.event_type FROM auditcore.p2_activity_events e, unit
                WHERE e.tenant_id=:t AND e.journey_id=:j AND e.subject_id=CAST(:q AS text)
                  AND e.event_type IN ('PAGE_REMOVED','PAGE_KEPT_AS_OTHER')
                  AND e.created_at_utc >= unit.clock
                ORDER BY e.event_id DESC LIMIT 1) AS person_event,
              EXISTS (SELECT 1 FROM unit) AS known
            """
        ),
        {"t": tenant_id, "j": journey_id, "q": str(queue_id), "closed": list(_OPEN_TASK_EXCLUDED),
         "outcomes": list(PERSON_CLOSED_OUTCOMES)},
    ).mappings().one()
    if not row["known"] or row["open_task"]:
        return None
    if row["closed_task"]:
        return f'its task "{row["closed_task"]}" was closed'
    if row["person_event"] == "PAGE_REMOVED":
        return "it was removed from the journey"
    if row["person_event"] == "PAGE_KEPT_AS_OTHER":
        return "it was kept as Others"
    return None


def stop_reason_text(why: str) -> str:
    """The page's reason once its automatic retries stop."""
    return (f"Not tried again automatically: {why}. Retry the page from the card, or upload it again, "
            "if it still needs to be read.")


def stopped_details(work_type: str, work_key: str, why: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"workType": work_type, "workKey": work_key, "why": why, **(extra or {})}
