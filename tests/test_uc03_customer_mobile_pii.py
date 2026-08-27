from uuid import uuid4

from audit_core import uc03_booking_capture
from audit_core.uc03_customer_mobile_pii import install_uc03_customer_mobile_pii


class _Connection:
    def __init__(self) -> None:
        self.parameters = None

    def execute(self, _statement, parameters):
        self.parameters = parameters
        return None


def test_customer_number_capture_persists_full_normalized_mobile(monkeypatch) -> None:
    install_uc03_customer_mobile_pii()
    tenant_id = "tenant-mobile-test"
    journey_id = uuid4()
    customer_id = uuid4()
    connection = _Connection()

    monkeypatch.setattr(
        uc03_booking_capture,
        "_validate_evidence_for_journey",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        uc03_booking_capture,
        "_journey_customer_id",
        lambda *args, **kwargs: customer_id,
    )

    domain, reference = uc03_booking_capture._write_typed_capture(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        field_key="CUSTOMER_NUMBER",
        value="+91 98765-43210",
        source_evidence_id=None,
    )

    assert domain == "CUSTOMER"
    assert reference == str(customer_id)
    assert connection.parameters == {
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "mobile_number": "+919876543210",
        "mobile_last4": "3210",
    }


def test_customer_number_capture_is_case_insensitive(monkeypatch) -> None:
    install_uc03_customer_mobile_pii()
    customer_id = uuid4()
    connection = _Connection()

    monkeypatch.setattr(
        uc03_booking_capture,
        "_validate_evidence_for_journey",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        uc03_booking_capture,
        "_journey_customer_id",
        lambda *args, **kwargs: customer_id,
    )

    uc03_booking_capture._write_typed_capture(
        connection,
        tenant_id="tenant-mobile-test",
        journey_id=uuid4(),
        field_key="customer_number",
        value="98765 40001",
        source_evidence_id=None,
    )

    assert connection.parameters["mobile_number"] == "9876540001"
    assert connection.parameters["mobile_last4"] == "0001"
