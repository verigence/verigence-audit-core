from uuid import UUID

from audit_core import uc03_delivery_capture_v2, uc03_document_capture_v2
from audit_core.security import HumanPrincipal
from audit_core.uc03_unified_document_capture import get_unified_capture_local

JOURNEY_ID = UUID("11111111-1111-1111-1111-111111111111")


class _FakeMappingResult:
    def mappings(self):
        return self

    def one_or_none(self):
        return None


class _FakeConnection:
    def execute(self, statement, params=None):
        return _FakeMappingResult()


class _FakeCaptureResponse:
    def __init__(self):
        self.requirements = []
        self.uploads = []


def _patch_common(monkeypatch) -> list[dict[str, object]]:
    """Replaces uc03_capture_local_reads.py's two former per-stage tests --
    same two regressions (Delivery not yet started, Booking closed), now
    against the one unified local read. Neither scenario may ever raise:
    get_unified_capture_local calls only _scope (never _authorize_booking/
    _authorize_delivery, which 404/409 on exactly these two states) and its
    own _stage_completed, which treats a missing/incomplete journey_stage_
    states row as simply "not submitted", not an error.
    """
    scope_calls: list[dict[str, object]] = []
    globals_ = get_unified_capture_local.__globals__
    monkeypatch.setitem(
        globals_, "_scope", lambda connection, **kwargs: scope_calls.append(kwargs),
    )
    monkeypatch.setitem(globals_, "_base_requirements", lambda *args: [])
    monkeypatch.setitem(globals_, "_delivery_requirements", lambda *args: [])
    monkeypatch.setitem(globals_, "linked_documents_for_journey", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        uc03_document_capture_v2, "_declarations", lambda *args: {},
    )
    monkeypatch.setattr(
        uc03_document_capture_v2,
        "_build_local_capture_response",
        lambda **kwargs: _FakeCaptureResponse(),
    )
    monkeypatch.setattr(
        uc03_delivery_capture_v2,
        "_build_local_delivery_capture_response",
        lambda **kwargs: _FakeCaptureResponse(),
    )
    return scope_calls


def test_unified_capture_local_read_allows_delivery_not_started(monkeypatch) -> None:
    """Direct user correction (2026-09-24), originally against Delivery's own
    capture-local endpoint: a journey still entirely in Booking (no DELIVERY
    row in journey_stage_states yet) must read successfully with
    deliverySubmitted=False, not 404 -- the normal case for a PC opening the
    combined Documents checklist before Delivery has started.
    """
    scope_calls = _patch_common(monkeypatch)
    result = get_unified_capture_local(
        tenant_id="tenant-1",
        journey_id=JOURNEY_ID,
        human_principal=HumanPrincipal(subject="pc-user"),
        authorization_client=object(),
        connection=_FakeConnection(),
    )
    assert result.journeyId == JOURNEY_ID
    assert result.deliverySubmitted is False
    assert scope_calls[0]["tenant_id"] == "tenant-1"


def test_unified_capture_local_read_allows_closed_booking(monkeypatch) -> None:
    """Originally against Booking's own capture-local endpoint: a Booking
    whose business_status is BOOKING_CLOSED must still read successfully --
    this is a read-only preview endpoint, not a capture/extraction mutation,
    so it must remain readable after Booking closure exactly like Delivery's
    own half stays readable before Delivery has started.
    """
    scope_calls = _patch_common(monkeypatch)
    result = get_unified_capture_local(
        tenant_id="tenant-1",
        journey_id=JOURNEY_ID,
        human_principal=HumanPrincipal(subject="pc-user"),
        authorization_client=object(),
        connection=_FakeConnection(),
    )
    assert result.journeyId == JOURNEY_ID
    assert result.bookingSubmitted is False
    assert scope_calls[0]["tenant_id"] == "tenant-1"
