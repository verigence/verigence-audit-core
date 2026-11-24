"""The same file uploaded again (product owner, 2026-10-31; rule of 2026-11-04).

PAN and/or Aadhaar, mobile, model, customer name, booking reference number and
outlet all those of another journey: the journey with fewer documents is
deleted (when the PAN is an individual's) and the Project alerted; no booking
reference read on a copy: alert only. The same customer at another outlet of
the same dealership pauses both PCs' uploads; at another dealership, a red
alert on every screen.
Real Postgres; Document Intelligence, Security and R2 are doubles.
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
    CROSS_OUTLET_KIND,
    CROSS_OUTLET_TITLE,
    DELETE_WORK_TYPE,
    KEPT_NO_BOOKING_REFERENCE,
    SAME_DEALERSHIP_KIND,
    SAME_DEALERSHIP_TITLE,
    FileIdentity,
    alert_body,
    booking_identified,
    cross_outlet_fields,
    cross_outlet_probability,
    is_individual_pan,
    kept_of,
    matched_fields,
    part_of_sale_fields,
    run_same_file_delete,
    same_name,
    sync_same_file_detection,
)
from audit_core.uc03_p2_tenant_alerts import list_tenant_alerts

INDIVIDUAL_PAN = "ABCPE1234F"
COMPANY_PAN = "ABCCE1234F"
AADHAAR = "2345 6789 1234"
BOOKING_REF = "BK/0012"


@pytest.fixture(autouse=True)
def _di_moves_read_documents(monkeypatch):
    """Carrying a read document moves it at DI (2026-11-24, no double reading): DI faked."""
    from audit_core import uc03_document_capture_v2, uc03_duplicate_merge

    class _Di:
        def move_document(self, **_kwargs):
            return {"outcome": "moved"}

    monkeypatch.setattr(uc03_document_capture_v2, "get_di_capture_v2_client", _Di)
    monkeypatch.setattr(uc03_duplicate_merge, "_di_context_refs",
                        lambda _e, _t, ids: ("token", [f"ctx-{i}" for i in ids]))


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
          name: str | None = "Amit Kumar Sahoo", model: str | None = "THAR", sku: UUID | None = None,
          booking_ref: str | None = BOOKING_REF, aadhaar: str | None = None) -> None:
    doc = uuid4()
    if pan or mobile:
        add_evidence(journey, di_document_id=doc, document_type_key="pan_card")
    if pan:
        add_extracted_field(journey, di_document_id=doc, field_key="pan_number", value=pan, document_type="pan_card")
    if mobile:
        add_extracted_field(journey, di_document_id=doc, field_key="customer_phone", value=mobile)
    if aadhaar:
        kyc = uuid4()
        add_evidence(journey, di_document_id=kyc, document_type_key="aadhaar")
        add_extracted_field(journey, di_document_id=kyc, field_key="aadhaar_number", value=aadhaar,
                            document_type="aadhaar")
    # The booking form, with its booking reference number, on every copy
    # (both copies hold one, so neither holds a document the other lacks).
    form = uuid4()
    add_evidence(journey, di_document_id=form, document_type_key="booking_form")
    if booking_ref:
        add_extracted_field(journey, di_document_id=form, field_key="booking_reference_number", value=booking_ref)
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
    # 2026-11-14: the journey with more pages is kept (the newer on a tie). The earlier one
    # holds more here, so the new file is the copy, as these tests were written.
    for number in (1, 2, 3):
        add_page(old, page_number=number, status="READY")
    _no_stored_pages(old)
    return old, new


def _no_stored_pages(journey: P2Journey) -> None:
    """Pages counted but never carried (judged another file's), so a deleted copy has nothing to copy."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET identity_status='FOREIGN' "
                                "WHERE tenant_id=:t AND journey_id=:j"),
                           {"t": journey.tenant_id, "j": journey.journey_id})


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


def _page(journey: P2Journey, *, sha: str, kind: str = "pan_card", identity: str | None = "BELONGS",
          status: str = "READY") -> tuple[UUID, UUID, UUID]:
    """A page with this fingerprint, read as ``kind``, its file judged ``identity``."""
    batch, queue, doc = add_page(journey, status=status)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_document_queue SET page_sha256=:s, unit_kind='PAGE' "
                                "WHERE tenant_id=:t AND queue_id=:q"), {"s": sha, "t": journey.tenant_id, "q": queue})
        connection.execute(text("UPDATE auditcore.p2_upload_batches SET identity_status=:i "
                                "WHERE tenant_id=:t AND batch_id=:b"), {"i": identity, "t": journey.tenant_id, "b": batch})
    add_evidence(journey, di_document_id=doc, document_type_key=kind)
    return batch, queue, doc


def _same_pages(old: P2Journey, new: P2Journey, n: int = 1) -> list[UUID]:
    """The same file uploaded again: pages with the same fingerprints on both journeys."""
    docs = []
    for _ in range(n):
        sha = uuid4().hex
        _page(old, sha=sha)
        docs.append(_page(new, sha=sha)[2])
    return docs


# ── matching, without a database ──────────────────────────────────────────────
def _identity(**overrides) -> FileIdentity:
    values = {"journey_id": uuid4(), "created_at": datetime.now(UTC), "reference": "R", "pc_name": "PC",
              "dealer_name": "D", "outlet_name": "O", "pan": INDIVIDUAL_PAN, "mobile": "9876543210",
              "name": "Amit Kumar Sahoo", "model_id": None, "model_name": "THAR", "variant_name": None,
              "closed": False, "booking_refs": frozenset({"BK-12"}), "booking_ref_display": "BK/0012"}
    values.update(overrides)
    return FileIdentity(**values)


def test_all_must_match():
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
    # Two master models of one nameplate ("SCORPIO N" and "NEW SCORPIO N", 2026-11-14) are one model;
    # two nameplates are not.
    assert matched_fields(_identity(model_id=uuid4(), model_name="NEW SCORPIO N"),
                          _identity(model_id=model, model_name="SCORPIO N")) is not None
    assert matched_fields(_identity(model_id=uuid4(), model_name="THAR ROXX"),
                          _identity(model_id=model, model_name="THAR")) is None


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
    assert "The copy ADI-RD-1 was deleted." in body
    assert "ADI-RD-0 (PC Mona, Adishakti/Patia)" in body
    assert "kept for review" in alert_body(payload, "KEPT_NOT_INDIVIDUAL")


# ── detection and delete, on the database ─────────────────────────────────────
def test_same_file_with_an_individual_pan_is_deleted_and_alerted(tenant):
    old, new = _pair(tenant)
    [di_document] = _same_pages(old, new)  # the same file, page for page
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
    # 2026-11-14, one rule: the identity values that matched, the model and the outlet.
    assert set(fields) == {"pan", "mobile", "model", "outlet"}
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
    assert record["reason"].startswith("Duplicate booking of OLD-")
    # What the Deleted tab shows (2026-11-02): PAN and mobile in full, the booking number, the copy kept.
    assert record["deletion_kind"] == "SAME_FILE"
    assert record["customer_pan"] == INDIVIDUAL_PAN and record["customer_mobile"] == "9876543210"
    assert record["booking_reference"] == BOOKING_REF and record["model_name"] == "THAR"
    assert record["kept_journey_id"] == old.journey_id and record["kept_journey_reference"].startswith("OLD-")
    assert record["customer_aadhaar_masked"] is None  # no Aadhaar read


@pytest.mark.parametrize("differs", [
    # Two people (a PAN read on both and different), another model, or no model to compare --
    # each with another OTF: the same OTF and mobile are one sale whatever else differs (2026-11-23).
    {"pan": "ABCPE9999F", "booking_ref": "BK/0099"}, {"model": "SCORPIO N", "booking_ref": "BK/0099"},
    {"model": None, "booking_ref": "BK/0099"},
])
def test_one_value_different_is_not_the_same_file(tenant, differs):
    _, new = _pair(tenant, **differs)
    assert _detect(new).get("raised", 0) == 0
    assert _alerts(new) == [] and _queued(new) == []


@pytest.mark.parametrize("differs", [
    # 2026-11-14, one rule: the name and the mobile do not decide it -- the same PAN, model and
    # OTF are enough.
    {"mobile": "9000000001"}, {"name": "Sunil Kumar Mallick"}, {"booking_ref": "BOOKING NO. BK/0012"},
])
def test_the_same_pan_and_model_is_one_booking_whatever_else_differs(tenant, differs):
    _, new = _pair(tenant, **differs)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    _quiet(new)
    assert _delete(new) == "DELETED" and not _exists(new)


@pytest.mark.parametrize("differs", [{"booking_ref": "BK/0013"}, {"booking_ref": None}])
def test_a_duplicate_without_the_same_otf_is_flagged_never_deleted(tenant, differs):
    """Product owner 2026-11-14: "Duplicate booking you keep finding with the rules you defined..
    but delete should happen only for journey which has same OTF" -- one customer can have
    several bookings, and then only the OTF differs."""
    _, new = _pair(tenant, **differs)
    assert _detect(new)["outcomes"] == [KEPT_NO_BOOKING_REFERENCE]
    [alert] = _alerts(new)
    assert alert["outcome"] == KEPT_NO_BOOKING_REFERENCE and "Team Lead decides" in alert["body"]
    assert _queued(new) == [] and _exists(new)


