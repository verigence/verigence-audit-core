"""The same file uploaded again (product owner, 2026-10-31).

PAN, mobile, customer name and model all those of an earlier journey: an
alert for the whole Project, and the new journey deleted when the PAN is an
individual's. Real Postgres; Document Intelligence, Security and R2 are doubles.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import delete_tenant_data
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

from audit_core.db import set_tenant_context
from audit_core.uc03_duplicate_journey_purge import (
    parse_tag,
    run_duplicate_journey_purge,
)
from audit_core.uc03_p2_same_file import (
    ALERT_KIND,
    DELETE_WORK_TYPE,
    FileIdentity,
    alert_body,
    is_individual_pan,
    matched_fields,
    run_same_file_delete,
    same_name,
    sync_same_file_detection,
)
from audit_core.uc03_p2_tenant_alerts import list_tenant_alerts

INDIVIDUAL_PAN = "ABCPE1234F"
COMPANY_PAN = "ABCCE1234F"


# ── doubles ───────────────────────────────────────────────────────────────────
class FakeSecurity:
    def get_service_token(self, *, audience: str) -> str:
        return "service-token"


class FakeDi:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_document(self, *, token: str, tenant_id: str, external_context_ref: str, document_id: str) -> None:
        self.deleted.append(document_id)


class FakeStorage:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def list_keys(self, prefix: str) -> list[str]:
        return []

    def delete_objects(self, keys: list[str]) -> int:
        self.deleted.extend(keys)
        return len(keys)


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def tenant():
    engine = database_engine()
    first = create_p2_journey(engine, prefix="same")
    try:
        yield first
    finally:
        delete_tenant_data(engine, first.tenant_id)
        engine.dispose()


def _journey(base: P2Journey, *, ref: str, created: datetime, pc: str = "Rakesh PC") -> P2Journey:
    """Another journey in the same tenant (and dealership) as ``base``."""
    with base.engine.begin() as connection:
        set_tenant_context(connection, base.tenant_id)
        customer_id = connection.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, display_name) "
                 "VALUES (:t, :d, :o, 'INDIVIDUAL', 'P2 Customer') RETURNING customer_id"),
            {"t": base.tenant_id, "d": base.dealer_id, "o": base.outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference, "
                 "created_at_utc, created_by_display_name) VALUES (:t, :d, :o, :c, :r, :at, :pc) RETURNING journey_id"),
            {"t": base.tenant_id, "d": base.dealer_id, "o": base.outlet_id, "c": customer_id, "r": ref,
             "at": created, "pc": pc},
        ).scalar_one()
    return P2Journey(engine=base.engine, tenant_id=base.tenant_id, journey_id=UUID(str(journey_id)),
                     actor_id=base.actor_id, dealer_id=base.dealer_id, outlet_id=base.outlet_id,
                     customer_id=UUID(str(customer_id)))


def _file(journey: P2Journey, *, pan: str | None = INDIVIDUAL_PAN, mobile: str | None = "+91 98765 43210",
          name: str | None = "Amit Kumar Sahoo", model: str | None = "THAR", sku: UUID | None = None) -> None:
    doc = uuid4()
    if pan or mobile:
        add_evidence(journey, di_document_id=doc, document_type_key="pan_card")
    if pan:
        add_extracted_field(journey, di_document_id=doc, field_key="pan_number", value=pan, document_type="pan_card")
    if mobile:
        add_extracted_field(journey, di_document_id=doc, field_key="customer_phone", value=mobile)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        if name:
            connection.execute(
                text("UPDATE auditcore.customers SET legal_name=:n, legal_name_status='VERIFIED' "
                     "WHERE tenant_id=:t AND customer_id=:c"),
                {"n": name, "t": journey.tenant_id, "c": journey.customer_id},
            )
        if model or sku:
            connection.execute(
                text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, model_name_snapshot, product_sku_id) "
                     "VALUES (:t, :j, :m, :s)"),
                {"t": journey.tenant_id, "j": journey.journey_id, "m": model, "s": sku},
            )


def _pair(tenant: P2Journey, **new_overrides):
    now = datetime.now(UTC)
    old = _journey(tenant, ref=f"OLD-{uuid4().hex[:6]}", created=now - timedelta(days=2), pc="Old PC")
    new = _journey(tenant, ref=f"NEW-{uuid4().hex[:6]}", created=now - timedelta(minutes=5), pc="New PC")
    _file(old)
    _file(new, **new_overrides)
    return old, new


def _quiet(journey: P2Journey, minutes: int = 60) -> None:
    """The journey's uploads are older than the delete's quiet period."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for table in ("p2_upload_batches", "p2_document_queue"):
            connection.execute(
                text(f"UPDATE auditcore.{table} SET created_at_utc = now() - (:m * interval '1 minute') "
                     "WHERE tenant_id=:t AND journey_id=:j"),
                {"m": minutes, "t": journey.tenant_id, "j": journey.journey_id},
            )


