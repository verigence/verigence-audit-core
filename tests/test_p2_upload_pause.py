"""Uploads paused for maintenance (product owner, 2026-10-31).

While paused, every endpoint that starts a NEW document upload refuses with
503 VAC-MAINT-001; reads, vehicle photos, task actions and the finalize of an
upload prepared before the pause keep working. With no pause row, or one that
has expired or says not paused, nothing changes. Real Postgres (the API runs
as the runtime role, so row security is exercised); storage, DI and Security
are doubles.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_evidence,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_api as api
from audit_core import uc03_p2_storage
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.security_integration import SecurityAdminContext
from audit_core.uc03_p2_tasks import create_p2_task
from audit_core.uc03_p2_upload_pause import (
    UPLOAD_PAUSED_ERROR_CODE,
    UPLOAD_PAUSED_TITLE,
    ist_text,
    pause_detail,
    upload_pause,
)

MESSAGE = "We are correcting bookings that hold other customers' files"


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def presign_put(self, key, *, content_type, expires_seconds=900):
        return f"https://storage.test/{key}"

    def head_object(self, key):
        return {"contentLength": len(self.objects[key]), "contentType": "application/pdf", "etag": "e"}

    def presign_get(self, key, *, expires_seconds=1800):
        return f"https://storage.test/get/{key}"


@pytest.fixture
def journey(monkeypatch):
    engine = database_engine()
    created = create_p2_journey(engine, prefix="pause")
    storage = Storage()
    monkeypatch.setattr(api, "get_p2_document_storage", lambda: storage)
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    app.dependency_overrides[get_di_client] = lambda: SimpleNamespace()
    app.dependency_overrides[get_security_oauth_client] = lambda: SimpleNamespace()
    _clear(engine, created.tenant_id)
    try:
        yield created, storage
    finally:
        app.dependency_overrides.clear()
        _clear(engine, created.tenant_id)
        with engine.begin() as connection:
            # A replacement batch points at the evidence it replaces, which
            # delete_tenant_data removes before the journeys these batches go with.
            connection.execute(text("DELETE FROM auditcore.p2_upload_batches WHERE tenant_id=:t"),
                               {"t": created.tenant_id})
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _clear(engine, tenant_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM auditcore.p2_upload_pause WHERE tenant_id IS NULL OR tenant_id = :t"),
            {"t": tenant_id},
        )


def _pause(engine, tenant_id: str | None, *, paused: bool = True, until: datetime | None = None,
           message: str | None = MESSAGE) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_pause (tenant_id, paused, message, until_utc, set_by_actor_id)
                VALUES (:t, :p, :m, :u, 'test-admin')
                ON CONFLICT ((COALESCE(tenant_id, '*'))) DO UPDATE
                SET paused = EXCLUDED.paused, message = EXCLUDED.message, until_utc = EXCLUDED.until_utc
                """
            ),
            {"t": tenant_id, "p": paused, "m": message, "u": until},
        )


def _status(journey) -> dict | None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return upload_pause(connection, journey.tenant_id)


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _base(journey) -> str:
    return f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"


def _init(client, journey, client_upload_id: str = "pause-file-0001"):
    return client.post(f"{_base(journey)}/uploads:init", json={"files": [{
        "filename": "booking.pdf", "contentType": "application/pdf", "sizeBytes": 4,
        "clientUploadId": client_upload_id}]})


def _replace(client, journey, document_id):
    return client.post(f"{_base(journey)}/documents/{document_id}/replace", json={
        "filename": "rescan.pdf", "contentType": "application/pdf", "sizeBytes": 4,
        "clientUploadId": "pause-replace-0001"})


def _new_booking(client, journey):
    return client.post(f"/p2/v1/tenants/{journey.tenant_id}/journeys",
                       json={"outletId": str(journey.outlet_id)},
                       headers={"Idempotency-Key": f"pause-{uuid4().hex}"})