def test_the_same_mobile_and_model_alone_is_one_booking(tenant):
    """No PAN or Aadhaar on the copy: the same mobile and model are enough (2026-11-14)."""
    _, new = _pair(tenant, pan=None)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]


def test_the_same_gstin_and_model_is_one_booking(tenant):
    now = datetime.now(UTC)
    old = _journey(tenant, ref="OLD-GST", created=now - timedelta(days=1))
    new = _journey(tenant, ref="NEW-GST", created=now)
    for journey in (old, new):
        _file(journey, pan=None, mobile=None, name="Sarthak Motors Pvt Ltd")
        doc = uuid4()
        add_evidence(journey, di_document_id=doc, document_type_key="gst_certificate")
        add_extracted_field(journey, di_document_id=doc, field_key="gstin", value="21AAICS1234F1Z5")
    add_page(old, status="READY")
    add_page(old, page_number=2, status="READY")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    fields = {f["field"] for f in _alerts(new)[0]["payload"]["matchedFields"]}
    assert "gstin" in fields


def test_company_pan_is_deleted_too(tenant):
    """2026-11-14: a company's booking is no exception any more."""
    now = datetime.now(UTC)
    old = _journey(tenant, ref="OLD-CO", created=now - timedelta(days=1))
    new = _journey(tenant, ref="NEW-CO", created=now)
    _file(old, pan=COMPANY_PAN, name="Sarthak Motors Pvt Ltd")
    _file(new, pan=COMPANY_PAN, name="Sarthak Motors Private Limited")
    add_page(old, status="READY")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]


def test_the_newer_file_is_kept_on_a_tie_and_the_previous_booking_deleted(tenant):
    """"new file has pages equal to or more than first file: delete the previous booking and add
    new journey" (2026-11-14)."""
    now = datetime.now(UTC)
    old = _journey(tenant, ref="TIE-OLD", created=now - timedelta(days=3))
    new = _journey(tenant, ref="TIE-NEW", created=now - timedelta(minutes=5))
    _file(old)
    _file(new)
    for journey in (old, new):
        _page(journey, sha="f" * 64)  # the same page on both: nothing to copy
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    [alert] = _alerts(new)
    assert alert["subject_journey_id"] == old.journey_id and alert["related_journey_id"] == new.journey_id
    _quiet(old)
    assert _delete(old) == "DELETED"
    assert not _exists(old) and _exists(new)


def test_another_outlet_of_the_same_dealership_is_deleted_too(tenant):
    old, new = _cross_pair(tenant)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    _quiet(new)
    assert _delete(new) == "DELETED" and _exists(old)


def test_different_vins_are_two_vehicles(tenant):
    old, new = _pair(tenant)
    for journey, vin in ((old, "MA1TJ2YD6T6J00001"), (new, "MA1TJ2YD6T6J00002")):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(text("INSERT INTO auditcore.journey_classifications (tenant_id, journey_id, vin_used, "
                                    "customer_type) VALUES (:t, :j, :v, 'INDIVIDUAL')"),
                               {"t": journey.tenant_id, "j": journey.journey_id, "v": vin})
    assert _detect(new).get("raised", 0) == 0


def test_reviewed_new_file_is_deleted(tenant):
    _, new = _pair(tenant)
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_stage_document_reviews (tenant_id, journey_id, stage_code, "
                 "reviewed_by_actor_id, reviewed_by_role_code, document_count, dealer_id, outlet_id) "
                 "VALUES (:t, :j, 'BOOKING', 'rev', 'REVIEWER', 1, :d, :o)"),
            {"t": new.tenant_id, "j": new.journey_id, "d": new.dealer_id, "o": new.outlet_id},
        )
    # 2026-11-14: a reviewer's review does not keep a duplicate booking.
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    _quiet(new)
    assert _delete(new) == "DELETED" and not _exists(new)


def test_reviewed_after_queueing_is_still_deleted(tenant):
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
    _quiet(new)
    assert _delete(new) == "DELETED"
    assert not _exists(new)


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


def test_either_journeys_run_flags_the_same_copy_and_ties_break_on_journey_id(tenant):
    at = datetime.now(UTC) - timedelta(hours=1)
    first = _journey(tenant, ref="TIE-A", created=at)
    second = _journey(tenant, ref="TIE-B", created=at)
    _file(first)
    _file(second)
    larger, smaller = sorted((first, second), key=lambda j: str(j.journey_id), reverse=True)
    # The same pages and the same time: the larger journey_id counts as the newer and is kept.
    assert _detect(larger)["raised"] == 1
    assert [a["subject_journey_id"] for a in _alerts(larger)] == [smaller.journey_id]
    assert _detect(smaller)["raised"] == 0
    assert len(_alerts(larger)) == 1


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
def test_an_older_pair_is_acted_on_too(tenant):
    """Product owner 2026-11-12: "i never put a rule of 48 hours"."""
    now = datetime.now(UTC)
    old = _journey(tenant, ref="HIST-OLD", created=now - timedelta(days=9))
    new = _journey(tenant, ref="HIST-NEW", created=now - timedelta(days=3))
    _file(old)
    _file(new)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    [work] = _queued(new)
    assert work["journey_id"] == old.journey_id  # the same pages: the newer is kept (2026-11-14)


# 2. a company booking with the signatory's personal PAN
def test_looks_like_company():
    from audit_core.uc03_p2_same_file import looks_like_company

    for name in ("M/S Shree Shyam Enterprises", "Sarthak Motors Pvt. Ltd.", "Patra & Co.", "Maa Tarini Traders",
                 "Odisha Infra Projects LLP", "Jena Agencies", "Mahakali Construction"):
        assert looks_like_company(name), name
    for name in ("Amit Kumar Sahoo", "Sunil Mallick", "Coral Das"):
        assert not looks_like_company(name), name


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
    _same_pages(_, new)  # the same pages again, uploaded just now
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


# ── a sole proprietor's GSTIN (2026-11-01) ────────────────────────────────────
def test_a_sole_proprietors_gstin_on_their_own_pan_is_not_a_company(tenant):
    """Prahallad Behera (ADI-RD-261031-003, 2026-11-01): his GSTIN 21BYXPB9740G1Z8
    is built on his own PAN; the copy was kept as if a company's."""
    old, new = _pair(tenant)
    for journey in (old, new):  # the same file: the certificate on both copies
        doc = uuid4()
        add_evidence(journey, di_document_id=doc, document_type_key="gst_certificate")
        add_extracted_field(journey, di_document_id=doc, field_key="gstin", value=f"21{INDIVIDUAL_PAN}1ZV")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]


def test_a_page_the_earlier_one_lacks_not_settled_as_the_customers_is_carried(tenant):
    """2026-11-14, one rule: a page of the copy not judged yet is carried to the kept booking (it
    was uploaded to this customer's booking); the copy is deleted."""
    old, new = _pair(tenant)
    _same_pages(old, new)
    _page(old, sha=uuid4().hex, kind="booking_form")                # the earlier journey is the fuller
    _page(old, sha=uuid4().hex, kind="payment_receipt")
    _page(new, sha=uuid4().hex, kind="tax_invoice_tally", identity="UNKNOWN")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert _alerts(new)[0]["payload"]["carryDocuments"] == ["tax_invoice_tally"]


def test_documents_only_on_reads_kinds(tenant):
    old, new = _pair(tenant)
    add_evidence(new, di_document_id=uuid4(), document_type_key="tax_invoice_tally")
    # A payment receipt on one and a dealer receipt on the other are one kind.
    from audit_core.uc03_p2_same_file import documents_only_on

    add_evidence(old, di_document_id=uuid4(), document_type_key="dealer_receipt")
    add_evidence(new, di_document_id=uuid4(), document_type_key="payment_receipt")
    with tenant.engine.begin() as connection:
        set_tenant_context(connection, tenant.tenant_id)
        assert documents_only_on(connection, tenant_id=tenant.tenant_id, new_journey_id=new.journey_id,
                                 existing_journey_id=old.journey_id) == ["tax_invoice_tally"]


# ── the booking reference number and the Aadhaar (product owner 2026-11-02) ──
def test_booking_reference_normalisation():
    from audit_core.uc03_p2_same_file import normalize_booking_reference as norm

    assert norm("bk/0012") == norm("BK-12") == norm(" BK 12 ") == norm("BK12") == "BK-12"
    assert norm("0000123") == norm("123") == "123"
    assert norm("BK/12/3") != norm("BK/1/23")
    for nothing in (None, "", "NA", "-", "0000", "N/A", "BK", "12"):
        assert norm(nothing) is None, nothing


