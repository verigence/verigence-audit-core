"""Journey files taken from the Project's Google Drive (product owner 7 and 12 Nov 2026): the
folders Verigence makes, what the change feed finds, a run that takes the waiting files into
the bulk upload and the journey's photos, whose journey a customer's folder is, closed
journeys, the archive, and who may ask for what.
Real Postgres; Google Drive, storage, Document Intelligence and Security are doubles."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    P2Journey,
    add_evidence,
    add_extracted_field,
    add_page,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import google_drive, uc03_p2_storage, uc03_p2_worker
from audit_core import uc03_p2_bulk_upload as bulk_upload
from audit_core import uc03_p2_drive_intake as drive
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.google_drive import FOLDER_TYPE, DriveNotFound, DriveSettings
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client

SETTINGS = DriveSettings(client_id="client", client_secret="secret")
PDF = "application/pdf"


class FakeDrive:
    """One Google account's Drive: items by id, and the feed of what changed, in order."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.data: dict[str, bytes] = {}
        self.log: list[str] = []
        self.downloads: list[str] = []
        self.shared: dict[str, dict[str, str]] = {}     # folder -> {permission id: email}
        self.locked: set[str] = set()
        self.refuse: set[str] = set()                   # emails Google will not share with

    def _new(self, name: str, mime: str, parent: str | None, data: bytes | None = None) -> dict:
        item = {"id": f"d{len(self.items) + 1:04d}", "name": name, "mimeType": mime,
                "parents": [parent] if parent else [], "trashed": False,
                "createdTime": datetime.now(UTC).isoformat(), "modifiedTime": datetime.now(UTC).isoformat()}
        if data is not None:
            item["size"] = str(len(data))
            item["md5Checksum"] = hashlib.md5(data).hexdigest()
            self.data[item["id"]] = data
        self.items[item["id"]] = item
        self.log.append(item["id"])
        return item

    # what a person does in the Drive app
    def folder(self, name: str, parent: str | None) -> str:
        return self._new(name, FOLDER_TYPE, parent)["id"]

    def put(self, name: str, parent: str, data: bytes, mime: str = PDF, owner: str | None = None) -> str:
        item = self._new(name, mime, parent, data)
        if owner:   # put there by a PC's own Google account
            item["owners"] = [{"emailAddress": owner}]
        return item["id"]

    def bin(self, item_id: str) -> None:
        self.items[item_id]["trashed"] = True
        self.log.append(item_id)

    def named(self, name: str) -> dict:
        [item] = [i for i in self.items.values() if i["name"] == name and not i["trashed"]]
        return item

    # what Verigence calls (google_drive.DriveClient)
    def account_email(self) -> str:
        return "project@example.com"

    def start_page_token(self) -> str:
        return str(len(self.log))

    def changes(self, page_token: str) -> tuple[list[dict], str]:
        return ([{"fileId": i, "removed": False, "file": dict(self.items[i])} for i in self.log[int(page_token):]],
                str(len(self.log)))

    def get(self, file_id: str) -> dict:
        if file_id not in self.items:
            raise DriveNotFound(404, "not found")
        return dict(self.items[file_id])

    def children(self, folder_id: str) -> list[dict]:
        return [dict(i) for i in self.items.values() if folder_id in i["parents"] and not i["trashed"]]

    def create_folder(self, name: str, parent_id: str | None) -> dict:
        if parent_id and (parent_id not in self.items or self.items[parent_id]["trashed"]):
            raise DriveNotFound(404, "parent gone")
        return dict(self._new(name, FOLDER_TYPE, parent_id))

    def move(self, file_id: str, new_parent_id: str, old_parent_id: str | None) -> None:
        self.items[file_id]["parents"] = [new_parent_id]
        self.log.append(file_id)

    def share(self, folder_id: str, email: str) -> str:
        if email in self.refuse:
            raise google_drive.DriveError(400, "Bad Request. User message: \"You cannot share this item\"")
        permission_id = f"p{sum(len(v) for v in self.shared.values()) + 1}-{email}"
        self.shared.setdefault(folder_id, {})[permission_id] = email
        return permission_id

    def unshare(self, folder_id: str, permission_id: str) -> None:
        self.shared.get(folder_id, {}).pop(permission_id, None)

    def lock_sharing(self, folder_id: str) -> None:
        self.locked.add(folder_id)

    def bin_folder(self, folder_id: str) -> bool:
        if self.children(folder_id):
            return False
        self.bin(folder_id)
        return True

    def download(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self.data[file_id]


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, key: str, payload: bytes, *, content_type: str) -> None:
        self.objects[key] = payload

    def get_object(self, key: str) -> bytes:
        if key not in self.objects and key.startswith("p2/"):  # a test page (p2_support.add_page)
            return b"%PDF-1.4 page " + key.encode()
        return self.objects[key]

    def head_object(self, key: str) -> dict:
        return {"contentLength": len(self.objects[key]), "contentType": PDF, "etag": "e"}

    def presign_get(self, key: str, *, expires_seconds: int = 1800) -> str:
        return f"https://r2.example/{key}"

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]

    def delete_objects(self, keys: list[str]) -> int:
        for k in keys:
            self.objects.pop(k, None)
        return len(keys)


class FakeSecurity:
    def get_service_token(self, *, audience: str) -> str:
        return "token"


class FakeDi:
    def delete_document(self, **_) -> None:
        return None


