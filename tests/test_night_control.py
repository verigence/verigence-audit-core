"""Night control (product owner, 6 Nov 2026): 20:00-08:30 IST nobody opens Journey 360
or downloads a file unless SuperAdmin gave access. Security decides; Audit Core
enforces on its own human requests with one application-wide mechanism."""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
import types
from uuid import uuid4

import httpx
import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine
from structlog.testing import capture_logs

from audit_core import night_control, security_authorization
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.night_control import (
    FILE_DOWNLOAD,
    JOURNEY_360,
    enforce_night_control,
    get_night_control_client,
    night_control_user_id,
    night_controlled_features,
)
from audit_core.security import HumanPrincipal, SecurityTokenError
from audit_core.security_authorization import (
    NightControlDecision,
    SecurityAuthorizationClient,
    get_security_authorization_client,
)

USER_ID = "8f1d2c3b-0000-4000-8000-000000000001"


class FakeNightControl:
    """Security night-control double: blocks the given features, records every ask."""

    def __init__(self, *blocked: str) -> None:
        self.blocked = set(blocked)
        self.calls: list[tuple[str, str]] = []

    def check_night_control(self, *, user_id: str, feature_key: str) -> NightControlDecision:
        self.calls.append((user_id, feature_key))
        blocked = feature_key in self.blocked
        return NightControlDecision(
            feature_key=feature_key,
            blocked=blocked,
            reason_code="NIGHT_MODE" if blocked else None,
            message="Night Mode On" if blocked else None,
            starts_at="20:00",
            ends_at="08:30",
        )


class BrokenNightControl:
    def check_night_control(self, *, user_id: str, feature_key: str) -> NightControlDecision:
        raise RuntimeError("Security exploded")


def _night(fake: object, user_id: str | None = USER_ID) -> None:
    app.dependency_overrides[get_night_control_client] = lambda: fake
    app.dependency_overrides[night_control_user_id] = lambda request: user_id


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2night")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


@pytest.fixture
def no_db_app():
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def _assert_night_mode(response: httpx.Response, feature_key: str) -> None:
    assert response.status_code == 403, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    correlation_id = body.pop("correlationId")
    assert correlation_id
    assert body == {
        "type": "urn:verigence:audit-core:error:NIGHT_MODE",
        "title": "Night Mode On",
        "status": 403,
        "detail": "Night Mode On",
        "errorCode": "NIGHT_MODE",
        "errorCategory": "SECURITY",
        "retryable": False,
        "featureKey": feature_key,
        "reasonCode": "NIGHT_MODE",
        "startsAt": "20:00",
        "endsAt": "08:30",
    }


# --- Enforcement on real routes (local Postgres) ----------------------------------


def test_journey_360_is_refused_with_night_mode(journey):
    fake = FakeNightControl(JOURNEY_360)
    _night(fake)
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"

    _assert_night_mode(client.get(f"{base}/360"), JOURNEY_360)
    _assert_night_mode(client.get(f"/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"), JOURNEY_360)
    _assert_night_mode(client.post(f"{base}:recheck"), JOURNEY_360)
    assert fake.calls[0] == (USER_ID, JOURNEY_360)


def test_journey_360_opens_when_security_does_not_block(journey):
    fake = FakeNightControl()
    _night(fake)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360")

    assert response.status_code == 200, response.text
    assert fake.calls == [(USER_ID, JOURNEY_360)]


def test_file_routes_are_refused_as_file_download(journey):
    fake = FakeNightControl(FILE_DOWNLOAD)
    _night(fake)
    client = TestClient(app, raise_server_exceptions=False)

    _assert_night_mode(
        client.get(f"/p2/v1/tenants/{journey.tenant_id}/reports/journey-exports/{uuid4()}/file"),
        FILE_DOWNLOAD,
    )
    assert fake.calls == [(USER_ID, FILE_DOWNLOAD)]

    # A Journey's document bytes: Journey 360 is open to this caller, the download is not.
    fake.calls.clear()
    _assert_night_mode(
        client.get(
            f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/documents/{uuid4()}/content"
        ),
        FILE_DOWNLOAD,
    )
    assert fake.calls == [(USER_ID, JOURNEY_360), (USER_ID, FILE_DOWNLOAD)]


