from uuid import UUID

import audit_core.uc03_document_capture_v2 as capture_v2
from audit_core.security import HumanPrincipal


JOURNEY_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_v2_capture_authorization_does_not_take_stage_write_lock(monkeypatch) -> None:
    observed: dict[str, bool] = {}

    monkeypatch.setattr(capture_v2, "_scope", lambda *args, **kwargs: {})
    monkeypatch.setattr(capture_v2, "_require_active_booking", lambda state: None)

    def fake_capture_phase_state(
        connection,
        *,
        tenant_id: str,
        journey_id: UUID,
        for_update: bool = False,
    ):
        observed["for_update"] = for_update
        return {
            "business_status": "IN_PROGRESS",
            "capture_completed_at_utc": None,
            "version_no": 1,
        }

    monkeypatch.setattr(capture_v2, "_capture_phase_state", fake_capture_phase_state)

    result = capture_v2._authorize_booking(
        object(),
        tenant_id="tenant-1",
        journey_id=JOURNEY_ID,
        human_principal=HumanPrincipal(subject="pc-user-1"),
        authorization_client=object(),
    )

    assert observed["for_update"] is False
    assert result["business_status"] == "IN_PROGRESS"
