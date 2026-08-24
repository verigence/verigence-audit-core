from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext

_EXPECTED_OEMS = {
    "MAHINDRA": "Mahindra",
    "HYUNDAI": "Hyundai",
    "MARUTI": "Maruti",
    "MERCEDES_BENZ": "Mercedes Benz",
    "BMW": "BMW",
    "SKODA": "Skoda",
    "VOLKSWAGEN": "Volkswagen",
    "TATA_MOTORS": "Tata Motors",
}
_EXPECTED_SEGMENTS = {
    "PASSENGER_VEHICLE": "Passenger Vehicle",
    "COMMERCIAL": "Commercial",
    "BATTERY_ELECTRIC": "Battery Electric",
}


def test_project_reference_data_exposes_universal_segments() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 project reference integration tests")

    engine = create_engine(database_url)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="superadmin-reference-data",
        bearer_token="same-human-superadmin-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-reference-data",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/v1/project-reference-data"
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert {
            item["oemCode"]: item["oemName"] for item in body["oems"]
        } == _EXPECTED_OEMS
        assert all(item["oemId"] for item in body["oems"])
        assert all("segments" not in item for item in body["oems"])
        assert "productCategories" not in body

        assert {
            item["segmentCode"]: item["segmentName"] for item in body["segments"]
        } == _EXPECTED_SEGMENTS
        assert all(item["segmentId"] for item in body["segments"])
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
