"""FILE_MOVE: another customer's file, taken out of a booking, goes to that customer's own booking (2026-11-01).

File identity (uc03_p2_file_identity) takes another customer's file out of a
booking: its documents become FOREIGN there and stop counting. The customer
still needs a booking of their own with those documents. The operator
plans each file's destination as a row of ``auditcore.p2_file_moves``
(migration 0183), after review, and this one-shot job carries the plan out:

- NEW_BOOKING: no booking of the customer's exists yet. A booking is started
  at the source booking's outlet for the PC who uploaded the file (the PC's
  own New booking, uc03_p2_api.start_p2_journey), named after the person the
  file names until its documents name the customer. The original file is
  copied, byte for byte (its SHA-256 checked), to the new booking's storage
  folder and uploaded there as a new file: it is split and read like any
  upload, and the new booking's own rules apply to it.
- OWN_BOOKING: the customer already has their own booking holding these
  documents (checked before planning). Nothing is copied.
- SAME_FILE: the same file, byte for byte, was in two bookings. It goes where
  the other row's file went, once that row is done.

Safeguards
- A file is moved only once it is taken out of its source booking
  (``identity_status`` FOREIGN and ``identity_enforced_at_utc`` set): its
  documents are no longer ACTIVE there, so the new booking is never paired
  with the source booking as the same customer (the same-file delete,
  uc03_p2_same_file, and DUPLICATE_BOOKING read ACTIVE documents only). A
  row whose file is not taken out yet waits (stays PLANNED).
- The new booking is created now; its date is not backdated: the same-file
  rule treats the later booking as the new one.
- The source booking, its file and its stored original are never changed or
  deleted. Everything done is in both bookings' history.
- Idempotent: the new booking comes from an idempotency key per move, the new
  upload has a fixed id per move, so a job run again after a failure carries
  on from where it stopped and never creates a second booking or upload.
- Dry run unless the tag ends with "-commit": then it only reports, one
  ``file_move_plan`` line per row (what it would do, or why it would wait or
  fail -- e.g. the PC is no longer assigned to the outlet).

Requested as a row of ``p2_worker_jobs``::

    INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, requested_by)
    VALUES ('FILE_MOVE', '<tenant_id>:move-2026-11-01-commit', '<who>');

``reports`` may list source journey references to act on only those.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.errors import AuditCoreError
from audit_core.uc03_p2_object_keys import original_key
from audit_core.uc03_p2_runtime import enqueue_work, record_activity
from audit_core.uc03_p2_storage import P2DocumentStorageError, get_p2_document_storage

logger = structlog.get_logger(__name__)

_JOB = "FILE_MOVE"


def parse_tag(tag: str) -> tuple[str, bool]:
    """(tenant_id, commit) from ``<tenant_id>:<label>``; commit when the label
    ends with "-commit". Raises ValueError without a tenant."""
    tenant_id, sep, label = (tag or "").strip().partition(":")
    if not sep or not tenant_id.strip() or not label.strip():
        raise ValueError(f"{_JOB} needs the tag '<tenant_id>:<label>' (label ending -commit to move)")
    return tenant_id.strip(), label.strip().endswith("-commit")


def new_batch_id(move_id: UUID | str) -> UUID:
    """The id of the upload a move creates: fixed per move, so a retry finds it."""
    return uuid5(NAMESPACE_URL, f"verigence:p2-file-move:{move_id}")


@dataclass
class Move:
    move_id: str
    action: str
    status: str
    source_journey_id: UUID
    source_reference: str | None
    source_batch_id: UUID
    target_journey_id: UUID | None
    same_as_move_id: str | None
    person_name: str | None
    # the source file
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str | None
    object_key: str
    uploaded_by: str
    identity_status: str | None
    taken_out: bool
    outlet_id: UUID
    dealer_id: UUID
    pc_name: str | None


def _moves(connection: Connection, tenant_id: str, references: list[str]) -> list[Move]:
    rows = connection.execute(
        text(
            """
            SELECT m.move_id, m.action, m.status, m.source_journey_id, sj.journey_reference AS source_reference,
                   m.source_batch_id, m.target_journey_id, m.same_as_move_id, m.person_name,
                   b.original_filename, b.content_type, b.size_bytes, b.sha256, b.original_object_key,
                   b.uploaded_by_actor_id, b.identity_status, b.identity_enforced_at_utc IS NOT NULL AS taken_out,
                   sj.outlet_id, sj.dealer_id, sj.created_by_display_name
            FROM auditcore.p2_file_moves m
            JOIN auditcore.journeys sj ON sj.tenant_id=m.tenant_id AND sj.journey_id=m.source_journey_id
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=m.tenant_id AND b.batch_id=m.source_batch_id
            WHERE m.tenant_id=:t AND m.status IN ('PLANNED', 'FAILED')
              AND (cardinality(CAST(:refs AS text[])) = 0 OR sj.journey_reference = ANY(CAST(:refs AS text[])))
            -- a SAME_FILE row after the row it follows; otherwise in upload order
            ORDER BY (m.action = 'SAME_FILE'), b.created_at_utc, m.move_id
            """
        ),
        {"t": tenant_id, "refs": references},
    ).mappings().all()
    return [Move(
        move_id=str(r["move_id"]), action=r["action"], status=r["status"],
        source_journey_id=r["source_journey_id"], source_reference=r["source_reference"],
        source_batch_id=r["source_batch_id"], target_journey_id=r["target_journey_id"],
        same_as_move_id=str(r["same_as_move_id"]) if r["same_as_move_id"] else None, person_name=r["person_name"],
        file_name=str(r["original_filename"] or "file.pdf"), content_type=str(r["content_type"] or ""),
        size_bytes=int(r["size_bytes"] or 0), sha256=r["sha256"], object_key=str(r["original_object_key"]),
        uploaded_by=str(r["uploaded_by_actor_id"]), identity_status=r["identity_status"],
        taken_out=bool(r["taken_out"]), outlet_id=r["outlet_id"], dealer_id=r["dealer_id"],
        pc_name=r["created_by_display_name"],
    ) for r in rows]


def _finish(connection: Connection, tenant_id: str, move: Move, *, status: str, detail: str,
            target_journey_id: UUID | None = None, batch_id: UUID | None = None) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_file_moves
            SET status=CAST(:s AS varchar), detail=:d, target_journey_id=COALESCE(CAST(:target AS uuid), target_journey_id),
                new_batch_id=COALESCE(CAST(:batch AS uuid), new_batch_id),
                done_at_utc=CASE WHEN CAST(:s AS varchar) = 'DONE' THEN now() ELSE done_at_utc END
            WHERE tenant_id=:t AND move_id=CAST(:m AS uuid)
            """
        ),
        {"t": tenant_id, "m": move.move_id, "s": status, "d": detail[:2000],
         "target": str(target_journey_id) if target_journey_id else None,
         "batch": str(batch_id) if batch_id else None},
    )


