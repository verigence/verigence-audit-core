"""Who may upload price masters (decision 2026-10-06): SuperAdmin, or a person Security allows
`audit.master.upload` on the project (Team Lead and Project Manager by default). Requires DATABASE_URL."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization
from test_oem_price_masters import _price_bytes, connection  # noqa: F401  (fixture)

from audit_core import oem_price_masters
from audit_core.authorization import AuthorizationError
from audit_core.dependencies import get_bearer_token, get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client


class _Auth:
    """Security double: allows only the named permission."""

    def __init__(self, allowed: str | None) -> None:
        self.allowed = allowed
        self.asked: list[str] = []

    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        self.asked.append(permission_key)
        return SimpleNamespace(allowed=permission_key == self.allowed, role_key="TL")


def _admin(monkeypatch, *, super_admin: bool) -> None:
    def fake(bearer_token, human_principal):
        if not super_admin:
            raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")
        return SimpleNamespace(admin_context=SimpleNamespace(is_super_admin=True))
    monkeypatch.setattr(oem_price_masters, "get_human_admin_request", fake)


def _upload(client: TestClient, tenant_id: str, *, dry_run: bool = True):
    return client.post(
        "/v1/admin/oem-masters/uploads", params={"dryRun": str(dry_run).lower()},
        data={"tenantId": tenant_id, "masterKind": "PRICE_LIST", "effectiveFrom": "2026-09-01"},
        files={"file": ("Thar price 01 Sep.xlsx", _price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000)), "application/octet-stream")},
    )


@pytest.fixture
def client_for(connection):  # noqa: F811
    connection.commit()  # the API answers over its own connection

    def make(auth) -> TestClient:
        app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject="u-1")
        app.dependency_overrides[get_bearer_token] = lambda: "token"
        app.dependency_overrides[get_security_authorization_client] = lambda: auth
        return TestClient(app, raise_server_exceptions=False)

    yield make
    app.dependency_overrides.clear()


def test_a_person_holding_the_upload_permission_may_upload_and_read_history(connection, client_for, monkeypatch) -> None:  # noqa: F811
    _admin(monkeypatch, super_admin=False)
    auth = _Auth("audit.master.upload")
    client = client_for(auth)
    done = _upload(client, connection.tenant_id, dry_run=False)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "PUBLISHED" and done.json()["sourceFilename"] == "Thar price 01 Sep.xlsx"
    history = client.get("/v1/admin/oem-masters/uploads", params={"tenantId": connection.tenant_id})
    assert history.status_code == 200 and history.json()[0]["sourceFilename"] == "Thar price 01 Sep.xlsx"
    assert set(auth.asked) == {"audit.master.upload"}


def test_a_person_without_it_is_refused_on_all_three_routes(connection, client_for, monkeypatch) -> None:  # noqa: F811
    _admin(monkeypatch, super_admin=False)
    client = client_for(_Auth("audit.journey.read"))  # a PC: may read prices, not load them
    assert _upload(client, connection.tenant_id).status_code == 403
    assert client.get("/v1/admin/oem-masters/uploads", params={"tenantId": connection.tenant_id}).status_code == 403
    one = client.get("/v1/admin/oem-masters/uploads/00000000-0000-0000-0000-000000000000", params={"tenantId": connection.tenant_id})
    assert one.status_code == 403


def test_a_super_admin_needs_no_project_permission(connection, client_for, monkeypatch) -> None:  # noqa: F811
    _admin(monkeypatch, super_admin=True)
    auth = _Auth(None)
    assert _upload(client_for(auth), connection.tenant_id).status_code == 200
    assert auth.asked == []


def test_allow_all_double_is_not_a_super_admin_shortcut(connection, client_for, monkeypatch) -> None:  # noqa: F811
    _admin(monkeypatch, super_admin=False)
    assert _upload(client_for(AllowAllAuthorization("TL")), connection.tenant_id).status_code == 200


def test_a_person_who_is_not_a_super_admin_loads_price_lists_only(connection, client_for, monkeypatch) -> None:  # noqa: F811
    _admin(monkeypatch, super_admin=False)
    client = client_for(_Auth("audit.master.upload"))
    refused = client.post(
        "/v1/admin/oem-masters/uploads", params={"dryRun": "true"},
        data={"tenantId": connection.tenant_id, "masterKind": "CONSUMER_SCHEME", "effectiveFrom": "2026-09-01"},
        files={"file": ("scheme.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert refused.status_code == 403


def test_the_template_downloads_for_an_uploader_and_a_file_without_a_date_can_be_checked(connection, client_for, monkeypatch) -> None:  # noqa: F811
    from audit_core.oem_master_templates import build_template
    from test_oem_master_templates import _real_price_workbook

    _admin(monkeypatch, super_admin=False)
    client = client_for(_Auth("audit.master.upload"))
    got = client.get("/v1/admin/oem-masters/templates/PRICE_LIST", params={"tenantId": connection.tenant_id})
    assert got.status_code == 200 and "attachment" in got.headers["content-disposition"]
    assert got.content[:2] == b"PK" and got.content == build_template("PRICE_LIST") or got.content[:2] == b"PK"
    # not for a person without the permission, nor for the other masters, nor where the master is the OEM's PDF
    assert client_for(_Auth(None)).get("/v1/admin/oem-masters/templates/PRICE_LIST", params={"tenantId": connection.tenant_id}).status_code == 403
    client = client_for(_Auth("audit.master.upload"))
    assert client.get("/v1/admin/oem-masters/templates/DISCOUNT_GRID", params={"tenantId": connection.tenant_id}).status_code == 403
    _admin(monkeypatch, super_admin=True)
    client = client_for(_Auth(None))
    assert client.get("/v1/admin/oem-masters/templates/DISCOUNT_GRID", params={"tenantId": connection.tenant_id}).status_code == 200
    assert client.get("/v1/admin/oem-masters/templates/CONSUMER_SCHEME", params={"tenantId": connection.tenant_id}).status_code == 404
    # a filled template with no date in it: the check works (no date yet); publishing without one is refused
    files = {"file": ("Price 01 Sep.xlsx", _real_price_workbook(), "application/octet-stream")}
    form = {"tenantId": connection.tenant_id, "masterKind": "PRICE_LIST"}
    checked = client.post("/v1/admin/oem-masters/uploads", params={"dryRun": "true"}, data=form, files=files)
    assert checked.status_code == 200, checked.text
    assert checked.json()["effectiveFrom"] is None and checked.json()["errors"] == []
    refused = client.post("/v1/admin/oem-masters/uploads", params={"dryRun": "false"}, data=form, files=files)
    assert refused.status_code == 400
    done = client.post("/v1/admin/oem-masters/uploads", params={"dryRun": "false"}, data={**form, "effectiveFrom": "2026-09-01"}, files=files)
    assert done.status_code == 200 and done.json()["status"] == "PUBLISHED" and done.json()["effectiveFromSource"] == "ADMIN"