@pytest.fixture
def s(monkeypatch):
    engine = database_engine()
    journey = create_p2_journey(engine, prefix="drive")
    fake, bucket = FakeDrive(), Bucket()
    monkeypatch.setenv("AUDIT_CORE_GOOGLE_DRIVE_CLIENT_ID", SETTINGS.client_id)
    monkeypatch.setenv("AUDIT_CORE_GOOGLE_DRIVE_CLIENT_SECRET", SETTINGS.client_secret)
    monkeypatch.setattr(drive, "open_client", lambda settings, token: fake)
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: bucket)
    monkeypatch.setattr(uc03_p2_worker, "get_p2_document_storage", lambda: bucket)
    monkeypatch.setattr(uc03_p2_worker, "get_security_oauth_client", FakeSecurity)
    monkeypatch.setattr(uc03_p2_worker, "get_di_capture_v2_client", FakeDi)
    with engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.projects SET project_name='Mahindra Orissa' WHERE tenant_id=:t"),
                  {"t": journey.tenant_id})
        c.execute(text("UPDATE auditcore.dealers SET dealer_name='Aditya Motors' WHERE tenant_id=:t"),
                  {"t": journey.tenant_id})
        c.execute(text("UPDATE auditcore.dealer_outlets SET outlet_name='Aditya Motors - Cuttack' WHERE tenant_id=:t"),
                  {"t": journey.tenant_id})
        c.execute(text("INSERT INTO auditcore.p2_drive_connections (tenant_id, google_email, refresh_token_sealed, "
                       "changes_token) VALUES (:t, 'project@example.com', :sealed, '0')"),
                  {"t": journey.tenant_id, "sealed": google_drive.seal(SETTINGS, "refresh")})
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=journey.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization("PC")
    out = SimpleNamespace(journey=journey, engine=engine, t=journey.tenant_id, fake=fake, bucket=bucket,
                          client=TestClient(app, raise_server_exceptions=False),
                          base=f"/p2/v1/tenants/{journey.tenant_id}")
    try:
        yield out
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, journey.tenant_id)
        engine.dispose()


def _as(s, actor_id: str, functional_role: str = "PC") -> None:
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization(functional_role)


def _rows(s, sql: str, **params) -> list[dict]:
    with s.engine.begin() as c:
        set_tenant_context(c, s.t)
        return [dict(r) for r in c.execute(text(sql), {"t": s.t, **params}).mappings().all()]


def _exec(s, sql: str, **params) -> None:
    with s.engine.begin() as c:
        set_tenant_context(c, s.t)
        c.execute(text(sql), {"t": s.t, **params})


def _tree(s) -> str:
    """The outlet's folder, after Verigence has made the tree."""
    client, _ = drive.connected_client(s.engine, s.t)
    drive.ensure_tree(s.engine, s.t, client)
    return s.fake.named("Aditya Motors - Cuttack")["id"]


def _sync(s) -> dict:
    client, link = drive.connected_client(s.engine, s.t)
    return drive.sync(s.engine, s.t, client, link)


def _files(s) -> dict[str, dict]:
    return {r["file_name"]: r for r in _rows(s, "SELECT * FROM auditcore.p2_drive_files WHERE tenant_id=:t")}


def _run(s, **body) -> dict:
    """A run asked for through the API and done by the worker's job."""
    created = s.client.post(f"{s.base}/drive/runs", json={"outletId": str(s.journey.outlet_id), **body})
    assert created.status_code == 201, created.text
    run_id = created.json()["runId"]
    [job] = _rows(s, "SELECT * FROM auditcore.p2_worker_jobs WHERE job_kind='DRIVE_RUN' AND tag=:tag",
                  tag=f"{s.t}:{run_id}")
    assert job["status"] == "PENDING"
    return {"runId": run_id, **drive.run_drive_intake(s.engine, job["tag"])}


def _bulk_files(s) -> list[dict]:
    return _rows(s, "SELECT f.*, u.source, u.status AS bulk_status, u.uploaded_by_actor_id FROM auditcore.p2_bulk_files f "
                    "JOIN auditcore.p2_bulk_uploads u ON u.tenant_id=f.tenant_id AND u.bulk_id=f.bulk_id "
                    "WHERE f.tenant_id=:t ORDER BY f.created_at_utc, f.original_filename")


def _read_and_settle(s, bulk_id) -> None:
    """The bulk upload's own jobs, with every page read (Document Intelligence is a double)."""
    tag = f"{s.t}:{bulk_id}"
    bulk_upload.run_bulk_upload(s.engine, tag)
    for row in _rows(s, "SELECT DISTINCT journey_id FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b "
                        "AND outcome='READING'", b=bulk_id):
        j = P2Journey(**{**s.journey.__dict__, "journey_id": row["journey_id"]})
        _exec(s, "UPDATE auditcore.p2_upload_batches SET batch_status='COMPLETED', identity_status='BELONGS' "
                 "WHERE tenant_id=:t AND journey_id=:j", j=row["journey_id"])
        batch, queue, doc = add_page(j, status="READY")
        _exec(s, "UPDATE auditcore.p2_document_queue SET page_sha256=:sha, unit_kind='PAGE' WHERE tenant_id=:t "
                 "AND queue_id=:q", sha=uuid4().hex, q=queue)
        _exec(s, "UPDATE auditcore.p2_upload_batches SET identity_status='BELONGS', batch_status='COMPLETED' "
                 "WHERE tenant_id=:t AND batch_id=:b", b=batch)
        add_evidence(j, di_document_id=doc, document_type_key="tax_invoice_tally")
    bulk_upload.run_bulk_settle(s.engine, tag)


def _close(s, journey_id, days_ago: int = 0) -> None:
    _exec(s, "INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status, "
             "business_completed_at_utc) VALUES (:t, :j, 'DELIVERY', 'DELIVERY_COMPLETED', :at)",
          j=journey_id, at=datetime.now(UTC) - timedelta(days=days_ago))


