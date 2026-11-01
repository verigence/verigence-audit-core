"""FILE_MOVE (2026-11-01): a file taken out of a booking goes to its customer's own booking.

Built on the ADI-RD-261022-022 case of test_p2_file_identity: Tapan's file in
Azizur's booking. Once it is taken out, the move starts Tapan's own booking at
the same outlet for the PC who uploaded it and uploads the original there
again, byte for byte; Azizur's booking and the original are left as they are."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

import pytest
import test_p2_file_identity as fid
from p2_support import MemoryStorage, one_page_pdf
from sqlalchemy import text

from audit_core import uc03_file_move as move
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_file_identity import files_view


class _Storage(MemoryStorage):
    def head_object(self, key: str) -> dict:
        return {"contentLength": len(self.objects[key])}


@pytest.fixture
def journey(monkeypatch):
    monkeypatch.delenv("P2_FILE_IDENTITY_ENFORCE", raising=False)
    storage = _Storage()
    monkeypatch.setattr(move, "get_p2_document_storage", lambda: storage)
    yield from _journey(storage)


def _journey(storage):
    from conftest import delete_tenant_data
    from p2_support import create_p2_journey, database_engine

    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2move")
    created.storage = storage  # type: ignore[attr-defined]
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, **params})


def _tapan_taken_out(journey) -> dict:
    """Tapan's file in Azizur's booking, its original stored, then taken out."""
    _, azizur, tapan = fid._azizur_then_tapan(journey)
    batch = tapan["pan_card:1"]["batch_id"]
    payload = one_page_pdf(321)
    key = f"p2-documents/{journey.tenant_id}/{journey.journey_id}/{batch}/original/Tapan_kumar_patra.pdf"
    journey.storage.objects[key] = payload
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET original_object_key=:k, sha256=:s, size_bytes=:n "
                  "WHERE tenant_id=:t AND batch_id=:b",
         k=key, s=hashlib.sha256(payload).hexdigest(), n=len(payload), b=batch)
    fid._settle(journey)  # decided, not yet taken out
    return {"azizur": azizur, "tapan": tapan, "batch": batch, "key": key, "payload": payload}


def _plan(journey, batch: UUID, action: str = "NEW_BOOKING", **columns) -> str:
    return str(_sql(
        journey,
        "INSERT INTO auditcore.p2_file_moves (tenant_id, source_journey_id, source_batch_id, action, "
        "target_journey_id, same_as_move_id, person_name, planned_by) VALUES (:t, :j, :b, :a, :target, "
        "CAST(:same AS uuid), :p, 'test') RETURNING move_id",
        j=journey.journey_id, b=batch, a=action, target=columns.get("target"), same=columns.get("same"),
        p=columns.get("person", "TAPAN KUMAR PATRA"),
    ).scalar_one())


def _run(journey, commit: bool, refs: list[str] | None = None) -> dict:
    return move.run_file_move(journey.engine, f"{journey.tenant_id}:move-test{'-commit' if commit else ''}", refs)


def _journeys(journey) -> list[dict]:
    return [dict(r) for r in _sql(
        journey, "SELECT j.journey_id, j.journey_reference, j.outlet_id, j.created_by_actor_id, c.display_name "
                 "FROM auditcore.journeys j JOIN auditcore.customers c ON c.tenant_id=j.tenant_id "
                 "AND c.customer_id=j.customer_id WHERE j.tenant_id=:t ORDER BY j.created_at_utc").mappings().all()]


def _move_row(journey, move_id: str) -> dict:
    return dict(_sql(journey, "SELECT * FROM auditcore.p2_file_moves WHERE tenant_id=:t AND move_id=CAST(:m AS uuid)",
                     m=move_id).mappings().one())


def _source_state(journey, case: dict) -> dict:
    return {
        "batch": dict(_sql(journey, "SELECT original_object_key, sha256, batch_status, identity_status, "
                                    "identity_enforced_at_utc FROM auditcore.p2_upload_batches "
                                    "WHERE tenant_id=:t AND batch_id=:b", b=case["batch"]).mappings().one()),
        "evidence": fid._state(journey, *case["tapan"].values()),
        "object": journey.storage.objects[case["key"]],
    }


def test_a_file_not_taken_out_yet_waits_and_nothing_is_created(journey):
    case = _tapan_taken_out(journey)
    move_id = _plan(journey, case["batch"])
    assert _run(journey, commit=False) == {"WAITING": 1}
    assert _run(journey, commit=True) == {"WAITING": 1}
    assert len(_journeys(journey)) == 1
    assert _move_row(journey, move_id)["status"] == "PLANNED"
    # The dry run already says when the new booking could not be made.
    _sql(journey, "UPDATE auditcore.business_assignments SET assignment_status='INACTIVE' WHERE tenant_id=:t")
    assert _run(journey, commit=False) == {"FAILED": 1}
    assert _move_row(journey, move_id)["status"] == "PLANNED"


def test_the_dry_run_reports_and_changes_nothing(journey):
    case = _tapan_taken_out(journey)
    fid._switch(journey, True)
    fid._settle(journey)
    move_id = _plan(journey, case["batch"])
    before = (_journeys(journey), fid._booking_state(journey), len(journey.storage.objects))
    assert _run(journey, commit=False) == {"WOULD_CREATE": 1}
    assert (_journeys(journey), fid._booking_state(journey), len(journey.storage.objects)) == before
    assert _move_row(journey, move_id)["status"] == "PLANNED"


def test_the_file_goes_to_a_new_booking_of_its_customer_and_the_source_is_left_as_it_is(journey):
    case = _tapan_taken_out(journey)
    fid._switch(journey, True)
    fid._settle(journey)
    move_id = _plan(journey, case["batch"])
    source_before = _source_state(journey, case)
    assert source_before["batch"]["identity_enforced_at_utc"] is not None

    assert _run(journey, commit=True) == {"DONE": 1}
    row = _move_row(journey, move_id)
    assert row["status"] == "DONE" and row["new_batch_id"] == move.new_batch_id(move_id)
    [source, new] = _journeys(journey)
    assert source["journey_id"] == journey.journey_id
    assert row["target_journey_id"] == new["journey_id"]
    # Same outlet, the PC who uploaded the file, named after the person until its documents name him.
    assert (new["outlet_id"], new["created_by_actor_id"], new["display_name"]) == (
        journey.outlet_id, journey.actor_id, "TAPAN KUMAR PATRA")
    # The original, byte for byte, uploaded to the new booking and queued to be split and read.
    batch = dict(_sql(journey, "SELECT journey_id, original_filename, original_object_key, batch_status, "
                               "uploaded_by_actor_id, size_bytes FROM auditcore.p2_upload_batches "
                               "WHERE tenant_id=:t AND batch_id=:b", b=row["new_batch_id"]).mappings().one())
    assert (batch["journey_id"], batch["original_filename"], batch["batch_status"], batch["uploaded_by_actor_id"]) == (
        new["journey_id"], "Tapan kumar patra.pdf", "UPLOADED", journey.actor_id)
    assert f"/{new['journey_id']}/" in batch["original_object_key"]
    assert journey.storage.objects[batch["original_object_key"]] == case["payload"]
    work = _sql(journey, "SELECT work_status, payload FROM auditcore.p2_work_queue WHERE tenant_id=:t "
                         "AND work_type='SPLIT_BATCH' AND work_key=:k", k=str(row["new_batch_id"])).mappings().one()
    assert work["work_status"] == "PENDING" and work["payload"]["uploadedBy"] == journey.actor_id
    # The source booking, its file and the stored original are as they were.
    assert _source_state(journey, case) == source_before
    assert fid._state(journey, *case["tapan"].values()) == ["FOREIGN"] * 4
    # Both histories say what happened; the source booking's notice names the new booking.
    events = {(str(r[0]), r[1]) for r in _sql(journey, "SELECT journey_id, event_type FROM auditcore.p2_activity_events "
                                                        "WHERE tenant_id=:t").all()}
    assert (str(journey.journey_id), "FILE_MOVED_TO_OWN_BOOKING") in events
    assert {(str(new["journey_id"]), e) for e in ("JOURNEY_STARTED", "UPLOAD_ACCEPTED",
                                                  "FILE_RECEIVED_FROM_OTHER_BOOKING")} <= events
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        view = files_view(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    tapan = next(f for f in view["files"] if f["fileName"] == "Tapan kumar patra.pdf")
    assert tapan["movedTo"] == {"journeyId": str(new["journey_id"]), "reference": new["journey_reference"]}
    assert all(f["movedTo"] is None for f in view["files"] if f["fileName"] != "Tapan kumar patra.pdf")

    # Run again: nothing more happens.
    assert _run(journey, commit=True) == {}
    assert len(_journeys(journey)) == 2


def test_a_move_stopped_half_way_carries_on_without_a_second_booking_or_upload(journey, monkeypatch):
    case = _tapan_taken_out(journey)
    fid._switch(journey, True)
    fid._settle(journey)
    move_id = _plan(journey, case["batch"])
    real_copy = move._copy_file

    def broken(*args, **kwargs):
        raise RuntimeError("storage went away")

    monkeypatch.setattr(move, "_copy_file", broken)
    assert _run(journey, commit=True) == {"FAILED": 1}
    row = _move_row(journey, move_id)
    assert row["status"] == "FAILED" and "storage went away" in row["detail"]
    assert row["target_journey_id"] is not None and len(_journeys(journey)) == 2  # the booking was started

    monkeypatch.setattr(move, "_copy_file", real_copy)
    assert _run(journey, commit=True) == {"DONE": 1}
    assert len(_journeys(journey)) == 2
    assert _move_row(journey, move_id)["target_journey_id"] == row["target_journey_id"]
    assert _sql(journey, "SELECT count(*) FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j",
                j=row["target_journey_id"]).scalar_one() == 1


def test_a_changed_original_is_never_moved(journey):
    case = _tapan_taken_out(journey)
    fid._switch(journey, True)
    fid._settle(journey)
    move_id = _plan(journey, case["batch"])
    journey.storage.objects[case["key"]] = b"%PDF-1.4 something else"
    assert _run(journey, commit=True) == {"FAILED": 1}
    assert "does not match" in _move_row(journey, move_id)["detail"]
    assert len(_journeys(journey)) == 1


def test_a_pc_no_longer_at_the_outlet_is_reported_and_nothing_is_created(journey):
    case = _tapan_taken_out(journey)
    fid._switch(journey, True)
    fid._settle(journey)
    move_id = _plan(journey, case["batch"])
    _sql(journey, "UPDATE auditcore.business_assignments SET assignment_status='INACTIVE' WHERE tenant_id=:t")
    assert _run(journey, commit=False) == {"FAILED": 1}
    assert _run(journey, commit=True) == {"FAILED": 1}
    assert "cannot start a booking" in _move_row(journey, move_id)["detail"]
    assert len(_journeys(journey)) == 1


def test_own_booking_points_there_and_the_same_file_follows_the_other_move(journey):
    case = _tapan_taken_out(journey)
    fid._switch(journey, True)
    fid._settle(journey)
    # Tapan's own booking at the same dealership.
    other = _sql(journey, "INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                          "journey_reference) VALUES (:t, :d, :o, :c, 'ADI-RD-261029-013') RETURNING journey_id",
                 d=journey.dealer_id, o=journey.outlet_id, c=journey.customer_id).scalar_one()
    own = _plan(journey, case["batch"], action="OWN_BOOKING", target=other)
    # The same file in a second batch of the booking (as it was in two mixed bookings).
    second = uuid4()
    _sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                  "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id, "
                  "identity_status, identity_enforced_at_utc) VALUES (:t, :b, :j, 'Tapan kumar patra.pdf', "
                  "'application/pdf', 1, 1, :k, 'COMPLETED', :a, 'FOREIGN', now())",
         b=second, j=journey.journey_id, k=case["key"], a=journey.actor_id)
    same = _plan(journey, second, action="SAME_FILE", same=own)
    assert _run(journey, commit=False) == {"WOULD_POINT": 1, "WAITING": 1}
    assert _run(journey, commit=True) == {"DONE": 2}
    assert _move_row(journey, own)["target_journey_id"] == other
    assert _move_row(journey, same)["target_journey_id"] == other
    assert len(_journeys(journey)) == 2  # nothing created
    assert _sql(journey, "SELECT count(*) FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j",
                j=other).scalar_one() == 0  # nothing copied
    details = [r[0] for r in _sql(journey, "SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                                           "AND event_type='FILE_MOVED_TO_OWN_BOOKING'").all()]
    assert len(details) == 2 and all(d["toBooking"] == "ADI-RD-261029-013" for d in details)
    assert json.dumps(details)  # serialisable history


def test_an_own_booking_at_another_dealership_is_refused(journey):
    case = _tapan_taken_out(journey)
    fid._switch(journey, True)
    fid._settle(journey)
    dealer = _sql(journey, "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                           "VALUES (:t, 'OTHER', 'Other') RETURNING dealer_id").scalar_one()
    outlet = _sql(journey, "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                           "VALUES (:t, :d, 'OTHER-O', 'Other outlet') RETURNING outlet_id", d=dealer).scalar_one()
    elsewhere = _sql(journey, "INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                              "journey_reference) VALUES (:t, :d, :o, :c, 'UTK-XX-261029-001') RETURNING journey_id",
                     d=dealer, o=outlet, c=journey.customer_id).scalar_one()
    own = _plan(journey, case["batch"], action="OWN_BOOKING", target=elsewhere)
    assert _run(journey, commit=True) == {"FAILED": 1}
    assert "another dealership" in _move_row(journey, own)["detail"]
