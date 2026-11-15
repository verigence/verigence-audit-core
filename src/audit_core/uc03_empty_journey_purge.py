"""Delete the journeys nothing was ever read for (product owner 2026-11-15).

    "delete empty journeys and close their all task.. clean them from system"

A journey is empty when no document of it is on file (no ACTIVE evidence) and
nothing of it is still being read. Its tasks -- mostly "send the pages again"
for an upload that was never readable -- go with it.

A journey is kept for its first day (product owner 2026-11-15: "delete empty
journey or journey even 1 day old"), and while any page of it is still being
read, so a booking opened today is never deleted from under the PC.

The deletion is the system journey delete (Document Intelligence, R2, Audit
Core, recorded in ``p2_journey_deletions`` with the reason), through
uc03_duplicate_journey_purge, whose guards stay (a reviewed or closed journey is
refused). A DRY RUN unless the tag ends with "-commit"; ``reports`` may list
journey references to limit the run, each still held to the rules above.

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by)
    VALUES ('EMPTY_JOURNEY_PURGE', '<tenant_id>:empty-2026-11-15-commit', '{}', '<who>');
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.uc03_duplicate_journey_purge import (
    parse_tag,
    run_duplicate_journey_purge,
)
from audit_core.uc03_p2_journey_views import _IN_FLIGHT

logger = structlog.get_logger(__name__)

REASON = "Empty journey removed on the product owner's instruction: no document was ever read for it"
GRACE_DAYS = 1

_EMPTY_SQL = """
    SELECT j.journey_reference FROM auditcore.journeys j
    WHERE j.tenant_id=:t AND j.created_at_utc < now() - make_interval(days => :grace)
      AND (CAST(:refs AS text[]) IS NULL OR j.journey_reference = ANY(CAST(:refs AS text[])))
      AND NOT EXISTS (SELECT 1 FROM auditcore.evidence e
                      WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id AND e.association_status='ACTIVE')
      AND NOT EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                      WHERE q.tenant_id=j.tenant_id AND q.journey_id=j.journey_id
                        AND q.queue_status = ANY(CAST(:in_flight AS text[])))
    ORDER BY j.created_at_utc
"""


def empty_journeys(engine: Engine, *, tenant_id: str, references: list[str] | None = None) -> list[str]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        return [str(r) for r in connection.execute(
            text(_EMPTY_SQL), {"t": tenant_id, "grace": GRACE_DAYS, "refs": references or None,
                               "in_flight": list(_IN_FLIGHT)}).scalars().all()]


def run_empty_journey_purge(engine: Engine, tag: str, reports: list[str], **clients: Any) -> dict[str, Any]:
    tenant_id, commit = parse_tag(tag)
    listed = [ref for entry in reports or [] for ref in str(entry).replace(",", " ").split() if ref]
    references = empty_journeys(engine, tenant_id=tenant_id, references=listed or None)
    logger.info("empty_journey_purge_selected", tag=tag, tenant_id=tenant_id, commit=commit,
                listed=len(listed), empty=len(references), references=references)
    totals = run_duplicate_journey_purge(engine, tag, references, reason=REASON, **clients)
    return {**totals, "empty": len(references)}
