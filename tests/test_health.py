from fastapi.testclient import TestClient

from audit_core.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_only_implemented_routes_are_exposed() -> None:
    assert {route.path for route in app.routes} == {
        "/health",
        "/v1/tenants/{tenant_id}/project",
    }