# --------------------------------------------------------------------------- names, state, sealing

def test_a_customers_folder_gives_the_name_and_the_number_in_either_order():
    assert drive.parse_customer_folder("Akansh-456909") == ("Akansh", "456909")
    assert drive.parse_customer_folder("456909 Akansh Kumar") == ("Akansh Kumar", "456909")
    assert drive.parse_customer_folder("OTF-BK12345-Ravi") == ("Ravi", "BK12345")
    assert drive.parse_customer_folder("Ravi Das-9876543210") == ("Ravi Das", "9876543210")
    assert drive.parse_customer_folder("Sunita") == ("Sunita", None)
    assert drive.number_key("bk/0012") == drive.number_key("BK12") == "BK-12"
    assert drive.mobile_of("Ravi 98765 43210") == "9876543210" and drive.mobile_of("456909") is None
    assert drive.is_photo_folder("Photos_other_docs") and drive.is_photo_folder("pics")
    assert not drive.is_photo_folder("Dcouments")
    assert drive.names_differ("Ravi", "Sunita Das") and not drive.names_differ("Akansh", "AKANSH KUMAR DAS")
    assert not drive.names_differ(None, "Sunita")


def test_the_end_of_the_day_is_nine_in_the_evening_india_time():
    nine = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)  # 21:00 IST
    assert drive.end_of_day_due(nine + timedelta(minutes=5)) == "2026-11-12"
    assert drive.end_of_day_due(nine - timedelta(minutes=5)) is None      # yesterday's slot is long past
    assert drive.end_of_day_due(nine + timedelta(hours=4)) is None        # the night does not run it


def test_the_approval_state_is_signed_and_expires_and_the_token_is_sealed():
    state = google_drive.sign_state(SETTINGS, tenant_id="t1", actor_id="admin", now=1000.0)
    assert google_drive.read_state(SETTINGS, state, now=1100.0) == {"tenant_id": "t1", "actor_id": "admin"}
    assert google_drive.read_state(SETTINGS, state, now=1000.0 + 901) is None
    assert google_drive.read_state(SETTINGS, state[:-2] + "xx", now=1100.0) is None
    assert google_drive.read_state(DriveSettings("client", "other"), state, now=1100.0) is None
    sealed = google_drive.seal(SETTINGS, "refresh-token")
    assert "refresh-token" not in sealed and google_drive.unseal(SETTINGS, sealed) == "refresh-token"
    with pytest.raises(google_drive.DriveAuthorizationLost):
        google_drive.unseal(DriveSettings("client", "other"), sealed)
    url = google_drive.consent_url(SETTINGS, state)
    assert "access_type=offline" in url and "prompt=consent" in url and "client_id=client" in url


# --------------------------------------------------------------------------- the folders

def test_verigence_makes_the_project_dealership_and_outlet_folders_once(s):
    outlet = _tree(s)
    names = sorted(i["name"] for i in s.fake.items.values())
    assert names == ["Aditya Motors", "Aditya Motors - Cuttack", "Mahindra Orissa"]
    assert s.fake.items[outlet]["parents"] == [s.fake.named("Aditya Motors")["id"]]
    client, _ = drive.connected_client(s.engine, s.t)
    assert drive.ensure_tree(s.engine, s.t, client) == {"made": 0}
    # A second PC at the outlet: still the outlet's one folder, with nothing made in it.
    _exec(s, "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, dealer_id, "
             "outlet_id) VALUES (:t, 'second-pc', 'PC', :d, :o)", d=s.journey.dealer_id, o=s.journey.outlet_id)
    assert drive.ensure_tree(s.engine, s.t, client) == {"made": 0}
    assert not [i for i in s.fake.items.values() if i["parents"] == [outlet]]


def test_the_pc_folders_of_12_nov_are_emptied_into_the_outlets_folder_and_binned(s):
    """Product owner 2026-11-12: "each outlet should have one folder". The folders per PC
    made before that go; what a PC had already put in one is still the outlet's."""
    outlet = _tree(s)
    used, empty = s.fake.folder("Ravi", outlet), s.fake.folder("Second PC", outlet)
    for folder_id, actor in ((used, s.journey.actor_id), (empty, "second-pc")):
        _exec(s, "INSERT INTO auditcore.p2_drive_folders (tenant_id, drive_folder_id, folder_kind, parent_folder_id, "
                 "folder_name, dealer_id, outlet_id, pc_actor_id, made_by_system) "
                 "VALUES (:t, :f, 'PC', :p, 'PC', :d, :o, :a, true)", f=folder_id, p=outlet,
              d=s.journey.dealer_id, o=s.journey.outlet_id, a=actor)
    customer = s.fake.folder("Akansh-456909", used)
    s.fake.put("docket.pdf", customer, b"%PDF docket")
    assert _sync(s)["new"] == 1
    client, _ = drive.connected_client(s.engine, s.t)
    assert drive.ensure_tree(s.engine, s.t, client) == {"made": 0, "retired": 2}
    assert s.fake.items[customer]["parents"] == [outlet]
    assert s.fake.items[used]["trashed"] and s.fake.items[empty]["trashed"]
    assert drive.ensure_tree(s.engine, s.t, client) == {"made": 0}       # once
    _sync(s)
    file = _files(s)["docket.pdf"]
    assert file["file_status"] == "NEW" and str(file["outlet_id"]) == str(s.journey.outlet_id)
    # A customer's folder put straight in the outlet's folder afterwards is the outlet's as before.
    s.fake.put("pan.pdf", s.fake.folder("Bimal-778899", outlet), b"%PDF pan")
    assert _sync(s)["new"] == 1


