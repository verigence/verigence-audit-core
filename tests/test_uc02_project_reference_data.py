from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from audit_core.dependencies import HumanAdminRequest, get_engine, require_super_admin_request
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


def test_project_reference_data_exposes_active_seeded_masters() -> None:
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

        oems_by_code = {item["oemCode"]: item for item in body["oems"]}
        for code, name in _EXPECTED_OEMS.items():
            assert oems_by_code[code]["oemName"] == name
            assert oems_by_code[code]["oemId"]

        categories_by_code = {
            item["categoryCode"]: item for item in body["productCategories"]
        }
        four_wheelers = categories_by_code["FOUR_WHEELERS"]
        assert four_wheelers["categoryName"] == "Four Wheelers"
        assert four_wheelers["productCategoryId"]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
