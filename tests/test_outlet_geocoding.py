import httpx
import pytest
from fastapi.testclient import TestClient

from audit_core import outlet_geocoding
from audit_core.dependencies import HumanAdminRequest, require_project_admin_request
from audit_core.errors import AuditCoreError
from audit_core.main import app
from audit_core.outlet_geocoding import OutletGeocoder, get_outlet_geocoder
from audit_core.security_integration import SecurityAdminContext

KEY = "test-maps-key-0000"
OK_BODY = {
    "status": "OK",
    "results": [
        {
            "formatted_address": "Cuttack Sadar, Cuttack, Odisha 753001, India",
            "place_id": "place-123",
            "geometry": {"location": {"lat": 20.4625, "lng": 85.8828}, "location_type": "ROOFTOP"},
        }
    ],
}


class _Google:
    """A fake Google that records every call it receives."""

    def __init__(self, body=None, status=200, error=None):
        self.body = OK_BODY if body is None else body
        self.status = status
        self.error = error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error:
            raise self.error
        return httpx.Response(self.status, json=self.body)


def _geocoder(fake: _Google, clock=None) -> OutletGeocoder:
    kwargs = {"clock": clock} if clock else {}
    return OutletGeocoder(api_key=KEY, transport=httpx.MockTransport(fake), **kwargs)


def test_returns_coordinates_with_precision_and_sends_the_agreed_request():
    fake = _Google()
    got = _geocoder(fake).geocode("Cuttack Sadar, Cuttack, Odisha, 753001")
    assert (got.latitude, got.longitude) == (20.4625, 85.8828)
    assert got.googlePlaceId == "place-123" and got.precision == "ROOFTOP"
    assert got.approximate is False and got.resultCount == 1
    params = dict(fake.requests[0].url.params)
    assert params["components"] == "country:IN" and params["key"] == KEY
    assert len(fake.requests) == 1


def test_non_rooftop_results_are_marked_approximate():
    body = {"status": "OK", "results": [{
        "formatted_address": "Cuttack, Odisha, India", "partial_match": True,
        "geometry": {"location": {"lat": 20.46, "lng": 85.88}, "location_type": "APPROXIMATE"},
    }]}
    got = _geocoder(_Google(body)).geocode("somewhere in Cuttack")
    assert got.approximate is True and got.partialMatch is True and got.googlePlaceId is None


def test_no_result_is_a_business_error_not_an_outage():
    with pytest.raises(AuditCoreError) as exc:
        _geocoder(_Google({"status": "ZERO_RESULTS", "results": []})).geocode("nowhere at all")
    assert exc.value.status_code == 422


@pytest.mark.parametrize("status", ["REQUEST_DENIED", "OVER_QUERY_LIMIT", "UNKNOWN_ERROR", "INVALID_REQUEST"])
def test_google_failures_are_503_and_never_leak_the_key_or_google_message(status):
    fake = _Google({"status": status, "error_message": f"The key {KEY} is invalid"})
    with pytest.raises(AuditCoreError) as exc:
        _geocoder(fake).geocode("Cuttack Sadar")
    assert exc.value.status_code == 503 and KEY not in exc.value.detail
    assert len(fake.requests) == 1  # never retried


@pytest.mark.parametrize(
    "fake",
    [_Google(status=500), _Google(error=httpx.ConnectTimeout("slow")), _Google(error=httpx.ConnectError("down"))],
)
def test_network_trouble_is_503_with_a_single_attempt(fake):
    with pytest.raises(AuditCoreError) as exc:
        _geocoder(fake).geocode("Cuttack Sadar")
    assert exc.value.status_code == 503 and len(fake.requests) == 1


def test_a_match_outside_india_is_refused():
    body = {"status": "OK", "results": [{
        "formatted_address": "Springfield, USA",
        "geometry": {"location": {"lat": 39.78, "lng": -89.65}, "location_type": "ROOFTOP"},
    }]}
    with pytest.raises(AuditCoreError) as exc:
        _geocoder(_Google(body)).geocode("Springfield")
    assert exc.value.status_code == 422 and "outside India" in exc.value.detail


def test_rate_limit_protects_the_bill_and_recovers_after_a_minute():
    now = [1000.0]
    fake = _Google()
    geocoder = _geocoder(fake, clock=lambda: now[0])
    for _ in range(20):
        geocoder.geocode("Cuttack Sadar")
    with pytest.raises(AuditCoreError) as exc:
        geocoder.geocode("Cuttack Sadar")
    assert exc.value.status_code == 429 and len(fake.requests) == 20
    now[0] += 61
    geocoder.geocode("Cuttack Sadar")
    assert len(fake.requests) == 21


# ---- the HTTP endpoint ------------------------------------------------------------------------

ADMIN = HumanAdminRequest(
    user_id="admin-1",
    bearer_token="t",
    admin_context=SecurityAdminContext(user_id="admin-1", is_super_admin=True, admin_scopes=()),
)
URL = "/v1/tenants/tenant-1/outlet-location/geocode"


@pytest.fixture()
def client():
    app.dependency_overrides.clear()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_endpoint_returns_the_location_for_an_admin(client):
    fake = _Google()
    app.dependency_overrides[require_project_admin_request] = lambda: ADMIN
    app.dependency_overrides[get_outlet_geocoder] = lambda: _geocoder(fake)
    r = client.post(URL, json={"addressText": "Station Road", "city": "Cuttack", "stateRegion": "Odisha", "postalCode": "753001"})
    assert r.status_code == 200 and r.json()["latitude"] == 20.4625
    assert dict(fake.requests[0].url.params)["address"] == "Station Road, Cuttack, Odisha, 753001"


def test_endpoint_needs_the_admin_check_and_cannot_be_called_anonymously(client):
    app.dependency_overrides[get_outlet_geocoder] = lambda: _geocoder(_Google())
    r = client.post(URL, json={"addressText": "Station Road", "city": "Cuttack"})
    assert r.status_code in (401, 403)


def test_empty_address_is_refused_before_google_is_called(client):
    fake = _Google()
    app.dependency_overrides[require_project_admin_request] = lambda: ADMIN
    app.dependency_overrides[get_outlet_geocoder] = lambda: _geocoder(fake)
    r = client.post(URL, json={"city": "  "})
    assert r.status_code == 422 and fake.requests == []


def test_missing_key_says_so_plainly(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    outlet_geocoding._shared_geocoder.cache_clear()
    app.dependency_overrides[require_project_admin_request] = lambda: ADMIN
    r = client.post(URL, json={"addressText": "Station Road", "city": "Cuttack"})
    assert r.status_code == 503 and "not set up" in r.json()["detail"]
    outlet_geocoding._shared_geocoder.cache_clear()