def _detect(journey: P2Journey) -> dict:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_same_file_detection(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                        correlation_id="test")


def _alerts(journey: P2Journey) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_tenant_alerts WHERE tenant_id=:t ORDER BY created_at_utc"),
            {"t": journey.tenant_id},
        ).mappings().all()]


def _queued(journey: P2Journey) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_work_queue WHERE tenant_id=:t AND work_type=:w"),
            {"t": journey.tenant_id, "w": DELETE_WORK_TYPE},
        ).mappings().all()]


def _exists(journey: P2Journey) -> bool:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j)"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()


def _delete(journey: P2Journey) -> str:
    return run_same_file_delete(
        journey.engine, tenant_id=journey.tenant_id, journey_id=journey.journey_id, correlation_id="test",
        security_client=FakeSecurity(), v2_client=FakeDi(), storage=FakeStorage(),
    )


# ── matching, without a database ──────────────────────────────────────────────
def _identity(**overrides) -> FileIdentity:
    values = {"journey_id": uuid4(), "created_at": datetime.now(UTC), "reference": "R", "pc_name": "PC",
              "dealer_name": "D", "outlet_name": "O", "pan": INDIVIDUAL_PAN, "mobile": "9876543210",
              "name": "Amit Kumar Sahoo", "model_id": None, "model_name": "THAR", "variant_name": None,
              "closed": False}
    values.update(overrides)
    return FileIdentity(**values)


def test_all_four_must_match():
    base = _identity()
    assert matched_fields(_identity(), base) is not None
    assert matched_fields(_identity(pan="ABCPE9999F"), base) is None
    assert matched_fields(_identity(mobile="9876500000"), base) is None
    assert matched_fields(_identity(name="Sunil Kumar Mallick"), base) is None
    assert matched_fields(_identity(model_name="SCORPIO N"), base) is None
    # A missing value is not a match.
    assert matched_fields(_identity(model_name=None), base) is None
    assert matched_fields(_identity(mobile=None), base) is None


def test_model_family_and_masked_names():
    base = _identity(model_name="THAR")
    assert matched_fields(_identity(model_name="NEW THAR 2WD"), base) is not None
    assert matched_fields(_identity(model_name="THAR ROXX"), base) is None
    assert same_name("AXXX KXXXR SXXXO", "Amit Kumar Sahoo")
    assert same_name("Mr. Amit Kumar Sahoo", "SAHOO AMIT KUMAR")
    assert not same_name("AXXX KXXXR SXXXO", "Anil Kumar Das")
    # Pinned on both sides: the master model decides, not the spelling.
    model = uuid4()
    assert matched_fields(_identity(model_id=model, model_name="X"), _identity(model_id=model, model_name="Y"))
    assert matched_fields(_identity(model_id=uuid4(), model_name="THAR"), _identity(model_id=model, model_name="THAR")) is None


def test_pan_holder_type():
    assert is_individual_pan("ABCPE1234F")
    for holder in "CFHATBLJG":
        assert not is_individual_pan(f"ABC{holder}E1234F")


def test_alert_text_names_customer_model_pc_and_dealership():
    payload = {"customerName": "Amit Kumar Sahoo", "model": "THAR",
               "newJourney": {"reference": "ADI-RD-1", "pcName": "Rakesh", "dealerName": "Adishakti", "outletName": "Rasulgarh"},
               "existingJourney": {"reference": "ADI-RD-0", "pcName": "Mona", "dealerName": "Adishakti", "outletName": "Patia"}}
    body = alert_body(payload, "DELETED")
    assert body.startswith("Same file being uploaded — Amit Kumar Sahoo, THAR, PC Rakesh, Adishakti/Rasulgarh.")
    assert "The new file ADI-RD-1 was deleted." in body
    assert "ADI-RD-0 (PC Mona, Adishakti/Patia)" in body
    assert "kept for review" in alert_body(payload, "KEPT_NOT_INDIVIDUAL")