def test_the_booking_reference_decides():
    base = _identity()
    # Read on both and different: two bookings.
    assert matched_fields(_identity(booking_refs=frozenset({"BK-13"})), base) is None
    # Not read on one: still a match, the booking row not exact (kept with an alert).
    fields = matched_fields(_identity(booking_refs=frozenset(), booking_ref_display=None), base)
    assert fields is not None and fields[-1] == {
        "field": "bookingReference", "label": "Booking reference number", "newValue": None,
        "existingValue": "BK/0012", "exact": False}
    # Each dealership numbers its own bookings.
    assert matched_fields(_identity(dealer_id=uuid4()), _identity(dealer_id=uuid4())) is None


def test_pan_or_aadhaar_is_enough_but_a_different_one_is_another_person():
    base = _identity(pan=None, aadhaar="234567891234")
    fields = matched_fields(_identity(pan=None, aadhaar="234567891234"), base)
    assert fields is not None and fields[0]["field"] == "aadhaar" and fields[0]["newValue"] == "XXXX XXXX 1234"
    # PAN on one copy only, the Aadhaar on both: the Aadhaar decides.
    assert matched_fields(_identity(aadhaar="234567891234"), base) is not None
    # The same PAN but different Aadhaar numbers: one customer since 2026-11-14 (the other Aadhaar is a
    # co-applicant's or a misread); different PANs and different Aadhaars: two people.
    assert matched_fields(_identity(aadhaar="234567899999"), _identity(aadhaar="234567891234")) is not None
    assert matched_fields(_identity(pan="ABCPE9999F", aadhaar="234567899999"),
                          _identity(aadhaar="234567891234")) is None
    # Neither identity: nothing.
    assert matched_fields(_identity(pan=None), _identity(pan=None)) is None


def test_mask_aadhaar_keeps_the_last_four_digits_only():
    from audit_core.uc03_p2_same_file import mask_aadhaar

    assert mask_aadhaar("2345 6789 1234") == "XXXX XXXX 1234"
    assert mask_aadhaar(None) is None


def test_same_booking_reference_differently_written_is_deleted(tenant):
    old, new = _pair(tenant, booking_ref="bk-12")  # the old copy reads "BK/0012"
    _quiet(new)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert _delete(new) == "DELETED"
    assert not _exists(new) and _exists(old)


def test_the_journey_with_more_pages_is_kept_even_when_it_is_the_older(tenant):
    """2026-11-14: the journey with more pages is kept; here the newer holds more."""
    old, new = _pair(tenant)
    _same_pages(old, new)
    for _ in range(4):
        _page(new, sha=uuid4().hex, kind="tax_invoice_tally")  # the newer upload holds more pages
    _quiet(old)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    [alert] = _alerts(new)
    assert alert["subject_journey_id"] == old.journey_id and alert["related_journey_id"] == new.journey_id
    assert [w["journey_id"] for w in _queued(new)] == [old.journey_id]
    assert _delete(old) == "DELETED"
    assert not _exists(old) and _exists(new)


def test_a_copy_that_gains_documents_before_the_delete_is_kept(tenant):
    _, new = _ready_to_delete(tenant)
    for _ in range(5):
        _page(new, sha=uuid4().hex, kind="tax_invoice_tally")  # pages still arriving
    _quiet(new)
    assert _delete(new) == "KEPT_CHANGED" and _exists(new)
    assert "now holds more pages" in _alerts(new)[0]["body"]


def test_another_dealership_is_never_the_same_file(tenant):
    now = datetime.now(UTC)
    old = _journey(tenant, ref="DLR-OLD", created=now - timedelta(days=1))
    with tenant.engine.begin() as connection:  # another dealership of the same Project
        set_tenant_context(connection, tenant.tenant_id)
        dealer = connection.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D2') RETURNING dealer_id"), {"t": tenant.tenant_id, "c": uuid4().hex[:8]},
        ).scalar_one()
        outlet = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O2') RETURNING outlet_id"),
            {"t": tenant.tenant_id, "d": dealer, "c": uuid4().hex[:8]},
        ).scalar_one()
    elsewhere = P2Journey(engine=tenant.engine, tenant_id=tenant.tenant_id, journey_id=tenant.journey_id,
                          actor_id=tenant.actor_id, dealer_id=UUID(str(dealer)), outlet_id=UUID(str(outlet)),
                          customer_id=tenant.customer_id)
    new = _journey(elsewhere, ref="DLR-NEW", created=now)
    _file(old)
    _file(new)
    # Never the same file (nothing deleted); since 2026-11-03 a duplicate booking across outlets.
    assert _detect(new).get("raised", 0) == 0 and _queued(new) == []
    assert [a["alert_kind"] for a in _alerts(new)] == [CROSS_OUTLET_KIND]


def test_an_aadhaar_only_match_is_deleted_and_recorded_masked(tenant):
    now = datetime.now(UTC)
    old = _journey(tenant, ref="AAD-OLD", created=now - timedelta(days=1))
    new = _journey(tenant, ref="AAD-NEW", created=now - timedelta(minutes=5))
    _file(old, pan=None, aadhaar=AADHAAR)
    _file(new, pan=None, aadhaar="234567891234")
    add_page(old, status="READY")  # the earlier one holds more pages: the new file is the copy
    _no_stored_pages(old)
    _quiet(new)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    payload = _alerts(new)[0]["payload"]
    assert payload["matchedFields"][0] == {"field": "aadhaar", "label": "Aadhaar", "newValue": "XXXX XXXX 1234",
                                           "existingValue": "XXXX XXXX 1234", "exact": True}
    assert _delete(new) == "DELETED"
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        record = connection.execute(
            text("SELECT * FROM auditcore.p2_journey_deletions WHERE tenant_id=:t AND journey_id=:j"),
            {"t": new.tenant_id, "j": new.journey_id},
        ).mappings().one()
        stored = " ".join(str(v) for v in record.values())
    assert record["customer_aadhaar_masked"] == "XXXX XXXX 1234"
    assert "234567891234" not in stored and "2345 6789 1234" not in stored  # never the full number
    assert record["customer_pan"] is None and record["customer_mobile"] == "9876543210"
    assert record["booking_reference"] == BOOKING_REF and record["kept_journey_reference"] == "AAD-OLD"


def test_the_deleted_tab_lists_deleted_copies_within_the_callers_scope(tenant):
    from audit_core.uc03_duplicate_bookings_report import get_deleted_duplicate_bookings

    now = datetime.now(UTC)
    old = _journey(tenant, ref=f"OLD-{uuid4().hex[:6]}", created=now - timedelta(days=1), pc="Old PC")
    new = _journey(tenant, ref=f"NEW-{uuid4().hex[:6]}", created=now - timedelta(minutes=5), pc="New PC")
    _file(old, aadhaar=AADHAAR)
    _file(new, aadhaar=AADHAAR)
    add_page(old, status="READY")
    _no_stored_pages(old)
    _quiet(new)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert _delete(new) == "DELETED"
    with tenant.engine.begin() as connection:
        set_tenant_context(connection, tenant.tenant_id)
        other_outlet = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'Other outlet') RETURNING outlet_id"),
            {"t": tenant.tenant_id, "d": tenant.dealer_id, "c": uuid4().hex[:8]},
        ).scalar_one()
        for actor, dealer, outlet in (("dup-tl", None, None), ("dup-pc-own", tenant.dealer_id, tenant.outlet_id),
                                      ("dup-pc-other", tenant.dealer_id, other_outlet)):
            connection.execute(
                text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                     "dealer_id, outlet_id) VALUES (:t, :a, :r, :d, :o)"),
                {"t": tenant.tenant_id, "a": actor, "r": "TL" if dealer is None else "PC", "d": dealer, "o": outlet},
            )
        # A Team Lead's own delete is not listed.
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_deletions (tenant_id, journey_id, dealer_id, outlet_id, reason, "
                 "deleted_by_actor_id, deleted_by_role, deletion_status) VALUES (:t, :j, :d, :o, "
                 "'A team lead removed it', 'tl', 'TL', 'COMPLETED')"),
            {"t": tenant.tenant_id, "j": uuid4(), "d": tenant.dealer_id, "o": tenant.outlet_id},
        )

        def listed(actor: str):
            return get_deleted_duplicate_bookings(
                tenant_id=tenant.tenant_id, human_principal=SimpleNamespace(subject=actor),
                authorization_client=AllowAllAuthorization(role_key="TL"), connection=connection).items

        [item] = listed("dup-tl")
        assert item.journeyId == new.journey_id and item.keptJourneyId == old.journey_id
        assert item.journeyReference.startswith("NEW-") and item.keptJourneyReference.startswith("OLD-")
        assert item.pan == INDIVIDUAL_PAN and item.mobile == "9876543210"
        assert item.aadhaarMasked == "XXXX XXXX 1234" and item.bookingReference == BOOKING_REF
        assert item.pcName == "New PC" and item.model == "THAR" and item.customerName == "Amit Kumar Sahoo"
        assert item.outletName and item.status == "COMPLETED"
        assert [i.journeyId for i in listed("dup-pc-own")] == [new.journey_id]
        assert listed("dup-pc-other") == [] and listed("nobody") == []


