"""Journey files taken from the Project's Google Drive (product owner, 7 and 12 Nov 2026).

"PC will upload the files into the Google Drive ... files have to come outlet wise
only ... end of the day or explicit submit button by PC." And on 12 Nov: "can't we
keep it simple: it will be customername-otf for Mahindra and customername-mobileno
for Hyundai; the PC can keep uploading the files in it; the system can check what
all has been uploaded already and then continue from there." A date folder was
dropped the same day: the PC does not know when a booking was made, so a folder
filed under a date cannot be found again.

The folders (the Project's own Google account, google_drive)
    <Project> / <Dealership> / <Outlet> / <Name-Number> / ...
    Verigence makes every level down to the outlet, from its own masters
    (``ensure_tree``): a journey belongs to an outlet, and a name typed by hand need
    not be the master's. An outlet has one folder and no two outlets share one
    (product owner 2026-11-12: "each outlet should have one folder ... no two
    outlets share one folder"): the PCs of an outlet all work in it, and a PC
    mapped to several outlets works in each outlet's own folder. The PCs do not
    hold the account's password ("I need option B"): each outlet's folder is
    shared with the Google account of every PC mapped to it -- their Verigence
    sign-in email -- once the PC has asked for it on Drive uploads, and the
    sharing is taken away when the PC is no longer mapped (``ensure_shares``).
    The PC makes only the customer's folder -- the name and the booking / OTF
    number or the mobile number, in either order -- and keeps putting that
    customer's files in it: the booking papers, later the delivery file, later
    whatever was missing. A folder inside it whose name starts with "photo" or "pic"
    holds photos and other papers that are kept on the journey and not read;
    everything else in the customer's folder is read as documents, whatever its
    sub-folder is called.

The record (p2_drive_folders, p2_drive_files, p2_drive_runs)
    Drive is the inbox; these tables and the copy in storage are the record. Every
    file is known by its Drive id and its content (MD5): what it is, whose folder it
    is in, when it arrived, and what became of it.

How a new file is found (``sync``)
    Google keeps a feed of everything added, changed or removed in the account's
    Drive. Each round asks for what came after the place it last read to, so a file
    added to a folder made weeks ago is found exactly as a new folder is, and the
    cost does not grow with the Drive. A file already taken (same id, same content)
    is never taken again; the same file put in again under another name is caught by
    the bulk upload's own fingerprint and set aside as a duplicate.

Whose journey a folder is (``journey_for_folder``), known before anything is read
    The journey its files went to before. Else another folder of the same outlet
    with the same number (a PC who made the folder again). Else the one journey of
    the outlet whose booking form or docket prints that number, or whose customer's
    mobile is that number -- and whose customer is not plainly someone else by name.
    When none is found the files are read on a journey of their own, and the bulk
    upload joins it to the customer's open booking by PAN / Aadhaar, or it is the
    new booking. Files for a journey that is closed are not taken: they are set
    aside and listed for the Team Lead.

Taking the files (job DRIVE_RUN, ``run_drive_intake``)
    A run is asked for by the PC ("Submit Drive uploads", for an outlet they upload
    for), by the day's own run (at the time the Project set, 21:00 IST to begin with,
    for the outlets it uses Drive for -- every outlet unless it chose some; it can be
    switched off), or on demand by a Team
    Lead, PM, Executive or Reviewer for a day of upload and an outlet, a dealership
    or all they are mapped to. Each waiting file is copied to storage under
    dealership / outlet / day / customer's folder / its own name, and only then
    handed on:
      documents  become a bulk upload of the outlet's PC (uc03_p2_bulk_upload).
      photos     are added to the journey's photos (uc03_p2_vehicle_photos), once
                 the folder's journey is known; until then they wait.
    Whose the files are: the PC folder they are in, else the outlet's only PC, else
    the PC who pressed Submit, else the PC mapped to the outlet first.

Archive (``archive_closed``)
    Nothing is deleted. Once a journey has been closed for the Project's retention
    period (a setting, 30 days to begin with), its customer's folder is moved to
    <Project> / Archive / <yyyy-mm> / <Outlet>.

The report of a run (``run_report``), and the Project Tracker's Drive figures
    (``tracker_rows``): files received, taken, duplicates, set aside; pages read;
    pages to scan again, each with its file, page number and reason; manual
    verification tasks opened.

Nothing here runs until the Project's Google account is connected
(``/integrations/google-drive``): no variables, no connection, no work.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, Engine, text

from audit_core import google_drive
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.google_drive import (
    FOLDER_TYPE,
    DriveAuthorizationLost,
    DriveClient,
    DriveError,
    DriveNotFound,
    DriveSettings,
)
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_gate_pass import scope_outlets
from audit_core.uc03_p2_object_keys import IST, safe_part

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/p2/v1", tags=["uc03-phase2-drive-intake"])

JOB_RUN = "DRIVE_RUN"
END_OF_DAY_RUN = "P2_DRIVE_END_OF_DAY"
ARCHIVE_RUN = "P2_DRIVE_ARCHIVE"
END_OF_DAY_IST = time(21, 0)
_END_OF_DAY_CATCHUP = timedelta(hours=3)
SYSTEM_ACTOR = "system"
ARCHIVE_NAME = "Archive"
_READ = "audit.journey.read"
_UPDATE = "audit.journey.update"
_ADMIN_ROLES = ("SUPER_ADMIN", "TENANT_ADMIN")
_LEAD_ROLES = frozenset({"TL", "PM", "EXECUTIVE", "EXEC", "REVIEWER"})
DOCUMENT_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_STALE_CLAIM = timedelta(hours=2)
_NAME_NOISE = frozenset({"OTF", "BOOKING", "BKG", "NO", "NUMBER", "DELIVERY", "FILE", "FILES", "CUSTOMER", "MOBILE",
                         "MOB", "PH", "PHONE"})
_PHOTO_FOLDER = re.compile(r"^\s*(photo|pic)", re.IGNORECASE)
_PLACED_BULK = ("NEW_BOOKING", "ADDED_TO_BOOKING", "KEPT_FOR_REVIEW")
# Closed as the same-file rule means it (uc03_p2_same_file._CLOSED_SQL), with when.
_CLOSED_AT_SQL = """
    SELECT max(COALESCE(st.business_completed_at_utc, st.closed_at_utc, st.updated_at_utc))
    FROM auditcore.journey_stage_states st
    WHERE st.tenant_id=cf.tenant_id AND st.journey_id=cf.journey_id
      AND ((st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
           OR (st.stage_code='BOOKING'
               AND (st.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                    OR st.closure_disposition='NO_DELIVERY')))
"""


# --------------------------------------------------------------------------- names

def ist_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(IST).date()


def parse_customer_folder(name: str) -> tuple[str | None, str | None]:
    """(customer name, number) of a customer's folder, in either order: "Akansh-456909",
    "456909 Akansh Kumar", "OTF-BK12345-Ravi", "Ravi Das-9876543210". The number is the
    part with the most digits (three at least) -- the booking / OTF number, or the mobile
    number where the make gives no OTF; the name is the words left."""
    parts = [p for p in re.split(r"[-_\s,.]+", str(name or "").strip()) if p]
    numbered = [p for p in parts if sum(ch.isdigit() for ch in p) >= 3]
    number = max(numbered, key=lambda p: sum(ch.isdigit() for ch in p)) if numbered else None
    words = [p for p in parts if p not in numbered and p.upper() not in _NAME_NOISE
             and any(ch.isalpha() for ch in p)]
    return (" ".join(words)[:300] or None), (number[:60] if number else None)


def number_key(number: Any) -> str | None:
    """A folder's number in one spelling, as a booking reference is compared
    (uc03_p2_same_file.normalize_booking_reference): "bk/0012" and "BK12" are one."""
    from audit_core.uc03_p2_same_file import normalize_booking_reference

    return normalize_booking_reference(number)


def mobile_of(number: Any) -> str | None:
    """The mobile number a folder's number is, when it is one (uc03_duplicate_booking_detection)."""
    from audit_core.uc03_duplicate_booking_detection import _normalize_mobile

    return _normalize_mobile(number)


def names_differ(left: Any, right: Any) -> bool:
    """Two names that are plainly two people: both given, and no word of three letters or
    more in common ("Akansh" and "Akansh Kumar Das" are not; "Ravi" and "Sunita" are)."""
    def words(value: Any) -> set[str]:
        return {w for w in re.sub(r"[^a-z]+", " ", str(value or "").lower()).split() if len(w) >= 3}

    a, b = words(left), words(right)
    return bool(a and b and not (a & b))


def is_photo_folder(name: str) -> bool:
    return bool(_PHOTO_FOLDER.match(str(name or "")))


_SYSTEM_NAMES = frozenset({"thumbs.db", "desktop.ini", "__macosx"})


def is_system_file(name: str) -> bool:
    """A file a computer leaves next to the PC's files (.DS_Store and the like from a Mac,
    Thumbs.db from Windows): never a document, never shown as "not taken" (12 Nov 2026)."""
    plain = str(name or "").strip()
    return plain.startswith(".") or plain.lower() in _SYSTEM_NAMES


def _plain(name: Any, fallback: str) -> str:
    """A name as it was given, without what a storage key cannot hold."""
    value = "".join(ch for ch in str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
                    if ch >= " " and ch not in '"<>|?*').strip()
    return value[:200] or fallback


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------- the connection

def open_client(settings: DriveSettings, refresh_token: str) -> DriveClient:
    """The Drive of the connected account (a seam: the tests put a double here)."""
    return DriveClient(settings, refresh_token)


def _connection_row(connection: Connection, tenant_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_drive_connections WHERE tenant_id=:t"), {"t": tenant_id},
    ).mappings().one_or_none()
    return dict(row) if row else None


def connected_client(engine: Engine, tenant_id: str) -> tuple[DriveClient, dict[str, Any]] | None:
    """The Project's Drive and its connection, or None while it is not connected (or the
    feature is not configured)."""
    settings = google_drive.drive_settings()
    if settings is None:
        return None
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        row = _connection_row(connection, tenant_id)
    if row is None or row["connection_status"] != "CONNECTED":
        return None
    return open_client(settings, google_drive.unseal(settings, row["refresh_token_sealed"])), row


def _note_connection(engine: Engine, tenant_id: str, *, error: str | None, lost: bool = False) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_drive_connections SET last_error=:e, updated_at_utc=now(), "
                 "connection_status = CASE WHEN :lost THEN 'ERROR' ELSE connection_status END WHERE tenant_id=:t"),
            {"e": error[:500] if error else None, "lost": lost, "t": tenant_id},
        )


def connected_tenants(engine: Engine, tenant_ids: list[str]) -> list[str]:
    out = []
    for tenant_id in tenant_ids:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            if connection.execute(
                text("SELECT 1 FROM auditcore.p2_drive_connections WHERE tenant_id=:t "
                     "AND connection_status='CONNECTED'"), {"t": tenant_id},
            ).first() is not None:
                out.append(tenant_id)
    return out


# --------------------------------------------------------------------------- outlets and their PCs