# ── detection and delete, on the database ─────────────────────────────────────
def test_same_file_with_an_individual_pan_is_deleted_and_alerted(tenant):
    old, new = _pair(tenant)
    di_document = add_page(new)[2]
    _quiet(new)

    result = _detect(new)
    assert result["raised"] == 1
    [alert] = _alerts(new)
    assert alert["subject_journey_id"] == new.journey_id and alert["related_journey_id"] == old.journey_id
    assert alert["alert_status"] == "PENDING"  # published once the delete is done
    [work] = _queued(new)
    assert work["journey_id"] == new.journey_id and work["work_key"] == f"same-file:{new.journey_id}"

    di = FakeDi()
    outcome = run_same_file_delete(
        new.engine, tenant_id=new.tenant_id, journey_id=new.journey_id, correlation_id="test",
        security_client=FakeSecurity(), v2_client=di, storage=FakeStorage(),
    )
    assert outcome == "DELETED"
    assert str(di_document) in di.deleted
    assert not _exists(new) and _exists(old)
    [alert] = _alerts(new)
    assert alert["alert_status"] == "PUBLISHED" and alert["outcome"] == "DELETED"
    assert alert["expires_at_utc"] > alert["published_at_utc"]
    assert "was deleted" in alert["body"] and "New PC" in alert["body"]
    fields = {f["field"]: f for f in alert["payload"]["matchedFields"]}
    assert set(fields) == {"pan", "mobile", "name", "model"}
    assert fields["pan"]["newValue"] == "XXXXXX234F"
    # The PC who entered the new file is named by id, so their own screen beeps (2026-10-31).
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        creator = connection.execute(text("SELECT created_by_actor_id FROM auditcore.journeys WHERE tenant_id=:t "
                                          "AND journey_id=:j"), {"t": old.tenant_id, "j": old.journey_id}).scalar_one()
    assert alert["payload"]["existingJourney"]["pcActorId"] == (str(creator) if creator else None)
    assert "pcActorId" in alert["payload"]["newJourney"]
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        record = connection.execute(
            text("SELECT * FROM auditcore.p2_journey_deletions WHERE tenant_id=:t AND journey_id=:j"),
            {"t": new.tenant_id, "j": new.journey_id},
        ).mappings().one()
    assert record["deleted_by_actor_id"] == "SYSTEM" and record["deleted_by_role"] == "SYSTEM"
    assert record["deletion_status"] == "COMPLETED"
    assert record["reason"].startswith("Same file uploaded again: matches OLD-")


@pytest.mark.parametrize("differs", [
    {"pan": "ABCPE9999F"}, {"mobile": "9000000001"}, {"name": "Sunil Kumar Mallick"}, {"model": "SCORPIO N"},
    {"model": None},
])
def test_three_of_four_is_not_the_same_file(tenant, differs):
    _, new = _pair(tenant, **differs)
    assert _detect(new).get("raised", 0) == 0
    assert _alerts(new) == [] and _queued(new) == []


def test_company_pan_is_alerted_but_kept(tenant):
    now = datetime.now(UTC)
    old = _journey(tenant, ref="OLD-CO", created=now - timedelta(days=1))
    new = _journey(tenant, ref="NEW-CO", created=now)
    _file(old, pan=COMPANY_PAN, name="Sarthak Motors Pvt Ltd")
    _file(new, pan=COMPANY_PAN, name="Sarthak Motors Private Limited")
    assert _detect(new)["outcomes"] == ["KEPT_NOT_INDIVIDUAL"]
    [alert] = _alerts(new)
    assert alert["alert_status"] == "PUBLISHED" and "kept for review" in alert["body"]
    assert _queued(new) == [] and _exists(new)


def test_reviewed_new_file_is_kept(tenant):
    _, new = _pair(tenant)
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                 "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                 "VALUES (:t, :j, 'BOOKING', 'rev', 'REVIEWER', 1, :d, :o)"),
            {"t": new.tenant_id, "j": new.journey_id, "d": new.dealer_id, "o": new.outlet_id},
        )
    assert _detect(new)["outcomes"] == ["KEPT_REVIEWED"]
    assert _queued(new) == [] and _exists(new)
    assert "already marked reviewed" in _alerts(new)[0]["body"]


def test_reviewed_after_queueing_is_kept_by_the_delete_guard(tenant):
    _, new = _pair(tenant)
    _detect(new)
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                 "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                 "VALUES (:t, :j, 'BOOKING', 'rev', 'REVIEWER', 1, :d, :o)"),
            {"t": new.tenant_id, "j": new.journey_id, "d": new.dealer_id, "o": new.outlet_id},
        )
    assert _delete(new) == "KEPT_REVIEWED"
    assert _exists(new)
    assert _alerts(new)[0]["outcome"] == "KEPT_REVIEWED"