# ── 2026-11-03: a duplicate booking across outlets ────────────────────────────
def _other_outlet(base: P2Journey, *, other_dealer: bool = False) -> P2Journey:
    """A journey of ``base``'s tenant at another outlet (of the same dealership, or of another)."""
    with base.engine.begin() as connection:
        set_tenant_context(connection, base.tenant_id)
        dealer_id = base.dealer_id
        if other_dealer:
            dealer_id = connection.execute(
                text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                     "VALUES (:t, :c, 'Other Dealer') RETURNING dealer_id"),
                {"t": base.tenant_id, "c": f"OD-{uuid4().hex[:6]}"}).scalar_one()
        outlet_id = connection.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'Other Outlet') RETURNING outlet_id"),
            {"t": base.tenant_id, "d": dealer_id, "c": f"OO-{uuid4().hex[:6]}"}).scalar_one()
    return P2Journey(engine=base.engine, tenant_id=base.tenant_id, journey_id=base.journey_id, actor_id=base.actor_id,
                     dealer_id=UUID(str(dealer_id)), outlet_id=UUID(str(outlet_id)), customer_id=base.customer_id)


def _cross_pair(tenant: P2Journey, *, other_dealer: bool = False, **new_overrides):
    now = datetime.now(UTC)
    old = _journey(tenant, ref=f"OLD-{uuid4().hex[:6]}", created=now - timedelta(days=2), pc="Old PC")
    elsewhere = _other_outlet(tenant, other_dealer=other_dealer)
    new = _journey(elsewhere, ref=f"NEW-{uuid4().hex[:6]}", created=now - timedelta(minutes=5), pc="New PC")
    _file(old)
    _file(new, **new_overrides)
    # 2026-11-14: the journey with more pages is kept (the newer on a tie). The earlier one
    # holds more here, so the new file is the copy, as these tests were written.
    for number in (1, 2, 3):
        add_page(old, page_number=number, status="READY")
    _no_stored_pages(old)
    return old, new


def test_cross_outlet_rule_without_a_database():
    a, b = uuid4(), uuid4()
    base = _identity(outlet_id=a)
    # Another outlet: the customer, mobile and model are enough -- no booking number, no name.
    assert cross_outlet_fields(_identity(outlet_id=b, booking_refs=frozenset({"BK-99"}), name=None), base)
    assert cross_outlet_fields(_identity(outlet_id=b, pan=None, aadhaar="234567891234"), _identity(
        outlet_id=a, pan=None, aadhaar="234567891234"))
    # 2026-11-17: the mobile may differ (90%), the name may not; the mobile too is 100%.
    assert cross_outlet_fields(_identity(outlet_id=b, mobile="9876500000"), base)
    assert cross_outlet_probability(_identity(outlet_id=b, mobile="9876500000"), base) == 90
    assert cross_outlet_probability(_identity(outlet_id=b), base) == 100
    assert cross_outlet_fields(_identity(outlet_id=b, name="Pankaj Nayak"), base) is None
    assert cross_outlet_fields(_identity(outlet_id=b, model_name="SCORPIO N"), base) is None
    assert cross_outlet_fields(_identity(outlet_id=b, pan="ABCPE9999F"), base) is None
    # The same outlet is never "across outlets"; and another outlet is never "the same file".
    assert cross_outlet_fields(_identity(outlet_id=a), base) is None
    assert matched_fields(_identity(outlet_id=b), base) is None
    assert matched_fields(_identity(outlet_id=a), base) is not None


# Another outlet of the same dealership is deleted since 2026-11-14
# (test_another_outlet_of_the_same_dealership_is_deleted_too); another dealership is alerted.
@pytest.mark.parametrize("other_dealer", [True])
def test_a_duplicate_booking_across_outlets_is_alerted_and_nothing_deleted(tenant, other_dealer):
    old, new = _cross_pair(tenant, other_dealer=other_dealer, booking_ref="OTHER/77", name="A K Sahoo")
    result = _detect(new)
    assert result["acrossOutlets"] == 1 and result["raised"] == 0
    kind = CROSS_OUTLET_KIND if other_dealer else SAME_DEALERSHIP_KIND
    [alert] = [a for a in _alerts(new) if a["alert_kind"] == kind]
    assert alert["alert_status"] == "PUBLISHED" and alert["subject_journey_id"] == new.journey_id
    assert alert["related_journey_id"] == old.journey_id
    payload = alert["payload"]
    assert payload["severity"] == "CRITICAL" and "Nothing was deleted." in alert["body"]
    if other_dealer:
        # Product owner 2026-11-04: a big red alert on every screen of the Project.
        assert alert["title"] == CROSS_OUTLET_TITLE
        assert "audienceOutletIds" not in payload and "uploadsBlocked" not in payload
    else:
        # Another outlet of the same dealership: both PCs' uploads paused until a Reviewer deletes one.
        assert alert["title"] == SAME_DEALERSHIP_TITLE and payload["uploadsBlocked"] is True
        assert sorted(payload["audienceOutletIds"]) == sorted([str(old.outlet_id), str(new.outlet_id)])
        assert "paused until a Reviewer deletes the duplicate booking" in alert["body"]
    assert _queued(new) == [] and _exists(new) and _exists(old)
    # Once per journey: the older journey's own run does not raise it again.
    assert _detect(old)["acrossOutlets"] == 0
    assert len([a for a in _alerts(new) if a["alert_kind"] == kind]) == 1


def test_the_same_outlet_is_still_the_same_file(tenant):
    _old, new = _pair(tenant)
    result = _detect(new)
    assert result["acrossOutlets"] == 0 and result["raised"] == 1
    assert [a["alert_kind"] for a in _alerts(new)] == [ALERT_KIND]


def _assign(journey: P2Journey, actor: str, role: str, *, outlet_id: UUID | None) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, "
                 "dealer_id, outlet_id) VALUES (:t, :a, :r, :d, :o)"),
            {"t": journey.tenant_id, "a": actor, "r": role, "d": None if outlet_id is None else journey.dealer_id,
             "o": outlet_id})


def _kinds_seen_by(tenant: P2Journey, actor: str) -> list[str]:
    with tenant.engine.begin() as connection:
        set_tenant_context(connection, tenant.tenant_id)
        response = list_tenant_alerts(tenant_id=tenant.tenant_id, human_principal=SimpleNamespace(subject=actor),
                                      authorization_client=AllowAllAuthorization(role_key="PC"),
                                      connection=connection)
        return [a.kind for a in response.items]


def test_the_cross_dealership_alert_is_on_every_screen(tenant):
    _old, new = _cross_pair(tenant, other_dealer=True)
    _detect(new)
    elsewhere = _other_outlet(tenant)
    _assign(elsewhere, "pc-elsewhere", "PC", outlet_id=elsewhere.outlet_id)
    _assign(tenant, "pm-any", "PM", outlet_id=tenant.outlet_id)
    for actor in ("pc-elsewhere", "pm-any", "nobody-assigned"):
        assert _kinds_seen_by(tenant, actor) == [CROSS_OUTLET_KIND], actor


# ── 2026-11-03: carry what only the copy holds, by page fingerprint ──────────
class CopyStorage:
    """The stored files: what _copy_unit reads and writes."""
    def __init__(self) -> None:
        self.put: dict[str, bytes] = {}

    def get_object(self, key: str) -> bytes:
        return b"%PDF-1.4 page " + key.encode()

    def put_object(self, key: str, payload: bytes, *, content_type: str) -> None:
        self.put[key] = payload


def _batches(journey: P2Journey) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT batch_id, client_upload_id, original_filename, uploaded_by_actor_id "
                 "FROM auditcore.p2_upload_batches "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()]


def test_the_same_set_of_documents_is_deleted_without_copying(tenant, monkeypatch):
    from audit_core import uc03_p2_storage

    old, new = _pair(tenant)
    _same_pages(old, new, n=3)
    _quiet(new)
    storage = CopyStorage()
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert "carryDocuments" not in _alerts(new)[0]["payload"]
    before = len(_batches(old))
    assert _delete(new) == "DELETED"
    assert storage.put == {} and len(_batches(old)) == before


class MovingDi:
    """DI's move of a read document to another Journey's context (2026-11-24)."""
    def __init__(self, *, holds: bool = True) -> None:
        self.holds = holds
        self.moved: list[tuple[str, str, str]] = []

    def move_document(self, *, token, tenant_id, external_context_ref, document_id, target_external_context_ref):
        from audit_core.di_capture_v2_client import DiCaptureV2Error

        if not self.holds:
            raise DiCaptureV2Error(status_code=404, detail="DOCUMENT_NOT_FOUND")
        self.moved.append((external_context_ref, document_id, target_external_context_ref))
        return {"outcome": "moved"}


@pytest.mark.parametrize(("kind", "held_at_di"), [("tax_invoice_tally", True), ("dealer_receipt", True),
                                                  ("tax_invoice_tally", False)])