def test_a_folder_deleted_in_drive_is_made_again(s):
    outlet = _tree(s)
    s.fake.bin(outlet)
    _sync(s)
    client, _ = drive.connected_client(s.engine, s.t)
    assert drive.ensure_tree(s.engine, s.t, client) == {"made": 1}
    assert s.fake.named("Aditya Motors - Cuttack")["id"] != outlet


# --------------------------------------------------------------------------- the PCs' own Google accounts

def test_a_pc_asks_for_the_folders_and_they_are_shared_with_their_google_account(s):
    """Product owner 2026-11-12: "I need option B" -- the PCs do not hold the account's password."""
    outlet = _tree(s)
    before = s.client.get(f"{s.base}/integrations/google-drive").json()
    assert before["myAccess"] == {"googleEmail": None, "folders": [
        {"outletId": str(s.journey.outlet_id), "outletName": "Aditya Motors - Cuttack", "dealerName": "Aditya Motors",
         "url": f"https://drive.google.com/drive/folders/{outlet}", "shared": False, "problem": None}]}
    assert s.client.post(f"{s.base}/drive/access", json={"googleEmail": "not an email"}).status_code == 422
    asked = s.client.post(f"{s.base}/drive/access", json={"googleEmail": " Ravi@Gmail.com "})
    assert asked.status_code == 200, asked.text
    assert asked.json()["googleEmail"] == "ravi@gmail.com" and asked.json()["folders"][0]["shared"] is True
    assert list(s.fake.shared[outlet].values()) == ["ravi@gmail.com"] and outlet in s.fake.locked
    # Asked again, and on every round: shared once.
    s.client.post(f"{s.base}/drive/access", json={"googleEmail": "ravi@gmail.com"})
    client, _ = drive.connected_client(s.engine, s.t)
    assert drive.ensure_shares(s.engine, s.t, client) == {"shared": 0, "removed": 0, "refused": 0}
    assert list(s.fake.shared[outlet].values()) == ["ravi@gmail.com"]
    # Another address: the folder moves to it.
    s.client.post(f"{s.base}/drive/access", json={"googleEmail": "ravi.k@gmail.com"})
    assert list(s.fake.shared[outlet].values()) == ["ravi.k@gmail.com"]
    # Someone who uploads for no outlet is given nothing; the administrator sees who has what.
    _as(s, "admin", "TENANT_ADMIN")
    assert s.client.post(f"{s.base}/drive/access", json={"googleEmail": "boss@gmail.com"}).status_code == 403
    [person] = s.client.get(f"{s.base}/integrations/google-drive").json()["people"]
    assert person["googleEmail"] == "ravi.k@gmail.com" and person["folders"][0]["shared"] is True


def test_a_pc_no_longer_mapped_loses_the_folder_and_a_refusal_is_kept_and_tried_again(s):
    outlet = _tree(s)
    s.fake.refuse.add("ravi@gmail.com")
    asked = s.client.post(f"{s.base}/drive/access", json={"googleEmail": "ravi@gmail.com"}).json()
    assert asked["folders"][0]["shared"] is False and "cannot share" in asked["folders"][0]["problem"]
    client, _ = drive.connected_client(s.engine, s.t)
    s.fake.refuse.clear()
    assert drive.ensure_shares(s.engine, s.t, client)["shared"] == 0                 # not before an hour has passed
    _exec(s, "UPDATE auditcore.p2_drive_shares SET updated_at_utc = now() - interval '2 hours' WHERE tenant_id=:t")
    assert drive.ensure_shares(s.engine, s.t, client)["shared"] == 1
    assert list(s.fake.shared[outlet].values()) == ["ravi@gmail.com"]
    _exec(s, "UPDATE auditcore.business_assignments SET assignment_status='INACTIVE' WHERE tenant_id=:t "
             "AND security_actor_id=:a", a=s.journey.actor_id)
    _exec(s, "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, dealer_id, "
             "outlet_id) VALUES (:t, 'second-pc', 'PC', :d, :o)", d=s.journey.dealer_id, o=s.journey.outlet_id)
    assert drive.ensure_shares(s.engine, s.t, client) == {"shared": 0, "removed": 1, "refused": 0}
    assert s.fake.shared[outlet] == {}


def test_in_an_outlet_with_two_pcs_a_file_is_the_pcs_whose_google_account_put_it(s):
    outlet = _tree(s)
    _exec(s, "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, dealer_id, "
             "outlet_id) VALUES (:t, 'second-pc', 'PC', :d, :o)", d=s.journey.dealer_id, o=s.journey.outlet_id)
    _exec(s, "INSERT INTO auditcore.p2_drive_people (tenant_id, actor_id, google_email) VALUES "
             "(:t, 'second-pc', 'Sita@gmail.com'), (:t, :a, 'ravi@gmail.com')", a=s.journey.actor_id)
    s.fake.put("docket.pdf", s.fake.folder("Akansh-456909", outlet), b"%PDF docket", owner="sita@gmail.com")
    s.fake.put("pan.pdf", s.fake.folder("Bimal-778899", outlet), b"%PDF pan", owner="ravi@gmail.com")
    s.fake.put("form.pdf", s.fake.folder("Chitta-112233", outlet), b"%PDF form")          # by the account itself
    _sync(s)
    files = _files(s)
    assert files["docket.pdf"]["pc_actor_id"] == "second-pc" and files["pan.pdf"]["pc_actor_id"] == s.journey.actor_id
    assert files["form.pdf"]["pc_actor_id"] is None
    assert drive.attributed_pc({"pcs": [{"actor_id": "a"}, {"actor_id": "b"}]}, "b", "a") == {"actor_id": "b"}
    assert drive.attributed_pc({"pcs": [{"actor_id": "a"}, {"actor_id": "b"}]}, None, "b") == {"actor_id": "b"}
    assert drive.attributed_pc({"pcs": [{"actor_id": "a"}, {"actor_id": "b"}]}, "gone", None) == {"actor_id": "a"}