def test_closed_older_journey_is_not_paired(tenant):
    old, new = _pair(tenant)
    with old.engine.begin() as connection:
        set_tenant_context(connection, old.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
                 "VALUES (:t, :j, 'BOOKING', 'BOOKING_CANCELLED')"),
            {"t": old.tenant_id, "j": old.journey_id},
        )
    assert _detect(new).get("raised", 0) == 0
    assert _alerts(new) == []


def test_the_older_journeys_run_flags_the_newer_one_and_ties_break_on_journey_id(tenant):
    at = datetime.now(UTC) - timedelta(hours=1)
    first = _journey(tenant, ref="TIE-A", created=at)
    second = _journey(tenant, ref="TIE-B", created=at)
    _file(first)
    _file(second)
    newer, older = sorted((first, second), key=lambda j: str(j.journey_id), reverse=True)
    # Whichever journey's check runs, the one with the larger journey_id is the new file.
    assert _detect(older)["raised"] == 1
    assert [a["subject_journey_id"] for a in _alerts(older)] == [newer.journey_id]
    assert _detect(newer)["raised"] == 0
    assert len(_alerts(older)) == 1


def test_recomputing_does_not_alert_twice(tenant):
    _, new = _pair(tenant)
    assert _detect(new)["raised"] == 1
    assert _detect(new)["raised"] == 0
    assert _detect(new)["raised"] == 0
    assert len(_alerts(new)) == 1 and len(_queued(new)) == 1


def test_booking_unit_runs_the_check_after_the_sku_step(tenant):
    from audit_core.uc03_run_all_rules import _run_audit_core_rules_for_stage

    _, new = _pair(tenant, model="NEW THAR 2WD")
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        _run_audit_core_rules_for_stage(connection, tenant_id=new.tenant_id, journey_id=new.journey_id,
                                        stage="BOOKING", correlation_id="test")
    assert [a["subject_journey_id"] for a in _alerts(new)] == [new.journey_id]
    assert len(_queued(new)) == 1


def test_pinned_skus_compare_the_master_model(tenant):
    suffix = uuid4().hex[:8]
    with tenant.engine.begin() as connection:
        oem = connection.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
                                 {"c": f"SF-OEM-{suffix}"}).scalar_one()
        model = connection.execute(
            text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) VALUES (:o, :c, 'THAR ROXX') "
                 "RETURNING model_id"), {"o": oem, "c": f"SF-M-{suffix}"}).scalar_one()
        skus = []
        for n in (1, 2):
            variant = connection.execute(
                text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                     "VALUES (:m, :c, :c) RETURNING variant_id"), {"m": model, "c": f"SF-V{n}-{suffix}"}).scalar_one()
            skus.append(connection.execute(
                text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                     "VALUES (:o, :m, :v, :c) RETURNING product_sku_id"),
                {"o": oem, "m": model, "v": variant, "c": f"SF-S{n}-{suffix}"}).scalar_one())
    try:
        now = datetime.now(UTC)
        old = _journey(tenant, ref="PIN-OLD", created=now - timedelta(days=1))
        new = _journey(tenant, ref="PIN-NEW", created=now)
        _file(old, model="THAR ROXX", sku=skus[0])
        _file(new, model="THAR ROXX AX7L", sku=skus[1])  # another variant of the same model
        assert _detect(new)["raised"] == 1
    finally:
        delete_tenant_data(tenant.engine, tenant.tenant_id)
        with tenant.engine.begin() as connection:
            connection.execute(text("DELETE FROM auditcore.product_skus WHERE model_id=:m"), {"m": model})
            connection.execute(text("DELETE FROM auditcore.product_variants WHERE model_id=:m"), {"m": model})
            connection.execute(text("DELETE FROM auditcore.product_models WHERE model_id=:m"), {"m": model})
            connection.execute(text("DELETE FROM auditcore.oems WHERE oem_id=:o"), {"o": oem})