def outlets_with_pcs(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Every outlet of the Project with the PCs mapped to it now, the first mapped first."""
    rows = connection.execute(
        text(
            """
            SELECT o.dealer_id, d.dealer_name, o.outlet_id, o.outlet_name,
                   ba.security_actor_id AS actor_id, pn.display_name, ba.effective_from
            FROM auditcore.dealer_outlets o
            JOIN auditcore.dealers d ON d.tenant_id=o.tenant_id AND d.dealer_id=o.dealer_id
            LEFT JOIN auditcore.business_assignments ba
              ON ba.tenant_id=o.tenant_id AND upper(ba.business_role_code)='PC' AND ba.assignment_status='ACTIVE'
             AND ba.effective_from <= now() AND (ba.effective_to IS NULL OR ba.effective_to >= now())
             AND (ba.dealer_id IS NULL
                  OR (o.dealer_id=ba.dealer_id AND (ba.outlet_id IS NULL OR o.outlet_id=ba.outlet_id)))
            LEFT JOIN auditcore.people_names pn ON pn.actor_id=ba.security_actor_id
            WHERE o.tenant_id=:t
            ORDER BY lower(d.dealer_name), lower(o.outlet_name), ba.effective_from, ba.security_actor_id
            """
        ),
        {"t": tenant_id},
    ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        node = out.setdefault(str(r["outlet_id"]), {
            "dealer_id": r["dealer_id"], "dealer_name": r["dealer_name"], "outlet_id": r["outlet_id"],
            "outlet_name": r["outlet_name"], "pcs": [],
        })
        if r["actor_id"] and all(p["actor_id"] != r["actor_id"] for p in node["pcs"]):
            node["pcs"].append({"actor_id": str(r["actor_id"]), "name": r["display_name"]})
    return list(out.values())


def uses_drive(link: dict[str, Any], outlet_id: Any) -> bool:
    """Whether the outlet is one the Project uses Drive for (every outlet, unless it chose some)."""
    chosen = link.get("outlet_ids")
    return chosen is None or str(outlet_id) in {str(o) for o in chosen}


def attributed_pc(outlet: dict[str, Any] | None, folder_pc: str | None, submitter: str | None) -> dict[str, Any] | None:
    """Whose the files of an outlet are: the PC whose Google account put the file in Drive
    (``folder_pc``: the PC recorded on the file, by the file's owner), else the outlet's only
    PC, else the PC who pressed Submit, else the PC mapped to the outlet first."""
    pcs = (outlet or {}).get("pcs") or []
    by_id = {p["actor_id"]: p for p in pcs}
    if folder_pc and folder_pc in by_id:
        return by_id[folder_pc]
    if len(pcs) == 1:
        return pcs[0]
    if submitter and submitter in by_id:
        return by_id[submitter]
    return pcs[0] if pcs else None


# --------------------------------------------------------------------------- the folders Verigence makes

_FOLDER_COLUMNS = ("drive_folder_id", "folder_kind", "parent_folder_id", "folder_name", "dealer_id", "outlet_id",
                   "folder_date", "pc_actor_id", "customer_folder_id", "photo_lane", "customer_name", "booking_number",
                   "number_key", "made_by_system")


def _folder(connection: Connection, tenant_id: str, folder_id: Any) -> dict[str, Any] | None:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_drive_folders WHERE tenant_id=:t AND drive_folder_id=:f"),
        {"t": tenant_id, "f": folder_id},
    ).mappings().one_or_none()
    return dict(row) if row else None


def _save_folder(connection: Connection, tenant_id: str, **values: Any) -> dict[str, Any]:
    row = {c: values.get(c) for c in _FOLDER_COLUMNS}
    row["photo_lane"] = bool(row["photo_lane"])
    row["made_by_system"] = bool(row["made_by_system"])
    row["folder_name"] = str(row["folder_name"] or "")[:500]
    saved = connection.execute(
        text(
            f"""
            INSERT INTO auditcore.p2_drive_folders (tenant_id, {", ".join(_FOLDER_COLUMNS)})
            VALUES (:t, {", ".join(":" + c for c in _FOLDER_COLUMNS)})
            ON CONFLICT (tenant_id, drive_folder_id) DO UPDATE SET
                folder_kind=EXCLUDED.folder_kind, parent_folder_id=EXCLUDED.parent_folder_id,
                folder_name=EXCLUDED.folder_name, dealer_id=EXCLUDED.dealer_id, outlet_id=EXCLUDED.outlet_id,
                folder_date=EXCLUDED.folder_date, pc_actor_id=EXCLUDED.pc_actor_id,
                customer_folder_id=EXCLUDED.customer_folder_id, photo_lane=EXCLUDED.photo_lane,
                customer_name=EXCLUDED.customer_name, booking_number=EXCLUDED.booking_number,
                number_key=EXCLUDED.number_key, gone_at_utc=NULL, updated_at_utc=now()
            RETURNING *
            """
        ),
        {"t": tenant_id, **row},
    ).mappings().one()
    return dict(saved)


def _made(connection: Connection, tenant_id: str, kind: str, **keys: Any) -> dict[str, Any] | None:
    """The folder Verigence made for these keys (a dealership, an outlet, an archive month)."""
    conditions = " AND ".join(
        f"{column} IS NULL" if value is None else f"{column} = :{column}" for column, value in keys.items())
    row = connection.execute(
        text(f"SELECT * FROM auditcore.p2_drive_folders WHERE tenant_id=:t AND folder_kind=:k AND made_by_system "
             f"AND gone_at_utc IS NULL AND {conditions} ORDER BY created_at_utc LIMIT 1"),
        {"t": tenant_id, "k": kind, **{c: v for c, v in keys.items() if v is not None}},
    ).mappings().first()
    return dict(row) if row else None


class _ParentGone(RuntimeError):
    def __init__(self, folder_id: str) -> None:
        super().__init__(folder_id)
        self.folder_id = folder_id


def _ensure(connection: Connection, tenant_id: str, client: DriveClient, kind: str, parent_id: str, name: str,
            **keys: Any) -> tuple[str, bool]:
    """(the folder's id, whether it was made now)."""
    found = _made(connection, tenant_id, kind, **keys)
    if found is not None:
        return str(found["drive_folder_id"]), False
    try:
        created = client.create_folder(name, parent_id)
    except DriveNotFound as exc:
        raise _ParentGone(parent_id) from exc
    _save_folder(connection, tenant_id, drive_folder_id=created["id"], folder_kind=kind, parent_folder_id=parent_id,
                 folder_name=name, made_by_system=True, **keys)
    return str(created["id"]), True


def ensure_tree(engine: Engine, tenant_id: str, client: DriveClient) -> dict[str, int]:
    """The Project's folders down to every outlet that has a PC -- one folder an outlet,
    however many PCs it has: made once, found by what they are for (not by their name), so
    a folder renamed in Drive is still the outlet's, and an outlet added later gets its
    folder on the next round. One worker at a time (the others skip the round)."""
    try:
        return _ensure_tree(engine, tenant_id, client)
    except _ParentGone as gone:
        # A folder Verigence made was deleted in Drive: forgotten, and made again next round.
        _removed(engine, tenant_id, gone.folder_id)
        logger.warning("p2_drive_folder_gone", tenant_id=tenant_id, folder_id=gone.folder_id)
        return {"made": 0, "gone": 1}


def _root(connection: Connection, tenant_id: str, client: DriveClient, link: dict[str, Any]) -> tuple[str, bool]:
    if link["root_folder_id"]:
        return str(link["root_folder_id"]), False
    project = connection.execute(
        text("SELECT project_name FROM auditcore.projects WHERE tenant_id=:t"), {"t": tenant_id},
    ).scalar_one_or_none() or "Verigence"
    created = client.create_folder(str(project), None)
    _save_folder(connection, tenant_id, drive_folder_id=created["id"], folder_kind="PROJECT",
                 folder_name=str(project), made_by_system=True)
    connection.execute(
        text("UPDATE auditcore.p2_drive_connections SET root_folder_id=:r, updated_at_utc=now() WHERE tenant_id=:t"),
        {"r": created["id"], "t": tenant_id},
    )
    return str(created["id"]), True


def _ensure_tree(engine: Engine, tenant_id: str, client: DriveClient) -> dict[str, int]:
    made = 0
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if not connection.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": f"p2-drive-tree:{tenant_id}"},
        ).scalar_one():
            return {"made": 0, "skipped": 1}
        link = _connection_row(connection, tenant_id)
        if link is None:
            return {"made": 0}
        root, new = _root(connection, tenant_id, client, link)
        made += new
        for outlet in outlets_with_pcs(connection, tenant_id):
            if not outlet["pcs"] or not uses_drive(link, outlet["outlet_id"]):
                continue
            dealer, new = _ensure(connection, tenant_id, client, "DEALER", root, str(outlet["dealer_name"]),
                                  dealer_id=outlet["dealer_id"], outlet_id=None)
            made += new
            place = {"dealer_id": outlet["dealer_id"], "outlet_id": outlet["outlet_id"]}
            _, new = _ensure(connection, tenant_id, client, "OUTLET", dealer, str(outlet["outlet_name"]), **place)
            made += new
        retired = _retire_pc_folders(connection, tenant_id, client)
    return {"made": made, "retired": retired} if retired else {"made": made}


def _retire_pc_folders(connection: Connection, tenant_id: str, client: DriveClient) -> int:
    """The folders per PC that Verigence made inside two-PC outlets for a few hours on
    12 Nov 2026, before the product owner's "each outlet should have one folder": whatever
    was put in one moves up into the outlet's folder (same Drive ids, so the record
    follows), and the folder, once empty, goes to Drive's bin. One that cannot be emptied
    or has lost its outlet folder is left for the next round."""
    retired = 0
    rows = connection.execute(
        text("SELECT pc.drive_folder_id, pc.parent_folder_id FROM auditcore.p2_drive_folders pc "
             "JOIN auditcore.p2_drive_folders o ON o.tenant_id=pc.tenant_id AND o.drive_folder_id=pc.parent_folder_id "
             " AND o.folder_kind='OUTLET' AND o.gone_at_utc IS NULL "
             "WHERE pc.tenant_id=:t AND pc.folder_kind='PC' AND pc.made_by_system AND pc.gone_at_utc IS NULL"),
        {"t": tenant_id},
    ).mappings().all()
    for row in rows:
        folder_id, outlet_folder = str(row["drive_folder_id"]), str(row["parent_folder_id"])
        try:
            for child in client.children(folder_id):
                client.move(str(child["id"]), outlet_folder, folder_id)
                connection.execute(
                    text("UPDATE auditcore.p2_drive_folders SET parent_folder_id=:o, pc_actor_id=NULL, "
                         "updated_at_utc=now() WHERE tenant_id=:t AND drive_folder_id=:f"),
                    {"t": tenant_id, "o": outlet_folder, "f": str(child["id"])},
                )
            binned = client.bin_folder(folder_id)
        except DriveNotFound:
            binned = True  # already gone from Drive
        except DriveAuthorizationLost:
            raise
        except DriveError as exc:  # left as it is; the round goes on and tries again next time
            logger.warning("p2_drive_pc_folder_not_retired", tenant_id=tenant_id, folder_id=folder_id, error=str(exc))
            continue
        if not binned:
            continue
        connection.execute(
            text("UPDATE auditcore.p2_drive_folders SET gone_at_utc=now(), updated_at_utc=now() "
                 "WHERE tenant_id=:t AND drive_folder_id=:f"), {"t": tenant_id, "f": folder_id},
        )
        retired += 1
    return retired


# --------------------------------------------------------------------------- the PCs' own Google accounts

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SHARE_RETRY = timedelta(hours=1)


def _people(connection: Connection, tenant_id: str) -> dict[str, str]:
    """The Google account each person uses for Drive, by actor."""
    return {str(r["actor_id"]): str(r["google_email"]) for r in connection.execute(
        text("SELECT actor_id, google_email FROM auditcore.p2_drive_people WHERE tenant_id=:t"), {"t": tenant_id},
    ).mappings()}


def _shares(connection: Connection, tenant_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    return {(str(r["drive_folder_id"]), str(r["actor_id"])): dict(r) for r in connection.execute(
        text("SELECT * FROM auditcore.p2_drive_shares WHERE tenant_id=:t"), {"t": tenant_id},
    ).mappings()}


def _shared(row: dict[str, Any] | None) -> bool:
    return bool(row and row["permission_id"] and row["removed_at_utc"] is None)


def _outlet_folders(connection: Connection, tenant_id: str, link: dict[str, Any]) -> list[dict[str, Any]]:
    """Every outlet that uses Drive and has its folder, with the folder's id."""
    out = []
    for outlet in outlets_with_pcs(connection, tenant_id):
        if not outlet["pcs"] or not uses_drive(link, outlet["outlet_id"]):
            continue
        folder = _made(connection, tenant_id, "OUTLET", dealer_id=outlet["dealer_id"], outlet_id=outlet["outlet_id"])
        if folder is not None:
            out.append({**outlet, "folder_id": str(folder["drive_folder_id"])})
    return out


def ensure_shares(engine: Engine, tenant_id: str, client: DriveClient, only_actor: str | None = None) -> dict[str, int]:
    """Each outlet's folder is shared with the Google account of every PC mapped to the outlet
    who has asked for it, and with nobody Verigence shared it with before who is no longer
    mapped (product owner 2026-11-12, option B). Google emails the PC the link when the
    folder is first shared. A sharing Google refused is kept with the reason and tried again
    after an hour. ``only_actor``: one person's folders only (they have just asked)."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        link = _connection_row(connection, tenant_id)
        if link is None:
            return {"shared": 0, "removed": 0, "refused": 0}
        people, before = _people(connection, tenant_id), _shares(connection, tenant_id)
        wanted = {(o["folder_id"], pc["actor_id"]): (people[pc["actor_id"]], o["outlet_id"])
                  for o in _outlet_folders(connection, tenant_id, link) for pc in o["pcs"] if pc["actor_id"] in people}
    counts = {"shared": 0, "removed": 0, "refused": 0}
    locked: set[str] = {folder_id for (folder_id, _), row in before.items() if row["permission_id"]}
    now = datetime.now(UTC)

    def save(folder_id: str, actor_id: str, **values: Any) -> None:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_drive_shares (tenant_id, drive_folder_id, actor_id, outlet_id,
                        google_email, permission_id, last_error, shared_at_utc, removed_at_utc)
                    VALUES (:t, :f, :a, :outlet_id, :google_email, :permission_id, :last_error, :shared_at_utc,
                            :removed_at_utc)
                    ON CONFLICT (tenant_id, drive_folder_id, actor_id) DO UPDATE SET
                        outlet_id=EXCLUDED.outlet_id, google_email=EXCLUDED.google_email,
                        permission_id=EXCLUDED.permission_id, last_error=EXCLUDED.last_error,
                        shared_at_utc=EXCLUDED.shared_at_utc, removed_at_utc=EXCLUDED.removed_at_utc,
                        updated_at_utc=now()
                    """
                ),
                {"t": tenant_id, "f": folder_id, "a": actor_id, **values},
            )

    def take_away(folder_id: str, actor_id: str, row: dict[str, Any]) -> bool:
        try:
            client.unshare(folder_id, str(row["permission_id"]))
        except DriveAuthorizationLost:
            raise
        except DriveError as exc:
            logger.warning("p2_drive_unshare_failed", tenant_id=tenant_id, folder_id=folder_id, error=str(exc))
            return False
        save(folder_id, actor_id, outlet_id=row["outlet_id"], google_email=row["google_email"], permission_id=None,
             last_error=None, shared_at_utc=row["shared_at_utc"], removed_at_utc=now)
        counts["removed"] += 1
        return True

    for (folder_id, actor_id), (email, outlet_id) in wanted.items():
        if only_actor and actor_id != only_actor:
            continue
        row = before.get((folder_id, actor_id))
        same = row is not None and str(row["google_email"]).lower() == email.lower()
        if _shared(row) and same:
            continue
        if _shared(row) and not take_away(folder_id, actor_id, row):  # another address before: that one goes first
            continue
        if (row is not None and same and row["last_error"] and not only_actor
                and row["updated_at_utc"] > now - _SHARE_RETRY):
            continue
        try:
            if folder_id not in locked:
                client.lock_sharing(folder_id)
                locked.add(folder_id)
            permission_id, error = client.share(folder_id, email), None
        except DriveAuthorizationLost:
            raise
        except DriveError as exc:
            permission_id, error = None, str(exc)[:500]
        save(folder_id, actor_id, outlet_id=outlet_id, google_email=email, permission_id=permission_id,
             last_error=error, shared_at_utc=now if permission_id else None, removed_at_utc=None)
        counts["shared" if permission_id else "refused"] += 1
    for (folder_id, actor_id), row in before.items():
        if (folder_id, actor_id) not in wanted and _shared(row) and not (only_actor and actor_id != only_actor):
            take_away(folder_id, actor_id, row)
    return counts


def my_access(connection: Connection, tenant_id: str, link: dict[str, Any] | None, actor_id: str,
              pc_outlets: set[str]) -> dict[str, Any]:
    """A PC's own way into Drive: the Google account their outlets' folders are shared with,
    and each of those folders with its link."""
    if link is None:
        return {"googleEmail": None, "folders": []}
    shares = _shares(connection, tenant_id)
    folders = []
    for outlet in _outlet_folders(connection, tenant_id, link):
        if str(outlet["outlet_id"]) not in pc_outlets:
            continue
        row = shares.get((outlet["folder_id"], actor_id))
        folders.append({
            "outletId": str(outlet["outlet_id"]), "outletName": outlet["outlet_name"], "dealerName": outlet["dealer_name"],
            "url": f"https://drive.google.com/drive/folders/{outlet['folder_id']}", "shared": _shared(row),
            "problem": row["last_error"] if row and not _shared(row) else None,
        })
    return {"googleEmail": _people(connection, tenant_id).get(actor_id), "folders": folders}


def access_overview(connection: Connection, tenant_id: str, link: dict[str, Any] | None) -> list[dict[str, Any]]:
    """For the administrator: every PC of an outlet that uses Drive, the Google account their
    folders are shared with (none: they have not asked yet), and each folder's state."""
    if link is None:
        return []
    people, shares = _people(connection, tenant_id), _shares(connection, tenant_id)
    out: dict[str, dict[str, Any]] = {}
    for outlet in _outlet_folders(connection, tenant_id, link):
        for pc in outlet["pcs"]:
            row = shares.get((outlet["folder_id"], pc["actor_id"]))
            node = out.setdefault(pc["actor_id"], {"actorId": pc["actor_id"], "name": pc["name"],
                                                   "googleEmail": people.get(pc["actor_id"]), "folders": []})
            node["folders"].append({"outletId": str(outlet["outlet_id"]), "outletName": outlet["outlet_name"],
                                    "dealerName": outlet["dealer_name"], "shared": _shared(row),
                                    "problem": row["last_error"] if row and not _shared(row) else None})
    return sorted(out.values(), key=lambda p: str(p["name"] or p["actorId"]).lower())


# --------------------------------------------------------------------------- finding what is new

class _Places:
    """Where a Drive folder sits in the Project's tree, learnt as it is met."""

    def __init__(self, engine: Engine, tenant_id: str, client: DriveClient) -> None:
        self.engine, self.tenant_id, self.client = engine, tenant_id, client
        self._known: dict[str, dict[str, Any] | None] = {}

    def folder(self, folder_id: str | None, meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The folder's place, or None when it is not in the Project's tree at all."""
        if not folder_id:
            return None
        if folder_id in self._known and meta is None:
            return self._known[folder_id]
        self._known[folder_id] = None  # a loop of parents ends here
        with self.engine.begin() as connection:
            set_tenant_context(connection, self.tenant_id)
            row = _folder(connection, self.tenant_id, folder_id)
        if row is None or (meta is not None and not row["made_by_system"] and row["archived_at_utc"] is None):
            row = self._learn(folder_id, meta, row)
        self._known[folder_id] = row
        return row

    def _learn(self, folder_id: str, meta: dict[str, Any] | None, known: dict[str, Any] | None) -> dict[str, Any] | None:
        if meta is None:
            try:
                meta = self.client.get(folder_id)
            except DriveNotFound:
                return known
        if meta.get("mimeType") != FOLDER_TYPE or meta.get("trashed"):
            return known
        parent = self.folder((meta.get("parents") or [None])[0])
        if parent is None:
            return None
        name = str(meta.get("name") or "")
        created = _when(meta.get("createdTime"))
        base = {"drive_folder_id": folder_id, "parent_folder_id": parent["drive_folder_id"], "folder_name": name,
                "dealer_id": parent["dealer_id"], "outlet_id": parent["outlet_id"],
                "pc_actor_id": parent["pc_actor_id"],
                "folder_date": created.astimezone(IST).date() if created else ist_today()}
        kind = parent["folder_kind"]
        if kind in ("CUSTOMER", "SUB"):
            values = {**base, "folder_kind": "SUB",
                      "customer_folder_id": parent["drive_folder_id"] if kind == "CUSTOMER"
                      else parent["customer_folder_id"],
                      "photo_lane": bool(parent["photo_lane"]) or is_photo_folder(name)}
        elif kind in ("OUTLET", "PC"):
            customer_name, number = parse_customer_folder(name)
            values = {**base, "folder_kind": "CUSTOMER", "customer_name": customer_name, "booking_number": number,
                      "number_key": number_key(number)}
        else:  # under the Project, a dealership or the archive: no outlet to give it to
            return {**base, "folder_kind": "UNPLACED", "customer_folder_id": None, "photo_lane": False}
        with self.engine.begin() as connection:
            set_tenant_context(connection, self.tenant_id)
            return _save_folder(connection, self.tenant_id, **values)


def _set_aside_reason(meta: dict[str, Any], lane: str) -> str | None:
    """Why a file cannot be taken as it is, or None."""
    from audit_core.uc03_p2_vehicle_photos import _CONTENT_TYPES, size_error

    mime = str(meta.get("mimeType") or "").lower()
    size = int(meta.get("size") or 0)
    name = str(meta.get("name") or "file")
    if mime.startswith("application/vnd.google-apps."):
        return "A Google Docs / Sheets file, not an uploaded PDF or image"
    if lane == "PHOTO":
        if mime not in _CONTENT_TYPES:
            return "Only photos (JPEG, PNG, WEBP or HEIC) or PDF files are kept as photos"
        return size_error(name, mime, size)
    if mime not in DOCUMENT_TYPES:
        return "Only PDF, JPEG and PNG files are read"
    if size <= 0:
        return "The file is empty"
    if size > MAX_DOCUMENT_BYTES:
        return "Larger than 50 MB"
    return None


def register_file(engine: Engine, tenant_id: str, places: _Places, meta: dict[str, Any]) -> str | None:
    """A file met in Drive, recorded once per content: NEW when it is to be taken, SET_ASIDE
    with the reason when it cannot be. Returns the status recorded, or None for a file that
    is not in the Project's tree (never recorded) or is already taken."""
    if is_system_file(str(meta.get("name") or "")):
        return None
    parent = places.folder((meta.get("parents") or [None])[0])
    if parent is None or is_system_file(str(parent.get("folder_name") or "")):
        return None
    kind = parent["folder_kind"]
    created = _when(meta.get("createdTime"))
    lane = "PHOTO" if kind == "SUB" and parent["photo_lane"] else "DOCUMENT"
    customer = parent["drive_folder_id"] if kind == "CUSTOMER" else parent.get("customer_folder_id")
    reason = _set_aside_reason(meta, lane)
    if kind in ("UNPLACED", "PROJECT", "DEALER", "ARCHIVE"):
        reason = "Not under an outlet's folder, so it belongs to no outlet"
    values = {
        "t": tenant_id, "id": str(meta["id"]), "md5": str(meta.get("md5Checksum") or ""),
        "name": str(meta.get("name") or "file")[:500], "mime": str(meta.get("mimeType") or "")[:150],
        "size": int(meta.get("size") or 0), "created": created, "modified": _when(meta.get("modifiedTime")),
        "parent": parent["drive_folder_id"], "customer": customer, "dealer": parent["dealer_id"],
        "outlet": parent["outlet_id"], "day": created.astimezone(IST).date() if created else ist_today(),
        "pc": parent["pc_actor_id"], "lane": lane, "status": "SET_ASIDE" if reason else "NEW", "detail": reason,
    }
    owner = str(((meta.get("owners") or [{}])[0] or {}).get("emailAddress") or "").strip().lower()
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        if owner:
            # Whose Google account put it in Drive: that PC's file (two PCs can share an outlet's folder).
            values["pc"] = connection.execute(
                text("SELECT actor_id FROM auditcore.p2_drive_people WHERE tenant_id=:t AND lower(google_email)=:e "
                     "ORDER BY asked_at_utc LIMIT 1"), {"t": tenant_id, "e": owner},
            ).scalar_one_or_none() or values["pc"]
        # An earlier content of the same file that was never taken is no longer the file.
        connection.execute(
            text("UPDATE auditcore.p2_drive_files SET file_status='SET_ASIDE', updated_at_utc=now(), "
                 "detail='Replaced in Drive by a newer version before it was taken' "
                 "WHERE tenant_id=:t AND drive_file_id=:id AND content_md5 <> :md5 AND file_status='NEW' "
                 "AND run_id IS NULL"), values,
        )
        status = connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_drive_files (
                    tenant_id, drive_file_id, content_md5, file_name, mime_type, size_bytes, drive_created_at_utc,
                    drive_modified_at_utc, parent_folder_id, customer_folder_id, dealer_id, outlet_id, upload_date,
                    pc_actor_id, lane, file_status, detail
                ) VALUES (:t, :id, :md5, :name, :mime, :size, :created, :modified, :parent, :customer, :dealer,
                          :outlet, :day, :pc, :lane, :status, :detail)
                ON CONFLICT (tenant_id, drive_file_id, content_md5) DO UPDATE SET
                    file_name=EXCLUDED.file_name, parent_folder_id=EXCLUDED.parent_folder_id,
                    customer_folder_id=EXCLUDED.customer_folder_id, dealer_id=EXCLUDED.dealer_id,
                    outlet_id=EXCLUDED.outlet_id, pc_actor_id=EXCLUDED.pc_actor_id,
                    lane=EXCLUDED.lane, file_status=EXCLUDED.file_status, detail=EXCLUDED.detail,
                    updated_at_utc=now()
                WHERE auditcore.p2_drive_files.file_status IN ('NEW', 'SET_ASIDE')
                  AND auditcore.p2_drive_files.run_id IS NULL
                RETURNING file_status
                """
            ),
            values,
        ).scalar_one_or_none()
    return status


def _removed(engine: Engine, tenant_id: str, file_id: str) -> None:
    """A file or folder taken out of Drive: what was still waiting is no longer to be taken
    (what was taken stays as it is -- the record and the copy in storage do not depend on Drive)."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_drive_files SET file_status='SET_ASIDE', updated_at_utc=now(), "
                 "detail='Removed from Drive before it was taken' "
                 "WHERE tenant_id=:t AND drive_file_id=:f AND file_status='NEW' AND run_id IS NULL"),
            {"t": tenant_id, "f": file_id},
        )
        connection.execute(
            text("UPDATE auditcore.p2_drive_folders SET gone_at_utc=now(), updated_at_utc=now() "
                 "WHERE tenant_id=:t AND drive_folder_id=:f AND gone_at_utc IS NULL"),
            {"t": tenant_id, "f": file_id},
        )
        connection.execute(
            text("UPDATE auditcore.p2_drive_connections SET updated_at_utc=now(), "
                 "root_folder_id = NULLIF(root_folder_id, :f), archive_folder_id = NULLIF(archive_folder_id, :f) "
                 "WHERE tenant_id=:t AND :f IN (root_folder_id, archive_folder_id)"), {"t": tenant_id, "f": file_id},
        )


