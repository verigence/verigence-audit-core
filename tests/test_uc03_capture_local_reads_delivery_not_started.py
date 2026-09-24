from uuid import UUID

from audit_core.security import HumanPrincipal
from audit_core.uc03_capture_local_reads import get_delivery_capture_local_v2

JOURNEY_ID = UUID("22222222-2222-2222-2222-222222222222")


class _FakeMappingResult:
    def mappings(self):
        return self

    def one_or_none(self):
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, statement, params=None):
        self.executed.append(str(statement))
        return _FakeMappingResult()


def test_delivery_capture_local_read_allows_delivery_not_yet_started(monkeypatch) -> None:
    """Direct user correction (2026-09-24): this endpoint used to call
    _authorize_delivery, which raises VAC-NF-005 ("Start Delivery before
    capturing Delivery documents") whenever journey_stage_states has no
    DELIVERY row yet -- i.e. for every journey still in Booking, the normal
    case for a PC opening the combined Documents checklist before Delivery
    has started. That 404 was silently swallowed by the frontend's React
    Query (retry: false, no error banner wired to this specific query), so
    the whole Delivery half of the checklist rendered empty with every
    visible card -- including "Missing" placeholders -- defaulting to a
    "BOOKING" label and no indication anything had failed. This is a
    read-only preview endpoint and must succeed (with submitted=False) even
    when Delivery hasn't started, exactly like booking/capture-local next to
    it stays readable after Booking closure.
    """
    scope_calls: list[dict[str, object]] = []
    globals_ = get_delivery_capture_local_v2.__globals__

    monkeypatch.setitem(
        globals_,
        "_scope",
        lambda connection, **kwargs: scope_calls.append(kwargs),
    )
    monkeypatch.setitem(
        globals_,
        "_delivery_requirements",
        lambda connection, tenant_id, journey_id: [],
    )
    monkeypatch.setitem(
        globals_,
        "_linked_delivery_documents",
        lambda connection, tenant_id, journey_id: [],
    )

    connection = _FakeConnection()
    result = get_delivery_capture_local_v2(
        tenant_id="tenant-1",
        journey_id=JOURNEY_ID,
        human_principal=HumanPrincipal(subject="pc-user"),
        authorization_client=object(),
        connection=connection,
    )

    assert result.journeyId == JOURNEY_ID
    assert result.phase == "DELIVERY"
    assert result.submitted is False
    assert scope_calls[0]["tenant_id"] == "tenant-1"
    assert any("seed_delivery_document_requirements" in sql for sql in connection.executed)
