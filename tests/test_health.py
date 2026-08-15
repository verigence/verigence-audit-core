from audit_core.main import app
from fastapi.testclient import TestClient


client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_only_exposed_route() -> None:
    assert {route.path for route in app.routes} == {"/health"}