def _meet(engine: Engine, tenant_id: str, places: _Places, meta: dict[str, Any], counts: dict[str, int]) -> None:
    if meta.get("trashed"):
        _removed(engine, tenant_id, str(meta["id"]))
        counts["removed"] += 1
    elif meta.get("mimeType") == FOLDER_TYPE:
        if places.folder(str(meta["id"]), meta) is not None:
            counts["folders"] += 1
    else:
        status = register_file(engine, tenant_id, places, meta)
        if status == "NEW":
            counts["new"] += 1
        elif status == "SET_ASIDE":
            counts["setAside"] += 1


def walk(engine: Engine, tenant_id: str, client: DriveClient, folder_id: str, places: _Places | None = None,
         counts: dict[str, int] | None = None) -> dict[str, int]:
    """Everything under a folder, met one by one (the whole tree, when the change feed has
    to be started again)."""
    places = places or _Places(engine, tenant_id, client)
    counts = counts if counts is not None else {"new": 0, "setAside": 0, "folders": 0, "removed": 0}
    pending = [folder_id]
    while pending:
        for meta in client.children(pending.pop()):
            _meet(engine, tenant_id, places, meta, counts)
            if meta.get("mimeType") == FOLDER_TYPE:
                pending.append(str(meta["id"]))
    return counts