def test_journey_lists_and_search_are_not_night_controlled(journey):
    fake = FakeNightControl(JOURNEY_360, FILE_DOWNLOAD)
    _night(fake)
    client = TestClient(app, raise_server_exceptions=False)

    listing = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys", params={"state": "open"})
    assert listing.status_code == 200, listing.text
    counts = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys:counts")
    assert counts.status_code != 403 or counts.json().get("errorCode") != "NIGHT_MODE"
    search = client.get(f"/v1/tenants/{journey.tenant_id}/uc03/journey-search", params={"q": "P2"})
    assert search.status_code != 403 or search.json().get("errorCode") != "NIGHT_MODE"
    assert fake.calls == []


def test_non_human_requests_are_not_checked(journey):
    fake = FakeNightControl(JOURNEY_360, FILE_DOWNLOAD)
    _night(fake, user_id=None)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360")

    assert response.status_code == 200, response.text
    assert fake.calls == []


def test_security_failure_allows_the_request(journey):
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/360"

    # The client itself raising (never expected, but never a refusal either).
    _night(BrokenNightControl())
    with capture_logs() as logs:
        assert client.get(url).status_code == 200
    assert any(e["event"] == "night_control_check_failed" and e["outcome"] == "allowed" for e in logs)

    # The real client against a Security that answers 503 (or 404 before PR #126 is deployed).
    for status in (503, 404):
        security = _security_client(lambda request, status=status: httpx.Response(status, json={}))
        _night(security)
        with capture_logs() as logs, security:
            assert client.get(url).status_code == 200
        [warning] = [e for e in logs if e["event"] == "night_control_check_failed"]
        assert warning["log_level"] == "warning" and warning["http_status"] == status


# --- Who is asked about ---------------------------------------------------------


def test_only_valid_human_tokens_are_checked(monkeypatch):
    class Validator:
        def validate_human(self, token: str) -> HumanPrincipal:
            if token == "human":
                return HumanPrincipal(subject=USER_ID)
            raise SecurityTokenError("Security token is not a human USER token")

    monkeypatch.setattr(night_control, "_token_validator", lambda: Validator())

    def request(headers: dict[str, str]) -> object:
        return types.SimpleNamespace(headers=httpx.Headers(headers))

    assert night_control_user_id(request({"Authorization": "Bearer human"})) == USER_ID
    assert night_control_user_id(request({"Authorization": "Bearer service-integration"})) is None
    assert night_control_user_id(request({"Authorization": "Basic abc"})) is None
    assert night_control_user_id(request({})) is None


# --- Security client ------------------------------------------------------------


def _security_client(night_handler) -> SecurityAuthorizationClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/v1/service/token":
            return httpx.Response(200, json={"accessToken": "service-token", "tokenType": "Bearer",
                                             "audience": "security"})
        if request.url.path == "/security/v1/service/night-control/check":
            assert request.headers["authorization"] == "Bearer service-token"
            return night_handler(request)
        raise AssertionError(f"Unexpected Security request: {request.url}")

    return SecurityAuthorizationClient(
        base_url="https://security.test", client_id="audit-core", client_secret="secret",
        transport=httpx.MockTransport(handler),
    )


