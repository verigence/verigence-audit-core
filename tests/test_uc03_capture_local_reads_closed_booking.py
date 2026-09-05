from uuid import UUID

from audit_core.security import HumanPrincipal
from audit_core.uc03_capture_local_reads import (
    get_booking_capture_local_v2,
)

JOURNEY_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_booking_capture_local_read_allows_closed_booking(monkeypatch) -> None:
    scope_calls: list[dict[str, object]] = []
    state_calls: list[dict[str, object]] = []
    globals_ = get_booking_capture_local_v2.__globals__

    monkeypatch.setitem(
        globals_,
        "_scope",
        lambda connection, **kwargs: scope_calls.append(kwargs),
    )
    monkeypatch.setitem(
        globals_,
        "_capture_phase_state",
        lambda connection, **kwargs: (
            state_calls.append(kwargs)
            or {
                "business_status": "BOOKING_CLOSED",
                "capture_completed_at_utc": object(),
                "version_no": 9,
            }
        ),
    )
    monkeypatch.setitem(globals_, "_base_requirements", lambda *args: [])
    monkeypatch.setitem(globals_, "_declarations", lambda *args: {})
    monkeypatch.setitem(globals_, "_linked_documents", lambda *args: [])

    result = get_booking_capture_local_v2(
        tenant_id="tenant-1",
        journey_id=JOURNEY_ID,
        human_principal=HumanPrincipal(subject="pc-user"),
        authorization_client=object(),
        connection=object(),
    )

    assert result.journeyId == JOURNEY_ID
    assert result.phase == "BOOKING"
    assert scope_calls[0]["tenant_id"] == "tenant-1"
    assert state_calls[0] == {
        "tenant_id": "tenant-1",
        "journey_id": JOURNEY_ID,
        "for_update": False,
    }