def sync(engine: Engine, tenant_id: str, client: DriveClient, link: dict[str, Any]) -> dict[str, int]:
    """What came into the Project's Drive since the last look (see the module's note)."""
    counts = {"new": 0, "setAside": 0, "folders": 0, "removed": 0}
    places = _Places(engine, tenant_id, client)
    token = link.get("changes_token")
    changes: list[dict[str, Any]] | None = None
    if token:
        try:
            changes, token = client.changes(str(token))
        except DriveAuthorizationLost:
            raise
        except DriveError as exc:
            logger.warning("p2_drive_changes_restarted", tenant_id=tenant_id, error=str(exc))
    if changes is None:
        # No place to read from (first look, or Google no longer has it): take a new place
        # first, then meet the whole tree once, so nothing put in meanwhile is missed.
        token = client.start_page_token()
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            root = (_connection_row(connection, tenant_id) or {}).get("root_folder_id")
        if root:
            walk(engine, tenant_id, client, str(root), places, counts)
    else:
        for change in changes:
            meta = change.get("file")
            if change.get("removed") or meta is None:
                _removed(engine, tenant_id, str(change.get("fileId")))
                counts["removed"] += 1
            else:
                _meet(engine, tenant_id, places, meta, counts)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_drive_connections SET changes_token=:c, last_synced_at_utc=now(), "
                 "last_error=NULL, updated_at_utc=now() WHERE tenant_id=:t"), {"c": token, "t": tenant_id},
        )
    return counts


# --------------------------------------------------------------------------- whose journey a folder is

def _journey(connection: Connection, tenant_id: str, journey_id: Any) -> dict[str, Any] | None:
    """A journey that is still there: its reference and whether it is closed."""
    if not journey_id:
        return None
    from audit_core.uc03_p2_same_file import _identities

    me = _identities(connection, tenant_id=tenant_id, journey_ids=[UUID(str(journey_id))]).get(UUID(str(journey_id)))
    if me is None:
        return None
    return {"journey_id": me.journey_id, "journey_reference": me.reference, "closed": me.closed}


def journey_for_folder(connection: Connection, tenant_id: str, folder: dict[str, Any] | None) -> dict[str, Any] | None:
    """The journey a customer's folder is, known before anything is read (see the module's
    note), with ``closed``; None when its files are to be read on a journey of their own. A
    journey found by the folder's number is kept on the folder."""
    if folder is None:
        return None
    own = _journey(connection, tenant_id, folder["journey_id"])
    if own is not None:
        return own
    key, outlet_id = folder["number_key"], folder["outlet_id"]
    if not key or not outlet_id:
        return None
    from audit_core.uc03_p2_same_file import _identities

    ids = {UUID(str(j)) for j in connection.execute(
        text("SELECT DISTINCT journey_id FROM auditcore.p2_drive_folders WHERE tenant_id=:t AND outlet_id=:o "
             "AND folder_kind='CUSTOMER' AND number_key=:k AND journey_id IS NOT NULL AND drive_folder_id <> :f"),
        {"t": tenant_id, "o": outlet_id, "k": key, "f": folder["drive_folder_id"]},
    ).scalars().all()}
    siblings = set(ids)
    digits = max((run for run in key.split("-") if run.isdigit()), key=len, default="")
    mobile = mobile_of(folder["booking_number"])
    if digits:
        ids |= {UUID(str(j)) for j in connection.execute(
            text(
                """
                SELECT DISTINCT f.journey_id
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.journeys j ON j.tenant_id=f.tenant_id AND j.journey_id=f.journey_id
                WHERE f.tenant_id=:t AND j.outlet_id=:o AND f.field_key IN ('booking_reference_number', 'customer_phone')
                  AND regexp_replace(COALESCE(f.effective_value #>> '{}', ''), '[^0-9]', '', 'g') LIKE :digits
                """
            ),
            {"t": tenant_id, "o": outlet_id, "digits": f"%{digits}%"},
        ).scalars().all()}
    found = [
        o for o in _identities(connection, tenant_id=tenant_id, journey_ids=list(ids)).values()
        if str(o.outlet_id) == str(outlet_id)
        and (o.journey_id in siblings or key in o.booking_refs or (mobile is not None and o.mobile == mobile))
        and not names_differ(folder["customer_name"], o.name)
    ]
    # One open journey is the customer's; with none open, one closed journey is (its files are held).
    for group in ([o for o in found if not o.closed], [o for o in found if o.closed]):
        if len(group) == 1:
            connection.execute(
                text("UPDATE auditcore.p2_drive_folders SET journey_id=:j, updated_at_utc=now() "
                     "WHERE tenant_id=:t AND drive_folder_id=:f"),
                {"j": group[0].journey_id, "t": tenant_id, "f": folder["drive_folder_id"]},
            )
            return {"journey_id": group[0].journey_id, "journey_reference": group[0].reference,
                    "closed": group[0].closed}
        if group:
            return None
    return None


def _closed_reason(journey: dict[str, Any]) -> str:
    return (f"The customer's journey {journey['journey_reference']} is closed: not taken. "
            "The Team Lead decides what to do with it")


# --------------------------------------------------------------------------- a run

def request_run(
    connection: Connection, *, tenant_id: str, trigger: str, actor_id: str, role: str | None = None,
    name: str | None = None, dealer_id: Any = None, outlet_id: Any = None, upload_date: date | None = None,
    mode: str = "NOW",
) -> dict[str, Any]:
    """A run asked for, and the worker job that does it."""
    from audit_core.uc03_p2_bulk_upload import request_job

    run = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_drive_runs (tenant_id, run_trigger, run_mode, requested_by_actor_id,
                requested_by_role, requested_by_name, dealer_id, outlet_id, upload_date)
            VALUES (:t, :trigger, :mode, :actor, :role, :name, :dealer, :outlet, :day) RETURNING *
            """
        ),
        {"t": tenant_id, "trigger": trigger, "mode": mode, "actor": actor_id[:200], "role": role,
         "name": (name or "").strip()[:300] or None, "dealer": dealer_id, "outlet": outlet_id, "day": upload_date},
    ).mappings().one()
    request_job(connection, kind=JOB_RUN, tenant_id=tenant_id, bulk_id=run["run_id"], by=actor_id)
    return dict(run)


def _set_run(engine: Engine, tenant_id: str, run_id: Any, **values: Any) -> None:
    if "summary" in values:
        values["summary"] = json.dumps(values["summary"], default=str)
    sets = ", ".join(f"{k}=CAST(:{k} AS jsonb)" if k == "summary" else f"{k}=:{k}" for k in values)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text(f"UPDATE auditcore.p2_drive_runs SET {sets}, updated_at_utc=now() WHERE tenant_id=:t AND run_id=:r"),
            {**values, "t": tenant_id, "r": run_id},
        )


def _set_file(connection: Connection, tenant_id: str, file_row_id: Any, **values: Any) -> None:
    sets = ", ".join(f"{k}=:{k}" for k in values)
    connection.execute(
        text(f"UPDATE auditcore.p2_drive_files SET {sets}, updated_at_utc=now() "
             "WHERE tenant_id=:t AND file_row_id=:f"), {**values, "t": tenant_id, "f": file_row_id},
    )


def _fail_file(engine: Engine, tenant_id: str, file: dict[str, Any], exc: Exception) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        _set_file(connection, tenant_id, file["file_row_id"], file_status="FAILED",
                  detail=f"{type(exc).__name__}: {exc}"[:500])


def _claim(engine: Engine, tenant_id: str, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The waiting files of the run's outlet(s) and day of upload, taken for this run only (a
    file a run left behind long ago is free again)."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        chosen = (_connection_row(connection, tenant_id) or {}).get("outlet_ids")
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_drive_files f SET run_id=NULL, updated_at_utc=now()
                WHERE f.tenant_id=:t AND f.file_status='NEW' AND f.run_id IS NOT NULL AND f.run_id <> :r
                  AND NOT EXISTS (SELECT 1 FROM auditcore.p2_drive_runs r
                                  WHERE r.tenant_id=f.tenant_id AND r.run_id=f.run_id
                                    AND r.run_status IN ('REQUESTED', 'RUNNING') AND r.requested_at_utc > :stale)
                """
            ),
            {"t": tenant_id, "r": run["run_id"], "stale": datetime.now(UTC) - _STALE_CLAIM},
        )
        rows = connection.execute(
            text(
                """
                UPDATE auditcore.p2_drive_files SET run_id=:r, updated_at_utc=now()
                WHERE tenant_id=:t AND file_status='NEW' AND (run_id IS NULL OR run_id=:r) AND outlet_id IS NOT NULL
                  AND (CAST(:outlet AS uuid) IS NULL OR outlet_id=CAST(:outlet AS uuid))
                  AND (CAST(:dealer AS uuid) IS NULL OR dealer_id=CAST(:dealer AS uuid))
                  AND (CAST(:day AS date) IS NULL OR upload_date=CAST(:day AS date))
                  AND (CAST(:only AS uuid[]) IS NULL OR outlet_id = ANY(CAST(:only AS uuid[])))
                RETURNING *
                """
            ),
            {"t": tenant_id, "r": run["run_id"], "outlet": run["outlet_id"], "dealer": run["dealer_id"],
             "day": run["upload_date"],
             # The daily run is for the outlets the Project uses Drive for; a run someone asks for is theirs.
             "only": [str(o) for o in chosen] if run["run_trigger"] == "END_OF_DAY" and chosen is not None else None},
        ).mappings().all()
    return sorted((dict(r) for r in rows), key=lambda r: (str(r["outlet_id"]), str(r["customer_folder_id"]),
                                                          r["file_name"]))


def _storage_key(connection: Connection, tenant_id: str, outlet: dict[str, Any], file: dict[str, Any],
                 folder_name: str | None) -> str:
    """dealership / outlet / day of upload / customer's folder / the file's own name (a second
    file of that name there gets " (2)")."""
    from audit_core.uc03_p2_bulk_upload import numbered

    base = (f"p2-bulk/{safe_part(tenant_id, fallback='tenant')}/{safe_part(outlet['dealer_name'], fallback='dealership')}/"
            f"{safe_part(outlet['outlet_name'], fallback='outlet')}/{file['upload_date']:%Y-%m-%d}/"
            f"{_plain(folder_name, 'no-folder')}")
    name = _plain(file["file_name"], "document")
    key, n = f"{base}/{name}", 1
    while connection.execute(
        text("SELECT 1 FROM auditcore.p2_bulk_files WHERE tenant_id=:t AND object_key=:k"), {"t": tenant_id, "k": key},
    ).first() is not None:
        n += 1
        key = f"{base}/{numbered(name, n)}"
    return key


def _group_key(file: dict[str, Any], folder: dict[str, Any] | None) -> str | None:
    """Files read together: a customer's folder -- and another folder of the outlet with the
    same number, which is the same customer's. A file in no customer's folder is grouped by
    its name, as the web's bulk upload groups it."""
    if folder is None:
        return None
    if folder["number_key"]:
        return f"drive:{file['outlet_id']}:{folder['number_key']}"[:300]
    return f"drive:{folder['drive_folder_id']}"