def _reference(connection: Connection, tenant_id: str, journey_id: UUID) -> str | None:
    return connection.execute(
        text("SELECT journey_reference FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()


def _wait_reason(move: Move) -> str | None:
    if move.identity_status != "FOREIGN" or not move.taken_out:
        return "the file is not taken out of its booking yet (switch file identity on for that booking first)"
    return None


def _pc_can_book(connection: Connection, tenant_id: str, move: Move) -> str | None:
    """Why the uploader cannot start a booking at the outlet, or None."""
    from audit_core.uc03_create_booking import _create_context

    try:
        _create_context(connection, tenant_id=tenant_id, actor_id=move.uploaded_by, outlet_id=move.outlet_id)
    except (AuditCoreError, AuthorizationError) as exc:  # reported, never raised
        return f"the PC who uploaded the file cannot start a booking at this outlet ({type(exc).__name__})"
    return None


def _new_booking_problem(engine: Engine, tenant_id: str, move: Move) -> str | None:
    """Dry run: why a new booking could not be made for this row, or None."""
    if not move.target_journey_id:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            problem = _pc_can_book(connection, tenant_id, move)
        if problem:
            return problem
    try:
        get_p2_document_storage().head_object(move.object_key)
    except (P2DocumentStorageError, RuntimeError) as exc:
        return f"the stored original cannot be read ({type(exc).__name__})"
    return None


def _plan_line(engine: Engine, tenant_id: str, move: Move, *, commit: bool, outcome: str, **extra: Any) -> None:
    logger.info("file_move_plan", tenant_id=tenant_id, commit=commit, move_id=move.move_id, action=move.action,
                source=move.source_reference, file_name=move.file_name, person=move.person_name,
                outcome=outcome, **extra)


def _create_booking(engine: Engine, tenant_id: str, move: Move) -> UUID:
    """The customer's new booking (created once per move; a retry reuses it)."""
    from audit_core.uc03_p2_api import start_p2_journey

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"p2-file-move:{tenant_id}:{move.move_id}"},
        )
        current = connection.execute(
            text("SELECT target_journey_id FROM auditcore.p2_file_moves WHERE tenant_id=:t AND move_id=CAST(:m AS uuid)"),
            {"t": tenant_id, "m": move.move_id},
        ).scalar_one_or_none()
        if current is not None:
            return UUID(str(current))
        created = start_p2_journey(
            connection, tenant_id=tenant_id, actor_id=move.uploaded_by, outlet_id=move.outlet_id,
            customer_name=move.person_name, created_by_name=move.pc_name,
            idempotency_key=f"p2-file-move:{move.move_id}",
            started_details={"startedBy": "FILE_MOVE", "fileName": move.file_name,
                             "fromBooking": move.source_reference, "fromJourneyId": str(move.source_journey_id)},
        )
        journey_id = UUID(created["journeyId"])
        connection.execute(
            text("UPDATE auditcore.p2_file_moves SET target_journey_id=:j WHERE tenant_id=:t AND move_id=CAST(:m AS uuid)"),
            {"t": tenant_id, "m": move.move_id, "j": journey_id},
        )
        return journey_id


