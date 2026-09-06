"""Ensure V2 Review facts carry their durable Audit Core evidence identity.

Document Capture V2 review reads documents from DI first and historically built
ReviewV2Document objects without the Core evidence_id.  Machine persistence and
post-submit review/correction findings need that durable Core identity so evidence
links are never lost.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from audit_core import uc03_document_review_v2 as review_v2

_installed = False
_original_booking_review_data: Any | None = None


def install_uc03_review_evidence_link_patch() -> None:
    global _installed, _original_booking_review_data
    if _installed:
        return

    _original_booking_review_data = review_v2._booking_review_data

    def booking_review_data_with_evidence(*args: Any, **kwargs: Any):
        if _original_booking_review_data is None:
            raise RuntimeError("UC03 review evidence patch is not initialized")

        requirements, documents, _, _ = _original_booking_review_data(*args, **kwargs)
        connection: Connection = kwargs["connection"]
        tenant_id: str = kwargs["tenant_id"]
        journey_id = kwargs["journey_id"]

        document_ids = [document.documentId for document in documents]
        if document_ids:
            rows = connection.execute(
                text(
                    """
                    SELECT di_document_id, evidence_id
                    FROM auditcore.evidence
                    WHERE tenant_id=:tenant_id
                      AND journey_id=:journey_id
                      AND association_status='ACTIVE'
                      AND di_document_id = ANY(:document_ids)
                    ORDER BY linked_at_utc DESC, evidence_id DESC
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "document_ids": document_ids,
                },
            ).mappings().all()
            evidence_by_document = {}
            for row in rows:
                evidence_by_document.setdefault(str(row["di_document_id"]), row["evidence_id"])
            for document in documents:
                if document.evidenceId is None:
                    document.evidenceId = evidence_by_document.get(str(document.documentId))

        # Sources embed evidenceId when attributes are built, so rebuild after the
        # document-level patch instead of returning the stale pre-patch attributes.
        attributes, unmapped = review_v2._build_attributes(
            documents,
            stages=("BOOKING",),
        )
        return requirements, documents, attributes, unmapped

    review_v2._booking_review_data = booking_review_data_with_evidence  # type: ignore[assignment]
    _installed = True