# ── the worker item ───────────────────────────────────────────────────────────
def test_worker_item_deletes_its_journey_and_settles_cleanly(tenant, monkeypatch):
    from concurrent.futures import Future

    from audit_core import uc03_p2_worker as worker

    old, new = _pair(tenant)
    _detect(new)
    monkeypatch.setattr(worker, "get_security_oauth_client", FakeSecurity)
    monkeypatch.setattr(worker, "get_di_capture_v2_client", FakeDi)
    monkeypatch.setattr(worker, "get_p2_document_storage", FakeStorage)
    [item] = [w for w in worker._claim_for_tenant(new.engine, new.tenant_id, 10) if w.work_type == DELETE_WORK_TYPE]
    future: Future = Future()
    try:
        worker.process_work(new.engine, item)
    except Exception as exc:  # noqa: BLE001 - the settle step decides
        future.set_exception(exc)
    else:
        future.set_result(None)
    assert isinstance(future.exception(), worker.JourneyDeleted)
    worker._settle_finished(new.engine, item, future)  # no LeaseLost, no retry
    assert not _exists(new) and _exists(old)
    assert _alerts(new)[0]["alert_status"] == "PUBLISHED"


# ── the alerts endpoint ───────────────────────────────────────────────────────
def test_alerts_endpoint_returns_published_unexpired_alerts(tenant):
    _pair(tenant, pan=None)  # nothing raised by detection here
    with tenant.engine.begin() as connection:
        set_tenant_context(connection, tenant.tenant_id)
        for status, published, expires, subject in (
            ("PUBLISHED", "now() - interval '1 hour'", "now() + interval '23 hours'", uuid4()),
            ("PUBLISHED", "now() - interval '2 days'", "now() - interval '1 day'", uuid4()),  # expired
            ("PENDING", "NULL", "NULL", uuid4()),  # delete still running
        ):
            connection.execute(
                text(f"INSERT INTO auditcore.p2_tenant_alerts (tenant_id, alert_kind, subject_journey_id, "
                     f"subject_journey_reference, alert_status, outcome, title, body, published_at_utc, expires_at_utc) "
                     f"VALUES (:t, :k, :s, 'REF', :st, 'DELETED', 'Same file being uploaded', 'body', "
                     f"{published}, {expires})"),
                {"t": tenant.tenant_id, "k": ALERT_KIND, "s": subject, "st": status},
            )
        auth = AllowAllAuthorization(role_key="REVIEWER")
        response = list_tenant_alerts(tenant_id=tenant.tenant_id, human_principal=SimpleNamespace(subject="any"),
                                      authorization_client=auth, connection=connection)
        assert [a.outcome for a in response.items] == ["DELETED"]
        assert auth.calls == ["audit.journey.read"]
        later = list_tenant_alerts(tenant_id=tenant.tenant_id, human_principal=SimpleNamespace(subject="any"),
                                   authorization_client=auth, connection=connection,
                                   since=datetime.now(UTC) - timedelta(minutes=5))
        assert later.items == []


def test_alerts_endpoint_refuses_without_permission(tenant):
    from audit_core.authorization import AuthorizationError

    class Deny:
        def check_user_permission(self, **_):
            return SimpleNamespace(allowed=False, role_key=None)

    with tenant.engine.begin() as connection, pytest.raises(AuthorizationError):
        list_tenant_alerts(tenant_id=tenant.tenant_id, human_principal=SimpleNamespace(subject="x"),
                           authorization_client=Deny(), connection=connection)


# ── item 8: the listed duplicates ─────────────────────────────────────────────
def test_purge_tag_names_the_tenant():
    assert parse_tag("afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572:item8") == ("afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572", False)
    assert parse_tag("t-1:item8-commit") == ("t-1", True)
    for bad in ("", "item8-commit", ":item8", "t-1:"):
        with pytest.raises(ValueError):
            parse_tag(bad)


def test_purge_dry_run_deletes_nothing_and_commit_only_the_listed(tenant):
    now = datetime.now(UTC)
    listed = _journey(tenant, ref="DUP-LISTED-1", created=now)
    kept = _journey(tenant, ref="DUP-KEPT-1", created=now - timedelta(days=1))
    add_page(listed)
    _quiet(listed)

    dry = run_duplicate_journey_purge(tenant.engine, f"{tenant.tenant_id}:item8", ["DUP-LISTED-1", "NO-SUCH-REF"])
    assert dry == {"listed": 2, "found": 1, "deleted": 0, "would_delete": 1, "refused": 0, "not_found": 1,
                   "ambiguous": 0, "failed": 0, "would_refuse": 0}
    assert _exists(listed) and _exists(kept)

    di = FakeDi()
    done = run_duplicate_journey_purge(tenant.engine, f"{tenant.tenant_id}:item8-commit", ["DUP-LISTED-1"],
                                       security_client=FakeSecurity(), v2_client=di, storage=FakeStorage())
    assert done["deleted"] == 1 and len(di.deleted) == 1
    assert not _exists(listed) and _exists(kept) and _exists(tenant)
    assert _alerts(tenant) == []  # no alert for this job
    # Run again: already gone.
    again = run_duplicate_journey_purge(tenant.engine, f"{tenant.tenant_id}:item8-commit", ["DUP-LISTED-1"],
                                        security_client=FakeSecurity(), v2_client=FakeDi(), storage=FakeStorage())
    assert again["not_found"] == 1 and again["deleted"] == 0


