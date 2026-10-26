"""Where a Phase 2 upload is kept in object storage (2026-10-26).

New uploads go under a date, so a week or a day can be found, audited and
cleaned up by one prefix:

    p2-documents/<tenant>/<yyyy-Www>/<yyyy-mm-dd>/<journey>/<batch>/original/<outlet>__<pc>__<file>

The date is the upload day in IST (Asia/Kolkata); the week is the ISO week.
The page files and merged files of a batch sit beside the original, under the
same ``<batch>`` folder, so a batch is found from its original's key alone.

Uploads made before this change stay where they are
(``p2-documents/<tenant>/<journey>/<batch>/...``): every reader takes the key
from the database, never builds it, so both layouts work side by side.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_ORIGINAL = "/original/"
_LABEL_MAX = 40
_FILE_MAX = 180


def safe_part(value: object, *, fallback: str, limit: int = _LABEL_MAX) -> str:
    """One path-safe word: letters, digits, '.', '_' and '-' only."""
    text_ = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9.-]+", "-", str(value or "").strip())).strip("-.")
    return text_[:limit].strip("-.") or fallback


def safe_filename(value: str) -> str:
    name = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip() or "document"))
    return name[:_FILE_MAX] or "document"


def ist_day(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(IST)


def date_folders(now: datetime | None = None) -> str:
    """``<yyyy-Www>/<yyyy-mm-dd>`` for the IST day."""
    day = ist_day(now)
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}/{day:%Y-%m-%d}"


def batch_root(tenant_id: str, journey_id: UUID | str, batch_id: UUID | str, *, now: datetime | None = None) -> str:
    return f"p2-documents/{tenant_id}/{date_folders(now)}/{journey_id}/{batch_id}"


def original_key(
    tenant_id: str, journey_id: UUID | str, batch_id: UUID | str, *, filename: str,
    outlet_name: object = None, pc_name: object = None, now: datetime | None = None,
) -> str:
    """The key of an uploaded original: the outlet and the PC are in the file
    name, so they show when the bucket is browsed."""
    name = f"{safe_part(outlet_name, fallback='outlet')}__{safe_part(pc_name, fallback='pc')}__{safe_filename(filename)}"
    return f"{batch_root(tenant_id, journey_id, batch_id, now=now)}/original/{name[:_FILE_MAX + 2 * (_LABEL_MAX + 2)]}"


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
