"""Bulk upload of journey files (product owner, 2026-11-03).

"The intent of bulk is to not ask anything from the PC: whatever comes up in
the folder could be a new booking, an update to an existing booking, or
delivery documents. A delivery file carries the KYC documents as well, which
can be used to find an existing journey -- but minimise Document Intelligence
for repeated documents."

The drop (one outlet, the one the PC has selected)
    The PC adds files; each goes straight to R2 under
    ``p2-bulk/<tenant>/<dealership>/<outlet>/<yyyy-mm-dd>/<the file's own name>``
    ("dealership - outlet - day of the month wise, files stored with the same
    name as uploaded by the PC"; a second file of the same name that day gets
    " (2)"). Nothing is processed until the PC submits; then the PC is asked
    not to upload new files until the drop is done (the web shows it to a PC
    mapped to more than one outlet).

What happens to each file (job BULK_UPLOAD, then BULK_SETTLE)
    1. Its SHA-256 is taken. A file already uploaded, byte for byte, to any
       booking of the Project (or earlier in a drop) is an EXACT_DUPLICATE:
       nothing is read, nothing changes.
    2. The other files are grouped by the part of the name that says whose
       file it is (uc03_p2_file_identity.file_stem; a generic name is a group
       of its own), and each group is read on a journey of its own at the
       outlet, started for the PC as their New booking would be (READING).
    3. Once every page of that journey is read (BULK_SETTLE, requested from
       the journey's rule run): when an open booking of the same outlet has
       the same customer -- PAN and/or Aadhaar, one read on both and never
       different, and the same model when both are known -- every page that
       booking does not have (by page fingerprint) is carried to it
       (uc03_p2_same_file.carry_documents) and the journey read for the drop
       is deleted: ADDED_TO_BOOKING. A page not settled as the customer's
       keeps the journey for review (KEPT_FOR_REVIEW); nothing is lost. No
       such booking: the journey is the customer's NEW_BOOKING.
    The same-file rule leaves a journey read for a drop to this (it would
    otherwise alert the whole Project about a file the PC was told to drop).

Read-only for the PC after submit; every file says where it went.

A drop taken from the Project's Google Drive (uc03_p2_drive_intake, 2026-11-12)
    is the same drop, made for the outlet's PC without the PC. Its files say more than
    a name: the customer's folder they came in is their group (``group_key`` is set when
    the file is added, not guessed from its name), a file added to a folder whose files
    already went to a journey goes straight to that journey (``target_journey_id``), and
    the folder's number -- the booking / OTF number, or the customer's mobile -- finds the
    customer's open booking at the outlet when the file itself carries no PAN or Aadhaar
    to find it by (``booking_number``; where both files have a PAN or Aadhaar they must
    still be the same customer's).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid5

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_gate_pass import scope_outlets
from audit_core.uc03_p2_object_keys import day_folder, safe_part

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-bulk-upload"])

_UPDATE = "audit.journey.update"
_READ = "audit.journey.read"
JOB_UPLOAD = "BULK_UPLOAD"
JOB_SETTLE = "BULK_SETTLE"
_ALLOWED = {"application/pdf", "image/jpeg", "image/png"}
_MAX_BYTES = 50 * 1024 * 1024
_MAX_FILES = 300
_WAITING_UNITS = (
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING",
    "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT",
)
_IN_FLIGHT_BATCHES = ("AWAITING_UPLOAD", "UPLOADED", "SPLITTING")
_SETTLED_OUTCOMES = ("EXACT_DUPLICATE", "NEW_BOOKING", "ADDED_TO_BOOKING", "KEPT_FOR_REVIEW", "FAILED", "REPLACED")


# --------------------------------------------------------------------------- storage names

def bulk_folder(tenant_id: str, dealer_name: Any, outlet_name: Any, now: datetime | None = None) -> str:
    """``p2-bulk/<tenant>/<dealership>/<outlet>/<yyyy-mm-dd>`` (IST day)."""
    return (f"p2-bulk/{safe_part(tenant_id, fallback='tenant')}/{safe_part(dealer_name, fallback='dealership')}/"
            f"{safe_part(outlet_name, fallback='outlet')}/{day_folder(now)}")


def plain_name(filename: str) -> str:
    """The file's own name, as the PC gave it: without any folder part, nothing a key cannot hold."""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in '"<>|?*').strip()
    return name[:240] or "document"