def test_a_page_only_the_copy_holds_goes_to_the_earlier_journey_before_the_delete(tenant, monkeypatch, kind,
                                                                                  held_at_di):
    """A Tally invoice the earlier journey lacks -- or a second receipt, a kind it already
    holds, which comparing kinds would have lost -- is carried to it, then the copy goes.
    Product owner 2026-11-24, "no double reading": it moves with its reading, and is read
    on the earlier journey only when DI no longer holds that reading."""
    from audit_core import (
        uc03_document_capture_v2,
        uc03_duplicate_merge,
        uc03_p2_storage,
    )

    di = MovingDi(holds=held_at_di)
    monkeypatch.setattr(uc03_document_capture_v2, "get_di_capture_v2_client", lambda: di)
    monkeypatch.setattr(uc03_duplicate_merge, "_di_context_refs",
                        lambda _e, _t, ids: ("token", [f"ctx-{i}" for i in ids]))

    old, new = _pair(tenant)
    _same_pages(old, new, n=2)
    # The earlier journey's own receipt: as many documents on each, so the earlier one is kept.
    _page(old, sha=uuid4().hex, kind="dealer_receipt")
    extra_batch, extra_queue, _doc = _page(new, sha=uuid4().hex, kind=kind)
    # The earlier booking was made by another PC of the outlet (2026-11-24).
    with old.engine.begin() as connection:
        set_tenant_context(connection, old.tenant_id)
        connection.execute(text("UPDATE auditcore.journeys SET created_by_actor_id='another-pc' "
                                "WHERE tenant_id=:t AND journey_id=:j"), {"t": old.tenant_id, "j": old.journey_id})
        uploader = connection.execute(text("SELECT uploaded_by_actor_id FROM auditcore.p2_upload_batches "
                                           "WHERE tenant_id=:t AND batch_id=:b"),
                                      {"t": new.tenant_id, "b": extra_batch}).scalar_one()
    _quiet(new)
    storage = CopyStorage()
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)

    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert _alerts(new)[0]["payload"]["carryDocuments"] == [kind]
    assert _delete(new) == "DELETED"
    assert not _exists(new) and _exists(old)
    [copied] = [b for b in _batches(old) if b["client_upload_id"] == f"dup-merge-{extra_queue}"]
    assert copied["original_filename"].startswith(f"{kind} from ")
    # Under the person who put the page in, not the earlier booking's creator (product owner
    # 2026-11-24: a Drive file joined to another PC's booking was missing from its PC's day).
    assert uploader and copied["uploaded_by_actor_id"] == uploader != "another-pc"
    assert len(storage.put) == 1
    [alert] = _alerts(new)
    assert alert["outcome"] == "DELETED" and f"after its {kind.replace('_', ' ')} was added to" in alert["body"]
    with old.engine.begin() as connection:
        set_tenant_context(connection, old.tenant_id)
        split = connection.execute(
            text("SELECT count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='SPLIT_BATCH' AND work_key=:k"),
            {"t": old.tenant_id, "j": old.journey_id, "k": str(copied["batch_id"])}).scalar_one()
        units = connection.execute(
            text("SELECT queue_status, di_document_id, unit_kind FROM auditcore.p2_document_queue "
                 "WHERE tenant_id=:t AND batch_id=:b"), {"t": old.tenant_id, "b": copied["batch_id"]},
        ).mappings().all()
        capture = connection.execute(
            text("SELECT count(*) FROM auditcore.document_capture_v2_documents WHERE tenant_id=:t "
                 "AND journey_id=:j AND di_document_id=:d"), {"t": old.tenant_id, "j": old.journey_id, "d": _doc},
        ).scalar_one()
        reconcile = connection.execute(
            text("SELECT count(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='JOURNEY_RECONCILE'"), {"t": old.tenant_id, "j": old.journey_id}).scalar_one()
    if held_at_di:
        # Moved at DI to the earlier journey's context and copied from there: no split, no reading.
        assert di.moved == [(f"ctx-{new.journey_id}", str(_doc), f"ctx-{old.journey_id}")]
        assert split == 0 and reconcile >= 1 and capture == 1
        assert [(u["queue_status"], u["di_document_id"]) for u in units] == [("CLASSIFYING", _doc)]
    else:
        # DI no longer holds the reading: the upload is split and read there like any upload.
        assert di.moved == [] and split == 1 and units == []


def test_a_copy_holding_another_customers_file_is_deleted_without_carrying_it(tenant):
    """2026-11-14: another customer's file is not copied to this customer's booking, and no
    longer keeps the copy."""
    old, new = _pair(tenant)
    _same_pages(old, new)
    _page(old, sha=uuid4().hex, kind="booking_form")                # the earlier journey is the fuller
    _page(new, sha=uuid4().hex, kind="pan_card", identity="FOREIGN")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert "carryDocuments" not in _alerts(new)[0]["payload"]


# ── one sale uploaded twice, known by its dealer receipt (2026-11-12) ─────────
def _receipt(journey: P2Journey, number: str) -> None:
    doc = uuid4()
    add_evidence(journey, di_document_id=doc, document_type_key="dealer_receipt")
    add_extracted_field(journey, di_document_id=doc, field_key="receipt_number", value=number,
                        document_type="dealer_receipt")


def _invoice(journey: P2Journey, number: str) -> None:
    doc = uuid4()
    add_evidence(journey, di_document_id=doc, document_type_key="customer_invoice_dms")
    add_extracted_field(journey, di_document_id=doc, field_key="invoice_number", value=number,
                        document_type="customer_invoice_dms")


def test_a_booking_file_then_its_delivery_file_become_one_journey(tenant):
    """Product owner 2026-11-12: "if the same details are found but with additional
    documents then we will update the existing booking"."""
    now = datetime.now(UTC)
    booking = _journey(tenant, ref="BOOKING-FILE", created=now - timedelta(days=1))
    delivery = _journey(tenant, ref="DELIVERY-FILE", created=now - timedelta(minutes=5))
    _file(booking, mobile="+91 93386 96711", booking_ref="OR26110204479797", model="Maxx HD 1.7L LX")
    _file(delivery, mobile="+91 93386 76711", booking_ref="OR26110204479497", model="BOL MAXX PUP HD 1.7L LX")
    _receipt(booking, "BRMM/2627/04861")
    _receipt(delivery, "BRMM/2627/04861")
    _invoice(delivery, "INV27P000427")
    _quiet(delivery)

    assert _detect(delivery)["raised"] == 1
    [alert] = _alerts(delivery)
    # The delivery file holds more documents: it is kept, the booking file is the copy.
    assert alert["subject_journey_id"] == booking.journey_id and alert["related_journey_id"] == delivery.journey_id
    # 2026-11-17: the OTF read differs by a digit, but both carry dealer receipt BRMM/2627/04861:
    # one payment is one booking, a misread OTF no longer holds it ("its reading problem of
    # system", "system has to improve"): the booking file's pages go to the delivery file's
    # journey and the copy is deleted -- not reported.
    assert {f["field"] for f in alert["payload"]["matchedFields"]} >= {"pan", "model"}
    assert alert["outcome"] == "PENDING_DELETE"
    assert len(_queued(booking)) == 1 and _exists(delivery)


def test_the_settle_job_finds_old_pairs_and_its_dry_run_changes_nothing(tenant):
    from audit_core.uc03_same_sale_settle import run_same_sale_settle

    now = datetime.now(UTC)
    booking = _journey(tenant, ref="OLD-BOOKING", created=now - timedelta(days=10))
    delivery = _journey(tenant, ref="OLD-DELIVERY", created=now - timedelta(days=8))
    _file(booking, mobile="+91 93386 96711", booking_ref="OR797")
    _file(delivery, mobile="+91 93386 76711", booking_ref="OR497")
    _receipt(booking, "BRMM/2627/04861")
    _receipt(delivery, "BRMM/2627/04861")
    _invoice(delivery, "INV27P000427")

    dry = run_same_sale_settle(tenant.engine, f"{tenant.tenant_id}:settle", [])
    # Another OTF read on each, one dealer receipt (2026-11-17): one booking -- to be deleted.
    assert dry["commit"] is False and dry["toDelete"] == 1 and dry["kept"] == 0
    assert _alerts(delivery) == [] and _alerts(booking) == []

    done = run_same_sale_settle(tenant.engine, f"{tenant.tenant_id}:settle-commit", [])
    assert done["toDelete"] == 1 and len(_queued(booking)) == 1


def test_the_settle_job_deletes_a_pair_with_the_same_otf(tenant):
    from audit_core.uc03_same_sale_settle import run_same_sale_settle

    now = datetime.now(UTC)
    booking = _journey(tenant, ref="OTF-BOOKING", created=now - timedelta(days=10))
    delivery = _journey(tenant, ref="OTF-DELIVERY", created=now - timedelta(days=8))
    _file(booking, mobile="+91 93386 96711", booking_ref="OTF NO-19669")
    _file(delivery, mobile="+91 93386 96711", booking_ref="019669")
    _receipt(booking, "BRMM/2627/04861")
    _receipt(delivery, "BRMM/2627/04861")
    _invoice(delivery, "INV27P000427")
    done = run_same_sale_settle(tenant.engine, f"{tenant.tenant_id}:settle-commit", [])
    assert done["toDelete"] == 1 and len(_queued(booking)) == 1


