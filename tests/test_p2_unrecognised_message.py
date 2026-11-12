"""The message on a page no document type fits (product owner 2026-11-12): "system doesn't
understand the document. Pls recheck and if required submit again. For issues with quality,
continue with same error message"."""
from __future__ import annotations

from datetime import UTC, datetime

from audit_core import uc03_p2_worker as worker


def _outcome(di_item):
    return worker.classify_page_outcome(di_item=di_item, extracted_count=0, submitted_at=datetime.now(UTC),
                                        facts_copy=None, now=datetime.now(UTC))


def test_a_page_no_type_fits_says_the_system_does_not_understand_it() -> None:
    outcome = _outcome({"state": "UNKNOWN"})
    assert outcome.status == "SUPPORTING"
    assert outcome.reason == ("The system did not understand this page. It is kept in Photos and other documents "
                              "(Extras) and is not read. If it needs to be read, check with your TL and upload it "
                              "again.")


def test_a_page_refused_for_its_scan_quality_keeps_its_own_message() -> None:
    outcome = _outcome({"state": "FAILED", "failureCode": "DI_QUALITY_IMAGE_BLUR_SCORE",
                        "failureDetail": "the page is blurred"})
    assert outcome.reason.startswith("Page rejected: the page is blurred.")
    assert "did not understand" not in outcome.reason


def test_a_page_not_understood_is_kept_in_the_extras_and_is_not_a_vehicle_photo():
    """"These documents which are not recognized can be uploaded in the photos ... otherwise they
    are copied to the extras folder"."""
    from conftest import delete_tenant_data
    from p2_support import add_batch_pages, create_p2_journey, database_engine
    from sqlalchemy import text

    from audit_core.db import set_tenant_context
    from audit_core.uc03_p2_stage import vehicle_proof
    from audit_core.uc03_p2_vehicle_photos import (
        _active_count,
        add_unrecognised_page,
        remove_unrecognised_page,
    )

    engine = database_engine()
    journey = create_p2_journey(engine, prefix="extra")
    try:
        batch_id, pages = add_batch_pages(journey, [("pan_card", "SUPPORTING"), (None, "SUPPORTING")])
        queue_id = pages[1]["queue_id"]
        with engine.begin() as c:
            set_tenant_context(c, journey.tenant_id)
            assert add_unrecognised_page(c, tenant_id=journey.tenant_id, queue_id=queue_id) is True
            assert add_unrecognised_page(c, tenant_id=journey.tenant_id, queue_id=queue_id) is False  # once
            [extra] = c.execute(
                text("SELECT object_key, view_code, original_filename FROM auditcore.delivery_vehicle_photos "
                     "WHERE tenant_id=:t AND deleted_at_utc IS NULL"), {"t": journey.tenant_id},
            ).mappings().all()
            assert extra["view_code"] == "EXTRA" and extra["object_key"] == pages[1]["object_key"]
            assert extra["original_filename"].endswith(" - page 2")
            # Not a vehicle photo: no proof of the vehicle, no room taken from the 24.
            assert vehicle_proof(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["photos"] == 0
            assert _active_count(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id) == 0
            # Given a type and read after all: it leaves the Extras.
            remove_unrecognised_page(c, tenant_id=journey.tenant_id, queue_id=queue_id, actor_id="pc")
            assert c.execute(
                text("SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos WHERE tenant_id=:t "
                     "AND deleted_at_utc IS NULL"), {"t": journey.tenant_id},
            ).scalar_one() == 0
    finally:
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()