def _blocked_answer(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(200, json={
        "featureKey": payload["featureKey"], "blocked": True, "reasonCode": "NIGHT_MODE",
        "message": "Night Mode On", "startsAt": "20:00", "endsAt": "08:30", "allowedBy": None,
    })


def test_client_asks_security_with_the_service_token_and_reads_the_answer():
    bodies: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _blocked_answer(request)

    with _security_client(answer) as client:
        decision = client.check_night_control(user_id=USER_ID, feature_key=JOURNEY_360)

    assert bodies == [{"userId": USER_ID, "featureKey": JOURNEY_360}]
    assert decision == NightControlDecision(
        feature_key=JOURNEY_360, blocked=True, reason_code="NIGHT_MODE", message="Night Mode On",
        starts_at="20:00", ends_at="08:30", allowed_by=None, fallback=False,
    )


def test_client_reuses_answers_per_user_and_feature_for_a_minute(monkeypatch):
    asked: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        asked.append((payload["userId"], payload["featureKey"]))
        return httpx.Response(200, json={"featureKey": payload["featureKey"], "blocked": False,
                                         "reasonCode": None, "message": None, "startsAt": "20:00",
                                         "endsAt": "08:30", "allowedBy": "GRANT"})

    assert security_authorization._NIGHT_CONTROL_REUSE_SECONDS == 60.0
    with _security_client(answer) as client:
        for _ in range(3):
            assert client.check_night_control(user_id=USER_ID, feature_key=JOURNEY_360).allowed_by == "GRANT"
        client.check_night_control(user_id=USER_ID, feature_key=FILE_DOWNLOAD)
        client.check_night_control(user_id="other-user", feature_key=JOURNEY_360)
        assert asked == [(USER_ID, JOURNEY_360), (USER_ID, FILE_DOWNLOAD), ("other-user", JOURNEY_360)]

        # Once the minute is over, Security is asked again.
        monkeypatch.setattr(security_authorization, "_NIGHT_CONTROL_REUSE_SECONDS", 0.0)
        client._night_cache.clear()
        client.check_night_control(user_id=USER_ID, feature_key=JOURNEY_360)
        client.check_night_control(user_id=USER_ID, feature_key=JOURNEY_360)
    assert asked[3:] == [(USER_ID, JOURNEY_360), (USER_ID, JOURNEY_360)]


@pytest.mark.parametrize(
    "night_handler",
    [
        lambda request: httpx.Response(503, json={}),
        lambda request: httpx.Response(404, json={"detail": "Not Found"}),
        lambda request: httpx.Response(200, content=b"not json"),
        lambda request: httpx.Response(200, json={"featureKey": "SOMETHING_ELSE", "blocked": True}),
        lambda request: httpx.Response(200, json={"featureKey": JOURNEY_360, "blocked": "yes"}),
    ],
)
def test_client_allows_when_security_cannot_answer(night_handler):
    calls = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return night_handler(request)

    with capture_logs() as logs, _security_client(answer) as client:
        decision = client.check_night_control(user_id=USER_ID, feature_key=JOURNEY_360)
        client.check_night_control(user_id=USER_ID, feature_key=JOURNEY_360)

    assert decision.blocked is False and decision.fallback is True
    assert calls == 1  # the fallback is reused briefly, not re-asked on every request
    assert any(e["event"] == "night_control_check_failed" and e["outcome"] == "allowed" for e in logs)
    assert security_authorization._NIGHT_CONTROL_FALLBACK_REUSE_SECONDS < (
        security_authorization._NIGHT_CONTROL_REUSE_SECONDS
    )


def test_client_allows_when_security_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/v1/service/token":
            return httpx.Response(200, json={"accessToken": "t", "tokenType": "Bearer", "audience": "security"})
        raise httpx.ConnectError("connection refused", request=request)

    with SecurityAuthorizationClient(
        base_url="https://security.test", client_id="audit-core", client_secret="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        decision = client.check_night_control(user_id=USER_ID, feature_key=FILE_DOWNLOAD)
    assert decision.blocked is False and decision.fallback is True


# --- Coverage: every journey and file route is under the mechanism --------------

_PARAM = re.compile(r"\{(\w+)(?::(\w+))?\}")


def _routes():
    from fastapi.routing import iter_route_contexts

    return [route for route in iter_route_contexts(app.routes) if route.path_format]


def test_every_route_carries_the_night_control_check():
    for route in _routes():
        dependant = route.dependant
        assert dependant is not None, route.path_format
        assert any(dep.call is enforce_night_control for dep in dependant.dependencies), route.path_format
        # The check classifies the matched route by its own template; it must be the full path.
        assert getattr(route.original_route, "path_format", None) == route.path_format


def test_every_route_addressing_one_journey_is_journey_360():
    addresses_journey = re.compile(r"/journeys/\{[^}]+\}|\{journey_id[^}]*\}")
    journey_routes = [r.path_format for r in _routes() if addresses_journey.search(r.path_format)]
    assert len(journey_routes) > 100
    for path in journey_routes:
        assert JOURNEY_360 in night_controlled_features(path), path
    for listing in (
        "/p2/v1/tenants/{tenant_id}/journeys",
        "/p2/v1/tenants/{tenant_id}/journeys:counts",
        "/p2/v1/tenants/{tenant_id}/journeys:summary",
        "/p2/v1/tenants/{tenant_id}/reviewer/journeys",
        "/v1/tenants/{tenant_id}/uc03/journey-search",
        "/v1/tenants/{tenant_id}/admin/housekeeping/journeys/preview",
    ):
        assert night_controlled_features(listing) == (), listing


_NAMED_FILE_ROUTES = {
    "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/documents/{document_id}/content",
    "/v2/tenants/{tenant_id}/journeys/{journey_id}/review/documents/{document_id}/content",
    "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/pages/{queue_id}/file",
    "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/uploads/{batch_id}/file",
    "/p2/v1/tenants/{tenant_id}/reports/journey-exports/{export_id}/file",
    "/p2/v1/tenants/{tenant_id}/gate-passes/{gate_pass_id}/image-url",
    "/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/vehicle-photos",
    "/v2/tenants/{tenant_id}/journeys/{journey_id}/delivery/vehicle-photos",
}

# Code that hands out file bytes or a signed link to a file.
_FILE_MARKERS = re.compile(
    r"presign_get\(|get_presigned_url\(|generate_presigned_url\(|FileResponse\("
    r"|StreamingResponse\(|\bResponse\(\s*content|Content-Disposition"
)
# Detected by the scan but not a file: the Journey live event stream (it is
# Journey 360 anyway).
_NOT_FILES = {"/p2/v1/tenants/{tenant_id}/journeys/{journey_id}/live"}


def _produces_file(func, memo: dict, stack: tuple = ()) -> bool:
    """Whether a route endpoint, or an audit_core function it calls, hands out a file."""
    func = inspect.unwrap(func)
    if func in memo:
        return memo[func]
    if func in stack or len(stack) > 6:
        return False
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return False
    found = bool(_FILE_MARKERS.search(source))
    if not found:
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name):
                target = func.__globals__.get(node.func.id)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                owner = func.__globals__.get(node.func.value.id)
                if isinstance(owner, types.ModuleType):
                    target = getattr(owner, node.func.attr, None)
            if (
                isinstance(target, types.FunctionType)
                and target.__module__.startswith("audit_core")
                and _produces_file(target, memo, (*stack, func))
            ):
                found = True
                break
    memo[func] = found
    return found


def test_every_route_that_hands_out_a_file_is_file_download():
    memo: dict = {}
    detected = {r.path_format for r in _routes() if r.endpoint and _produces_file(r.endpoint, memo)}
    assert _NAMED_FILE_ROUTES <= detected
    assert _NOT_FILES <= detected
    for path in sorted(detected - _NOT_FILES):
        assert FILE_DOWNLOAD in night_controlled_features(path), path
    for path in _NAMED_FILE_ROUTES:
        assert FILE_DOWNLOAD in night_controlled_features(path), path


def _sample_path(path_format: str) -> str:
    def value(match: re.Match[str]) -> str:
        return "1" if match.group(2) in ("int", "float") else str(uuid4())

    return _PARAM.sub(value, path_format)


def test_every_covered_route_answers_night_mode_when_blocked(no_db_app):
    """End to end over the real route table: a blocked caller is refused on every
    covered route before the route itself runs (no database is touched)."""
    fake = FakeNightControl(JOURNEY_360, FILE_DOWNLOAD)
    _night(fake)
    covered = 0
    for route in _routes():
        features = night_controlled_features(route.path_format)
        if not features:
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            response = no_db_app.request(
                method,
                _sample_path(route.path_format),
                headers={"Authorization": "Bearer human", "Idempotency-Key": "night-control-test"},
            )
            assert response.status_code == 403, (method, route.path_format, response.text)
            assert response.json()["errorCode"] == "NIGHT_MODE", (method, route.path_format)
            assert response.json()["featureKey"] == features[0], (method, route.path_format)
            covered += 1
    assert covered > 100