# --------------------------------------------------------------------------- what the feed finds

def test_only_what_is_in_a_customers_folder_is_recorded_and_only_once(s):
    outlet = _tree(s)
    customer = s.fake.folder("Akansh-456909", outlet)
    s.fake.put("docket.pdf", customer, b"%PDF docket")
    s.fake.put("car.jpg", s.fake.folder("Photos_other_docs", customer), b"jpeg", "image/jpeg")
    s.fake.put("notes.docx", s.fake.folder("Dcouments", customer), b"docx",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    s.fake.put("own holiday.pdf", s.fake.folder("Personal", None), b"%PDF private")   # elsewhere in the account
    s.fake.put("stray.pdf", s.fake.named("Mahindra Orissa")["id"], b"%PDF stray")     # under no outlet
    found = _sync(s)
    assert found["new"] == 2 and found["setAside"] == 2
    files = _files(s)
    assert set(files) == {"docket.pdf", "car.jpg", "notes.docx", "stray.pdf"}
    assert (files["docket.pdf"]["lane"], files["docket.pdf"]["file_status"]) == ("DOCUMENT", "NEW")
    assert (files["car.jpg"]["lane"], files["car.jpg"]["file_status"]) == ("PHOTO", "NEW")
    assert files["notes.docx"]["file_status"] == "SET_ASIDE" and "PDF, JPEG and PNG" in files["notes.docx"]["detail"]
    assert files["stray.pdf"]["file_status"] == "SET_ASIDE" and "no outlet" in files["stray.pdf"]["detail"]
    assert files["docket.pdf"]["outlet_id"] == s.journey.outlet_id
    assert files["docket.pdf"]["customer_folder_id"] == customer == files["car.jpg"]["customer_folder_id"]
    [folder] = _rows(s, "SELECT * FROM auditcore.p2_drive_folders WHERE tenant_id=:t AND folder_kind='CUSTOMER'")
    assert (folder["customer_name"], folder["booking_number"], folder["number_key"]) == ("Akansh", "456909", "456909")
    assert _sync(s) == {"new": 0, "setAside": 0, "folders": 0, "removed": 0}
    # Taken out of Drive before it was taken: no longer waiting.
    s.fake.bin(s.fake.named("docket.pdf")["id"])
    _sync(s)
    assert _files(s)["docket.pdf"]["file_status"] == "SET_ASIDE"
    status = s.client.get(f"{s.base}/integrations/google-drive").json()
    assert status["connected"] and status["waiting"] == 1 and status["account"] == "project@example.com"
    assert status["folderUrl"].endswith(s.fake.named("Mahindra Orissa")["id"]) and status["retentionDays"] == 30


def test_files_a_computer_leaves_behind_are_ignored(s):
    """A folder uploaded from a Mac carries .DS_Store (seen in the 12 Nov sample zip)."""
    outlet = _tree(s)
    customer = s.fake.folder("Akansh-456909", outlet)
    s.fake.put(".DS_Store", customer, b"mac", "application/octet-stream")
    s.fake.put("Thumbs.db", customer, b"win", "application/octet-stream")
    s.fake.put("docket.pdf", s.fake.folder("__MACOSX", customer), b"%PDF mac copy")
    s.fake.put("docket.pdf", customer, b"%PDF docket")
    found = _sync(s)
    assert found["new"] == 1 and found["setAside"] == 0
    assert set(_files(s)) == {"docket.pdf"}


def test_the_feed_started_again_meets_the_whole_tree(s):
    outlet = _tree(s)
    s.fake.put("docket.pdf", s.fake.folder("Akansh-456909", outlet), b"%PDF docket")
    _exec(s, "UPDATE auditcore.p2_drive_connections SET changes_token=NULL WHERE tenant_id=:t")
    assert _sync(s)["new"] == 1
    [link] = _rows(s, "SELECT changes_token FROM auditcore.p2_drive_connections WHERE tenant_id=:t")
    assert link["changes_token"] == str(len(s.fake.log))


# --------------------------------------------------------------------------- a run

def test_a_pcs_submit_takes_the_folder_into_a_bulk_upload_and_the_folder_learns_its_journey(s):
    outlet = _tree(s)
    customer = s.fake.folder("Bimal-778899", outlet)
    s.fake.put("docket.pdf", customer, b"%PDF bimal docket")
    s.fake.put("pan.pdf", customer, b"%PDF bimal pan")
    s.fake.put("car.jpg", s.fake.folder("Photos_other_docs", customer), b"jpeg bimal", "image/jpeg")
    run = _run(s)
    assert (run["documents"], run["photosWaiting"], run["failed"]) == (2, 1, 0)
    files = _bulk_files(s)
    assert [f["original_filename"] for f in files] == ["docket.pdf", "pan.pdf"]
    assert {f["source"] for f in files} == {"DRIVE"} and {f["uploaded_by_actor_id"] for f in files} == {s.journey.actor_id}
    assert {f["group_key"] for f in files} == {f"drive:{s.journey.outlet_id}:778899"}
    assert {f["booking_number"] for f in files} == {"778899"} and {f["target_journey_id"] for f in files} == {None}
    assert files[0]["object_key"].endswith("/Aditya-Motors/Aditya-Motors-Cuttack/"
                                           f"{drive.ist_today():%Y-%m-%d}/Bimal-778899/docket.pdf")
    assert s.bucket.objects[files[0]["object_key"]] == b"%PDF bimal docket"
    assert _files(s)["car.jpg"]["file_status"] == "WAITING"
    listed = s.client.get(f"{s.base}/drive/runs").json()
    assert listed["inProgress"] == 1 and listed["items"][0]["trigger"] == "PC_SUBMIT"

    _read_and_settle(s, files[0]["bulk_id"])
    files = _bulk_files(s)
    assert {f["outcome"] for f in files} == {"NEW_BOOKING"} and len({f["placed_journey_id"] for f in files}) == 1
    journey_id = files[0]["placed_journey_id"]
    assert drive.settle(s.engine, s.t) == {"folders": 1, "photos": 1, "runs": 1}
    [folder] = _rows(s, "SELECT * FROM auditcore.p2_drive_folders WHERE tenant_id=:t AND drive_folder_id=:f", f=customer)
    assert folder["journey_id"] == journey_id
    [photo] = _rows(s, "SELECT * FROM auditcore.delivery_vehicle_photos WHERE tenant_id=:t AND journey_id=:j", j=journey_id)
    assert photo["original_filename"] == "car.jpg" and s.bucket.objects[photo["object_key"]] == b"jpeg bimal"
    report = s.client.get(f"{s.base}/drive/runs/{run['runId']}").json()
    assert report["run"]["status"] == "DONE"
    assert report["totals"]["files"] == 3 and report["totals"]["newBookings"] == 2 and report["totals"]["photos"] == 1
    [customer_row] = report["customers"]
    assert customer_row["customerFolder"] == "Bimal-778899" and customer_row["journeyId"] == str(journey_id)
    assert {f["state"] for f in customer_row["files"]} == {"NEW_BOOKING", "TAKEN"}

    # Later: the delivery file is put in the same folder. Only it is taken, straight to the journey.
    before = len(s.fake.downloads)
    s.fake.put("invoice.pdf", customer, b"%PDF bimal invoice")
    run = _run(s)
    assert run["documents"] == 1 and len(s.fake.downloads) == before + 1
    [new] = [f for f in _bulk_files(s) if f["original_filename"] == "invoice.pdf"]
    assert new["target_journey_id"] == journey_id
    bulk_upload.run_bulk_upload(s.engine, f"{s.t}:{new['bulk_id']}")
    [new] = [f for f in _bulk_files(s) if f["original_filename"] == "invoice.pdf"]
    assert new["outcome"] == "ADDED_TO_BOOKING" and new["placed_journey_id"] == journey_id
    assert drive.settle(s.engine, s.t)["runs"] == 1
    # And the same file put in again under another name is not read again.
    s.fake.put("invoice copy.pdf", customer, b"%PDF bimal invoice")
    _run(s)
    [copy] = [f for f in _bulk_files(s) if f["original_filename"] == "invoice copy.pdf"]
    bulk_upload.run_bulk_upload(s.engine, f"{s.t}:{copy['bulk_id']}")
    [copy] = [f for f in _bulk_files(s) if f["original_filename"] == "invoice copy.pdf"]
    assert copy["outcome"] == "EXACT_DUPLICATE"


def test_a_folder_made_again_with_the_same_number_is_the_same_journey(s):
    outlet = _tree(s)
    first = s.fake.folder("Chitra-112233", outlet)
    _sync(s)
    _exec(s, "UPDATE auditcore.p2_drive_folders SET journey_id=:j WHERE tenant_id=:t AND drive_folder_id=:f",
          j=s.journey.journey_id, f=first)
    again = s.fake.folder("Chitra Sahu 112233", outlet)
    s.fake.put("invoice.pdf", again, b"%PDF chitra invoice")
    _run(s)
    [file] = _bulk_files(s)
    assert file["target_journey_id"] == s.journey.journey_id
    assert file["group_key"] == f"drive:{s.journey.outlet_id}:112233"


def test_a_booking_uploaded_in_the_app_is_found_by_its_booking_number_or_the_mobile(s):
    docket = uuid4()
    add_evidence(s.journey, di_document_id=docket, document_type_key="booking_docket")
    add_extracted_field(s.journey, di_document_id=docket, field_key="booking_reference_number", value="OTF/0456909",
                        document_type="booking_docket")
    add_extracted_field(s.journey, di_document_id=docket, field_key="customer_phone", value="+91 98765 43210")
    outlet = _tree(s)
    by_otf = s.fake.folder("Akansh-OTF456909", outlet)
    by_mobile = s.fake.folder("Akansh-9876543210", outlet)
    other = s.fake.folder("Deepak-9000000001", outlet)
    _sync(s)
    with s.engine.begin() as c:
        set_tenant_context(c, s.t)
        for folder_id, expected in ((by_otf, s.journey.journey_id), (by_mobile, s.journey.journey_id), (other, None)):
            found = drive.journey_for_folder(c, s.t, drive._folder(c, s.t, folder_id))
            assert (found["journey_id"] if found else None) == expected
    # Kept on the folder, so the next look does not search again.
    assert {r["journey_id"] for r in _rows(s, "SELECT journey_id FROM auditcore.p2_drive_folders WHERE tenant_id=:t "
                                              "AND drive_folder_id = ANY(:ids)", ids=[by_otf, by_mobile])} == {
        s.journey.journey_id}


def test_files_for_a_closed_journey_are_held_for_the_team_lead(s):
    outlet = _tree(s)
    customer = s.fake.folder("Esha-445566", outlet)
    s.fake.put("late.pdf", customer, b"%PDF late")
    s.fake.put("late.jpg", s.fake.folder("Photos", customer), b"jpeg late", "image/jpeg")
    _sync(s)
    _exec(s, "UPDATE auditcore.p2_drive_folders SET journey_id=:j WHERE tenant_id=:t AND drive_folder_id=:f",
          j=s.journey.journey_id, f=customer)
    _close(s, s.journey.journey_id)
    run = _run(s)
    assert (run["documents"], run["photos"], run["held"]) == (0, 0, 2)
    files = _files(s)
    assert {f["file_status"] for f in files.values()} == {"SET_ASIDE"}
    assert "is closed" in files["late.pdf"]["detail"] and not s.fake.downloads and not _bulk_files(s)


# --------------------------------------------------------------------------- the archive

def test_a_closed_journeys_folder_goes_to_the_archive_after_the_retention_and_nothing_is_deleted(s):
    outlet = _tree(s)
    customer = s.fake.folder("Farah-909090", outlet)
    file_id = s.fake.put("docket.pdf", customer, b"%PDF farah")
    _sync(s)
    _exec(s, "UPDATE auditcore.p2_drive_folders SET journey_id=:j WHERE tenant_id=:t AND drive_folder_id=:f",
          j=s.journey.journey_id, f=customer)
    _exec(s, "UPDATE auditcore.p2_drive_files SET file_status='TAKEN' WHERE tenant_id=:t")
    _close(s, s.journey.journey_id, days_ago=10)
    client, _ = drive.connected_client(s.engine, s.t)
    assert drive.archive_closed(s.engine, s.t, client) == {"archived": 0}       # 30 days to begin with
    _as(s, "admin", "TENANT_ADMIN")
    saved = s.client.put(f"{s.base}/integrations/google-drive/settings", json={"retentionDays": 7}).json()
    assert saved == {"retentionDays": 7, "dailyRunTime": "21:00", "dailyRunEnabled": True, "outletIds": None}
    assert drive.archive_closed(s.engine, s.t, client) == {"archived": 1}
    month = f"{datetime.now(UTC) - timedelta(days=10):%Y-%m}"
    path = []
    parent = s.fake.items[customer]["parents"][0]
    while parent:
        path.append(s.fake.items[parent]["name"])
        parent = (s.fake.items[parent]["parents"] or [None])[0]
    assert path[:3] == ["Aditya Motors - Cuttack", month, "Archive"] and path[-1] == "Mahindra Orissa"
    assert not s.fake.items[customer]["trashed"] and not s.fake.items[file_id]["trashed"]
    assert drive.archive_closed(s.engine, s.t, client) == {"archived": 0}
    # The move shows in the feed; the folder stays the customer's.
    _sync(s)
    [folder] = _rows(s, "SELECT * FROM auditcore.p2_drive_folders WHERE tenant_id=:t AND drive_folder_id=:f", f=customer)
    assert folder["folder_kind"] == "CUSTOMER" and folder["archived_at_utc"] is not None
    assert folder["journey_id"] == s.journey.journey_id


# --------------------------------------------------------------------------- who may ask

def test_who_may_run_and_who_may_connect(s):
    _tree(s)
    other_outlet = uuid4()
    assert s.client.post(f"{s.base}/drive/runs", json={"outletId": str(other_outlet)}).status_code == 404
    assert s.client.post(f"{s.base}/drive/runs", json={}).status_code == 403            # a PC names an outlet
    assert s.client.post(f"{s.base}/integrations/google-drive/connect").status_code == 403
    assert s.client.put(f"{s.base}/integrations/google-drive/settings", json={"retentionDays": 5}).status_code == 403
    # A Team Lead of the dealership: on demand, for a day.
    _exec(s, "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, dealer_id) "
             "VALUES (:t, 'lead', 'TL', :d)", d=s.journey.dealer_id)
    _as(s, "lead", "TL")
    assert s.client.post(f"{s.base}/drive/runs", json={}).status_code == 422             # not the whole Project
    made = s.client.post(f"{s.base}/drive/runs", json={"dealerId": str(s.journey.dealer_id), "date": "2026-11-12",
                                                       "mode": "batch", "requestedByName": "Lead One"})
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["trigger"], body["mode"], body["date"], body["requestedByRole"]) == (
        "ON_DEMAND", "BATCH", "2026-11-12", "TL")
    # An administrator connects: sent to Google with a state only this service can read.
    _as(s, "admin", "TENANT_ADMIN")
    url = s.client.post(f"{s.base}/integrations/google-drive/connect").json()["authUrl"]
    assert url.startswith(google_drive.AUTH_URL)
    state = url.split("state=")[1].split("&")[0]
    assert google_drive.read_state(SETTINGS, state)["tenant_id"] == s.t