def _evidence_upload(client, journey):
    return client.post(
        f"/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/stages/BOOKING/documents/booking_form/evidence",
        files={"file": ("booking.pdf", b"%PDF", "application/pdf")},
        headers={"Idempotency-Key": f"pause-{uuid4().hex}"},
    )


def _assert_refused(response, *, until: datetime | None = None) -> None:
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["errorCode"] == UPLOAD_PAUSED_ERROR_CODE
    assert body["title"] == UPLOAD_PAUSED_TITLE
    assert body["detail"].startswith(MESSAGE + ".")
    if until is not None:
        assert body["detail"].endswith(f"Uploads are back at {ist_text(until)}.")


# ── the helper ────────────────────────────────────────────────────────────────
def test_no_row_means_not_paused(journey):
    j, _ = journey
    assert _status(j) is None


def test_a_project_row_pauses_only_that_project(journey):
    j, _ = journey
    _pause(j.engine, j.tenant_id)
    assert _status(j) == {"scope": "PROJECT", "message": MESSAGE, "untilUtc": None}
    other = create_p2_journey(j.engine, prefix="pause-other")
    try:
        assert _status(other) is None
    finally:
        delete_tenant_data(j.engine, other.tenant_id)


def test_the_all_projects_row_pauses_every_project(journey):
    j, _ = journey
    _pause(j.engine, None)
    assert _status(j)["scope"] == "ALL"


def test_a_project_row_that_says_not_paused_does_not_lift_the_all_projects_pause(journey):
    j, _ = journey
    _pause(j.engine, None)
    _pause(j.engine, j.tenant_id, paused=False)
    assert _status(j)["scope"] == "ALL"
    _pause(j.engine, None, paused=False)
    assert _status(j) is None


def test_the_pause_ends_by_itself_at_until(journey):
    j, _ = journey
    _pause(j.engine, j.tenant_id, until=datetime.now(UTC) - timedelta(minutes=1))
    assert _status(j) is None
    later = datetime.now(UTC) + timedelta(hours=2)
    _pause(j.engine, j.tenant_id, until=later)
    assert _status(j)["untilUtc"] == later


def test_with_two_pauses_the_one_that_ends_last_is_reported(journey):
    j, _ = journey
    soon, later = datetime.now(UTC) + timedelta(hours=1), datetime.now(UTC) + timedelta(hours=3)
    _pause(j.engine, j.tenant_id, until=soon)
    _pause(j.engine, None, until=later)
    assert _status(j)["untilUtc"] == later
    _pause(j.engine, None, until=None)
    assert _status(j)["scope"] == "ALL" and _status(j)["untilUtc"] is None


def test_the_refusal_names_the_time_in_ist():
    until = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
    assert ist_text(until) == "31 Oct 2026, 11:30 PM IST"
    assert pause_detail({"message": "Correcting bookings", "untilUtc": until}) == (
        "Correcting bookings. Uploads are back at 31 Oct 2026, 11:30 PM IST.")
    assert pause_detail({"message": None, "untilUtc": None}) == "Uploads are paused for maintenance."


# ── unpaused: exactly as before ───────────────────────────────────────────────
def test_unpaused_every_upload_endpoint_works_as_before(journey):
    j, storage = journey
    client = _client()
    init = _init(client, j)
    assert init.status_code == 200, init.text
    [upload] = init.json()["uploads"]
    assert upload["status"] == "AWAITING_UPLOAD" and upload["uploadUrl"].startswith("https://storage.test/")
    storage.objects[upload["uploadUrl"].split("https://storage.test/", 1)[1]] = b"%PDF"
    assert client.post(f"{_base(j)}/uploads/{upload['batchId']}:finalize").status_code == 202

    document_id = uuid4()
    add_evidence(j, di_document_id=document_id, document_type_key="booking_form")
    replaced = _replace(client, j, document_id)
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["uploadUrl"]

    assert _new_booking(client, j).status_code == 201
    # The old evidence upload goes on to its own checks (the booking stage was never started).
    assert _evidence_upload(client, j).status_code == 409

    status = client.get(f"/p2/v1/tenants/{j.tenant_id}/upload-pause")
    assert status.status_code == 200
    assert status.json() == {"paused": False, "message": None, "untilUtc": None}