def _take_documents(engine: Engine, tenant_id: str, run: dict[str, Any], client: DriveClient,
                    outlet: dict[str, Any], pc: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, int]:
    """The outlet's waiting documents of one PC: a bulk upload, submitted for them."""
    from audit_core.uc03_p2_bulk_upload import JOB_UPLOAD, bulk_folder, request_job
    from audit_core.uc03_p2_storage import get_p2_document_storage

    storage = get_p2_document_storage()
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        bulk = dict(connection.execute(
            text(
                """
                INSERT INTO auditcore.p2_bulk_uploads (tenant_id, dealer_id, outlet_id, uploaded_by_actor_id,
                    uploaded_by_name, folder, status, source, drive_run_id)
                VALUES (:t, :d, :o, :a, :n, :f, 'SUBMITTED', 'DRIVE', :run) RETURNING *
                """
            ),
            {"t": tenant_id, "d": outlet["dealer_id"], "o": outlet["outlet_id"], "a": pc["actor_id"],
             "n": pc["name"], "f": bulk_folder(tenant_id, outlet["dealer_name"], outlet["outlet_name"]),
             "run": run["run_id"]},
        ).mappings().one())
    out = {"taken": 0, "held": 0, "failed": 0}
    for file in files:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                folder = _folder(connection, tenant_id, file["customer_folder_id"]) if file["customer_folder_id"] else None
                journey = journey_for_folder(connection, tenant_id, folder)
                if journey is not None and journey["closed"]:
                    _set_file(connection, tenant_id, file["file_row_id"], file_status="SET_ASIDE",
                              journey_id=journey["journey_id"], detail=_closed_reason(journey))
                    out["held"] += 1
                    continue
                key = _storage_key(connection, tenant_id, outlet, file, folder["folder_name"] if folder else None)
            payload = client.download(str(file["drive_file_id"]))
            # Only once the file is in storage is anything else done with it (2026-11-02).
            storage.put_object(key, payload, content_type=str(file["mime_type"]))
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                bulk_file_id = connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.p2_bulk_files (tenant_id, bulk_id, original_filename, content_type,
                            size_bytes, object_key, upload_status, group_key, target_journey_id, booking_number)
                        VALUES (:t, :b, :name, :ct, :size, :k, 'UPLOADED', :g, :target, :number) RETURNING file_id
                        """
                    ),
                    {"t": tenant_id, "b": bulk["bulk_id"], "name": _plain(file["file_name"], "document"),
                     "ct": str(file["mime_type"]).lower(), "size": len(payload), "k": key,
                     "g": _group_key(file, folder), "target": journey["journey_id"] if journey else None,
                     "number": folder["booking_number"] if folder else None},
                ).scalar_one()
                _set_file(connection, tenant_id, file["file_row_id"], file_status="TAKEN", object_key=key,
                          sha256=hashlib.sha256(payload).hexdigest(), bulk_id=bulk["bulk_id"],
                          bulk_file_id=bulk_file_id, pc_actor_id=pc["actor_id"], taken_at_utc=datetime.now(UTC),
                          detail=None)
            out["taken"] += 1
        except Exception as exc:
            logger.warning("p2_drive_file_failed", tenant_id=tenant_id, file=str(file["drive_file_id"]), exc_info=True)
            out["failed"] += 1
            _fail_file(engine, tenant_id, file, exc)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_bulk_uploads SET status = CASE WHEN :n > 0 THEN status ELSE 'FAILED' END, "
                 "file_count=:n, submitted_at_utc=now(), updated_at_utc=now() WHERE tenant_id=:t AND bulk_id=:b"),
            {"n": out["taken"], "t": tenant_id, "b": bulk["bulk_id"]},
        )
        if out["taken"]:
            request_job(connection, kind=JOB_UPLOAD, tenant_id=tenant_id, bulk_id=bulk["bulk_id"], by=SYSTEM_ACTOR)
    return out


def _attach_photo(engine: Engine, tenant_id: str, client: DriveClient, file: dict[str, Any],
                  journey: dict[str, Any], actor_id: str) -> str:
    """A photo (or other paper) of the customer's folder, kept on the journey as the PC's
    own photo upload is; not read. Returns TAKEN, or SET_ASIDE when the journey is full."""
    from audit_core.uc03_p2_runtime import note_facts_changed, record_activity
    from audit_core.uc03_p2_storage import get_p2_document_storage
    from audit_core.uc03_p2_vehicle_photos import (
        MAX_PDFS_PER_JOURNEY,
        MAX_PHOTOS_PER_JOURNEY,
        PDF_TYPE,
        _active_count,
        _active_pdf_count,
        object_key_for,
        pdf_limit_error,
    )

    mime = str(file["mime_type"]).lower()
    client_upload_id = f"drive-{file['drive_file_id']}"[:128]
    key = object_key_for(tenant_id, journey["journey_id"], client_upload_id, mime)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        full = None
        if _active_count(connection, tenant_id=tenant_id, journey_id=journey["journey_id"]) >= MAX_PHOTOS_PER_JOURNEY:
            full = f"At most {MAX_PHOTOS_PER_JOURNEY} photos are kept per booking"
        elif mime == PDF_TYPE:
            pdfs = _active_pdf_count(connection, tenant_id=tenant_id, journey_id=journey["journey_id"])
            if pdfs >= MAX_PDFS_PER_JOURNEY:
                full = pdf_limit_error(pdfs)
        if full:
            _set_file(connection, tenant_id, file["file_row_id"], file_status="SET_ASIDE", detail=full,
                      journey_id=journey["journey_id"])
            return "SET_ASIDE"
    payload = client.download(str(file["drive_file_id"]))
    get_p2_document_storage().put_object(key, payload, content_type=mime)
    correlation = f"p2-drive:{file['file_row_id']}"
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        photo_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.delivery_vehicle_photos (tenant_id, journey_id, object_key, original_filename,
                    content_type, size_bytes, uploaded_by_actor_id, client_upload_id, view_code, capture_source)
                VALUES (:t, :j, :key, :name, :type, :size, :actor, :client, 'OTHER', 'P2')
                ON CONFLICT (tenant_id, journey_id, client_upload_id) WHERE client_upload_id IS NOT NULL DO NOTHING
                RETURNING photo_id
                """
            ),
            {"t": tenant_id, "j": journey["journey_id"], "key": key, "name": str(file["file_name"])[:255],
             "type": mime, "size": len(payload), "actor": actor_id, "client": client_upload_id},
        ).scalar_one_or_none()
        if photo_id is not None:
            record_activity(
                connection, tenant_id=tenant_id, journey_id=journey["journey_id"], event_type="VEHICLE_PHOTO_ADDED",
                subject_type="VEHICLE_PHOTO", subject_id=str(photo_id),
                details={"filename": file["file_name"], "viewCode": "OTHER", "by": "DRIVE"}, correlation_id=correlation,
            )
            note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey["journey_id"],
                               reason="VEHICLE_PHOTO_ADDED", correlation_id=correlation)
        _set_file(connection, tenant_id, file["file_row_id"], file_status="TAKEN", object_key=key,
                  sha256=hashlib.sha256(payload).hexdigest(), photo_id=photo_id, journey_id=journey["journey_id"],
                  pc_actor_id=actor_id, taken_at_utc=datetime.now(UTC), detail=None)
    return "TAKEN"