def test_the_callback_keeps_the_sealed_token_and_makes_the_folders(s, monkeypatch):
    _exec(s, "DELETE FROM auditcore.p2_drive_connections WHERE tenant_id=:t")
    monkeypatch.setattr(google_drive, "exchange_code", lambda settings, code: {"refresh_token": "new-refresh"})
    state = google_drive.sign_state(SETTINGS, tenant_id=s.t, actor_id="admin")
    gone = s.client.get("/p2/v1/integrations/google-drive/callback", params={"code": "c", "state": "bad"},
                        follow_redirects=False)
    assert gone.status_code == 303 and gone.headers["location"].endswith("drive=expired")
    back = s.client.get("/p2/v1/integrations/google-drive/callback", params={"code": "c", "state": state},
                        follow_redirects=False)
    # ...and says which Project it is about: the page shows the one the person has open.
    assert back.status_code == 303 and back.headers["location"].endswith(f"drive=connected&project={s.t}")
    [link] = _rows(s, "SELECT * FROM auditcore.p2_drive_connections WHERE tenant_id=:t")
    assert link["google_email"] == "project@example.com" and link["connected_by_actor_id"] == "admin"
    assert google_drive.unseal(SETTINGS, link["refresh_token_sealed"]) == "new-refresh"
    assert link["root_folder_id"] == s.fake.named("Mahindra Orissa")["id"]
    assert s.fake.named("Aditya Motors - Cuttack")


