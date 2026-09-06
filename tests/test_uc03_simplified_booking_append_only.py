import inspect

from audit_core.uc03_simplified_booking_flow import (
    create_booking_journey_first_reference,
)


def test_simplified_booking_create_never_updates_append_only_workflow_events() -> None:
    """BOOKING_CREATED is append-only; create must not rewrite its safe payload."""

    source = inspect.getsource(create_booking_journey_first_reference)
    assert "UPDATE auditcore.journey_workflow_events" not in source
    assert "UPDATE auditcore.customers" in source