def test_an_expired_pause_refuses_nothing(journey):
    j, _ = journey
    _pause(j.engine, None, until=datetime.now(UTC) - timedelta(seconds=1))
    client = _client()
    assert _init(client, j).status_code == 200
    assert client.get(f"/p2/v1/tenants/{j.tenant_id}/upload-pause").json()["paused"] is False


# ── paused ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scope", ["PROJECT", "ALL"])
def test_paused_every_new_upload_is_refused_with_503(journey, scope):
    j, _ = journey
    until = datetime.now(UTC) + timedelta(hours=2)
    _pause(j.engine, j.tenant_id if scope == "PROJECT" else None, until=until)
    client = _client()
    document_id = uuid4()
    add_evidence(j, di_document_id=document_id, document_type_key="booking_form")

    _assert_refused(_init(client, j), until=until)
    _assert_refused(_replace(client, j, document_id), until=until)
    _assert_refused(_new_booking(client, j), until=until)
    _assert_refused(_evidence_upload(client, j), until=until)

    with j.engine.begin() as connection:
        set_tenant_context(connection, j.tenant_id)
        batches = connection.execute(
            text("SELECT count(*) FROM auditcore.p2_upload_batches WHERE tenant_id=:t"), {"t": j.tenant_id},
        ).scalar_one()
        journeys = connection.execute(
            text("SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:t"), {"t": j.tenant_id},
        ).scalar_one()
    assert batches == 0 and journeys == 1  # nothing was prepared and no booking was started

    status = client.get(f"/p2/v1/tenants/{j.tenant_id}/upload-pause").json()
    assert status["paused"] is True and status["message"] == MESSAGE
    assert datetime.fromisoformat(status["untilUtc"]) == until


def test_paused_an_upload_prepared_before_the_pause_still_finishes(journey):
    j, storage = journey
    client = _client()
    [upload] = _init(client, j).json()["uploads"]
    storage.objects[upload["uploadUrl"].split("https://storage.test/", 1)[1]] = b"%PDF"
    _pause(j.engine, None)
    assert client.post(f"{_base(j)}/uploads/{upload['batchId']}:finalize").status_code == 202
    with j.engine.begin() as connection:
        set_tenant_context(connection, j.tenant_id)
        work = connection.execute(
            text("SELECT work_status FROM auditcore.p2_work_queue "
                 "WHERE tenant_id=:t AND work_type='SPLIT_BATCH' AND work_key=:k"),
            {"t": j.tenant_id, "k": upload["batchId"]},
        ).scalar_one()
    assert work == "PENDING"  # the worker processes it as usual


def test_paused_reads_photos_and_task_actions_still_work(journey):
    j, _ = journey
    with j.engine.begin() as connection:
        set_tenant_context(connection, j.tenant_id)
        task_id = create_p2_task(
            connection, tenant_id=j.tenant_id, journey_id=j.journey_id,
            task_type="MANUAL_VERIFICATION_REVIEW", category="DOCUMENT_VERIFICATION", origin_kind="SYSTEM",
            source_type="DOCUMENT_FIELD", source_code="MANUAL_VERIFICATION", dedupe_key=f"t:{uuid4()}",
            title="Verify 2 fields on Aadhaar", description="Check the values", reference={}, severity="MEDIUM",
            priority="NORMAL", assigned_role_code="PC", assigned_actor_id=None, raised_by_actor_id=None,
            raised_by_role_code=None, allowed_actions=["REVIEW_DOCUMENT", "ADD_COMMENT"],
            completion_protocol="MACHINE_VERIFIED",
        )
    _pause(j.engine, None)
    client = _client()

    assert client.get(f"/p2/v1/tenants/{j.tenant_id}/journeys").status_code == 200
    assert client.get(f"{_base(j)}/documents").status_code == 200
    assert client.get(f"/p2/v1/tenants/{j.tenant_id}/tasks").status_code == 200
    action = client.post(f"/p2/v1/tenants/{j.tenant_id}/tasks/{task_id}/actions",
                         json={"action": "ADD_COMMENT", "comment": "Dealer will resend"})
    assert action.status_code == 200, action.text
    photos = client.post(f"{_base(j)}/vehicle-photos:upload-intents", json={"files": [{
        "filename": "front.jpg", "contentType": "image/jpeg", "sizeBytes": 1000,
        "clientUploadId": "pause-photo-0001", "viewCode": "FRONT"}]})
    assert photos.status_code == 200, photos.text