def _photo(engine: Engine, tenant_id: str, client: DriveClient, file: dict[str, Any], actor_id: str) -> str:
    """A photo of a customer's folder: kept on the folder's journey now, held when that
    journey is closed, or left waiting for the journey to be known. Returns what it did."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        folder = _folder(connection, tenant_id, file["customer_folder_id"]) if file["customer_folder_id"] else None
        journey = journey_for_folder(connection, tenant_id, folder)
        if journey is None:
            _set_file(connection, tenant_id, file["file_row_id"], file_status="WAITING", pc_actor_id=actor_id,
                      detail="Waits for the customer's documents: kept on the journey once it is known")
            return "WAITING"
        if journey["closed"]:
            _set_file(connection, tenant_id, file["file_row_id"], file_status="SET_ASIDE",
                      journey_id=journey["journey_id"], detail=_closed_reason(journey))
            return "HELD"
    return _attach_photo(engine, tenant_id, client, file, journey, actor_id)


def run_drive_intake(engine: Engine, tag: str, tenant_ids: list[str] | None = None) -> dict[str, Any]:
    """DRIVE_RUN: look at Drive, then take every waiting file the run is for. Idempotent: a
    file already taken is not taken again."""
    tenant_id, _, raw = (tag or "").partition(":")
    run_id = UUID(raw.strip())
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        run = connection.execute(
            text("UPDATE auditcore.p2_drive_runs SET run_status='RUNNING', started_at_utc=COALESCE(started_at_utc, now()), "
                 "updated_at_utc=now() WHERE tenant_id=:t AND run_id=:r AND run_status IN ('REQUESTED', 'RUNNING') "
                 "RETURNING *"), {"t": tenant_id, "r": run_id},
        ).mappings().one_or_none()
        if run is None:
            return {"runId": str(run_id), "skipped": "not waiting"}
        run = dict(run)
        outlets = {str(o["outlet_id"]): o for o in outlets_with_pcs(connection, tenant_id)}
    try:
        linked = connected_client(engine, tenant_id)
        if linked is None:
            raise RuntimeError("The Project's Google Drive is not connected")
        client, link = linked
        ensure_tree(engine, tenant_id, client)
        found = sync(engine, tenant_id, client, link)
    except Exception as exc:
        _set_run(engine, tenant_id, run_id, run_status="FAILED", finished_at_utc=datetime.now(UTC),
                 detail=f"{type(exc).__name__}: {exc}"[:1000])
        _note_connection(engine, tenant_id, error=str(exc), lost=isinstance(exc, DriveAuthorizationLost))
        raise
    files = _claim(engine, tenant_id, run)
    submitter = run["requested_by_actor_id"] if run["run_trigger"] == "PC_SUBMIT" else None
    documents: dict[tuple[str, str], list[dict[str, Any]]] = {}
    counts = {"documents": 0, "photos": 0, "photosWaiting": 0, "held": 0, "failed": 0, "noPc": 0}
    for file in files:
        outlet = outlets.get(str(file["outlet_id"]))
        pc = attributed_pc(outlet, file["pc_actor_id"], submitter)
        if outlet is None or pc is None:
            counts["noPc"] += 1
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                _set_file(connection, tenant_id, file["file_row_id"], run_id=None,
                          detail="The outlet has no PC mapped: it waits until one is")
            continue
        if file["lane"] == "DOCUMENT":
            documents.setdefault((str(file["outlet_id"]), pc["actor_id"]), []).append(file)
            continue
        try:
            did = _photo(engine, tenant_id, client, file, pc["actor_id"])
            counts[{"TAKEN": "photos", "WAITING": "photosWaiting", "HELD": "held"}.get(did, "failed")] += 1
        except Exception as exc:
            logger.warning("p2_drive_file_failed", tenant_id=tenant_id, file=str(file["drive_file_id"]), exc_info=True)
            counts["failed"] += 1
            _fail_file(engine, tenant_id, file, exc)
    for (outlet_id, actor_id), group in documents.items():
        outlet = outlets[outlet_id]
        pc = next(p for p in outlet["pcs"] if p["actor_id"] == actor_id)
        did = _take_documents(engine, tenant_id, run, client, outlet, pc, group)
        counts["documents"] += did["taken"]
        counts["held"] += did["held"]
        counts["failed"] += did["failed"]
    _set_run(engine, tenant_id, run_id, run_status="READING", summary={"found": found, "taken": counts})
    settle(engine, tenant_id, client)
    result = {"runId": str(run_id), "found": found, **counts}
    logger.info("p2_drive_run_taken", tenant_id=tenant_id, **result)
    return result


# --------------------------------------------------------------------------- after the reading

def settle(engine: Engine, tenant_id: str, client: DriveClient | None = None) -> dict[str, int]:
    """What the bulk upload decided, brought back to the record: a customer's folder learns
    the journey its files went to; photos that waited for it are added; a run whose files
    are all settled is done."""
    done = {"folders": 0, "photos": 0, "runs": 0}
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        placed = connection.execute(
            text(
                """
                UPDATE auditcore.p2_drive_files f SET journey_id=b.placed_journey_id, updated_at_utc=now()
                FROM auditcore.p2_bulk_files b
                WHERE f.tenant_id=:t AND b.tenant_id=f.tenant_id AND b.file_id=f.bulk_file_id
                  AND b.outcome = ANY(:placed) AND b.placed_journey_id IS NOT NULL
                  AND f.journey_id IS DISTINCT FROM b.placed_journey_id
                RETURNING f.customer_folder_id, b.placed_journey_id
                """
            ),
            {"t": tenant_id, "placed": list(_PLACED_BULK)},
        ).mappings().all()
        for row in placed:
            if row["customer_folder_id"]:
                done["folders"] += connection.execute(
                    text("UPDATE auditcore.p2_drive_folders SET journey_id=:j, updated_at_utc=now() "
                         "WHERE tenant_id=:t AND drive_folder_id=:f AND journey_id IS DISTINCT FROM :j"),
                    {"j": row["placed_journey_id"], "t": tenant_id, "f": row["customer_folder_id"]},
                ).rowcount
        waiting = [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_drive_files WHERE tenant_id=:t AND file_status='WAITING' AND lane='PHOTO' "
                 "ORDER BY seen_at_utc LIMIT 500"), {"t": tenant_id},
        ).mappings().all()]
    if waiting and client is None:
        linked = connected_client(engine, tenant_id)
        client = linked[0] if linked else None
    for file in waiting if client is not None else []:
        try:
            if _photo(engine, tenant_id, client, file, file["pc_actor_id"] or SYSTEM_ACTOR) == "TAKEN":
                done["photos"] += 1
        except Exception as exc:
            logger.warning("p2_drive_file_failed", tenant_id=tenant_id, file=str(file["drive_file_id"]), exc_info=True)
            _fail_file(engine, tenant_id, file, exc)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        reading = connection.execute(
            text(
                """
                SELECT r.run_id FROM auditcore.p2_drive_runs r
                WHERE r.tenant_id=:t AND r.run_status='READING'
                  AND NOT EXISTS (SELECT 1 FROM auditcore.p2_bulk_uploads b
                                  WHERE b.tenant_id=r.tenant_id AND b.drive_run_id=r.run_id
                                    AND b.status IN ('OPEN', 'SUBMITTED', 'PROCESSING'))
                """
            ),
            {"t": tenant_id},
        ).scalars().all()
        for run_id in reading:
            report = run_report(connection, tenant_id=tenant_id, run_id=run_id, detail=False)
            connection.execute(
                text("UPDATE auditcore.p2_drive_runs SET run_status='DONE', finished_at_utc=now(), updated_at_utc=now(), "
                     "summary = summary || CAST(:s AS jsonb) WHERE tenant_id=:t AND run_id=:r"),
                {"s": json.dumps({"totals": report["totals"]}, default=str), "t": tenant_id, "r": run_id},
            )
            done["runs"] += 1
    return done


# --------------------------------------------------------------------------- the archive

def archive_closed(engine: Engine, tenant_id: str, client: DriveClient, now: datetime | None = None) -> dict[str, int]:
    """The folders of journeys closed for the Project's retention period, moved to
    <Project> / Archive / <yyyy-mm of the closing> / <Outlet> (product owner 2026-11-12:
    "keep the retention configurable; we need to archive it, no hard delete"). A folder with
    a file still waiting to be taken stays where it is."""
    now = now or datetime.now(UTC)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        link = _connection_row(connection, tenant_id)
        if link is None or not link["root_folder_id"]:
            return {"archived": 0}
        due = [dict(r) for r in connection.execute(
            text(
                f"""
                SELECT cf.*, c.closed_at, o.outlet_name
                FROM auditcore.p2_drive_folders cf
                JOIN auditcore.dealer_outlets o ON o.tenant_id=cf.tenant_id AND o.outlet_id=cf.outlet_id
                CROSS JOIN LATERAL ({_CLOSED_AT_SQL}) AS c(closed_at)
                WHERE cf.tenant_id=:t AND cf.folder_kind='CUSTOMER' AND cf.archived_at_utc IS NULL
                  AND cf.gone_at_utc IS NULL AND cf.journey_id IS NOT NULL AND c.closed_at IS NOT NULL
                  AND c.closed_at <= :before
                  AND NOT EXISTS (SELECT 1 FROM auditcore.p2_drive_files f
                                  WHERE f.tenant_id=cf.tenant_id AND f.customer_folder_id=cf.drive_folder_id
                                    AND f.file_status IN ('NEW', 'WAITING'))
                ORDER BY c.closed_at LIMIT 500
                """
            ),
            {"t": tenant_id, "before": now - timedelta(days=int(link["retention_days"]))},
        ).mappings().all()]
    archived = 0
    for folder in due:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                link = _connection_row(connection, tenant_id)
                archive = link["archive_folder_id"]
                if not archive:
                    archive, _ = _ensure(connection, tenant_id, client, "ARCHIVE", str(link["root_folder_id"]),
                                         ARCHIVE_NAME, folder_date=None, outlet_id=None)
                    connection.execute(
                        text("UPDATE auditcore.p2_drive_connections SET archive_folder_id=:a, updated_at_utc=now() "
                             "WHERE tenant_id=:t"), {"a": archive, "t": tenant_id},
                    )
                month = folder["closed_at"].astimezone(IST).date().replace(day=1)
                month_folder, _ = _ensure(connection, tenant_id, client, "ARCHIVE", archive, f"{month:%Y-%m}",
                                          folder_date=month, outlet_id=None)
                target, _ = _ensure(connection, tenant_id, client, "ARCHIVE", month_folder, str(folder["outlet_name"]),
                                    folder_date=month, outlet_id=folder["outlet_id"])
            client.move(str(folder["drive_folder_id"]), target, folder["parent_folder_id"])
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                connection.execute(
                    text("UPDATE auditcore.p2_drive_folders SET archived_at_utc=now(), parent_folder_id=:p, "
                         "updated_at_utc=now() WHERE tenant_id=:t AND drive_folder_id=:f"),
                    {"p": target, "t": tenant_id, "f": folder["drive_folder_id"]},
                )
            archived += 1
        except DriveNotFound:
            _removed(engine, tenant_id, str(folder["drive_folder_id"]))
        except Exception:
            logger.warning("p2_drive_archive_failed", tenant_id=tenant_id, folder=str(folder["drive_folder_id"]),
                           exc_info=True)
    return {"archived": archived}


# --------------------------------------------------------------------------- the report

_FILE_SQL = """
    SELECT f.file_row_id, f.file_name, f.lane, f.file_status, f.detail, f.size_bytes, f.outlet_id, f.pc_actor_id,
           f.upload_date, f.customer_folder_id, f.seen_at_utc, f.taken_at_utc, f.run_id,
           cf.folder_name AS customer_folder, cf.booking_number,
           b.outcome AS bulk_outcome, b.detail AS bulk_detail, b.batch_id,
           COALESCE(b.placed_journey_id, b.journey_id, f.journey_id) AS journey_id, b.placed_reference,
           ub.page_count, ub.batch_status, ub.failure_reason
    FROM auditcore.p2_drive_files f
    LEFT JOIN auditcore.p2_drive_folders cf ON cf.tenant_id=f.tenant_id AND cf.drive_folder_id=f.customer_folder_id
    LEFT JOIN auditcore.p2_bulk_files b ON b.tenant_id=f.tenant_id AND b.file_id=f.bulk_file_id
    LEFT JOIN auditcore.p2_upload_batches ub ON ub.tenant_id=b.tenant_id AND ub.batch_id=b.batch_id
"""
_WAITING_UNITS = ("QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING", "CLASSIFYING",
                  "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT")
_STATE_TOTAL = {"WAITING": "waiting", "SET_ASIDE": "setAside", "FAILED": "failed", "DUPLICATE": "duplicates",
                "READING": "reading", "NEW_BOOKING": "newBookings", "ADDED_TO_BOOKING": "addedToBookings",
                "KEPT_FOR_REVIEW": "keptForReview"}


def _blank_totals() -> dict[str, int]:
    return {"files": 0, "documents": 0, "photos": 0, "taken": 0, "duplicates": 0, "setAside": 0, "failed": 0,
            "waiting": 0, "reading": 0, "newBookings": 0, "addedToBookings": 0, "keptForReview": 0, "pages": 0,
            "pagesRead": 0, "pagesToRescan": 0, "pagesReading": 0, "manualVerificationTasks": 0}


def _state(file: dict[str, Any]) -> str:
    """Where one Drive file stands, in one word."""
    state = {"NEW": "WAITING", "WAITING": "WAITING", "SET_ASIDE": "SET_ASIDE", "FAILED": "FAILED"}.get(
        file["file_status"], "TAKEN")
    if state == "TAKEN" and file["lane"] == "DOCUMENT":
        return {"EXACT_DUPLICATE": "DUPLICATE", "FAILED": "FAILED", "NEW_BOOKING": "NEW_BOOKING",
                "ADDED_TO_BOOKING": "ADDED_TO_BOOKING", "KEPT_FOR_REVIEW": "KEPT_FOR_REVIEW"}.get(
                    str(file["bulk_outcome"]), "READING")
    return state


def _figures(connection: Connection, tenant_id: str, files: list[dict[str, Any]], *,
             since: datetime | None) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """Each file with its pages and the pages to scan again; the manual verification tasks
    each journey got since ``since`` (a task belongs to the journey, so it is counted once
    per journey); and the totals."""
    from audit_core.uc03_p2_document_task_groups import document_category
    from audit_core.uc03_p2_runtime import swept_pending_sql

    batches = [f["batch_id"] for f in files if f["batch_id"]]
    units: dict[str, list[dict[str, Any]]] = {}
    if batches:
        for u in connection.execute(
            text(
                f"""
                SELECT q.batch_id, q.unit_kind, q.queue_status, q.page_number, q.page_numbers, q.status_reason,
                       q.last_error, q.merged_into_queue_id, {swept_pending_sql("q")} AS swept
                FROM auditcore.p2_document_queue q
                WHERE q.tenant_id=:t AND q.batch_id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"t": tenant_id, "ids": [str(b) for b in batches]},
        ).mappings():
            units.setdefault(str(u["batch_id"]), []).append(dict(u))
    journeys = sorted({str(f["journey_id"]) for f in files if f["journey_id"]})
    tasks: dict[str, int] = {}
    if journeys:
        for task in connection.execute(
            text("SELECT journey_id, task_type, category, source_code FROM auditcore.p2_tasks WHERE tenant_id=:t "
                 "AND journey_id = ANY(CAST(:ids AS uuid[])) AND (CAST(:since AS timestamptz) IS NULL "
                 "OR created_at_utc >= CAST(:since AS timestamptz))"),
            {"t": tenant_id, "ids": journeys, "since": since},
        ).mappings():
            if document_category(task["task_type"], task["category"], task["source_code"]) == "MANUAL_VERIFICATION":
                tasks[str(task["journey_id"])] = tasks.get(str(task["journey_id"]), 0) + 1
    totals = _blank_totals()
    rows = []
    for f in files:
        state = _state(f)
        totals["files"] += 1
        totals["documents" if f["lane"] == "DOCUMENT" else "photos"] += 1
        if state in _STATE_TOTAL:
            totals[_STATE_TOTAL[state]] += 1
        if state not in ("WAITING", "SET_ASIDE", "FAILED"):
            totals["taken"] += 1
        pages = int(f["page_count"] or 0)
        rescan, reading_pages = [], 0
        for u in units.get(str(f["batch_id"]), []) if f["batch_id"] else []:
            if u["merged_into_queue_id"] is not None:
                continue  # counted with the document it became part of
            numbers = [int(p) for p in (u["page_numbers"] or ([u["page_number"]] if u["page_number"] else []))]
            if u["queue_status"] in ("FAILED", "DEAD_LETTER") or u["swept"]:
                rescan.append({"pages": numbers, "reason": u["status_reason"] or "It could not be read.",
                               "code": u["last_error"]})
            elif u["queue_status"] in _WAITING_UNITS:
                reading_pages += len(numbers) or 1
        if f["batch_status"] == "FAILED" and pages == 0 and f["batch_id"]:
            rescan.append({"pages": [], "reason": f["failure_reason"] or "The file could not be split into pages.",
                           "code": None})
        rescan_pages = sum(len(r["pages"]) for r in rescan)
        read = max(pages - rescan_pages - reading_pages, 0)
        totals["pages"] += pages
        totals["pagesToRescan"] += rescan_pages
        totals["pagesReading"] += reading_pages
        totals["pagesRead"] += read
        rows.append({**f, "state": state, "pages": pages, "rescan": rescan, "pagesReading": reading_pages,
                     "pagesRead": read})
    totals["manualVerificationTasks"] = sum(tasks.values())
    return rows, tasks, totals


