"""An extra copy (SUPPORTING) has no values in the Journey: confirming or
correcting it says so instead of "The extracted field changed" (2026-10-27)."""
from __future__ import annotations

from uuid import uuid4

from conftest import delete_tenant_data
from p2_support import add_batch_pages, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_documents import _EXTRA_COPY_DETAIL, _field_missing_detail


def test_missing_field_on_an_extra_copy_is_explained():
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="extracopy")
    try:
        add_batch_pages(journey, [("booking_form", "READY")])
        with engine.begin() as c:
            set_tenant_context(c, journey.tenant_id)
            copy = _field_missing_detail(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                         document_id=uuid4())
            assert copy == _EXTRA_COPY_DETAIL
            doc = c.execute(text("SELECT di_document_id FROM auditcore.journey_document_extracted_fields "
                                 "WHERE tenant_id=:t AND journey_id=:j LIMIT 1"),
                            {"t": journey.tenant_id, "j": journey.journey_id}).scalar()
            if doc is not None:
                changed = _field_missing_detail(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                                document_id=doc)
                assert "changed" in changed
    finally:
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()
