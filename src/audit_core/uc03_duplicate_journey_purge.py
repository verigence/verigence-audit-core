"""Remove the duplicate journeys the product owner listed (2026-10-31).

    "Also pls remove all the journey which have same PAN, Customer Name,
    Model, Dealership."

He chose the journeys himself (the copy kept in each group is its most
progressed file; doubtful groups were left out), so this job deletes exactly
the references it is given and nothing else:

- the tenant comes from the tag, ``<tenant_id>:<label>``; the job refuses a
  tag without one, or a tenant that is not a Project;
- each journey comes from ``reports``: one journey reference per entry, looked
  up by exact reference in that tenant only. A reference that names no
  journey, or more than one, is skipped and logged;
- each is deleted by the system journey delete (Document Intelligence, R2,
  Audit Core; recorded in ``p2_journey_deletions`` as SYSTEM, with the reason),
  one at a time, never inside an open transaction. Its guards stay: a journey
  whose documents are marked reviewed, or a closed one, is refused and logged.
- no alert is raised (the same-file alert is for a PC uploading again).

A DRY RUN -- one ``duplicate_journey_purge_item`` log line per reference with
what would be deleted, nothing written -- unless the tag ends with "-commit".
Repeatable: a reference already deleted is reported as not found.

Requested as a row of ``p2_worker_jobs``::

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by)
    VALUES ('DUPLICATE_JOURNEY_PURGE', '<tenant_id>:item8-2026-10-31',
            ARRAY['ADI-RD-261026-039', ...], '<who>');
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.errors import ConflictError, NotFoundError
from audit_core.uc03_p2_journey_delete import (
    _DI_DOCUMENTS_SQL,
    _OBJECT_KEYS_SQL,
    system_delete_journey,
)
from audit_core.uc03_p2_same_file import recheck_pairs_of

logger = structlog.get_logger(__name__)

REASON = "Duplicate journey removed on the product owner's instruction (same PAN, customer name, model, dealership)"

_LOOKUP_SQL = """
    SELECT j.journey_id, j.journey_reference, COALESCE(c.legal_name, c.display_name) AS customer_name,
           j.created_by_display_name AS pc_name, d.dealer_name, o.outlet_name,
           NULLIF(concat_ws(' · ', NULLIF(jp.model_name_snapshot, ''), NULLIF(jp.variant_name_snapshot, '')), '')
             AS vehicle,
           (SELECT count(*) FROM auditcore.p2_upload_batches b
             WHERE b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id) AS uploads,
           (SELECT count(*) FROM auditcore.evidence e
             WHERE e.tenant_id=j.tenant_id AND e.journey_id=j.journey_id AND e.association_status='ACTIVE')
             AS documents_on_file,
           EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews r
                    WHERE r.tenant_id=j.tenant_id AND r.journey_id=j.journey_id) AS reviewed
    FROM auditcore.journeys j
    JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
    JOIN auditcore.dealers d ON d.tenant_id=j.tenant_id AND d.dealer_id=j.dealer_id
    JOIN auditcore.dealer_outlets o
      ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
    LEFT JOIN auditcore.journey_products jp ON jp.tenant_id=j.tenant_id AND jp.journey_id=j.journey_id
    WHERE j.tenant_id=:t AND j.journey_reference=:ref