def run_report(connection: Connection, *, tenant_id: str, run_id: Any, detail: bool = True) -> dict[str, Any]:
    """The report of one run (product owner 2026-11-07): how many files were processed,
    pages read, manual verification tasks opened, and each page to scan again with its
    file, page number and reason -- per customer's folder."""
    run = connection.execute(
        text("SELECT * FROM auditcore.p2_drive_runs WHERE tenant_id=:t AND run_id=:r"), {"t": tenant_id, "r": run_id},
    ).mappings().one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Drive run not found.")
    files = [dict(r) for r in connection.execute(
        text(_FILE_SQL + " WHERE f.tenant_id=:t AND f.run_id=:r ORDER BY f.outlet_id, cf.folder_name, f.file_name"),
        {"t": tenant_id, "r": run_id},
    ).mappings().all()]
    rows, tasks, totals = _figures(connection, tenant_id, files, since=run["started_at_utc"])
    out = {"run": _public_run(dict(run)), "totals": totals}
    if not detail:
        return out
    names = {str(o["outlet_id"]): o for o in outlets_with_pcs(connection, tenant_id)}
    customers: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        outlet = names.get(str(r["outlet_id"])) or {}
        key = (str(r["outlet_id"]), str(r["customer_folder_id"] or f"file:{r['file_row_id']}"))
        node = customers.setdefault(key, {
            "outletId": str(r["outlet_id"]) if r["outlet_id"] else None, "outletName": outlet.get("outlet_name"),
            "dealerName": outlet.get("dealer_name"), "customerFolder": r["customer_folder"],
            "bookingNumber": r["booking_number"], "journeyId": None, "journeyReference": None, "files": [],
            "pages": 0, "pagesRead": 0, "pagesToRescan": 0, "manualVerificationTasks": 0,
        })
        if r["journey_id"] and r["state"] != "DUPLICATE":
            node["journeyId"] = str(r["journey_id"])
            node["journeyReference"] = r["placed_reference"] or node["journeyReference"]
            node["manualVerificationTasks"] = tasks.get(str(r["journey_id"]), 0)
        node["pages"] += r["pages"]
        node["pagesRead"] += r["pagesRead"]
        node["pagesToRescan"] += sum(len(x["pages"]) for x in r["rescan"])
        node["files"].append(_public_file(r))
    out["customers"] = list(customers.values())
    return out


def _public_file(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": r["file_name"], "kind": r["lane"], "state": r["state"], "detail": r["bulk_detail"] or r["detail"],
        "uploadDate": r["upload_date"].isoformat() if r["upload_date"] else None,
        "seenAtUtc": r["seen_at_utc"].isoformat() if r["seen_at_utc"] else None,
        "pages": r["pages"], "pagesRead": r["pagesRead"], "rescan": r["rescan"],
    }


def tracker_rows(connection: Connection, *, tenant_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The Project Tracker's Drive figures for a period, per outlet and PC: the files that
    arrived in Drive in it (by when Verigence first saw them) and what became of them."""
    files = [dict(r) for r in connection.execute(
        text(_FILE_SQL + " WHERE f.tenant_id=:t AND f.seen_at_utc >= :s AND f.seen_at_utc < :e "
                         "AND f.outlet_id IS NOT NULL"), {"t": tenant_id, "s": start, "e": end},
    ).mappings().all()]
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for f in files:
        groups.setdefault((str(f["outlet_id"]), f["pc_actor_id"]), []).append(f)
    out = []
    for (outlet_id, actor_id), members in groups.items():
        _, _, totals = _figures(connection, tenant_id, members, since=start)
        out.append({"outletId": outlet_id, "actorId": actor_id, **totals})
    return out


# --------------------------------------------------------------------------- the worker's round

def end_of_day_due(now: datetime | None = None, at: time | None = None) -> str | None:
    """The daily slot that is open now: the Project's run time (India time; 21:00 unless it
    set another) passed, at most three hours ago -- so a worker that was down then still
    runs it, and it does not run at any hour after."""
    local = (now or datetime.now(UTC)).astimezone(IST)
    slot = datetime.combine(local.date(), at or END_OF_DAY_IST, tzinfo=IST)
    if local < slot:
        slot -= timedelta(days=1)
    return slot.date().isoformat() if local - slot < _END_OF_DAY_CATCHUP else None


def drive_round(engine: Engine, tenant_ids: list[str], *, worker_id: str | None = None,
                now: datetime | None = None) -> dict[str, Any]:
    """Every few minutes, for each Project whose Drive is connected: the outlets' folders are
    there; what came into Drive is recorded (so the PC sees what waits before submitting);
    the end-of-day run is asked for once; what the reading decided is brought back; and once
    a day the folders of journeys closed long enough go to the archive."""
    from audit_core.uc03_p2_runtime import claim_scheduled_run

    out: dict[str, Any] = {}
    if google_drive.drive_settings() is None:
        return out
    for tenant_id in connected_tenants(engine, tenant_ids):
        try:
            linked = connected_client(engine, tenant_id)
            if linked is None:
                continue
            client, link = linked
            result: dict[str, Any] = {"tree": ensure_tree(engine, tenant_id, client),
                                      "shares": ensure_shares(engine, tenant_id, client),
                                      "found": sync(engine, tenant_id, client, link)}
            due = end_of_day_due(now, link["daily_run_time"]) if link["daily_run_enabled"] else None
            if due is not None:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    if claim_scheduled_run(connection, tenant_id=tenant_id, run_name=END_OF_DAY_RUN, run_key=due,
                                           claimed_by=worker_id):
                        run = request_run(connection, tenant_id=tenant_id, trigger="END_OF_DAY", actor_id=SYSTEM_ACTOR,
                                          role="SYSTEM", name="System - end of day")
                        result["endOfDayRun"] = str(run["run_id"])
            result["settled"] = settle(engine, tenant_id, client)
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                archive = claim_scheduled_run(connection, tenant_id=tenant_id, run_name=ARCHIVE_RUN,
                                              run_key=ist_today(now).isoformat(), claimed_by=worker_id)
            if archive:
                result["archive"] = archive_closed(engine, tenant_id, client, now)
            out[tenant_id] = result
        except Exception as exc:
            logger.warning("p2_drive_round_failed", tenant_id=tenant_id, exc_info=True)
            _note_connection(engine, tenant_id, error=str(exc), lost=isinstance(exc, DriveAuthorizationLost))
    return out


# --------------------------------------------------------------------------- API

class DriveRunCommand(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    outletId: UUID | None = None
    dealerId: UUID | None = None
    day: date | None = Field(default=None, alias="date")
    mode: str | None = None
    requestedByName: str | None = Field(default=None, max_length=300)


class DriveAccessCommand(BaseModel):
    googleEmail: str = Field(min_length=3, max_length=320)


class DriveSettingsCommand(BaseModel):
    """Only what is sent is changed. ``outletIds``: the outlets that use Drive; null for every outlet."""

    retentionDays: int | None = Field(default=None, ge=0, le=3650)
    dailyRunTime: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    dailyRunEnabled: bool | None = None
    outletIds: list[UUID] | None = None


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    def at(value: Any) -> str | None:
        return value.isoformat() if value else None

    return {
        "runId": str(run["run_id"]), "trigger": run["run_trigger"], "mode": run["run_mode"],
        "status": run["run_status"], "requestedBy": run["requested_by_actor_id"],
        "requestedByRole": run["requested_by_role"], "requestedByName": run["requested_by_name"],
        "dealerId": str(run["dealer_id"]) if run["dealer_id"] else None,
        "outletId": str(run["outlet_id"]) if run["outlet_id"] else None,
        "date": run["upload_date"].isoformat() if run["upload_date"] else None,
        "summary": dict(run["summary"] or {}), "detail": run["detail"],
        "requestedAtUtc": at(run["requested_at_utc"]), "startedAtUtc": at(run["started_at_utc"]),
        "finishedAtUtc": at(run["finished_at_utc"]),
    }


def _caller(connection: Connection, tenant_id: str, principal: HumanPrincipal, client: SecurityAuthorizationClient,
            permission: str) -> dict[str, Any]:
    """Who is asking: an administrator, a lead (Team Lead, PM, Executive, Reviewer) and / or
    a PC, with the outlets they are mapped to."""
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                          authorization_client=client, permission_key=permission)
    admin = (access.functional_role or "").upper() in _ADMIN_ROLES
    outlets = scope_outlets(connection, tenant_id, principal.subject)

    def roles_of(outlet: dict[str, Any]) -> set[str]:
        return {str(r).upper() for r in (outlet.get("roles") or [])}

    roles = {r for o in outlets for r in roles_of(o)}
    lead_outlets = {str(o["outlet_id"]) for o in outlets if _LEAD_ROLES & roles_of(o)}
    pc_outlets = {str(o["outlet_id"]) for o in outlets if "PC" in roles_of(o)}
    if admin:
        lead_outlets = {str(o["outlet_id"]) for o in outlets_with_pcs(connection, tenant_id)}
    lead_role = next((r for r in ("EXECUTIVE", "EXEC", "PM", "TL", "REVIEWER") if r in roles), None)
    return {"admin": admin, "lead": admin or bool(lead_outlets), "leadOutlets": lead_outlets, "pcOutlets": pc_outlets,
            "role": (access.functional_role if admin else lead_role or ("PC" if pc_outlets else None)),
            "outlets": lead_outlets | pc_outlets}


def _public_settings(link: dict[str, Any] | None) -> dict[str, Any]:
    if link is None:
        return {"retentionDays": None, "dailyRunTime": f"{END_OF_DAY_IST:%H:%M}", "dailyRunEnabled": True,
                "outletIds": None}
    return {
        "retentionDays": int(link["retention_days"]), "dailyRunTime": f"{link['daily_run_time']:%H:%M}",
        "dailyRunEnabled": bool(link["daily_run_enabled"]),
        "outletIds": None if link["outlet_ids"] is None else [str(o) for o in link["outlet_ids"]],
    }


def _settings_or_503() -> DriveSettings:
    settings = google_drive.drive_settings()
    if settings is None:
        raise HTTPException(status_code=503, detail="Google Drive is not set up for Verigence yet.")
    return settings


@router.get("/tenants/{tenant_id}/integrations/google-drive")
def get_drive_connection(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Whether the Project's Google Drive is connected, as which account, and how many files
    of the caller's outlets wait to be taken."""
    who = _caller(connection, tenant_id, principal, client, _READ)
    link = _connection_row(connection, tenant_id)
    waiting = {str(r["outlet_id"]): int(r["n"]) for r in connection.execute(
        text("SELECT outlet_id, count(*) AS n FROM auditcore.p2_drive_files WHERE tenant_id=:t AND file_status='NEW' "
             "AND outlet_id IS NOT NULL GROUP BY outlet_id"), {"t": tenant_id},
    ).mappings() if str(r["outlet_id"]) in who["outlets"]}
    return {
        "configured": google_drive.drive_settings() is not None,
        "connected": bool(link and link["connection_status"] == "CONNECTED"),
        "status": link["connection_status"] if link else "NOT_CONNECTED",
        "account": link["google_email"] if link else None,
        "folderUrl": (f"https://drive.google.com/drive/folders/{link['root_folder_id']}"
                      if link and link["root_folder_id"] else None),
        **_public_settings(link),
        # For the administrator's choice of outlets: every outlet, with whether it has a PC.
        "outlets": [{"dealerId": str(o["dealer_id"]), "dealerName": o["dealer_name"], "outletId": str(o["outlet_id"]),
                     "outletName": o["outlet_name"], "pcs": len(o["pcs"]),
                     "usesDrive": bool(o["pcs"]) and (link is None or uses_drive(link, o["outlet_id"]))}
                    for o in outlets_with_pcs(connection, tenant_id)] if who["admin"] else None,
        "lastCheckedAtUtc": link["last_synced_at_utc"].isoformat() if link and link["last_synced_at_utc"] else None,
        "problem": link["last_error"] if link else None,
        "canConnect": who["admin"], "canRun": who["lead"] or bool(who["pcOutlets"]),
        "waitingByOutlet": waiting, "waiting": sum(waiting.values()),
        # A PC: the Google account their outlets' folders are shared with, and the folders.
        "myAccess": my_access(connection, tenant_id, link, principal.subject, who["pcOutlets"])
        if who["pcOutlets"] else None,
        # The administrator: every PC and whether their folders are shared.
        "people": access_overview(connection, tenant_id, link) if who["admin"] else None,
    }


