from __future__ import annotations

from uuid import UUID

import pytest

from audit_core.attendance_context import current_attendance_context
from audit_core.authorization import AuthorizationError
from audit_core.security import HumanPrincipal

USER_ID = "00000000-0000-4000-8000-000000000101"
TENANT_ID = "00000000-0000-4000-8000-000000000201"
DEALER_ID = "00000000-0000-4000-8000-000000000301"
OUTLET_ID = "00000000-0000-4000-8000-000000000401"


class _Result:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def one(self) -> dict[str, object]:
        return self._row


class _Connection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.parameters: dict[str, object] | None = None

    def execute(self, statement: object, parameters: dict[str, object]) -> _Result:
        del statement
        self.parameters = parameters
        return _Result(self.row)


def test_pc_context_returns_existing_assigned_outlet_coordinates() -> None:
    connection = _Connection(
        {
            "operating_role": "PC",
            "operating_role_count": 1,
            "outlets": [
                {
                    "dealerId": DEALER_ID,
                    "outletId": OUTLET_ID,
                    "outletName": "Assigned Outlet",
                    "latitude": 20.2961,
                    "longitude": 85.8245,
                }
            ],
        }
    )

    result = current_attendance_context(
        tenant_id=TENANT_ID,
        human_principal=HumanPrincipal(subject=USER_ID),
        connection=connection,  # type: ignore[arg-type]
    )

    assert result.userId == UUID(USER_ID)
    assert result.operatingRole == "PC"
    assert result.geofenceRequired is True
    assert len(result.outlets) == 1
    assert result.outlets[0].dealerId == UUID(DEALER_ID)
    assert result.outlets[0].outletId == UUID(OUTLET_ID)
    assert connection.parameters == {"tenant_id": TENANT_ID, "actor_id": USER_ID}


def test_tl_context_captures_role_without_geofence_outlets() -> None:
    connection = _Connection(
        {
            "operating_role": "TL",
            "operating_role_count": 1,
            "outlets": [],
        }
    )

    result = current_attendance_context(
        tenant_id=TENANT_ID,
        human_principal=HumanPrincipal(subject=USER_ID),
        connection=connection,  # type: ignore[arg-type]
    )

    assert result.operatingRole == "TL"
    assert result.geofenceRequired is False
    assert result.outlets == []


def test_context_requires_existing_business_assignment() -> None:
    connection = _Connection(
        {
            "operating_role": None,
            "operating_role_count": 0,
            "outlets": [],
        }
    )

    with pytest.raises(AuthorizationError) as exc_info:
        current_attendance_context(
            tenant_id=TENANT_ID,
            human_principal=HumanPrincipal(subject=USER_ID),
            connection=connection,  # type: ignore[arg-type]
        )

    assert exc_info.value.error_code == "VAC-AUTH-004"