def test_the_round_asks_for_the_end_of_day_run_once(s):
    _tree(s)
    nine = datetime(2026, 11, 12, 15, 35, tzinfo=UTC)
    first = drive.drive_round(s.engine, [s.t], worker_id="w1", now=nine)
    assert "endOfDayRun" in first[s.t]
    assert "endOfDayRun" not in drive.drive_round(s.engine, [s.t], worker_id="w2", now=nine)[s.t]
    [run] = _rows(s, "SELECT * FROM auditcore.p2_drive_runs WHERE tenant_id=:t")
    assert (run["run_trigger"], run["requested_by_actor_id"], run["outlet_id"]) == ("END_OF_DAY", "system", None)
    assert UUID(str(run["run_id"]))


def test_the_project_tracker_shows_the_drive_figures(s):
    from audit_core.uc03_p2_project_tracker import build_project_tracker

    outlet = _tree(s)
    customer = s.fake.folder("Gita-121212", outlet)
    s.fake.put("docket.pdf", customer, b"%PDF gita docket")
    s.fake.put("pan.pdf", customer, b"%PDF gita pan")
    _sync(s)
    with s.engine.begin() as c:
        set_tenant_context(c, s.t)
        before = build_project_tracker(c, tenant_id=s.t, actor_id="admin", period="DAY", day=drive.ist_today(),
                                       admin=True)["totals"]["drive"]
    assert (before["files"], before["waiting"], before["taken"]) == (2, 2, 0)
    _run(s)
    with s.engine.begin() as c:
        set_tenant_context(c, s.t)
        tracker = build_project_tracker(c, tenant_id=s.t, actor_id="admin", period="DAY", day=drive.ist_today(),
                                        admin=True)
    assert (tracker["totals"]["drive"]["taken"], tracker["totals"]["drive"]["reading"]) == (2, 2)
    [person] = [p for d in tracker["dealers"] for o in d["outlets"] for p in o["people"]
                if p["actorId"] == s.journey.actor_id]
    assert person["drive"]["files"] == 2