def _original(move: Move) -> bytes:
    """The stored original, checked against the uploaded file's SHA-256."""
    payload = get_p2_document_storage().get_object(move.object_key)
    digest = hashlib.sha256(payload).hexdigest()
    if move.sha256 and digest != move.sha256:
        raise ValueError(f"the stored original does not match the uploaded file (SHA-256 {digest[:12]} vs "
                         f"{move.sha256[:12]}); nothing was moved")
    return payload


def _copy_file(engine: Engine, tenant_id: str, move: Move, journey_id: UUID, payload: bytes) -> UUID:
    """The original, byte for byte, uploaded again to ``journey_id``; the new upload's id."""
    batch_id = new_batch_id(move.move_id)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        existing = connection.execute(
            text("SELECT batch_id FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND batch_id=:b"),
            {"t": tenant_id, "b": batch_id},
        ).scalar_one_or_none()
        labels = connection.execute(
            text(
                """
                SELECT o.outlet_name, j.created_by_display_name
                FROM auditcore.journeys j
                LEFT JOIN auditcore.dealer_outlets o
                  ON o.tenant_id=j.tenant_id AND o.dealer_id=j.dealer_id AND o.outlet_id=j.outlet_id
                WHERE j.tenant_id=:t AND j.journey_id=:j
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).one()
    if existing is not None:
        return batch_id
    storage = get_p2_document_storage()
    key = original_key(tenant_id, journey_id, batch_id, filename=move.file_name,
                       outlet_name=labels[0], pc_name=labels[1])
    # Object storage outside any transaction; a retry writes the same key.
    storage.put_object(key, payload, content_type=move.content_type or "application/pdf")
    correlation = f"p2-file-move:{move.move_id}"
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        inserted = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, client_upload_id,
                    original_filename, content_type, size_bytes, page_count,
                    original_object_key, batch_status, uploaded_by_actor_id, correlation_id
                ) VALUES (
                    :t, :b, :j, :client, :name, :ct, :size, 0, :key, 'UPLOADED', :by, :correlation
                )
                ON CONFLICT DO NOTHING
                RETURNING batch_id
                """
            ),
            {"t": tenant_id, "b": batch_id, "j": journey_id, "client": f"file-move-{move.source_batch_id}",
             "name": move.file_name, "ct": move.content_type or "application/pdf", "size": len(payload),
             "key": key, "by": move.uploaded_by, "correlation": correlation},
        ).scalar_one_or_none()
        if inserted is None:
            return batch_id
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=journey_id, work_type="SPLIT_BATCH", work_key=str(batch_id),
            payload={"batchId": str(batch_id), "uploadedBy": move.uploaded_by, "uploadedByRole": "PC"},
            correlation_id=correlation,
        )
        record_activity(
            connection, tenant_id=tenant_id, journey_id=journey_id, event_type="UPLOAD_ACCEPTED",
            subject_type="UPLOAD_BATCH", subject_id=str(batch_id),
            details={"filename": move.file_name, "sizeBytes": len(payload), "movedFrom": move.source_reference,
                     "movedFromJourneyId": str(move.source_journey_id), "by": "FILE_MOVE"},
            correlation_id=correlation,
        )
    return batch_id