def test_purge_never_crosses_tenants(tenant):
    other = create_p2_journey(tenant.engine, prefix="same-other")
    try:
        with other.engine.begin() as connection:
            set_tenant_context(connection, other.tenant_id)
            ref = connection.execute(text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t"),
                                     {"t": other.tenant_id}).scalar_one()
        result = run_duplicate_journey_purge(tenant.engine, f"{tenant.tenant_id}:x-commit", [ref],
                                             security_client=FakeSecurity(), v2_client=FakeDi(), storage=FakeStorage())
        assert result["not_found"] == 1 and _exists(other)
    finally:
        delete_tenant_data(other.engine, other.tenant_id)


def test_deleting_the_new_file_rechecks_the_kept_copys_duplicate_finding(tenant):
    from audit_core.uc03_delivery_commands import _machine_flag

    old, new = _pair(tenant)
    _detect(new)
    with old.engine.begin() as connection:
        set_tenant_context(connection, old.tenant_id)
        _machine_flag(connection, tenant_id=old.tenant_id, journey_id=old.journey_id, stage_code="BOOKING",
                      rule_key=f"DUPLICATE_BOOKING:{new.journey_id}", finding_type="DUPLICATE_BOOKING",
                      severity="CRITICAL", title="Possible duplicate booking", description="test",
                      correlation_id="test", safe_payload={"matchBasis": "PAN"})
    assert _delete(new) == "DELETED"
    with old.engine.begin() as connection:
        set_tenant_context(connection, old.tenant_id)
        units = connection.execute(
            text("SELECT payload FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='CONTROL_EVALUATE'"),
            {"t": old.tenant_id, "j": old.journey_id},
        ).scalars().all()
    assert {"unit": "NATIVE:BOOKING", "force": True} in units


# ── review of #594 (2026-10-31) ───────────────────────────────────────────────
def _stage(journey: P2Journey, status: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
                 "VALUES (:t, :j, 'BOOKING', :s)"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": status},
        )


def _ready_to_delete(tenant: P2Journey, **new_overrides):
    old, new = _pair(tenant, **new_overrides)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    return old, new


# 1. only a file uploaded in the last 48 hours
def test_an_older_pair_is_left_to_the_duplicate_booking_rule(tenant, monkeypatch):
    from audit_core import uc03_p2_same_file

    now = datetime.now(UTC)
    old = _journey(tenant, ref="HIST-OLD", created=now - timedelta(days=9))
    new = _journey(tenant, ref="HIST-NEW", created=now - timedelta(days=3))
    _file(old)
    _file(new)
    assert _detect(new)["raised"] == 0 and _detect(old)["raised"] == 0
    assert _alerts(new) == [] and _queued(new) == []
    monkeypatch.setattr(uc03_p2_same_file, "SAME_FILE_WINDOW_HOURS", 24 * 4)
    assert _detect(old)["raised"] == 1


# 2. a company booking with the signatory's personal PAN
def test_looks_like_company():
    from audit_core.uc03_p2_same_file import looks_like_company

    for name in ("M/S Shree Shyam Enterprises", "Sarthak Motors Pvt. Ltd.", "Patra & Co.", "Maa Tarini Traders",
                 "Odisha Infra Projects LLP", "Jena Agencies", "Mahakali Construction"):
        assert looks_like_company(name), name
    for name in ("Amit Kumar Sahoo", "Sunil Mallick", "Coral Das"):
        assert not looks_like_company(name), name


def test_a_company_pan_on_any_document_keeps_the_new_file(tenant):
    old, new = _pair(tenant)
    other = uuid4()
    add_evidence(old, di_document_id=other, document_type_key="booking_form")
    add_extracted_field(old, di_document_id=other, field_key="pan_number", value="AAACS1234K", confidence=40.0)
    assert _detect(new)["outcomes"] == ["KEPT_NOT_INDIVIDUAL"]
    [alert] = _alerts(new)
    assert "a company or firm PAN is on file" in alert["body"] and _queued(new) == []