def test_the_settle_job_checks_only_the_journeys_named(tenant):
    """2026-11-23: given references, the job looks those journeys up directly (the Project-wide
    candidate search timed out) -- and still settles their pairs."""
    from audit_core.uc03_same_sale_settle import run_same_sale_settle

    now = datetime.now(UTC)
    booking = _journey(tenant, ref="NAMED-BOOKING", created=now - timedelta(days=10))
    delivery = _journey(tenant, ref="NAMED-DELIVERY", created=now - timedelta(days=8))
    _file(booking, mobile="+91 93386 96711", booking_ref="OTF NO-19669")
    _file(delivery, mobile="+91 93386 96711", booking_ref="019669")
    other = _journey(tenant, ref="NOT-NAMED", created=now - timedelta(days=9))
    _file(other, mobile="+91 93386 96711", booking_ref="019669")
    dry = run_same_sale_settle(tenant.engine, f"{tenant.tenant_id}:named", ["NAMED-BOOKING", "NAMED-DELIVERY"])
    assert dry["journeys"] == 2 and dry["commit"] is False


# ── exactly the same documents are deleted whatever else is said (2026-11-12) ──
def test_exactly_the_same_documents_of_a_company_are_deleted(tenant):
    now = datetime.now(UTC)
    old = _journey(tenant, ref="CO-OLD", created=now - timedelta(days=5))
    new = _journey(tenant, ref="CO-NEW", created=now - timedelta(minutes=30))
    _file(old, pan=COMPANY_PAN, name="Sarthak Motors Pvt Ltd")
    _file(new, pan=COMPANY_PAN, name="Sarthak Motors Pvt Ltd")
    _same_pages(old, new, n=2)
    _quiet(old)
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    # The same pages: the newer is kept, the previous booking deleted (2026-11-14).
    assert _delete(old) == "DELETED" and not _exists(old) and _exists(new)


def test_exactly_the_same_documents_without_a_booking_number_are_kept_for_the_team_lead(tenant):
    """2026-11-14: no OTF read on the copy -- flagged, not deleted ("delete should happen only
    for journey which has same OTF")."""
    old, new = _pair(tenant, booking_ref=None)
    _same_pages(old, new)
    _quiet(new)
    assert _detect(new)["outcomes"] == [KEPT_NO_BOOKING_REFERENCE]
    assert _queued(new) == [] and _exists(new)


def test_a_deleted_copy_counts_under_the_pc_who_uploaded_it(tenant):
    """Product owner 2026-11-12: a count of duplicate uploads per PC, in the trackers."""
    from audit_core.uc03_p2_same_file import duplicate_uploads_deleted

    old, new = _pair(tenant)
    _same_pages(old, new)
    _quiet(new)
    _detect(new)
    assert _delete(new) == "DELETED"
    now = datetime.now(UTC)
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        rows = duplicate_uploads_deleted(connection, tenant_id=new.tenant_id, start=now - timedelta(days=1),
                                         end=now + timedelta(minutes=1))
        none_before = duplicate_uploads_deleted(connection, tenant_id=new.tenant_id,
                                                start=now - timedelta(days=30), end=now - timedelta(hours=1))
    assert [r["journey_id"] for r in rows] == [new.journey_id] and str(rows[0]["outlet_id"]) == str(new.outlet_id)
    assert none_before == []


def test_a_rephotographed_document_the_earlier_journey_holds_is_not_carried(tenant, monkeypatch):
    """Product owner 2026-11-12, "add additional documents": the PAN photographed again for
    the delivery file differs by fingerprint but is not an additional document."""
    from audit_core import uc03_p2_storage

    old, new = _pair(tenant)
    _same_pages(old, new)
    _page(old, sha=uuid4().hex, kind="pan_card")
    _page(new, sha=uuid4().hex, kind="pan_card")  # the PAN again, a new photograph
    _quiet(new)
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: CopyStorage())
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert "carryDocuments" not in _alerts(new)[0]["payload"]
    assert _delete(new) == "DELETED" and _batches_copied(old) == []


@pytest.mark.parametrize(("number", "carried"), [("BRMM/2627/04861", False), ("BRMM/2627/09999", True)])
def test_a_receipt_is_carried_only_when_its_number_is_new(tenant, number, carried):
    old, new = _pair(tenant)
    _same_pages(old, new)
    _receipt(old, "BRMM/2627/04861")
    _page(old, sha=uuid4().hex, kind="dealer_receipt")
    _, _, doc = _page(new, sha=uuid4().hex, kind="dealer_receipt")
    add_extracted_field(new, di_document_id=doc, field_key="receipt_number", value=number,
                        document_type="dealer_receipt")
    _quiet(new)
    _detect(new)
    payload = _alerts(new)[0]["payload"]
    assert ("dealer_receipt" in payload.get("carryDocuments", [])) is carried


def _batches_copied(journey: P2Journey) -> list[dict]:
    return [b for b in _batches(journey) if str(b["client_upload_id"] or "").startswith("dup-merge-")]


@pytest.mark.parametrize(("left", "right", "same"), [
    # Product owner 2026-11-14: the same booking, the model written two ways.
    ("MAHINDRA BOLERO NEO", "BOLERO NEO", True), ("XUV 3XO", "MAHINDRA XUV 3XO", True),
    ("BOL MAXX PUP HD 1.7 VXI", "MAXX HD 1.7 VXI", True), ("SCORPIO N", "SCORPIO-N", True),
    ("BOLERO", "MAHINDRA BOLERO B6 BSVI", True), ("THAR OGY", "THAR", True),
    # Different models stay apart.
    ("THAR ROXX", "THAR", False), ("BOLERO NEO", "BOLERO", False), ("XUV3XO EV", "XUV 3XO", False),
    ("BOLERO CAMPER GOLD ZX D", "MAHINDRA BOLERO NEO", False), ("XEV 9S", "XUV 7S", False),
])
def test_one_model_written_two_ways_is_one_family(tenant, left, right, same):
    from audit_core import uc03_p2_same_file as rule

    with tenant.engine.begin() as connection:
        for name in ("BOLERO", "BOLERO NEO", "MAXX HD", "NEW SCORPIO N", "NEW THAR 2WD & 4WD", "THAR ROXX",
                     "XUV3XO", "XUV3XO EV", "XEV9S"):
            if not connection.execute(text("SELECT 1 FROM auditcore.product_models WHERE model_name=:n"),
                                      {"n": name}).first():
                oem = connection.execute(text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') "
                                              "RETURNING oem_id"), {"c": f"FAM-{uuid4().hex[:8]}"}).scalar_one()
                connection.execute(text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                                        "VALUES (:o, :c, :n)"), {"o": oem, "c": f"FAM-{uuid4().hex[:8]}", "n": name})
        rule._families = (0.0, ())
        rule._load_families(connection)
    assert (rule.model_family(left) == rule.model_family(right)) is same


def _kyc(journey: P2Journey, *, pan: str | None = None, aadhaar: str | None = None) -> None:
    """Another KYC document on the file: a co-applicant's Aadhaar, a firm's PAN."""
    doc = uuid4()
    add_evidence(journey, di_document_id=doc, document_type_key="aadhaar" if aadhaar else "pan_card")
    if pan:
        add_extracted_field(journey, di_document_id=doc, field_key="pan_number", value=pan, document_type="pan_card")
    if aadhaar:
        add_extracted_field(journey, di_document_id=doc, field_key="aadhaar_number", value=aadhaar,
                            document_type="aadhaar")


def test_a_co_applicants_aadhaar_beside_the_same_pan_is_still_one_customer(tenant):
    """2026-11-14 (UTK-JP-261104-001 / UTK-JI-261026-007): the same PAN, and a different Aadhaar on
    the file -- a co-applicant's or a misread -- is not two people."""
    _, new = _pair(tenant, aadhaar="2345 6789 1234")
    _kyc(new, aadhaar="9876 5432 1098")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]


def test_a_firms_pan_beside_the_proprietors_is_still_one_customer(tenant):
    """2026-11-14 (ADI-RD-261022-021 / ADI-RD-261102-016): a firm's PAN read on one file next to the
    proprietor's, which the other file holds."""
    _, new = _pair(tenant)
    _kyc(new, pan="AAFCJ2048H")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]


def test_no_pan_in_common_is_still_two_people(tenant):
    # Another OTF too: the same OTF and mobile are one sale whatever the PANs (2026-11-23).
    _, new = _pair(tenant, pan="ABCPE9999F", booking_ref="BK/0099")
    _kyc(new, pan="ABCPE8888F")
    assert _detect(new).get("raised", 0) == 0