def numbered(name: str, n: int) -> str:
    """``scan.pdf`` -> ``scan (2).pdf``."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name} ({n})"
    return f"{stem} ({n}).{ext}"


_NUMBERED = re.compile(r" \(\d+\)(\.[^.]*)?$")


def unnumbered(name: str) -> str:
    """The name before ``numbered``: ``scan (2).pdf`` -> ``scan.pdf`` (the SQL in migration
    0260 is the same pattern)."""
    return _NUMBERED.sub(lambda m: m.group(1) or "", str(name or ""))


def _same_file_earlier(
    earlier: list[dict[str, Any]], claimed: set[str], name: str, size: int, content_type: str,
) -> dict[str, Any] | None:
    """A file of the drop, from before this request, that this one is: the same name (as the
    PC gave it, or numbered by an earlier retry) and the same size. One already in storage is
    preferred; a file still pending is the one to put again. 24 Nov 2026: a retry used to add a
    "(2)" copy and leave the first record waiting, which then failed at submit."""
    matches = [f for f in earlier
               if str(f["file_id"]) not in claimed and f["outcome"] == "PENDING"
               and int(f["size_bytes"] or 0) == int(size) and str(f["content_type"]) == content_type
               and unnumbered(str(f["original_filename"])) == unnumbered(name)]
    matches.sort(key=lambda f: f["upload_status"] != "UPLOADED")
    return matches[0] if matches else None


def _take_files_already_in_storage(connection: Connection, tenant_id: str, bulk_id: UUID) -> int:
    """At submit, a file whose "uploaded" call never arrived (the browser put it in storage, then
    lost the connection) is in storage all the same: it is taken, not failed."""
    from audit_core.uc03_p2_storage import get_p2_document_storage

    waiting = connection.execute(
        text("SELECT file_id, object_key, size_bytes FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b "
             "AND upload_status='AWAITING_UPLOAD' AND outcome='PENDING'"), {"t": tenant_id, "b": bulk_id},
    ).mappings().all()
    if not waiting:
        return 0
    try:
        storage = get_p2_document_storage()
    except RuntimeError:
        return 0
    taken = 0
    for row in waiting:
        try:
            head = storage.head_object(str(row["object_key"]))
        except Exception:  # noqa: BLE001 - any failure to find it means it is not in storage
            logger.info("p2_bulk_file_not_in_storage_at_submit", tenant_id=tenant_id, file_id=str(row["file_id"]))
            continue
        connection.execute(
            text("UPDATE auditcore.p2_bulk_files SET upload_status='UPLOADED', size_bytes=:s, updated_at_utc=now() "
                 "WHERE tenant_id=:t AND file_id=:f"),
            {"s": int(head.get("contentLength") or row["size_bytes"] or 0), "t": tenant_id, "f": row["file_id"]},
        )
        taken += 1
    return taken


def _close_files_not_uploaded(connection: Connection, tenant_id: str, bulk_id: UUID) -> None:
    """At submit, a file still not in storage is REPLACED when the drop holds it again, uploaded
    (the same name, numbered or not, and the same size) -- nothing is missing, so the PC is not
    told it failed. Only a file the drop does not hold in any form is FAILED."""
    connection.execute(
        text(
            r"""
            UPDATE auditcore.p2_bulk_files f
               SET outcome='REPLACED', detail='Uploaded again as ' || m.again, updated_at_utc=now()
              FROM (
                SELECT DISTINCT ON (w.file_id) w.file_id, g.original_filename AS again
                  FROM auditcore.p2_bulk_files w
                  JOIN auditcore.p2_bulk_files g
                    ON g.tenant_id=w.tenant_id AND g.bulk_id=w.bulk_id AND g.file_id <> w.file_id
                   AND g.upload_status='UPLOADED' AND g.size_bytes=w.size_bytes
                   AND regexp_replace(g.original_filename, ' \(\d+\)(\.[^.]*)?$', '\1')
                     = regexp_replace(w.original_filename, ' \(\d+\)(\.[^.]*)?$', '\1')
                 WHERE w.tenant_id=:t AND w.bulk_id=:b AND w.upload_status='AWAITING_UPLOAD'
                   AND w.outcome='PENDING'
                 ORDER BY w.file_id, g.created_at_utc
              ) m
             WHERE f.tenant_id=:t AND f.file_id=m.file_id
            """
        ),
        {"t": tenant_id, "b": bulk_id},
    )
    connection.execute(
        text("UPDATE auditcore.p2_bulk_files SET outcome='FAILED', detail='Not uploaded before submit', "
             "updated_at_utc=now() WHERE tenant_id=:t AND bulk_id=:b AND upload_status='AWAITING_UPLOAD' "
             "AND outcome='PENDING'"),
        {"t": tenant_id, "b": bulk_id},
    )


def group_key(filename: str, file_id: str) -> str:
    """Files whose names say they are one customer's are read together."""
    from audit_core.uc03_p2_file_identity import file_stem

    stem = file_stem(filename)
    return f"stem:{stem}" if stem else f"file:{file_id}"


# --------------------------------------------------------------------------- API

class BulkCreateCommand(BaseModel):
    outletId: UUID
    createdByName: str | None = Field(default=None, max_length=300)


