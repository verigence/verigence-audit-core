"""Phase 2 upload status: what a PC or TL watches while documents are read.

There is no submit step and no button completes a stage: completion is rule
driven from the documents themselves (uc03_p2_stage). This module only
reports the counts shown on the booking screen.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

_NOT_CLASSIFIED = (
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING", "CLASSIFYING", "RETRY_WAIT",
)
_BATCH_IN_FLIGHT = ("AWAITING_UPLOAD", "UPLOADED", "SPLITTING")
_NOT_EXTRACTED = ("FAILED", "DEAD_LETTER", "NEEDS_REVIEW")


def upload_status(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """Uploaded, identified, read, not read, supporting and duplicate counts."""
    row = connection.execute(
        text(
            f"""
            WITH units AS (
              SELECT q.unit_kind, q.queue_status, q.page_sha256
              FROM auditcore.p2_document_queue q
              WHERE q.tenant_id=:t AND q.journey_id=:j AND q.queue_status <> 'CANCELLED'
            ),
            docs AS (SELECT * FROM units WHERE queue_status <> 'MERGED')
            SELECT
              (SELECT COUNT(*) FROM docs) AS documents,
              (SELECT COUNT(*) FROM units WHERE unit_kind='PAGE') AS pages,
              (SELECT COUNT(*) FROM docs WHERE queue_status NOT IN {_NOT_CLASSIFIED}) AS classified,
              (SELECT COUNT(*) FROM docs WHERE queue_status='READY') AS extracted,
              (SELECT COUNT(*) FROM docs WHERE queue_status='SUPPORTING') AS supporting,
              (SELECT COUNT(*) FROM docs WHERE queue_status IN {_NOT_EXTRACTED}) AS not_extracted,
              (SELECT COUNT(*) FROM docs WHERE queue_status IN {_NOT_CLASSIFIED}) AS not_classified,
              (SELECT COALESCE(SUM(n - 1), 0) FROM (
                 SELECT COUNT(*) AS n FROM units
                 WHERE unit_kind='PAGE' AND page_sha256 IS NOT NULL
                 GROUP BY page_sha256 HAVING COUNT(*) > 1
               ) d) AS duplicates,
              (SELECT COUNT(*) FROM auditcore.p2_upload_batches b
                WHERE b.tenant_id=:t AND b.journey_id=:j AND b.batch_status IN {_BATCH_IN_FLIGHT}) AS uploading
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    return {
        "counts": {
            "documents": int(row["documents"] or 0),
            "pages": int(row["pages"] or 0),
            "uploading": int(row["uploading"] or 0),
            "classified": int(row["classified"] or 0),
            "extracted": int(row["extracted"] or 0),
            "supporting": int(row["supporting"] or 0),
            "notExtracted": int(row["not_extracted"] or 0),
            "notClassified": int(row["not_classified"] or 0),
            "duplicates": int(row["duplicates"] or 0),
        },
    }