def test_the_same_chassis_is_one_vehicle_whatever_the_model_text(tenant):
    old, new = _pair(tenant, model="BOLERO NEO")
    for journey in (old, new):
        with journey.engine.begin() as connection:
            set_tenant_context(connection, journey.tenant_id)
            connection.execute(text("INSERT INTO auditcore.journey_classifications (tenant_id, journey_id, vin_used, "
                                    "customer_type) VALUES (:t, :j, 'MA1XL2TVXT5J27592', 'INDIVIDUAL')"),
                               {"t": journey.tenant_id, "j": journey.journey_id})
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert "vin" in {f["field"] for f in _alerts(new)[0]["payload"]["matchedFields"]}


def test_one_receipt_or_one_vin_is_one_booking_whatever_the_otf_was_read_as():
    """Product owner 2026-11-17 (ASHOK BEHERA): the same booking form read INV-3693 on one
    copy and 3714 on the other, both with dealer receipt AMCB336272627 -- one booking."""
    from audit_core.uc03_p2_same_file import booking_identified

    outlet, dealer = uuid4(), uuid4()
    old = _identity(booking_refs=frozenset({"INV3693"}), receipt_numbers=frozenset({"AMCB336272627"}),
                    outlet_id=outlet, dealer_id=dealer)
    new = _identity(booking_refs=frozenset({"3714"}), receipt_numbers=frozenset({"AMCB336272627"}),
                    outlet_id=outlet, dealer_id=dealer)
    assert booking_identified(new, old)
    # Another receipt and no VIN in common: the customer may have two bookings -- the Team Lead.
    other = _identity(booking_refs=frozenset({"3714"}), receipt_numbers=frozenset({"AMCB999"}),
                      outlet_id=outlet, dealer_id=dealer)
    assert not booking_identified(other, old)
    # The same receipt at another outlet of the dealership is the same booking too.
    elsewhere = _identity(booking_refs=frozenset({"B-13509319"}), receipt_numbers=frozenset({"AMCB336272627"}),
                          outlet_id=uuid4(), dealer_id=dealer)
    assert booking_identified(elsewhere, old)
    assert not booking_identified(_identity(booking_refs=frozenset({"9"}), receipt_numbers=frozenset({"AMCB336272627"}),
                                            outlet_id=uuid4(), dealer_id=uuid4()), old)  # another dealership
    # The same chassis is the same sale.
    assert booking_identified(_identity(booking_refs=frozenset({"1"}), vin="MA1RA2TTKT6J34768"),
                              _identity(booking_refs=frozenset({"2"}), vin="MA1RA2TTKT6J34768"))


def test_the_same_booking_merged_is_not_told():
    """Product owner 2026-11-17: the same booking's new pages are added, not reported."""
    from audit_core.uc03_p2_tenant_alerts import is_silent_merge

    assert is_silent_merge("SAME_FILE_UPLOADED", {"outcome": "DELETED"})
    assert is_silent_merge("SAME_FILE_UPLOADED", {"outcome": "PENDING_DELETE"})
    assert not is_silent_merge("SAME_FILE_UPLOADED", {"outcome": "KEPT_NO_BOOKING_REFERENCE"})
    assert not is_silent_merge("DUPLICATE_ACROSS_OUTLETS", {"outcome": "DELETED"})



# ── a file with no customer on it joins its booking (product owner 2026-11-20) ──
_RECEIPT = "AMC-C/5141/26-27"
_RECEIPT_KEY = "AMCC51412627|10000.00|SEKH ASAKAR ALLI"


def _nameless(**overrides) -> FileIdentity:
    """A receipt or delivery papers uploaded alone: no PAN, Aadhaar, mobile or GSTIN."""
    values = {"pan": None, "mobile": None, "name": None, "model_name": None, "booking_refs": frozenset(),
              "booking_ref_display": None, **overrides}
    return _identity(**values)


def test_a_nameless_file_with_the_same_receipt_is_part_of_the_booking():
    dealer = uuid4()
    booking = _identity(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY}))
    copy = _nameless(dealer_id=dealer, receipt_keys=frozenset({"AMCC51412627|10000.00|SEKH ASAKAR ALLl"}))
    assert part_of_sale_fields(copy, booking) is not None
    assert booking_identified(copy, booking)
    # the booking with the customer is kept, however many pages the copy holds
    assert kept_of([copy, booking], {copy.journey_id: 11, booking.journey_id: 2}) is booking


def test_a_nameless_file_with_the_same_chassis_is_part_of_the_booking():
    dealer = uuid4()
    booking = _identity(dealer_id=dealer, chassis=frozenset({"T5K46116"}))
    copy = _nameless(dealer_id=dealer, chassis=frozenset({"T5K46116"}), model_name="MAXX HD")
    assert part_of_sale_fields(copy, booking) is not None


@pytest.mark.parametrize("copy_overrides", [
    {"receipt_keys": frozenset({"AMCC51412627|10000.00|ANOTHER PAYER"})},          # another payer
    {"receipt_keys": frozenset({"AMCC51412627|62500.00|SEKH ASAKAR ALLI"})},       # another amount
    {"receipt_keys": frozenset({_RECEIPT_KEY}), "chassis": frozenset({"T6J29723"})},  # two vehicles
    {"receipt_keys": frozenset({_RECEIPT_KEY}), "model_name": "SCORPIO"},          # another model
])
def test_a_nameless_file_is_not_joined_without_the_same_receipt_or_vehicle(copy_overrides):
    dealer = uuid4()
    booking = _identity(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY}), chassis=frozenset({"T6J21387"}))
    assert part_of_sale_fields(_nameless(dealer_id=dealer, **copy_overrides), booking) is None


def test_two_named_files_or_two_dealerships_are_left_to_the_other_rules():
    dealer = uuid4()
    booking = _identity(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY}))
    assert part_of_sale_fields(_identity(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY})), booking) is None
    assert part_of_sale_fields(_nameless(dealer_id=uuid4(), receipt_keys=frozenset({_RECEIPT_KEY})), booking) is None
    assert part_of_sale_fields(_nameless(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY})),
                               _nameless(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY}))) is None


def _receipt_on(journey: P2Journey, *, number=_RECEIPT, amount="10000", payer="SEKH ASAKAR ALLI-AZYPA8525F") -> None:
    doc = uuid4()
    add_evidence(journey, di_document_id=doc, document_type_key="dealer_receipt")
    for field_key, value in (("receipt_number", number), ("amount_paid", amount), ("customer_name", payer)):
        add_extracted_field(journey, di_document_id=doc, field_key=field_key, value=value,
                            document_type="dealer_receipt")


def _chassis_on(journey: P2Journey, chassis: str) -> None:
    doc = uuid4()
    add_evidence(journey, di_document_id=doc, document_type_key="customer_invoice_dms")
    add_extracted_field(journey, di_document_id=doc, field_key="chassis_number", value=chassis,
                        document_type="customer_invoice_dms")


def _named_and_nameless(tenant: P2Journey) -> tuple[P2Journey, P2Journey]:
    now = datetime.now(UTC)
    booking = _journey(tenant, ref=f"BOOK-{uuid4().hex[:6]}", created=now - timedelta(days=5))
    copy = _journey(tenant, ref=f"ID-{uuid4().hex[:6]}", created=now - timedelta(minutes=5))
    _file(booking, model="MAXX HD")
    for number in (1, 2, 3, 4, 5):
        add_page(copy, page_number=number, status="READY")  # the copy holds more pages
    _no_stored_pages(copy)
    return booking, copy


def test_the_receipt_uploaded_alone_is_deleted_and_the_booking_kept(tenant):
    """Live: ADI-RD-261118-009, Jagannath Maity's receipt alone, beside his booking ADI-RD-261113-019."""
    booking, copy = _named_and_nameless(tenant)
    _receipt_on(booking)
    _receipt_on(copy, payer="Sekh Asakar Alli")
    result = _detect(copy)
    assert result.get("raised") == 1, result
    (alert,) = [a for a in _alerts(tenant) if a["alert_kind"] == ALERT_KIND]
    assert alert["subject_journey_id"] == copy.journey_id and alert["related_journey_id"] == booking.journey_id
    assert alert["outcome"] == "PENDING_DELETE"
    assert [q["journey_id"] for q in _queued(tenant)] == [copy.journey_id]


def test_the_delivery_papers_uploaded_alone_join_by_the_chassis_from_the_booking_side(tenant):
    """Live: UTK-PP-261106-001 (no customer, chassis T5K46116) and Deepak Mallik's UTK-PP-261112-001."""
    booking, copy = _named_and_nameless(tenant)
    _chassis_on(booking, "MA1RV2TUKT5K46116")
    _chassis_on(copy, "T5K46116")
    result = _detect(booking)  # found from the booking's side too
    assert result.get("raised") == 1, result
    assert [q["journey_id"] for q in _queued(tenant)] == [copy.journey_id]


def test_a_nameless_file_of_another_vehicle_is_left_alone(tenant):
    booking, copy = _named_and_nameless(tenant)
    _receipt_on(booking)
    _receipt_on(copy)
    _chassis_on(booking, "MA1RV2TUKT6J21387")
    _chassis_on(copy, "MA1RV2TUKT6J29723")
    assert _detect(copy).get("raised", 0) == 0
    assert _queued(tenant) == []