class BulkFileItem(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    contentType: str = Field(min_length=1, max_length=100)
    sizeBytes: int = Field(ge=1)


class BulkFilesCommand(BaseModel):
    files: list[BulkFileItem] = Field(min_length=1, max_length=100)


def _my_pc_outlet(connection: Connection, tenant_id: str, actor_id: str, outlet_id: UUID | str) -> dict[str, Any]:
    for outlet in scope_outlets(connection, tenant_id, actor_id):
        if str(outlet["outlet_id"]) == str(outlet_id) and "PC" in {str(r).upper() for r in outlet.get("roles") or []}:
            return outlet
    raise HTTPException(status_code=404, detail="This outlet is not one you upload for as a PC.")


def _bulk(connection: Connection, tenant_id: str, bulk_id: UUID, actor_id: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_bulk_uploads WHERE tenant_id=:t AND bulk_id=:b"),
        {"t": tenant_id, "b": bulk_id},
    ).mappings().one_or_none()
    if row is None or row["uploaded_by_actor_id"] != actor_id:
        raise HTTPException(status_code=404, detail="Bulk upload not found.")
    return dict(row)


def _public_file(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "fileId": str(row["file_id"]), "filename": row["original_filename"], "sizeBytes": int(row["size_bytes"] or 0),
        "uploaded": row["upload_status"] == "UPLOADED", "outcome": row["outcome"],
        "journeyId": str(row["placed_journey_id"] or row["journey_id"]) if (row["placed_journey_id"] or row["journey_id"])
        else None,
        "journeyReference": row["placed_reference"], "detail": row["detail"],
    }


def _public_bulk(row: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "bulkId": str(row["bulk_id"]), "outletId": str(row["outlet_id"]), "dealerId": str(row["dealer_id"]),
        "status": row["status"], "folder": row["folder"], "fileCount": int(row["file_count"] or 0),
        "summary": dict(row["summary"] or {}),
        "createdAtUtc": row["created_at_utc"].isoformat() if row["created_at_utc"] else None,
        "submittedAtUtc": row["submitted_at_utc"].isoformat() if row["submitted_at_utc"] else None,
        "finishedAtUtc": row["finished_at_utc"].isoformat() if row["finished_at_utc"] else None,
        "files": [_public_file(f) for f in files],
    }


def _files(connection: Connection, tenant_id: str, bulk_ids: list[Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {str(b): [] for b in bulk_ids}
    if not bulk_ids:
        return out
    for row in connection.execute(
        # A file REPLACED by its own later upload is not the PC's to see: nothing is missing.
        text("SELECT * FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id = ANY(CAST(:ids AS uuid[])) "
             "AND outcome <> 'REPLACED' ORDER BY created_at_utc, original_filename"),
        {"t": tenant_id, "ids": [str(b) for b in bulk_ids]},
    ).mappings():
        out[str(row["bulk_id"])].append(dict(row))
    return out


@router.post("/bulk-uploads", status_code=201)
def create_bulk_upload(
    tenant_id: str,
    command: BulkCreateCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Open a drop for the outlet the PC has selected (an open one of today is reused)."""
    from audit_core.uc03_p2_upload_pause import refuse_upload_if_paused

    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_UPDATE)
    refuse_upload_if_paused(connection, tenant_id, principal.subject)
    outlet = _my_pc_outlet(connection, tenant_id, principal.subject, command.outletId)
    folder = bulk_folder(tenant_id, outlet["dealer_name"], outlet["outlet_name"])
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_bulk_uploads WHERE tenant_id=:t AND uploaded_by_actor_id=:a "
             "AND outlet_id=:o AND status='OPEN' AND folder=:f AND source='WEB' ORDER BY created_at_utc DESC LIMIT 1"),
        {"t": tenant_id, "a": principal.subject, "o": command.outletId, "f": folder},
    ).mappings().one_or_none()
    if row is None:
        row = connection.execute(
            text("INSERT INTO auditcore.p2_bulk_uploads (tenant_id, dealer_id, outlet_id, uploaded_by_actor_id, "
                 "uploaded_by_name, folder) VALUES (:t, :d, :o, :a, :n, :f) RETURNING *"),
            {"t": tenant_id, "d": outlet["dealer_id"], "o": command.outletId, "a": principal.subject,
             "n": (command.createdByName or "").strip() or None, "f": folder},
        ).mappings().one()
    row = dict(row)
    return _public_bulk(row, _files(connection, tenant_id, [row["bulk_id"]])[str(row["bulk_id"])])


@router.post("/bulk-uploads/{bulk_id}/files")
def add_bulk_files(
    tenant_id: str,
    bulk_id: UUID,
    command: BulkFilesCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """A storage link for each file: the browser puts the file straight into the drop's folder."""
    from audit_core.uc03_p2_storage import get_p2_document_storage

    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_UPDATE)
    bulk = _bulk(connection, tenant_id, bulk_id, principal.subject)
    if bulk["status"] != "OPEN":
        raise HTTPException(status_code=409, detail="This bulk upload is already submitted.")
    count = connection.execute(
        text("SELECT count(*) FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b"),
        {"t": tenant_id, "b": bulk_id},
    ).scalar_one()
    if count + len(command.files) > _MAX_FILES:
        raise HTTPException(status_code=413, detail=f"A bulk upload takes up to {_MAX_FILES} files.")
    try:
        storage = get_p2_document_storage()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Document storage is not configured.") from exc
    # The drop's files from before this request: a file picked again (a retry, or the page
    # reloaded and the files picked again) is the same file, not a "(2)" copy of it.
    earlier = [dict(r) for r in connection.execute(
        text("SELECT * FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b ORDER BY created_at_utc"),
        {"t": tenant_id, "b": bulk_id},
    ).mappings().all()]
    claimed: set[str] = set()
    out = []
    for item in command.files:
        content_type = item.contentType.lower().strip()
        if content_type not in _ALLOWED:
            raise HTTPException(status_code=415, detail=f"{item.filename}: only PDF, JPEG and PNG files.")
        if item.sizeBytes > _MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"{item.filename}: larger than 50 MB.")
        name = plain_name(item.filename)
        same = _same_file_earlier(earlier, claimed, name, item.sizeBytes, content_type)
        if same is not None:
            claimed.add(str(same["file_id"]))
            uploaded = same["upload_status"] == "UPLOADED"
            out.append({"fileId": str(same["file_id"]), "filename": same["original_filename"],
                        "objectKey": same["object_key"], "contentType": content_type,
                        "alreadyUploaded": uploaded,
                        "uploadUrl": None if uploaded else storage.presign_put(
                            str(same["object_key"]), content_type=content_type, expires_seconds=3600)})
            continue
        key, n = f"{bulk['folder']}/{name}", 1
        while connection.execute(
            text("SELECT 1 FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND object_key=:k"),
            {"t": tenant_id, "k": key},
        ).first() is not None:
            n += 1
            key = f"{bulk['folder']}/{numbered(name, n)}"
        row = connection.execute(
            text("INSERT INTO auditcore.p2_bulk_files (tenant_id, bulk_id, original_filename, content_type, "
                 "size_bytes, object_key) VALUES (:t, :b, :name, :ct, :size, :k) RETURNING file_id"),
            {"t": tenant_id, "b": bulk_id, "name": name if n == 1 else numbered(name, n), "ct": content_type,
             "size": item.sizeBytes, "k": key},
        ).mappings().one()
        out.append({"fileId": str(row["file_id"]), "filename": name if n == 1 else numbered(name, n),
                    "objectKey": key, "contentType": content_type, "alreadyUploaded": False,
                    "uploadUrl": storage.presign_put(key, content_type=content_type, expires_seconds=3600)})
    return {"bulkId": str(bulk_id), "files": out}


@router.post("/bulk-uploads/{bulk_id}/files/{file_id}/complete")
def complete_bulk_file(
    tenant_id: str,
    bulk_id: UUID,
    file_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The browser finished putting the file: it is in storage (checked)."""
    from audit_core.uc03_p2_storage import get_p2_document_storage

    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_UPDATE)
    _bulk(connection, tenant_id, bulk_id, principal.subject)
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b AND file_id=:f"),
        {"t": tenant_id, "b": bulk_id, "f": file_id},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found in this bulk upload.")
    try:
        head = get_p2_document_storage().head_object(str(row["object_key"]))
    except Exception as exc:  # not there (yet)
        raise HTTPException(status_code=409, detail="The file has not reached storage yet.") from exc
    size = int(head.get("contentLength") or row["size_bytes"] or 0)
    updated = connection.execute(
        text("UPDATE auditcore.p2_bulk_files SET upload_status='UPLOADED', size_bytes=:s, updated_at_utc=now() "
             "WHERE tenant_id=:t AND file_id=:f RETURNING *"),
        {"s": size, "t": tenant_id, "f": file_id},
    ).mappings().one()
    return _public_file(dict(updated))


@router.post("/bulk-uploads/{bulk_id}/submit")
def submit_bulk_upload(
    tenant_id: str,
    bulk_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Submit the drop: its files are processed; a file never put in storage is left out."""
    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_UPDATE)
    bulk = _bulk(connection, tenant_id, bulk_id, principal.subject)
    if bulk["status"] != "OPEN":
        raise HTTPException(status_code=409, detail="This bulk upload is already submitted.")
    uploaded = connection.execute(
        text("SELECT count(*) FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b "
             "AND upload_status='UPLOADED'"), {"t": tenant_id, "b": bulk_id},
    ).scalar_one()
    uploaded += _take_files_already_in_storage(connection, tenant_id, bulk_id)
    if not uploaded:
        raise HTTPException(status_code=409, detail="No file has finished uploading yet.")
    _close_files_not_uploaded(connection, tenant_id, bulk_id)
    row = connection.execute(
        text("UPDATE auditcore.p2_bulk_uploads SET status='SUBMITTED', file_count=:n, submitted_at_utc=now(), "
             "updated_at_utc=now() WHERE tenant_id=:t AND bulk_id=:b RETURNING *"),
        {"n": uploaded, "t": tenant_id, "b": bulk_id},
    ).mappings().one()
    request_job(connection, kind=JOB_UPLOAD, tenant_id=tenant_id, bulk_id=bulk_id, by=principal.subject)
    return _public_bulk(dict(row), _files(connection, tenant_id, [bulk_id])[str(bulk_id)])


@router.get("/bulk-uploads")
def list_bulk_uploads(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """The PC's own drops, newest first, each file with where it went; ``inProgress`` counts
    the drops still being processed (the "don't upload new files" message)."""
    authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                 authorization_client=client, permission_key=_READ)
    rows = [dict(r) for r in connection.execute(
        text("SELECT * FROM auditcore.p2_bulk_uploads WHERE tenant_id=:t AND uploaded_by_actor_id=:a "
             "ORDER BY created_at_utc DESC LIMIT :n"),
        {"t": tenant_id, "a": principal.subject, "n": limit},
    ).mappings().all()]
    files = _files(connection, tenant_id, [r["bulk_id"] for r in rows])
    pc_outlets = [o for o in scope_outlets(connection, tenant_id, principal.subject)
                  if "PC" in {str(r).upper() for r in o.get("roles") or []}]
    return {
        "items": [_public_bulk(r, files[str(r["bulk_id"])]) for r in rows],
        "inProgress": sum(1 for r in rows if r["status"] in ("SUBMITTED", "PROCESSING")),
        "multiOutlet": len(pc_outlets) > 1,
    }


# --------------------------------------------------------------------------- jobs

def request_job(connection: Connection, *, kind: str, tenant_id: str, bulk_id: UUID | str, by: str) -> None:
    """A one-off worker job (p2_worker_jobs) for the drop, unless one is already waiting."""
    tag = f"{tenant_id}:{bulk_id}"
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, requested_by)
            SELECT CAST(:k AS varchar), CAST(:tag AS varchar), CAST(:by AS varchar)
            WHERE NOT EXISTS (SELECT 1 FROM auditcore.p2_worker_jobs
                              WHERE job_kind = CAST(:k AS varchar) AND tag = CAST(:tag AS varchar)
                                AND status = 'PENDING')
            """
        ),
        {"k": kind, "tag": tag, "by": by[:200]},
    )


def request_settle(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """From a journey's rule run: a journey read for a drop asks for its drop to be settled.
    One indexed probe for every other journey; never raises."""
    try:
        row = connection.execute(
            text("SELECT bulk_id FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND journey_id=:j "
                 "AND outcome='READING' LIMIT 1"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one_or_none()
        if row is None:
            return False
        request_job(connection, kind=JOB_SETTLE, tenant_id=tenant_id, bulk_id=row, by="system")
        return True
    except Exception:
        logger.warning("p2_bulk_settle_request_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                       exc_info=True)
        return False


def reading_for_bulk(connection: Connection, *, tenant_id: str, journey_ids: list[UUID]) -> set[UUID]:
    """The journeys still being read for a drop (the same-file rule leaves them to BULK_SETTLE)."""
    if not journey_ids:
        return set()
    return {UUID(str(j)) for j in connection.execute(
        text("SELECT DISTINCT journey_id FROM auditcore.p2_bulk_files WHERE tenant_id=:t "
             "AND journey_id = ANY(CAST(:ids AS uuid[])) AND outcome='READING'"),
        {"t": tenant_id, "ids": [str(j) for j in journey_ids]},
    ).scalars().all()}


def _parse_tag(tag: str) -> tuple[str, UUID]:
    tenant_id, sep, bulk = (tag or "").partition(":")
    if not sep:
        raise ValueError("a bulk upload job needs the tag '<tenant_id>:<bulk_id>'")
    return tenant_id.strip(), UUID(bulk.strip())


def _set_file(engine: Engine, tenant_id: str, file_id: Any, **values: Any) -> None:
    sets = ", ".join(f"{k}=:{k}" for k in values)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text(f"UPDATE auditcore.p2_bulk_files SET {sets}, updated_at_utc=now() WHERE tenant_id=:t AND file_id=:f"),
            {**values, "t": tenant_id, "f": file_id},
        )


def _earlier_copy(connection: Connection, tenant_id: str, sha: str, file_id: Any) -> dict[str, Any] | None:
    """The upload (or a drop's file) that is this file, byte for byte, already."""
    row = connection.execute(
        text(
            """
            SELECT b.journey_id, j.journey_reference
            FROM auditcore.p2_upload_batches b
            JOIN auditcore.journeys j ON j.tenant_id=b.tenant_id AND j.journey_id=b.journey_id
            WHERE b.tenant_id=:t AND b.sha256=:s AND b.batch_status <> 'CANCELLED'
              AND b.batch_id <> :own   -- this file's own upload, from a run that was interrupted
            ORDER BY b.created_at_utc LIMIT 1
            """
        ),
        {"t": tenant_id, "s": sha, "own": uuid5(NAMESPACE_URL, f"verigence:p2-bulk:{file_id}")},
    ).mappings().first()
    if row:
        return dict(row)
    row = connection.execute(
        text("SELECT COALESCE(placed_journey_id, journey_id) AS journey_id, placed_reference AS journey_reference "
             "FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND sha256=:s AND file_id <> :f "
             "AND outcome IN ('READING','NEW_BOOKING','ADDED_TO_BOOKING','KEPT_FOR_REVIEW') "
             "ORDER BY created_at_utc LIMIT 1"),
        {"t": tenant_id, "s": sha, "f": file_id},
    ).mappings().first()
    return dict(row) if row else None


def _open_target(engine: Engine, tenant_id: str, journey_id: Any) -> dict[str, Any] | None:
    """The journey a file is to go straight to, when it is still there and still open; else
    None, and the file is read on a journey of its own like any other."""
    if not journey_id:
        return None
    from audit_core.uc03_p2_same_file import _identities

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        me = _identities(connection, tenant_id=tenant_id, journey_ids=[UUID(str(journey_id))]).get(UUID(str(journey_id)))
    if me is None or me.closed:
        return None
    return {"journey_id": me.journey_id, "journey_reference": me.reference}


def _journey_for_group(engine: Engine, tenant_id: str, bulk: dict[str, Any], key: str, names: list[str]) -> UUID:
    """The journey a group is read on: the PC's New booking at the outlet (idempotent per group)."""
    from audit_core.uc03_p2_api import start_p2_journey

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        created = start_p2_journey(
            connection, tenant_id=tenant_id, actor_id=bulk["uploaded_by_actor_id"], outlet_id=bulk["outlet_id"],
            customer_name=None, created_by_name=bulk["uploaded_by_name"],
            idempotency_key=f"p2-bulk:{bulk['bulk_id']}:{key}"[:200],
            started_details={"startedBy": "BULK_UPLOAD", "bulkId": str(bulk["bulk_id"]), "files": names[:20]},
        )
    return UUID(created["journeyId"])


def _upload_to(engine: Engine, tenant_id: str, bulk: dict[str, Any], file: dict[str, Any], journey_id: UUID,
               payload: bytes) -> UUID:
    """The file, byte for byte, uploaded to the journey (split and read like any upload)."""
    from audit_core.uc03_p2_object_keys import original_key, upload_labels
    from audit_core.uc03_p2_runtime import enqueue_work, record_activity
    from audit_core.uc03_p2_storage import get_p2_document_storage

    batch_id = uuid5(NAMESPACE_URL, f"verigence:p2-bulk:{file['file_id']}")
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        labels = upload_labels(connection, tenant_id=tenant_id, journey_id=journey_id)
    key = original_key(tenant_id, journey_id, batch_id, filename=file["original_filename"], outlet_name=labels[0],
                       pc_name=labels[1], customer_name=labels[2])
    get_p2_document_storage().put_object(key, payload, content_type=file["content_type"])
    correlation = f"p2-bulk:{file['file_id']}"
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        inserted = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_upload_batches (
                    tenant_id, batch_id, journey_id, client_upload_id, original_filename, content_type, size_bytes,
                    sha256, page_count, original_object_key, batch_status, uploaded_by_actor_id, correlation_id
                ) VALUES (:t, :b, :j, :client, :name, :ct, :size, :sha, 0, :key, 'UPLOADED', :by, :correlation)
                ON CONFLICT DO NOTHING RETURNING batch_id
                """
            ),
            {"t": tenant_id, "b": batch_id, "j": journey_id, "client": f"bulk-{file['file_id']}",
             "name": str(file["original_filename"])[:250], "ct": file["content_type"], "size": len(payload),
             "sha": file["sha256"], "key": key, "by": bulk["uploaded_by_actor_id"], "correlation": correlation},
        ).scalar_one_or_none()
        if inserted is not None:
            enqueue_work(
                connection, tenant_id=tenant_id, journey_id=journey_id, work_type="SPLIT_BATCH",
                work_key=str(batch_id),
                payload={"batchId": str(batch_id), "uploadedBy": bulk["uploaded_by_actor_id"], "uploadedByRole": "PC"},
                correlation_id=correlation,
            )
            record_activity(
                connection, tenant_id=tenant_id, journey_id=journey_id, event_type="UPLOAD_ACCEPTED",
                subject_type="UPLOAD_BATCH", subject_id=str(batch_id),
                details={"filename": file["original_filename"], "sizeBytes": len(payload), "by": "BULK_UPLOAD",
                         "bulkId": str(bulk["bulk_id"]), "bulkFile": file["object_key"]},
                correlation_id=correlation,
            )
    return batch_id


def _finish_if_settled(engine: Engine, tenant_id: str, bulk_id: UUID) -> bool:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        counts = {r["outcome"]: int(r["n"]) for r in connection.execute(
            text("SELECT outcome, count(*) AS n FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b "
                 "GROUP BY outcome"), {"t": tenant_id, "b": bulk_id},
        ).mappings()}
        done = not any(k not in _SETTLED_OUTCOMES for k in counts)
        connection.execute(
            text("UPDATE auditcore.p2_bulk_uploads SET summary=CAST(:s AS jsonb), updated_at_utc=now(), "
                 "status = CASE WHEN :done THEN 'DONE' ELSE 'PROCESSING' END, "
                 "finished_at_utc = CASE WHEN :done THEN now() END WHERE tenant_id=:t AND bulk_id=:b"),
            {"s": json.dumps({k: n for k, n in counts.items() if k != "REPLACED"}), "done": done,
             "t": tenant_id, "b": bulk_id},
        )
    return done


def run_bulk_upload(engine: Engine, tag: str, tenant_ids: list[str] | None = None) -> dict[str, Any]:
    """BULK_UPLOAD: fingerprint each submitted file, set aside exact duplicates, read the rest
    on a journey per group. Idempotent: a file already handled is skipped."""
    from audit_core.uc03_p2_storage import get_p2_document_storage

    tenant_id, bulk_id = _parse_tag(tag)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        bulk = connection.execute(
            text("UPDATE auditcore.p2_bulk_uploads SET status='PROCESSING', updated_at_utc=now() "
                 "WHERE tenant_id=:t AND bulk_id=:b AND status IN ('SUBMITTED','PROCESSING') RETURNING *"),
            {"t": tenant_id, "b": bulk_id},
        ).mappings().one_or_none()
        if bulk is None:
            return {"bulkId": str(bulk_id), "skipped": "not submitted"}
        bulk = dict(bulk)
        files = [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b AND outcome='PENDING' "
                 "AND upload_status='UPLOADED' ORDER BY created_at_utc, original_filename"),
            {"t": tenant_id, "b": bulk_id},
        ).mappings().all()]
    storage = get_p2_document_storage()
    groups: dict[str, list[dict[str, Any]]] = {}
    duplicates = direct = 0
    for file in files:
        try:
            payload = storage.get_object(str(file["object_key"]))
            file["sha256"] = hashlib.sha256(payload).hexdigest()
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                earlier = _earlier_copy(connection, tenant_id, file["sha256"], file["file_id"])
            if earlier is not None:
                duplicates += 1
                _set_file(engine, tenant_id, file["file_id"], sha256=file["sha256"], outcome="EXACT_DUPLICATE",
                          placed_journey_id=earlier["journey_id"], placed_reference=earlier["journey_reference"],
                          detail="The same file, byte for byte, was already uploaded")
                continue
            target = _open_target(engine, tenant_id, file.get("target_journey_id"))
            if target is not None:
                batch_id = _upload_to(engine, tenant_id, bulk, file, target["journey_id"], payload)
                _set_file(engine, tenant_id, file["file_id"], sha256=file["sha256"], outcome="ADDED_TO_BOOKING",
                          journey_id=target["journey_id"], batch_id=batch_id,
                          placed_journey_id=target["journey_id"], placed_reference=target["journey_reference"],
                          detail=f"Added to {target['journey_reference']}: its customer's folder is this booking's")
                direct += 1
                continue
            key = file.get("group_key") or group_key(file["original_filename"], str(file["file_id"]))
            _set_file(engine, tenant_id, file["file_id"], sha256=file["sha256"], group_key=key)
            groups.setdefault(key, []).append(file)
        except Exception as exc:
            logger.warning("p2_bulk_file_failed", tenant_id=tenant_id, bulk_id=str(bulk_id),
                           file_id=str(file["file_id"]), exc_info=True)
            _set_file(engine, tenant_id, file["file_id"], outcome="FAILED", detail=f"{type(exc).__name__}: {exc}"[:500])
    reading = 0
    for key, members in groups.items():
        try:
            journey_id = _journey_for_group(engine, tenant_id, bulk, key, [m["original_filename"] for m in members])
        except Exception as exc:
            logger.warning("p2_bulk_group_failed", tenant_id=tenant_id, bulk_id=str(bulk_id), group=key, exc_info=True)
            for member in members:
                _set_file(engine, tenant_id, member["file_id"], outcome="FAILED",
                          detail=f"The booking could not be started: {exc}"[:500])
            continue
        for member in members:
            try:
                payload = storage.get_object(str(member["object_key"]))
                batch_id = _upload_to(engine, tenant_id, bulk, member, journey_id, payload)
                _set_file(engine, tenant_id, member["file_id"], outcome="READING", journey_id=journey_id,
                          batch_id=batch_id)
                reading += 1
            except Exception as exc:
                logger.warning("p2_bulk_file_failed", tenant_id=tenant_id, bulk_id=str(bulk_id),
                               file_id=str(member["file_id"]), exc_info=True)
                _set_file(engine, tenant_id, member["file_id"], outcome="FAILED",
                          detail=f"{type(exc).__name__}: {exc}"[:500])
    _finish_if_settled(engine, tenant_id, bulk_id)
    result = {"bulkId": str(bulk_id), "files": len(files), "exactDuplicates": duplicates, "groups": len(groups),
              "reading": reading, "addedToTheirBooking": direct}
    logger.info("p2_bulk_upload_done", tenant_id=tenant_id, **result)
    return result


def _still_reading(connection: Connection, tenant_id: str, journey_id: UUID) -> bool:
    return bool(connection.execute(
        text(
            """
            SELECT EXISTS (SELECT 1 FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j
                           AND batch_status = ANY(:flight))
                OR EXISTS (SELECT 1 FROM auditcore.p2_document_queue WHERE tenant_id=:t AND journey_id=:j
                           AND queue_status = ANY(:waiting))
            """
        ),
        {"t": tenant_id, "j": journey_id, "flight": list(_IN_FLIGHT_BATCHES), "waiting": list(_WAITING_UNITS)},
    ).scalar_one())


def find_booking(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Any | None:
    """The open booking of the same outlet with the same customer as the journey read for the
    drop -- PAN and/or Aadhaar (one read on both and never different), and the same model
    when both are known -- the earliest when several; None when there is none or the
    journey has no PAN / Aadhaar read."""
    from audit_core.uc03_p2_same_file import (
        _SAME_CUSTOMER_SQL,
        _identities,
        same_customer,
        same_model,
        same_outlet,
    )

    me = _identities(connection, tenant_id=tenant_id, journey_ids=[journey_id]).get(journey_id)
    if me is None or not (me.pan or me.aadhaar):
        return None
    ids = [UUID(str(v)) for v in connection.execute(
        text(_SAME_CUSTOMER_SQL), {"tenant_id": tenant_id, "journey_id": journey_id, "pan": me.pan,
                                   "aadhaar": me.aadhaar, "mobile": None, "gstin": None, "dealer_id": None},
    ).scalars().all()]
    others = _identities(connection, tenant_id=tenant_id, journey_ids=ids)
    busy = reading_for_bulk(connection, tenant_id=tenant_id, journey_ids=list(others))
    candidates = [
        o for o in others.values()
        if not o.closed and same_outlet(me, o) and same_customer(me, o) is not None
        and (not (me.model_id or me.model_key) or not (o.model_id or o.model_key) or same_model(me, o))
        # Another file of the drop still being read is joined only when it came first.
        and (o.journey_id not in busy or o.order_key < me.order_key)
    ]
    return min(candidates, key=lambda o: o.order_key) if candidates else None


def find_booking_by_number(connection: Connection, *, tenant_id: str, journey_id: UUID) -> Any | None:
    """For a file whose folder gives a number (a Drive drop): the one open booking of the same
    outlet whose booking form or docket prints that number, or whose customer's mobile it
    is -- used only when the PAN / Aadhaar found none (``find_booking``). Where both journeys
    have a PAN or an Aadhaar they must be the same customer's; none when no booking, or more
    than one, has the number."""
    from audit_core.uc03_duplicate_booking_detection import _normalize_mobile
    from audit_core.uc03_p2_same_file import (
        _identities,
        normalize_booking_reference,
        same_outlet,
    )

    numbers = {code: raw for raw in connection.execute(
        text("SELECT DISTINCT booking_number FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND journey_id=:j "
             "AND outcome='READING' AND booking_number IS NOT NULL"), {"t": tenant_id, "j": journey_id},
    ).scalars().all() if (code := normalize_booking_reference(raw))}
    if len(numbers) != 1:
        return None
    code, raw = next(iter(numbers.items()))
    mobile = _normalize_mobile(raw)
    me = _identities(connection, tenant_id=tenant_id, journey_ids=[journey_id]).get(journey_id)
    if me is None or me.outlet_id is None:
        return None
    digits = max((run for run in code.split("-") if run.isdigit()), key=len, default="")
    if not digits:
        return None
    ids = [UUID(str(v)) for v in connection.execute(
        text(
            """
            SELECT DISTINCT f.journey_id
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.journeys j ON j.tenant_id=f.tenant_id AND j.journey_id=f.journey_id
            WHERE f.tenant_id=:t AND f.journey_id <> :j AND j.outlet_id=:o
              AND f.field_key IN ('booking_reference_number', 'customer_phone')
              AND regexp_replace(COALESCE(f.effective_value #>> '{}', ''), '[^0-9]', '', 'g') LIKE :digits
            """
        ),
        {"t": tenant_id, "j": journey_id, "o": me.outlet_id, "digits": f"%{digits}%"},
    ).scalars().all()]
    others = _identities(connection, tenant_id=tenant_id, journey_ids=ids)
    busy = reading_for_bulk(connection, tenant_id=tenant_id, journey_ids=list(others))

    def another_customer(o: Any) -> bool:
        return bool((me.pan and o.pan and me.pan != o.pan) or (me.aadhaar and o.aadhaar and me.aadhaar != o.aadhaar))

    candidates = [
        o for o in others.values()
        if not o.closed and same_outlet(me, o) and not another_customer(o)
        and (code in o.booking_refs or (mobile is not None and o.mobile == mobile))
        and (o.journey_id not in busy or o.order_key < me.order_key)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _folder_numbers(connection: Connection, tenant_id: str, journey_id: UUID) -> frozenset[str]:
    """The booking numbers the drop's folder names gave this journey's files, normalised."""
    from audit_core.uc03_p2_same_file import normalize_booking_reference

    return frozenset(code for raw in connection.execute(
        text("SELECT DISTINCT booking_number FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND journey_id=:j "
             "AND booking_number IS NOT NULL"), {"t": tenant_id, "j": journey_id},
    ).scalars().all() if (code := normalize_booking_reference(raw)))


BULK_JOIN_KIND = "BULK_JOIN"
_JOIN_REASON = "Bulk upload: read and joined to the customer's booking "
# Removing the read journey after its pages were carried is tried again at each settle; after
# this many failed tries the files are recorded on the booking anyway (their pages are there).
_JOIN_DELETE_TRIES = 5


def _earlier_join(connection: Connection, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    """The join a settle already started for this read journey -- its pages carried to the
    customer's booking, then the read journey's deletion begun -- with the booking it went to
    (``kept_journey_id``; for joins recorded before 2026-11-20, found from the reason's
    reference), whether the journey is gone, and how many deletion tries failed."""
    rows = connection.execute(
        text(
            """
            SELECT deletion_status, kept_journey_id, kept_journey_reference, reason
            FROM auditcore.p2_journey_deletions
            WHERE tenant_id=:t AND journey_id=:j AND deleted_by_role='SYSTEM' AND reason LIKE :like
            ORDER BY requested_at_utc DESC
            """
        ),
        {"t": tenant_id, "j": journey_id, "like": _JOIN_REASON + "%"},
    ).mappings().all()
    if not rows:
        return None
    kept_id = next((r["kept_journey_id"] for r in rows if r["kept_journey_id"]), None)
    kept_ref = next((r["kept_journey_reference"] for r in rows if r["kept_journey_reference"]), None)
    if kept_ref is None:
        kept_ref = rows[0]["reason"][len(_JOIN_REASON):].split()[0].rstrip(".,;:") or None
    if kept_id is None and kept_ref:
        kept_id = connection.execute(
            text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_reference=:r"),
            {"t": tenant_id, "r": kept_ref},
        ).scalar_one_or_none()
    gone = connection.execute(
        text("SELECT NOT EXISTS (SELECT 1 FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j)"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    return {"kept_journey_id": kept_id, "kept_reference": kept_ref,
            "gone": bool(gone) or any(r["deletion_status"] == "COMPLETED" for r in rows),
            "failed": sum(1 for r in rows if r["deletion_status"] == "FAILED")}


def run_bulk_settle(engine: Engine, tag: str, tenant_ids: list[str] | None = None) -> dict[str, Any]:
    """BULK_SETTLE: each journey of the drop that has finished reading joins the customer's
    booking (its new pages carried, the journey deleted) or stays as the new booking."""
    from audit_core.uc03_p2_journey_delete import system_delete_journey
    from audit_core.uc03_p2_same_file import (
        _identities,
        booking_identified,
        carry_documents,
        carry_plan,
    )
    from audit_core.uc03_p2_storage import get_p2_document_storage
    from audit_core.uc03_p2_worker import (
        get_di_capture_v2_client,
        get_security_oauth_client,
    )

    tenant_id, bulk_id = _parse_tag(tag)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys = [UUID(str(j)) for j in connection.execute(
            text("SELECT DISTINCT journey_id FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND bulk_id=:b "
                 "AND outcome='READING' AND journey_id IS NOT NULL"), {"t": tenant_id, "b": bulk_id},
        ).scalars().all()]
    settled: dict[str, int] = {}
    files_sql = ("UPDATE auditcore.p2_bulk_files SET outcome=:o, placed_journey_id=:p, placed_reference=:r, "
                 "detail=:d, updated_at_utc=now() WHERE tenant_id=:t AND journey_id=:j AND outcome='READING'")

    def record(journey_id: UUID, outcome: str, placed: Any, ref: Any, detail: str) -> None:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            connection.execute(text(files_sql), {"o": outcome, "p": placed, "r": ref, "d": detail[:500],
                                                 "t": tenant_id, "j": journey_id})
        settled[outcome] = settled.get(outcome, 0) + 1
        logger.info("p2_bulk_journey_settled", tenant_id=tenant_id, bulk_id=str(bulk_id), journey_id=str(journey_id),
                    outcome=outcome, target=str(placed))

    def remove_read_journey(journey_id: UUID, target_id: Any, target_ref: Any) -> None:
        system_delete_journey(
            engine, tenant_id=tenant_id, journey_id=journey_id, reason=f"{_JOIN_REASON}{target_ref}",
            correlation_id=f"p2-bulk-settle:{journey_id}", security_client=get_security_oauth_client(),
            v2_client=get_di_capture_v2_client(), storage=get_p2_document_storage(),
            record_details=lambda _c: {"deletion_kind": BULK_JOIN_KIND, "kept_journey_id": target_id,
                                       "kept_journey_reference": target_ref},
        )

    for journey_id in journeys:
        # A join an earlier settle started (a worker restart, or the read journey's deletion
        # failing part way after its pages were carried): its pages are on the customer's
        # booking -- recorded there, the deletion finished first if it was not. Never recorded
        # on the deleted journey (19 Nov review: 9 files said "kept for review" on journeys
        # already gone, and the Drive folders learnt those journeys).
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            joined = _earlier_join(connection, tenant_id, journey_id)
        if joined is not None and joined["kept_journey_id"] is not None:
            target_id, target_ref = joined["kept_journey_id"], joined["kept_reference"]
            if not joined["gone"]:
                try:
                    remove_read_journey(journey_id, target_id, target_ref)
                except Exception:
                    logger.warning("p2_bulk_join_delete_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                                   exc_info=True)
                    if joined["failed"] + 1 < _JOIN_DELETE_TRIES:
                        continue  # tried again at the next settle
            record(journey_id, "ADDED_TO_BOOKING", target_id, target_ref,
                   f"Added to {target_ref}: its pages were carried there"
                   + ("" if joined["gone"] else " (the read journey could not be removed)"))
            continue
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            if _still_reading(connection, tenant_id, journey_id):
                continue
            target = (find_booking(connection, tenant_id=tenant_id, journey_id=journey_id)
                      or find_booking_by_number(connection, tenant_id=tenant_id, journey_id=journey_id))
            me = _identities(connection, tenant_id=tenant_id, journey_ids=[journey_id]).get(journey_id)
            unsure = (carry_plan(connection, tenant_id=tenant_id, new=me, existing=target).unsure
                      if target is not None and me is not None else None)
            # Joined (and the read journey deleted) only on the same OTF (product owner 2026-11-14:
            # "delete should happen only for journey which has same OTF"); otherwise it stays a
            # booking of its own, which the duplicate-booking check flags for the Team Lead.
            if target is not None and me is not None and not unsure and not (
                    booking_identified(me, target)
                    or _folder_numbers(connection, tenant_id, journey_id) & target.booking_refs):
                unsure = "no booking reference number (OTF) in common"
        if target is None or me is None:
            outcome, placed, ref = "NEW_BOOKING", journey_id, me.reference if me else None
            detail = "No open booking of this customer at the outlet: a new booking"
        elif unsure:
            outcome, placed, ref = "KEPT_FOR_REVIEW", journey_id, me.reference
            detail = f"Kept for review next to {target.reference}: {unsure}"
        else:
            try:
                carried = carry_documents(engine, tenant_id=tenant_id, new_journey_id=journey_id,
                                          existing_journey_id=target.journey_id)
            except Exception as exc:  # nothing carried: the read journey and its pages stay
                logger.warning("p2_bulk_join_failed", tenant_id=tenant_id, journey_id=str(journey_id), exc_info=True)
                outcome, placed, ref = "KEPT_FOR_REVIEW", journey_id, me.reference
                detail = f"Kept for review next to {target.reference}: it could not be joined ({type(exc).__name__})"
            else:
                # The pages are on the booking now. Removing the read journey can fail part way
                # (Document Intelligence, R2): the files then stay READING, and the next settle
                # finishes the removal and records them on the booking (_earlier_join).
                try:
                    remove_read_journey(journey_id, target.journey_id, target.reference)
                except Exception:
                    logger.warning("p2_bulk_join_delete_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                                   exc_info=True)
                    continue
                outcome, placed, ref = "ADDED_TO_BOOKING", target.journey_id, target.reference
                detail = (f"Added to {target.reference}: {', '.join(carried).replace('_', ' ')}" if carried
                          else f"Nothing new for {target.reference}: every page was already on it")
        record(journey_id, outcome, placed, ref, detail)
    done = _finish_if_settled(engine, tenant_id, bulk_id)
    if not done:
        # Some journeys are still being read: settled again shortly (a job is polled each minute).
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            request_job(connection, kind=JOB_SETTLE, tenant_id=tenant_id, bulk_id=bulk_id, by="system")
    return {"bulkId": str(bulk_id), "settled": settled, "done": done,
            "finishedAtUtc": datetime.now(UTC).isoformat() if done else None}