def test_a_gstin_keeps_the_new_file(tenant):
    _, new = _pair(tenant)
    doc = uuid4()
    add_evidence(new, di_document_id=doc, document_type_key="gst_certificate")
    add_extracted_field(new, di_document_id=doc, field_key="gstin", value="21ABCPE1234F1ZV")
    assert _detect(new)["outcomes"] == ["KEPT_NOT_INDIVIDUAL"]
    assert "a GSTIN is on file" in _alerts(new)[0]["body"]


def test_a_firm_name_keeps_the_new_file(tenant):
    now = datetime.now(UTC)
    old = _journey(tenant, ref="FIRM-OLD", created=now - timedelta(days=1))
    new = _journey(tenant, ref="FIRM-NEW", created=now)
    _file(old, name="M/S Shree Shyam Enterprises")
    _file(new, name="M/S Shree Shyam Enterprises")
    assert _detect(new)["outcomes"] == ["KEPT_NOT_INDIVIDUAL"]
    assert "company's or firm's" in _alerts(new)[0]["body"]


# 3. re-checked at delete time
def test_the_original_closed_since_keeps_the_new_file(tenant):
    old, new = _ready_to_delete(tenant)
    _stage(old, "BOOKING_CANCELLED")
    assert _delete(new) == "KEPT_CHANGED"
    assert _exists(new)
    [alert] = _alerts(new)
    assert alert["alert_status"] == "PUBLISHED" and alert["outcome"] == "KEPT_CHANGED"
    assert "is closed" in alert["body"]
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        assert connection.execute(text("SELECT count(*) FROM auditcore.p2_journey_deletions WHERE tenant_id=:t"),
                                  {"t": new.tenant_id}).scalar_one() == 0


def test_the_original_gone_since_keeps_the_new_file(tenant):
    old, new = _ready_to_delete(tenant)
    run_duplicate_journey_purge(tenant.engine, f"{tenant.tenant_id}:x-commit", [_ref(old)],
                                security_client=FakeSecurity(), v2_client=FakeDi(), storage=FakeStorage())
    assert _delete(new) == "KEPT_CHANGED" and _exists(new)
    assert "no longer exists" in _alerts(new)[0]["body"]


def test_a_value_changed_since_keeps_the_new_file(tenant):
    _, new = _ready_to_delete(tenant)
    with new.engine.begin() as connection:  # the KYC page carrying PAN and mobile is removed
        set_tenant_context(connection, new.tenant_id)
        connection.execute(text("UPDATE auditcore.evidence SET association_status='VOIDED' "
                                "WHERE tenant_id=:t AND journey_id=:j"), {"t": new.tenant_id, "j": new.journey_id})
    assert _delete(new) == "KEPT_CHANGED" and _exists(new)
    assert "no longer match" in _alerts(new)[0]["body"]


def test_a_gstin_uploaded_since_keeps_the_new_file(tenant):
    _, new = _ready_to_delete(tenant)
    doc = uuid4()
    add_evidence(new, di_document_id=doc, document_type_key="gst_certificate")
    add_extracted_field(new, di_document_id=doc, field_key="gstin", value="21ABCPE1234F1ZV")
    assert _delete(new) == "KEPT_CHANGED" and _exists(new)


# 4. values of removed documents
def test_a_pan_from_a_removed_document_pairs_nothing(tenant):
    old, new = _pair(tenant)
    with old.engine.begin() as connection:
        set_tenant_context(connection, old.tenant_id)
        connection.execute(text("UPDATE auditcore.evidence SET association_status='SUPERSEDED' "
                                "WHERE tenant_id=:t AND journey_id=:j"), {"t": old.tenant_id, "j": old.journey_id})
    assert _detect(new).get("raised", 0) == 0 and _alerts(new) == []


