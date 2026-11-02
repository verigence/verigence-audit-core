"""Where a Phase 2 upload is kept in object storage.

Product owner 2026-11-02: every uploaded file is kept per outlet and per day,
and its name carries the customer's name, so the bucket can be browsed outlet
by outlet, day by day, and a file told apart by whose it is:

    p2-documents/<tenant>/<outlet>/<yyyy-mm-dd>/<journey>/<batch>/original/<file>__<customer>__<pc>.<ext>

The day is the upload day in IST (Asia/Kolkata). The outlet is the booking's
outlet; the customer is the booking's customer, by the KYC name once one is on
file, else as the PC entered them (``upload_labels``); the PC is the one who
made the booking (2026-10-26: outlet and PC in the name). The page
files and merged files of a batch sit beside the original, under the same
``<batch>`` folder, so a batch is found from its original's key alone. Nothing
is read before the file is in storage: the upload is accepted only once the
stored object is there at its full size (uc03_p2_api finalize), and only then
is it split and read.

Uploads made before stay where they are -- 26 Oct to 2 Nov under
``p2-documents/<tenant>/<yyyy-Www>/<yyyy-mm-dd>/<journey>/<batch>/original/<outlet>__<pc>__<file>``,
earlier under ``p2-documents/<tenant>/<journey>/<batch>/...``: every reader
takes the key from the database, never builds it, so all layouts work side by
side.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from sqlalchemy import Connection

IST = ZoneInfo("Asia/Kolkata")
_ORIGINAL = "/original/"
_LABEL_MAX = 40
_OUTLET_MAX = 60
_FILE_MAX = 180
_EXTENSION = re.compile(r"^[A-Za-z0-9]{1,5}$")


def safe_part(value: object, *, fallback: str, limit: int = _LABEL_MAX) -> str:
    """One path-safe word: letters, digits, '.', '_' and '-' only."""
    text_ = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9.-]+", "-", str(value or "").strip()))
    text_ = re.sub(r"-{2,}", "-", text_).strip("-.")
    return text_[:limit].strip("-.") or fallback


def safe_filename(value: str) -> str:
    name = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip() or "document"))
    return name[:_FILE_MAX] or "document"


def ist_day(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(IST)


def day_folder(now: datetime | None = None) -> str:
    """``<yyyy-mm-dd>``, the IST day."""
    return f"{ist_day(now):%Y-%m-%d}"


def batch_root(
    tenant_id: str, journey_id: UUID | str, batch_id: UUID | str, *, outlet_name: object = None,
    now: datetime | None = None,
) -> str:
    """The folder of one upload: per outlet, then per IST day."""
    outlet = safe_part(outlet_name, fallback="outlet", limit=_OUTLET_MAX)
    return f"p2-documents/{tenant_id}/{outlet}/{day_folder(now)}/{journey_id}/{batch_id}"


def original_key(
    tenant_id: str, journey_id: UUID | str, batch_id: UUID | str, *, filename: str,
    outlet_name: object = None, customer_name: object = None, pc_name: object = None,
    now: datetime | None = None,
) -> str:
    """The key of an uploaded original: under its outlet and day, the file's
    own name with the customer's name (and the PC's) appended, so they show
    when the bucket is browsed."""
    stem, dot, extension = str(filename or "").strip().rpartition(".")
    if not dot or not stem.strip() or not _EXTENSION.match(extension):
        stem, extension = str(filename or ""), ""
    stem = safe_filename(stem)
    name = (f"{stem[:_FILE_MAX]}__{safe_part(customer_name, fallback='customer')}"
            f"__{safe_part(pc_name, fallback='pc')}" + (f".{extension}" if extension else ""))
    return f"{batch_root(tenant_id, journey_id, batch_id, outlet_name=outlet_name, now=now)}/original/{name}"


def root_of(original_object_key: str) -> str | None:
    """The batch folder that holds an original, whichever layout it uses."""
    head, found, _ = str(original_object_key).rpartition(_ORIGINAL)
    return head if found else None


def journey_folder_of(key: str, journey_id: UUID | str) -> str | None:
    """The ``.../<journey>/`` folder a key sits in, for the journey-delete
    sweep of anything the database does not list."""
    marker = f"/{journey_id}/"
    head, found, _ = str(key).partition(marker)
    return f"{head}{marker}" if found else None


def upload_labels(
    connection: Connection, *, tenant_id: str, journey_id: UUID | str,
) -> tuple[str | None, str | None, str | None]:
    """The outlet name, the PC's name and the customer's name of a Journey,
    for where and under what name its uploads are stored. The customer's name
    is the KYC name once one is on file (legal_name: the customer's name rule),
    else the name the PC entered on the booking. A booking started from an
    upload has no name yet (its entered name is only a placeholder, the
    journey's id): its file is stored under "customer" until a name is read."""
    from sqlalchemy import text

    row = connection.execute(
        text(
            """
            SELECT o.outlet_name, j.created_by_display_name,
                   CASE
                     WHEN NULLIF(btrim(c.legal_name), '') IS NOT NULL THEN c.legal_name
                     WHEN c.display_name = CAST(j.journey_id AS text)
                       OR c.display_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN NULL
                     ELSE c.display_name
                   END
            FROM auditcore.journeys j
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
            LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            WHERE j.tenant_id = :t AND j.journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).one_or_none()
    return (row[0], row[1], row[2]) if row else (None, None, None)