"""


def parse_tag(tag: str) -> tuple[str, bool]:
    """(tenant_id, commit) from ``<tenant_id>:<label>``; commit when the tag
    ends with "-commit". Raises ValueError without a tenant."""
    tenant_id, sep, label = (tag or "").strip().partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError("DUPLICATE_JOURNEY_PURGE needs the tag '<tenant_id>:<label>' (label ending -commit to delete)")
    return tenant_id.strip(), tag.strip().endswith("-commit")


def _references(reports: list[str]) -> list[str]:
    seen: list[str] = []
    for entry in reports or []:
        for ref in str(entry).replace(",", " ").split():
            if ref and ref not in seen:
                seen.append(ref)
    return seen


def run_duplicate_journey_purge(
    engine: Engine,
    tag: str,
    reports: list[str],
    *,
    security_client: Any = None,
    v2_client: Any = None,
    storage: Any = None,
) -> dict[str, Any]:
    tenant_id, commit = parse_tag(tag)
    references = _references(reports)
    totals: dict[str, Any] = {"listed": len(references), "found": 0, "deleted": 0, "would_delete": 0,
                              "refused": 0, "not_found": 0, "ambiguous": 0, "failed": 0}
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if connection.execute(text("SELECT 1 FROM auditcore.projects WHERE tenant_id=:t LIMIT 1"),
                              {"t": tenant_id}).scalar_one_or_none() is None:
            raise ValueError(f"No Project for tenant {tenant_id!r}")
    if commit and references:
        from audit_core.uc03_document_capture_v2 import (
            get_di_capture_v2_client,
            get_security_oauth_client,
        )
        from audit_core.uc03_p2_storage import get_p2_document_storage

        security_client = security_client or get_security_oauth_client()
        v2_client = v2_client or get_di_capture_v2_client()
        storage = storage or get_p2_document_storage()
    correlation_id = f"duplicate-journey-purge:{tag}"[:128]
    logger.info("duplicate_journey_purge_started", tag=tag, tenant_id=tenant_id, commit=commit,
                references=len(references))
    for reference in references:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            rows = connection.execute(text(_LOOKUP_SQL), {"t": tenant_id, "ref": reference}).mappings().all()
            counts = {"di": 0, "files": 0}
            if len(rows) == 1:
                params = {"tenant_id": tenant_id, "journey_id": rows[0]["journey_id"]}
                counts["di"] = len(connection.execute(text(_DI_DOCUMENTS_SQL), params).scalars().all())
                counts["files"] = len([k for k in connection.execute(text(_OBJECT_KEYS_SQL), params).scalars().all()
                                       if k])
        if len(rows) != 1:
            key = "not_found" if not rows else "ambiguous"
            totals[key] += 1
            logger.warning("duplicate_journey_purge_item", tag=tag, tenant_id=tenant_id, commit=commit,
                           journey_reference=reference, outcome=key.upper(), matches=len(rows))
            continue
        row = rows[0]
        totals["found"] += 1
        item = {
            "journey_reference": reference, "journey_id": str(row["journey_id"]),
            "customer": row["customer_name"], "pc": row["pc_name"], "dealer": row["dealer_name"],
            "outlet": row["outlet_name"], "vehicle": row["vehicle"], "di_documents": counts["di"], "files_recorded": counts["files"],
            "uploads": int(row["uploads"]), "documents_on_file": int(row["documents_on_file"]),
            "reviewed": bool(row["reviewed"]),
        }
        if not commit:
            totals["would_delete"] += 1
            logger.info("duplicate_journey_purge_item", tag=tag, tenant_id=tenant_id, commit=False,
                        outcome="WOULD_DELETE" if not item["reviewed"] else "WOULD_BE_REFUSED_REVIEWED", **item)
            continue
        journey_id = UUID(str(row["journey_id"]))

        def recheck(connection: Connection, gone: UUID = journey_id) -> None:
            # The kept copy's duplicate-booking finding naming this one closes.
            recheck_pairs_of(connection, tenant_id=tenant_id, deleted_journey_id=gone,
                             correlation_id=correlation_id)

        try:
            result = system_delete_journey(
                engine, tenant_id=tenant_id, journey_id=journey_id, reason=REASON,
                correlation_id=correlation_id, security_client=security_client,
                v2_client=v2_client, storage=storage,
                on_deleted=recheck,
            )
        except (ConflictError, NotFoundError) as exc:
            totals["refused"] += 1
            logger.warning("duplicate_journey_purge_item", tag=tag, tenant_id=tenant_id, commit=True,
                           outcome="REFUSED", why=exc.detail, **item)
            continue
        except Exception as exc:  # noqa: BLE001 - one journey's failure does not stop the others
            totals["failed"] += 1
            logger.warning("duplicate_journey_purge_item", tag=tag, tenant_id=tenant_id, commit=True,
                           outcome="FAILED", error=f"{type(exc).__name__}: {exc}"[:300], **item)
            continue
        totals["deleted"] += 1
        logger.info("duplicate_journey_purge_item", tag=tag, tenant_id=tenant_id, commit=True, outcome="DELETED",
                    deletion_id=str(result.deletionId), di_documents_deleted=result.diDocumentsDeleted,
                    storage_objects_deleted=result.storageObjectsDeleted, **item)
    logger.info("duplicate_journey_purge_finished", tag=tag, tenant_id=tenant_id, commit=commit, **totals)
    return totals