# ── SuperAdmin ────────────────────────────────────────────────────────────────
def _as_super_admin() -> None:
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="sa-1", bearer_token="t",
        admin_context=SecurityAdminContext(user_id="sa-1", is_super_admin=True, admin_scopes=()))


def test_the_super_admin_pauses_and_resumes_all_projects(journey):
    j, _ = journey
    _as_super_admin()
    client = _client()
    until = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0)
    set_ = client.put("/v1/admin/upload-pause", json={
        "tenantId": None, "paused": True, "message": f"  {MESSAGE}  ", "untilUtc": until.isoformat()})
    assert set_.status_code == 200, set_.text
    [row] = [r for r in set_.json()["items"] if r["tenantId"] is None]
    assert row["paused"] is True and row["active"] is True and row["message"] == MESSAGE
    assert row["setByActorId"] == "sa-1"
    _assert_refused(_init(client, j), until=until)
    assert client.get("/v1/admin/upload-pause").json()["items"][0]["active"] is True

    cleared = client.put("/v1/admin/upload-pause", json={"tenantId": None, "paused": False})
    assert cleared.status_code == 200, cleared.text
    [row] = [r for r in cleared.json()["items"] if r["tenantId"] is None]
    assert row["paused"] is False and row["active"] is False and row["untilUtc"] is None
    assert _init(client, j).status_code == 200


def test_the_super_admin_pauses_one_project(journey):
    j, _ = journey
    _as_super_admin()
    client = _client()
    assert client.put("/v1/admin/upload-pause", json={"tenantId": j.tenant_id, "paused": True}).status_code == 200
    response = _init(client, j)
    assert response.status_code == 503
    assert response.json()["detail"] == "Uploads are paused for maintenance."


def test_the_super_admin_request_is_checked(journey):
    j, _ = journey
    _as_super_admin()
    client = _client()
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    assert client.put("/v1/admin/upload-pause", json={"paused": True, "untilUtc": past}).status_code == 422
    assert client.put("/v1/admin/upload-pause", json={
        "paused": True, "untilUtc": "2099-01-01T10:00:00"}).status_code == 400
    assert client.put("/v1/admin/upload-pause", json={
        "tenantId": "tenant-that-does-not-exist", "paused": True}).status_code == 404
    assert _status(j) is None


def test_only_a_super_admin_may_change_it(journey):
    app.dependency_overrides.pop(require_super_admin_request, None)
    client = _client()
    assert client.put("/v1/admin/upload-pause", json={"paused": True}).status_code in (401, 403)
    assert client.get("/v1/admin/upload-pause").status_code in (401, 403)


def test_the_change_is_logged(journey, monkeypatch):
    from audit_core import uc03_p2_upload_pause as pause_module

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(pause_module, "logger",
                        SimpleNamespace(info=lambda event, **fields: events.append((event, fields))))
    _as_super_admin()
    client = _client()
    assert client.put("/v1/admin/upload-pause", json={"paused": True, "message": MESSAGE}).status_code == 200
    assert client.put("/v1/admin/upload-pause", json={"paused": False}).status_code == 200
    assert [(e, f["scope"], f["paused"], f["previous_paused"], f["actor_id"]) for e, f in events] == [
        ("upload_pause_changed", "ALL", True, None, "sa-1"),
        ("upload_pause_changed", "ALL", False, True, "sa-1"),
    ]