@router.post("/tenants/{tenant_id}/drive/access")
def ask_drive_access(
    tenant_id: str,
    command: DriveAccessCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    """A PC asks for the folders of the outlets they upload for: each is shared with their own
    Google account -- the email they sign in to Verigence with, sent by the app -- and Google
    emails them the links. Asked again with another address, the folders move to that one."""
    who = _caller(connection, tenant_id, principal, client, _UPDATE)
    if not who["pcOutlets"]:
        raise HTTPException(status_code=403, detail="Only a PC is given the folders of the outlets they upload for.")
    email = command.googleEmail.strip().lower()
    if not _EMAIL.match(email):
        raise HTTPException(status_code=422, detail="Give the email of your Google account.")
    link = _connection_row(connection, tenant_id)
    if link is None or link["connection_status"] != "CONNECTED":
        raise HTTPException(status_code=409, detail="The Project's Google Drive is not connected.")
    with engine.begin() as own:
        set_tenant_context(own, tenant_id)
        own.execute(
            text("INSERT INTO auditcore.p2_drive_people (tenant_id, actor_id, google_email) VALUES (:t, :a, :e) "
                 "ON CONFLICT (tenant_id, actor_id) DO UPDATE SET google_email=EXCLUDED.google_email, "
                 "updated_at_utc=now()"), {"t": tenant_id, "a": principal.subject, "e": email},
        )
    linked = connected_client(engine, tenant_id)
    if linked is None:
        raise HTTPException(status_code=409, detail="The Project's Google Drive is not connected.")
    try:
        ensure_shares(engine, tenant_id, linked[0], only_actor=principal.subject)
    except DriveError as exc:
        logger.warning("p2_drive_access_failed", tenant_id=tenant_id, actor_id=principal.subject, error=str(exc))
        raise HTTPException(status_code=502, detail="Google Drive could not share the folders just now. "
                                                    "Try again in a few minutes.") from exc
    with engine.begin() as own:
        set_tenant_context(own, tenant_id)
        return my_access(own, tenant_id, _connection_row(own, tenant_id), principal.subject, who["pcOutlets"])


@router.put("/tenants/{tenant_id}/integrations/google-drive/settings")
def set_drive_settings(
    tenant_id: str,
    command: DriveSettingsCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The Project's Drive settings (an administrator): how many days after a journey is closed
    its folder goes to the archive, when the daily run starts (India time) and whether it runs
    at all, and which outlets use Drive."""
    who = _caller(connection, tenant_id, principal, client, _UPDATE)
    if not who["admin"]:
        raise HTTPException(status_code=403, detail="Only an administrator changes the Drive settings.")
    link = _connection_row(connection, tenant_id)
    if link is None:
        raise HTTPException(status_code=409, detail="The Project's Google Drive is not connected.")
    sent = command.model_fields_set
    values: dict[str, Any] = {}
    if command.retentionDays is not None:
        values["retention_days"] = command.retentionDays
    if command.dailyRunTime is not None:
        values["daily_run_time"] = time.fromisoformat(command.dailyRunTime)
    if command.dailyRunEnabled is not None:
        values["daily_run_enabled"] = command.dailyRunEnabled
    if "outletIds" in sent:
        known = {str(o["outlet_id"]) for o in outlets_with_pcs(connection, tenant_id)}
        chosen = None if command.outletIds is None else sorted({str(o) for o in command.outletIds})
        if chosen is not None and not set(chosen) <= known:
            raise HTTPException(status_code=422, detail="An outlet chosen is not one of this Project's.")
        values["outlet_ids"] = chosen
    if values:
        sets = ", ".join(f"{k}=CAST(:{k} AS uuid[])" if k == "outlet_ids" else f"{k}=:{k}" for k in values)
        connection.execute(
            text(f"UPDATE auditcore.p2_drive_connections SET {sets}, updated_at_utc=now() WHERE tenant_id=:t"),
            {**values, "t": tenant_id},
        )
        link = _connection_row(connection, tenant_id)
    return _public_settings(link)


@router.post("/tenants/{tenant_id}/integrations/google-drive/connect")
def connect_drive(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Where to send the browser to approve as the Project's Google account (an administrator)."""
    who = _caller(connection, tenant_id, principal, client, _UPDATE)
    if not who["admin"]:
        raise HTTPException(status_code=403, detail="Only an administrator connects the Project's Google Drive.")
    settings = _settings_or_503()
    state = google_drive.sign_state(settings, tenant_id=tenant_id, actor_id=principal.subject)
    return {"authUrl": google_drive.consent_url(settings, state)}


@router.get("/integrations/google-drive/callback", include_in_schema=False)
def drive_callback(
    engine: Annotated[Engine, Depends(get_engine)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Google sends the browser back here after the approval. It carries no Verigence login:
    the signed state says which Project asked and who. The refresh token is sealed and kept;
    the browser goes back to Verigence with the outcome. One Google account may be the Drive
    of several Projects: each has its own Project folder in it."""
    settings = _settings_or_503()
    asked = google_drive.read_state(settings, state or "")

    def back(outcome: str) -> RedirectResponse:
        # The page the browser lands on shows the Project the person has open in Verigence,
        # which need not be the one that asked: the answer says which Project it is about.
        about = {"drive": outcome, **({"project": asked["tenant_id"]} if asked and asked["tenant_id"] else {})}
        separator = "&" if "?" in settings.return_url else "?"
        return RedirectResponse(f"{settings.return_url}{separator}{urlencode(about)}", status_code=303)

    if asked is None or not asked["tenant_id"]:
        return back("expired")
    if error or not code:
        return back("declined")
    tenant_id = asked["tenant_id"]
    try:
        tokens = google_drive.exchange_code(settings, code)
        refresh_token = str(tokens.get("refresh_token") or "")
        if not refresh_token:
            return back("no-offline-access")
        drive = open_client(settings, refresh_token)
        email, start = drive.account_email(), drive.start_page_token()
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            if connection.execute(
                text("SELECT 1 FROM auditcore.projects WHERE tenant_id=:t"), {"t": tenant_id},
            ).first() is None:
                return back("expired")
            before = _connection_row(connection, tenant_id)
            same = before is not None and before["google_email"] == email
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.p2_drive_connections (tenant_id, google_email, refresh_token_sealed,
                        changes_token, connected_by_actor_id)
                    VALUES (:t, :email, :sealed, :token, :actor)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        google_email=EXCLUDED.google_email, refresh_token_sealed=EXCLUDED.refresh_token_sealed,
                        connection_status='CONNECTED', last_error=NULL,
                        connected_by_actor_id=EXCLUDED.connected_by_actor_id, connected_at_utc=now(),
                        updated_at_utc=now(),
                        root_folder_id = CASE WHEN :same THEN auditcore.p2_drive_connections.root_folder_id END,
                        archive_folder_id = CASE WHEN :same THEN auditcore.p2_drive_connections.archive_folder_id END,
                        changes_token = CASE WHEN :same THEN COALESCE(auditcore.p2_drive_connections.changes_token,
                                                                       EXCLUDED.changes_token)
                                             ELSE EXCLUDED.changes_token END
                    """
                ),
                {"t": tenant_id, "email": email, "sealed": google_drive.seal(settings, refresh_token),
                 "token": start, "actor": asked["actor_id"], "same": same},
            )
            if before is not None and not same:
                # Another account: its Drive has none of the folders made in the first one.
                connection.execute(
                    text("UPDATE auditcore.p2_drive_folders SET gone_at_utc=now(), updated_at_utc=now() "
                         "WHERE tenant_id=:t AND gone_at_utc IS NULL"), {"t": tenant_id},
                )
        ensure_tree(engine, tenant_id, drive)
    except Exception:
        logger.warning("p2_drive_connect_failed", tenant_id=tenant_id, exc_info=True)
        return back("failed")
    logger.info("p2_drive_connected", tenant_id=tenant_id, account=email)
    return back("connected")


@router.post("/tenants/{tenant_id}/drive/runs", status_code=201)
def start_drive_run(
    tenant_id: str,
    command: DriveRunCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Take the files waiting in Drive. A PC submits an outlet they upload for; a Team Lead,
    PM, Executive or Reviewer asks for a day of upload and an outlet, a dealership, or
    everything they are mapped to (an administrator: the Project)."""
    who = _caller(connection, tenant_id, principal, client, _UPDATE)
    link = _connection_row(connection, tenant_id)
    if link is None or link["connection_status"] != "CONNECTED":
        raise HTTPException(status_code=409, detail="The Project's Google Drive is not connected.")
    mode = (command.mode or "NOW").strip().upper()
    if mode not in ("NOW", "BATCH"):
        raise HTTPException(status_code=422, detail="mode is NOW or BATCH.")
    outlet_id = str(command.outletId) if command.outletId else None
    places = {str(o["outlet_id"]): o for o in outlets_with_pcs(connection, tenant_id)}
    if outlet_id and outlet_id not in places:
        raise HTTPException(status_code=404, detail="Outlet not found in this Project.")
    if who["lead"] and (outlet_id is None or outlet_id in who["leadOutlets"]):
        trigger = "ON_DEMAND"
        of_dealer = [o for o in places.values() if str(o["dealer_id"]) == str(command.dealerId)]
        if command.dealerId and not any(str(o["outlet_id"]) in who["leadOutlets"] for o in of_dealer):
            raise HTTPException(status_code=404, detail="Dealership not found in your Project scope.")
        wide = who["admin"] or connection.execute(
            text("SELECT 1 FROM auditcore.business_assignments WHERE tenant_id=:t AND security_actor_id=:a "
                 "AND assignment_status='ACTIVE' AND effective_from <= now() "
                 "AND (effective_to IS NULL OR effective_to >= now()) AND dealer_id IS NULL LIMIT 1"),
            {"t": tenant_id, "a": principal.subject},
        ).first() is not None
        if not outlet_id and not command.dealerId and not wide:
            raise HTTPException(status_code=422, detail="Choose a dealership or an outlet you are mapped to.")
        if command.dealerId and not outlet_id and not wide and any(
                str(o["outlet_id"]) not in who["leadOutlets"] for o in of_dealer):
            raise HTTPException(status_code=422, detail="Choose an outlet: you are not mapped to the whole dealership.")
    elif outlet_id and outlet_id in who["pcOutlets"]:
        trigger = "PC_SUBMIT"
    else:
        raise HTTPException(status_code=403, detail="Choose an outlet you upload for.")
    run = request_run(
        connection, tenant_id=tenant_id, trigger=trigger, actor_id=principal.subject,
        role="PC" if trigger == "PC_SUBMIT" else who["role"], name=command.requestedByName,
        dealer_id=None if outlet_id else command.dealerId, outlet_id=command.outletId,
        upload_date=None if trigger == "PC_SUBMIT" else command.day, mode=mode,
    )
    return _public_run(run)


def _visible(run: dict[str, Any], who: dict[str, Any], places: dict[str, Any], actor_id: str) -> bool:
    if who["admin"] or run["requested_by_actor_id"] == actor_id:
        return True
    if run["outlet_id"]:
        return str(run["outlet_id"]) in who["outlets"]
    if run["dealer_id"]:
        return any(str(places[o]["dealer_id"]) == str(run["dealer_id"]) for o in who["leadOutlets"] if o in places)
    return who["lead"]


@router.get("/tenants/{tenant_id}/drive/runs")
def list_drive_runs(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """The runs the caller may see, newest first: their own, their outlets', and for a lead
    the Project-wide ones (the end of the day)."""
    who = _caller(connection, tenant_id, principal, client, _READ)
    places = {str(o["outlet_id"]): o for o in outlets_with_pcs(connection, tenant_id)}
    rows = [dict(r) for r in connection.execute(
        text("SELECT * FROM auditcore.p2_drive_runs WHERE tenant_id=:t ORDER BY requested_at_utc DESC LIMIT 300"),
        {"t": tenant_id},
    ).mappings().all()]
    items = [_public_run(r) for r in rows if _visible(r, who, places, principal.subject)][:limit]
    return {"items": items, "inProgress": sum(1 for r in items if r["status"] in ("REQUESTED", "RUNNING", "READING"))}


@router.get("/tenants/{tenant_id}/drive/runs/{run_id}")
def get_drive_run(
    tenant_id: str,
    run_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """The report of a run, limited to the outlets the caller is mapped to."""
    who = _caller(connection, tenant_id, principal, client, _READ)
    report = run_report(connection, tenant_id=tenant_id, run_id=run_id)
    if not who["admin"]:
        report["customers"] = [c for c in report["customers"] if c["outletId"] in who["outlets"]]
        if not report["customers"] and report["run"]["requestedBy"] != principal.subject:
            raise HTTPException(status_code=404, detail="Drive run not found.")
    return report


@router.get("/tenants/{tenant_id}/drive/files")
def list_drive_files(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    day: Annotated[date | None, Query(alias="date")] = None,
    outlet_id: Annotated[UUID | None, Query(alias="outletId")] = None,
    waiting_only: Annotated[bool, Query(alias="waitingOnly")] = False,
) -> dict[str, Any]:
    """The record: every file put in Drive on a day (today by default) in the caller's
    outlets, with what became of it; or only the ones still waiting to be taken."""
    who = _caller(connection, tenant_id, principal, client, _READ)
    day = day or ist_today()
    files = [dict(r) for r in connection.execute(
        text(_FILE_SQL + """
             WHERE f.tenant_id=:t AND f.outlet_id IS NOT NULL
               AND (CAST(:outlet AS uuid) IS NULL OR f.outlet_id=CAST(:outlet AS uuid))
               AND ((:waiting AND f.file_status IN ('NEW', 'WAITING')) OR (NOT :waiting AND f.upload_date=:day))
             ORDER BY f.outlet_id, cf.folder_name, f.file_name LIMIT 2000"""),
        {"t": tenant_id, "outlet": outlet_id, "waiting": waiting_only, "day": day},
    ).mappings().all() if str(r["outlet_id"]) in who["outlets"]]
    rows, _, totals = _figures(connection, tenant_id, files, since=None)
    return {
        "date": day.isoformat(), "totals": totals,
        "files": [{
            "outletId": str(r["outlet_id"]), "customerFolder": r["customer_folder"], "bookingNumber": r["booking_number"],
            "journeyId": str(r["journey_id"]) if r["journey_id"] else None, "journeyReference": r["placed_reference"],
            **_public_file(r),
        } for r in rows],
    }
