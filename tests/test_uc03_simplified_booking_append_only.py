import inspect

from audit_core.uc03_simplified_booking_flow import (
    create_booking_journey_first_reference,
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