def test_a_file_whose_booking_form_is_not_read_is_not_joined_on_a_receipt():
    """2026-11-21: ADI-BO-261120-026 held customer B's booking form (not read yet) beside
    customer A's receipt. Called nameless, it was joined into A's booking on the receipt alone
    and its booking form replaced A's. A file with a customer's papers on it, read or not, is
    not nameless; a receipt or delivery papers on their own still join."""
    dealer = uuid4()
    booking = _identity(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY}))
    for kind in ("booking_form", "booking_docket", "pan_card", "aadhaar", "gst_certificate"):
        unread = _nameless(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY}),
                           identity_documents=frozenset({kind}))
        assert unread.unread_customer
        assert part_of_sale_fields(unread, booking) is None
    papers = _nameless(dealer_id=dealer, receipt_keys=frozenset({_RECEIPT_KEY}),
                       identity_documents=frozenset())
    assert part_of_sale_fields(papers, booking) is not None



# ── 2026-11-23: the same OTF and the same mobile, with the same model or booking date ─────────
def _booked(journey: P2Journey, day: str) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) VALUES (:t, :j, :d) "
                                "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET booking_date = EXCLUDED.booking_date"),
                           {"t": journey.tenant_id, "j": journey.journey_id, "d": day})


def test_the_same_otf_mobile_and_model_are_one_sale_whatever_the_pan(tenant):
    """Product owner 2026-11-23: "different PAN is a wrong document upload"."""
    _, new = _pair(tenant, pan="ABCPE9999F")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    _quiet(new)
    assert _delete(new) == "DELETED" and not _exists(new)


@pytest.mark.parametrize("differs", [{"model": "MAXX"}, {"model": None}])
def test_the_same_otf_mobile_and_booking_date_are_one_sale_whatever_the_model(tenant, differs):
    """A booking form reading only the nameplate, or a delivery file with no model read: the same
    booking date stands in for the model ("same model or same booking date")."""
    old, new = _pair(tenant, pan="ABCPE9999F", **differs)
    _booked(old, "2026-11-14")
    _booked(new, "2026-11-14")
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]


def test_the_same_otf_and_mobile_without_the_same_model_or_date_are_kept_apart(tenant):
    old, new = _pair(tenant, pan="ABCPE9999F", model="MAXX")
    _booked(old, "2026-11-04")
    _booked(new, "2026-11-14")
    assert _detect(new).get("raised", 0) == 0 and _exists(new)


@pytest.mark.parametrize("differs", [{"mobile": "9000000001", "pan": "ABCPE9999F"},
                                     {"booking_ref": "BK/0099", "pan": "ABCPE9999F"}])
def test_the_same_otf_or_mobile_alone_does_not_join_two_peoples_files(tenant, differs):
    _, new = _pair(tenant, **differs)
    assert _detect(new).get("raised", 0) == 0 and _exists(new)


def test_same_otf_and_mobile_but_two_vehicles_is_not_one_sale():
    from audit_core.uc03_p2_same_file import same_otf_mobile_fields

    dealer = uuid4()
    left = _identity(pan="ABCPE9999F", vin="MA1AAAAAAAAA11111", dealer_id=dealer)
    right = _identity(vin="MA1BBBBBBBBB22222", dealer_id=dealer)
    assert same_otf_mobile_fields(left, right) is None
    assert same_otf_mobile_fields(_identity(dealer_id=dealer), _identity(dealer_id=uuid4())) is None
    fields = same_otf_mobile_fields(_identity(pan="ABCPE9999F", dealer_id=dealer), _identity(dealer_id=dealer))
    assert [f["field"] for f in fields][:3] == ["booking_reference", "mobile", "pan"]
    assert next(f for f in fields if f["field"] == "pan")["exact"] is False


def test_when_the_pans_differ_the_copys_pan_is_carried_not_lost(tenant, monkeypatch):
    """2026-11-23: one of the two PANs is a wrong upload. The kept journey holds a PAN already,
    but the copy's is carried too -- deleting the copy must not lose the right one; the
    customer-identity check then asks the PC which is wrong."""
    from audit_core import uc03_p2_storage

    old, new = _pair(tenant, pan="ABCPE9999F")
    _same_pages(old, new)
    _page(old, sha=uuid4().hex, kind="pan_card")
    _page(new, sha=uuid4().hex, kind="pan_card")  # another person's PAN
    _quiet(new)
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: CopyStorage())
    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert _alerts(new)[0]["payload"]["carryDocuments"] == ["pan_card"]
    assert _delete(new) == "DELETED" and len(_batches_copied(old)) == 1



def test_an_enquiry_number_is_no_booking_reference():
    """2026-11-23: "enquiry number cant be booking number" -- two journeys sharing only an
    enquiry number read as the booking number are not one booking by it."""
    from audit_core.uc03_p2_same_file import normalize_booking_reference

    assert normalize_booking_reference("ENQ-38100019") is None
    assert normalize_booking_reference("0038100003") is None
    assert normalize_booking_reference("B-13100014") is not None


def test_a_multi_page_document_moves_whole_and_keeps_its_page_fingerprints(tenant, monkeypatch):
    """A document put together from pages (a GROUP unit) is moved with its reading as one
    document, and its pages' fingerprints go with it: a later file repeating those pages is
    known to be on the earlier booking already (product owner 2026-11-24)."""
    from audit_core import (
        uc03_document_capture_v2,
        uc03_duplicate_merge,
        uc03_p2_storage,
    )

    di = MovingDi()
    monkeypatch.setattr(uc03_document_capture_v2, "get_di_capture_v2_client", lambda: di)
    monkeypatch.setattr(uc03_duplicate_merge, "_di_context_refs",
                        lambda _e, _t, ids: ("token", [f"ctx-{i}" for i in ids]))
    storage = CopyStorage()
    monkeypatch.setattr(uc03_p2_storage, "get_p2_document_storage", lambda: storage)
    old, new = _pair(tenant)
    _same_pages(old, new, n=2)
    _page(old, sha=uuid4().hex, kind="dealer_receipt")
    shas = [uuid4().hex, uuid4().hex]
    first = _page(new, sha=shas[0], kind="tax_invoice_tally")
    second = _page(new, sha=shas[1], kind="tax_invoice_tally")
    group_doc = uuid4()
    with new.engine.begin() as connection:
        set_tenant_context(connection, new.tenant_id)
        group = connection.execute(text(
            "INSERT INTO auditcore.p2_document_queue (tenant_id, batch_id, journey_id, page_number, page_numbers, "
            "page_sha256, page_object_key, client_upload_id, queue_status, unit_kind, group_source, di_document_id, "
            "classified_document_type) VALUES (:t, :b, :j, 1, ARRAY[1,2], :sha, 'grp.pdf', :c, 'READY', 'GROUP', "
            "'SYSTEM', :d, 'tax_invoice_tally') RETURNING queue_id"),
            {"t": new.tenant_id, "b": first[0], "j": new.journey_id, "sha": uuid4().hex, "c": f"grp-{uuid4()}",
             "d": group_doc}).scalar_one()
        connection.execute(text(
            "UPDATE auditcore.p2_document_queue SET queue_status='MERGED', merged_into_queue_id=:g "
            "WHERE tenant_id=:t AND queue_id = ANY(:q)"), {"t": new.tenant_id, "g": group, "q": [first[1], second[1]]})
        connection.execute(text("DELETE FROM auditcore.evidence WHERE tenant_id=:t AND di_document_id=:d"),
                           {"t": new.tenant_id, "d": second[2]})
        connection.execute(text("UPDATE auditcore.evidence SET di_document_id=:d WHERE tenant_id=:t "
                                "AND di_document_id=:old"), {"t": new.tenant_id, "d": group_doc, "old": first[2]})
    _quiet(new)

    assert _detect(new)["outcomes"] == ["PENDING_DELETE"]
    assert _delete(new) == "DELETED"
    assert di.moved == [(f"ctx-{new.journey_id}", str(group_doc), f"ctx-{old.journey_id}")]
    [copied] = [b for b in _batches(old) if b["client_upload_id"] == f"dup-merge-{group}"]
    with old.engine.begin() as connection:
        set_tenant_context(connection, old.tenant_id)
        units = connection.execute(text(
            "SELECT unit_kind, queue_status, page_numbers, page_sha256, merged_into_queue_id, di_document_id "
            "FROM auditcore.p2_document_queue WHERE tenant_id=:t AND batch_id=:b ORDER BY unit_kind, page_number"),
            {"t": old.tenant_id, "b": copied["batch_id"]}).mappings().all()
    [whole] = [u for u in units if u["unit_kind"] == "GROUP"]
    pages = [u for u in units if u["unit_kind"] == "PAGE"]
    assert (whole["queue_status"], whole["di_document_id"], whole["page_numbers"]) == ("CLASSIFYING", group_doc, [1, 2])
    assert [p["page_sha256"] for p in pages] == shas
    assert all(p["queue_status"] == "MERGED" and p["di_document_id"] is None for p in pages)
