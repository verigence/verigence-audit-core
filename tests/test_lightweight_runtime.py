from __future__ import annotations

from starlette.requests import Request

from audit_core import dependencies
from audit_core.security_integration import SecurityAdminContext


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "scheme": "https",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        }
    )


def test_only_approved_control_plane_and_master_gets_use_lightweight_auth() -> None:
    approved = (
        "/v1/projects",
        "/v1/project-reference-data",
        "/v1/tenants/t-1/project-masters",
        "/v1/tenants/t-1/project-masters/AUDIT_CORE/PRICE_LIST/versions",
        "/v1/tenants/t-1/project-masters/DI/DOCUMENT_TYPES/template",
    )
    for path in approved:
        assert dependencies._is_lightweight_authenticated_read(_request("GET", path))

    assert not dependencies._is_lightweight_authenticated_read(
        _request("POST", "/v1/projects")
    )
    assert not dependencies._is_lightweight_authenticated_read(
        _request("GET", "/v1/tenants/t-1/dealers")
    )
    assert not dependencies._is_lightweight_authenticated_read(
        _request("POST", "/v1/tenants/t-1/project-masters/AUDIT_CORE/PRICE_LIST/versions/v-1/publish")
    )
    assert not dependencies._is_lightweight_authenticated_read(
        _request("GET", "/v1/tenants/t-1/project-master-imports/i-1/error-report")
    )


def test_admin_context_is_reused_for_short_page_bursts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dependencies._admin_context_cache.clear()
    now = {"value": 100.0}
    monkeypatch.setattr(dependencies.time, "monotonic", lambda: now["value"])
    context = SecurityAdminContext(
        user_id="user-1",
        is_super_admin=True,
        admin_scopes=(),
    )

    dependencies._remember_admin_context(context)
    assert dependencies._cached_admin_context("user-1") is context

    now["value"] += dependencies._ADMIN_CONTEXT_TTL_SECONDS + 0.01
    assert dependencies._cached_admin_context("user-1") is None
    dependencies._admin_context_cache.clear()


def test_postgresql_engine_uses_simple_bounded_pool_configuration(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    observed: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(url: str, **kwargs: object) -> object:
        observed["url"] = url
        observed.update(kwargs)
        return sentinel

    dependencies._engine.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/test")
    monkeypatch.setattr(dependencies, "create_engine", fake_create_engine)
    try:
        assert dependencies.get_engine() is sentinel
    finally:
        dependencies._engine.cache_clear()

    assert observed["pool_pre_ping"] is True
    assert observed["pool_timeout"] == 5
    assert observed["connect_args"] == {
        "connect_timeout": 5,
        "options": "-c statement_timeout=10000",
    }


def test_token_validator_is_reused_server_side(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    created: list[object] = []

    class FakeValidator:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

    dependencies._token_validator.cache_clear()
    monkeypatch.setenv("SECURITY_JWKS_URL", "https://security.test/.well-known/jwks.json")
    monkeypatch.setenv("SECURITY_ISSUER", "verigence-security")
    monkeypatch.setenv("SECURITY_AUDIENCE", "verigence-platform")
    monkeypatch.setattr(dependencies, "SecurityTokenValidator", FakeValidator)
    try:
        first = dependencies._token_validator()
        second = dependencies._token_validator()
    finally:
        dependencies._token_validator.cache_clear()

    assert first is second
    assert len(created) == 1