# 5. mid-upload
def test_a_journey_still_receiving_pages_is_not_deleted_yet(tenant, monkeypatch):
    from audit_core import uc03_p2_worker as worker
    from audit_core.uc03_p2_same_file import UploadInProgress

    _, new = _ready_to_delete(tenant)
    add_page(new)  # uploaded just now
    with pytest.raises(UploadInProgress) as waiting:
        _delete(new)
    assert 0 < waiting.value.delay_seconds <= 10 * 60 + 30
    assert _exists(new) and _alerts(new)[0]["alert_status"] == "PENDING"
    monkeypatch.setattr(worker, "get_security_oauth_client", FakeSecurity)
    monkeypatch.setattr(worker, "get_di_capture_v2_client", FakeDi)
    monkeypatch.setattr(worker, "get_p2_document_storage", FakeStorage)
    [item] = [w for w in worker._claim_for_tenant(new.engine, new.tenant_id, 10) if w.work_type == DELETE_WORK_TYPE]
    with pytest.raises(worker.RescheduleWork):
        worker.process_work(new.engine, item)
    _quiet(new)
    assert _delete(new) == "DELETED"


def test_files_written_during_the_delete_are_swept_afterwards(tenant):
    _, new = _ready_to_delete(tenant)
    late = f"p2-documents/{new.tenant_id}/{new.journey_id}/late-page.pdf"

    class LateStorage(FakeStorage):
        swept = False

        def list_keys(self, prefix: str) -> list[str]:
            # The late page appears only after the first sweep.
            return [late] if self.swept and prefix.startswith("p2-documents/") else []

        def delete_objects(self, keys: list[str]) -> int:
            self.swept = True
            return super().delete_objects(keys)

    storage = LateStorage()
    run_same_file_delete(new.engine, tenant_id=new.tenant_id, journey_id=new.journey_id, correlation_id="t",
                         security_client=FakeSecurity(), v2_client=FakeDi(), storage=storage)
    assert late in storage.deleted


# 6. an unfinished system delete is finished
def test_an_unfinished_system_delete_is_continued_past_the_reviewed_guard(tenant):
    from audit_core.uc03_p2_journey_delete import system_delete_journey

    _, new = _pair(tenant)
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_deletions (tenant_id, journey_id, reason, deleted_by_actor_id, "
                 "deleted_by_role, deletion_status) VALUES (:t, :j, 'Same file uploaded again: earlier try', "
                 "'SYSTEM', 'SYSTEM', 'FAILED')"),
            {"t": new.tenant_id, "j": new.journey_id},
        )
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                 "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                 "VALUES (:t, :j, 'BOOKING', 'rev', 'REVIEWER', 1, :d, :o)"),
            {"t": new.tenant_id, "j": new.journey_id, "d": new.dealer_id, "o": new.outlet_id},
        )
    system_delete_journey(new.engine, tenant_id=new.tenant_id, journey_id=new.journey_id,
                          reason="Same file uploaded again: finishing", correlation_id="t",
                          security_client=FakeSecurity(), v2_client=FakeDi(), storage=FakeStorage())
    assert not _exists(new)


def test_a_first_system_delete_still_refuses_a_reviewed_journey(tenant):
    from audit_core.errors import ConflictError
    from audit_core.uc03_p2_journey_delete import system_delete_journey

    _, new = _pair(tenant)
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                 "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                 "VALUES (:t, :j, 'BOOKING', 'rev', 'REVIEWER', 1, :d, :o)"),
            {"t": new.tenant_id, "j": new.journey_id, "d": new.dealer_id, "o": new.outlet_id},
        )
    with pytest.raises(ConflictError):
        system_delete_journey(new.engine, tenant_id=new.tenant_id, journey_id=new.journey_id,
                              reason="Same file uploaded again: first try", correlation_id="t",
                              security_client=FakeSecurity(), v2_client=FakeDi(), storage=FakeStorage())
    assert _exists(new)


# 7. removed another way; purge dry run reports closed journeys
def test_a_new_file_removed_another_way_publishes_its_alert(tenant):
    _, new = _ready_to_delete(tenant)
    run_duplicate_journey_purge(tenant.engine, f"{tenant.tenant_id}:x-commit", [_ref(new)],
                                security_client=FakeSecurity(), v2_client=FakeDi(), storage=FakeStorage())
    [alert] = _alerts(new)
    assert alert["alert_status"] == "PUBLISHED" and alert["outcome"] == "REMOVED"
    assert "has been removed" in alert["body"]


def test_purge_dry_run_reports_closed_journeys(tenant):
    closed = _journey(tenant, ref="DUP-CLOSED-1", created=datetime.now(UTC))
    _stage(closed, "BOOKING_CANCELLED")
    dry = run_duplicate_journey_purge(tenant.engine, f"{tenant.tenant_id}:item8", ["DUP-CLOSED-1"])
    assert dry["would_refuse"] == 1 and dry["would_delete"] == 0


def _ref(journey: P2Journey) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t "
                                       "AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()