def test_the_daily_run_time_and_the_outlets_that_use_drive_are_the_projects_to_set(s):
    _tree(s)
    _as(s, "admin", "TENANT_ADMIN")
    settings = f"{s.base}/integrations/google-drive/settings"
    shown = s.client.get(f"{s.base}/integrations/google-drive").json()
    assert (shown["dailyRunTime"], shown["dailyRunEnabled"], shown["outletIds"]) == ("21:00", True, None)
    assert [(o["outletName"], o["usesDrive"]) for o in shown["outlets"]] == [("Aditya Motors - Cuttack", True)]
    assert s.client.put(settings, json={"dailyRunTime": "25:00"}).status_code == 400  # the request itself is not valid
    assert s.client.put(settings, json={"outletIds": [str(uuid4())]}).status_code == 422
    saved = s.client.put(settings, json={"dailyRunTime": "18:30"}).json()
    assert (saved["dailyRunTime"], saved["retentionDays"], saved["outletIds"]) == ("18:30", 30, None)
    # 18:30 IST is 13:00 UTC: due then, not at nine.
    six_thirty = datetime(2026, 11, 12, 13, 5, tzinfo=UTC)
    assert "endOfDayRun" in drive.drive_round(s.engine, [s.t], worker_id="w1", now=six_thirty)[s.t]
    assert drive.end_of_day_due(datetime(2026, 11, 12, 12, 55, tzinfo=UTC), drive.time(18, 30)) is None
    # Switched off: the next day's slot asks for nothing.
    assert s.client.put(settings, json={"dailyRunEnabled": False}).json()["dailyRunEnabled"] is False
    next_day = six_thirty + timedelta(days=1)
    assert "endOfDayRun" not in drive.drive_round(s.engine, [s.t], worker_id="w1", now=next_day)[s.t]
    assert len(_rows(s, "SELECT 1 FROM auditcore.p2_drive_runs WHERE tenant_id=:t")) == 1


def test_the_daily_run_takes_only_the_outlets_that_use_drive_but_a_run_asked_for_takes_its_own(s):
    outlet = _tree(s)
    s.fake.put("docket.pdf", s.fake.folder("Hari-343434", outlet), b"%PDF hari")
    _sync(s)
    _as(s, "admin", "TENANT_ADMIN")
    saved = s.client.put(f"{s.base}/integrations/google-drive/settings", json={"outletIds": []}).json()
    assert saved["outletIds"] == []
    shown = s.client.get(f"{s.base}/integrations/google-drive").json()
    assert [o["usesDrive"] for o in shown["outlets"]] == [False]
    with s.engine.begin() as c:
        set_tenant_context(c, s.t)
        daily = drive.request_run(c, tenant_id=s.t, trigger="END_OF_DAY", actor_id="system")
    assert drive.run_drive_intake(s.engine, f"{s.t}:{daily['run_id']}")["documents"] == 0
    assert _files(s)["docket.pdf"]["file_status"] == "NEW"
    _as(s, s.journey.actor_id, "PC")
    assert _run(s)["documents"] == 1
    # Chosen again (null: every outlet): the daily run is for it once more.
    _as(s, "admin", "TENANT_ADMIN")
    assert s.client.put(f"{s.base}/integrations/google-drive/settings", json={"outletIds": None}).json()["outletIds"] is None
