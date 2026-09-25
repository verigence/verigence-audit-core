import inspect

from audit_core.uc03_simplified_booking_flow import (
    create_booking_journey_first_reference,
    submit_booking_from_review,
)
from audit_core.uc03_simplified_create_atomic import (
    execute_simplified_create_booking_atomic,
)


def test_simplified_booking_create_never_mutates_append_only_or_customer_reference() -> None:
    """Journey reference is written at Customer insert; no post-create rewrite is allowed."""

    route_source = inspect.getsource(create_booking_journey_first_reference)
    atomic_source = inspect.getsource(execute_simplified_create_booking_atomic)

    assert "UPDATE auditcore.journey_workflow_events" not in route_source
    assert "UPDATE auditcore.customers" not in route_source
    assert "UPDATE auditcore.journey_workflow_events" not in atomic_source
    assert "UPDATE auditcore.customers" not in atomic_source
    assert "gen_random_uuid() AS journey_id" in atomic_source
    assert "ids.journey_id::text" in atomic_source
    assert "customer_id, journey_id" in atomic_source


def test_submit_no_longer_blocks_on_unreviewed_low_confidence_fields() -> None:
    # Document completeness is the sole criterion for Booking to finish
    # (2026-09-13 design change) -- confidence review is a separate,
    # always-available concern, not a precondition for Submit. Previously
    # this hard-blocked Submit on any unreviewed <90% field. Source-inspected
    # rather than exercised end-to-end: the full execute() body needs a large
    # requirements/documents/version fixture that adds nothing to this
    # specific assertion (which is purely about what condition gates Submit).
    source = inspect.getsource(submit_booking_from_review)
    assert "_unreviewed_low_confidence_count" not in source
    assert "VAC-CONFLICT-012" not in source
    # The real, sole completion criterion must still be the deciding factor.
    assert "_mandatory_booking_documents_complete(" in source