def _record_moved(connection: Connection, tenant_id: str, move: Move, target: UUID, target_reference: str | None,
                  how: str) -> None:
    """The source booking's history says where the file went; the target's that it came."""
    details = {"fileName": move.file_name, "person": move.person_name, "action": move.action, "how": how,
               "sourceBatchId": str(move.source_batch_id)}
    record_activity(
        connection, tenant_id=tenant_id, journey_id=move.source_journey_id, event_type="FILE_MOVED_TO_OWN_BOOKING",
        subject_type="UPLOAD_BATCH", subject_id=str(move.source_batch_id),
        details=details | {"toBooking": target_reference, "toJourneyId": str(target)},
        correlation_id=f"p2-file-move:{move.move_id}",
    )
    if target != move.source_journey_id:
        record_activity(
            connection, tenant_id=tenant_id, journey_id=target, event_type="FILE_RECEIVED_FROM_OTHER_BOOKING",
            subject_type="UPLOAD_BATCH", subject_id=str(move.source_batch_id),
            details=details | {"fromBooking": move.source_reference, "fromJourneyId": str(move.source_journey_id)},
            correlation_id=f"p2-file-move:{move.move_id}",
        )


def _carry_out(engine: Engine, tenant_id: str, move: Move, *, commit: bool) -> str:
    """One row: 'DONE', 'WAITING', 'FAILED' or (dry run) 'WOULD_*'."""
    wait = _wait_reason(move)
    if wait:
        # A dry run still checks a new booking can be made, so a problem is
        # known before the file is taken out.
        problem = _new_booking_problem(engine, tenant_id, move) if not commit and move.action == "NEW_BOOKING" else None
        _plan_line(engine, tenant_id, move, commit=commit, outcome="FAILED" if problem else "WAITING",
                   reason=problem or wait)
        return "FAILED" if problem else "WAITING"
    if move.action == "OWN_BOOKING":
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            target = connection.execute(
                text("SELECT journey_id, journey_reference, dealer_id FROM auditcore.journeys "
                     "WHERE tenant_id=:t AND journey_id=:j"),
                {"t": tenant_id, "j": move.target_journey_id},
            ).mappings().one_or_none()
            problem = ("the customer's booking is no longer there" if target is None
                       else "the customer's booking is the same booking" if target["journey_id"] == move.source_journey_id
                       else "the customer's booking is at another dealership" if target["dealer_id"] != move.dealer_id
                       else None)
            if problem:
                if commit:
                    _finish(connection, tenant_id, move, status="FAILED", detail=problem)
                _plan_line(engine, tenant_id, move, commit=commit, outcome="FAILED", reason=problem)
                return "FAILED"
            _plan_line(engine, tenant_id, move, commit=commit, outcome="DONE" if commit else "WOULD_POINT",
                       to=target["journey_reference"])
            if commit:
                _record_moved(connection, tenant_id, move, target["journey_id"], target["journey_reference"],
                              "already in the customer's own booking; nothing copied")
                _finish(connection, tenant_id, move, status="DONE",
                        detail=f"Already in the customer's own booking {target['journey_reference']}; nothing copied.")
            return "DONE" if commit else "WOULD_POINT"
    if move.action == "SAME_FILE":
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            other = connection.execute(
                text("SELECT m.status, m.target_journey_id, j.journey_reference FROM auditcore.p2_file_moves m "
                     "LEFT JOIN auditcore.journeys j ON j.tenant_id=m.tenant_id AND j.journey_id=m.target_journey_id "
                     "WHERE m.tenant_id=:t AND m.move_id=CAST(:m AS uuid)"),
                {"t": tenant_id, "m": move.same_as_move_id},
            ).mappings().one_or_none()
            if other is None or other["status"] != "DONE" or other["target_journey_id"] is None:
                _plan_line(engine, tenant_id, move, commit=commit, outcome="WAITING",
                           reason="the row it follows is not done yet")
                return "WAITING"
            _plan_line(engine, tenant_id, move, commit=commit, outcome="DONE" if commit else "WOULD_POINT",
                       to=other["journey_reference"])
            if commit:
                _record_moved(connection, tenant_id, move, other["target_journey_id"], other["journey_reference"],
                              "the same file was moved from another booking; nothing copied")
                _finish(connection, tenant_id, move, status="DONE", target_journey_id=other["target_journey_id"],
                        detail=f"The same file went to {other['journey_reference']}; nothing copied.")
            return "DONE" if commit else "WOULD_POINT"
    # NEW_BOOKING
    if commit:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            problem = None if move.target_journey_id else _pc_can_book(connection, tenant_id, move)
            if problem:
                _finish(connection, tenant_id, move, status="FAILED", detail=problem)
    else:
        problem = _new_booking_problem(engine, tenant_id, move)
    if problem:
        _plan_line(engine, tenant_id, move, commit=commit, outcome="FAILED", reason=problem)
        return "FAILED"
    if not commit:
        _plan_line(engine, tenant_id, move, commit=False, outcome="WOULD_CREATE", pc=move.pc_name,
                   size_bytes=move.size_bytes)
        return "WOULD_CREATE"
    # The original is read and checked before anything is created.
    payload = _original(move)
    journey_id = _create_booking(engine, tenant_id, move)
    batch_id = _copy_file(engine, tenant_id, move, journey_id, payload)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        reference = _reference(connection, tenant_id, journey_id)
        _record_moved(connection, tenant_id, move, journey_id, reference, "new booking started; file uploaded to it")
        _finish(connection, tenant_id, move, status="DONE", target_journey_id=journey_id, batch_id=batch_id,
                detail=f"New booking {reference} started for {move.person_name or 'the customer'}; the file was "
                       "uploaded to it and is read there.")
    _plan_line(engine, tenant_id, move, commit=True, outcome="DONE", to=reference, new_batch_id=str(batch_id))
    return "DONE"


def run_file_move(engine: Engine, tag: str, references: list[str] | None = None) -> dict[str, int]:
    tenant_id, commit = parse_tag(tag)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        moves = _moves(connection, tenant_id, list(references or []))
    counts: dict[str, int] = {}
    for move in moves:
        try:
            outcome = _carry_out(engine, tenant_id, move, commit=commit)
        except Exception as exc:  # one row's failure never stops the others
            outcome = "FAILED"
            logger.warning("file_move_failed", tenant_id=tenant_id, move_id=move.move_id, exc_info=True)
            if commit:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    _finish(connection, tenant_id, move, status="FAILED", detail=f"{type(exc).__name__}: {exc}")
        counts[outcome] = counts.get(outcome, 0) + 1
    logger.info("file_move_finished", tenant_id=tenant_id, commit=commit, rows=len(moves), **counts)
    return counts
