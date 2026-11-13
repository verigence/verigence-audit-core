"""The same file uploaded again, and a duplicate booking across outlets.

Product owner 2026-11-04 (enhancements to the rules below, which all still hold):
    - The same file at ONE OUTLET (PAN/Aadhaar + mobile + model + outlet, and,
      as before, the customer name and the booking reference number): the
      journey with FEWER documents is deleted ("keep deleting the files where
      we have less number of documents uploaded in the journey"); the earlier
      one is kept on a tie.
    - The same at another outlet of the SAME DEALERSHIP: nothing is deleted;
      the PCs of both bookings may not start an upload until a Reviewer
      deletes the duplicate ("temporary block the upload for them ... they have
      to contact the reviewer to get the file deleted, once duplicate file got
      deleted, their access will be back"): DUPLICATE_SAME_DEALERSHIP, for the
      PCs, Reviewers and Team Leads of both outlets, up while both bookings
      are open (uc03_p2_upload_pause.duplicate_block).
    - The same at ANOTHER DEALERSHIP: nothing is deleted; a big red alert on
      every screen of the Project, with a sound for the PCs of both bookings
      (DUPLICATE_ACROSS_OUTLETS, no audience).
    - The same mobile only, at another dealership: a DUPLICATE_BOOKING finding
      of less probability (uc03_duplicate_booking_detection,
      MOBILE_OTHER_DEALERSHIP).

Product owner 2026-11-03: the "same file" is a PC's mistake only within ONE
OUTLET. The same customer (PAN and/or Aadhaar), mobile and model on open
journeys of two DIFFERENT outlets -- each dealership numbers its own bookings,
so no booking reference number is compared -- is a DUPLICATE BOOKING ACROSS
OUTLETS, raised once per newer journey created within the window below.

Documents only the copy holds (product owner 2026-11-03, for every upload,
bulk or not; within one outlet): "before delete, check if the duplicate file
has additional documents which match the given customer; if yes, upload those
documents to the journey; if it is the same set of documents, the journey gets
deleted" -- by page fingerprint ("avoid any loss of document"): a page of the
copy with no page of the same SHA-256 on the earlier journey is new, and the
document holding it is copied, byte for byte, to a new upload of the earlier
journey (uc03_duplicate_merge._copy_unit: split and read there like any
upload) before the copy is deleted (``carry_plan``). Only the customer's own
files (file identity BELONGS) are carried; a new page of a file not settled as
the customer's, or another customer's file on the copy, keeps the copy for
review (KEPT_OWN_DOCUMENTS) -- nothing is deleted that could be lost.

The same file uploaded again (product owner, 2026-10-31).

    "When ever a PC enters a Journey which has existing PAN, Mobile No,
    Customer Name, Model same, Pls raise an Alert across all PC telling Same
    File Being Uploaded. Give the name of customer, Model, PC Name and
    Dealership, and delete the journey completely. This alert should be seen
    by all team on their respective screens, if they are active."

What is "the same file" (product owner, 2026-11-02; the same outlet, 2026-11-03)
    Two open journeys of one dealership that agree on ALL of:
      - the customer: the same PAN and/or the same Aadhaar -- either one
        equal is enough (``pan_number`` in PAN shape by ``_valid_pan``;
        ``aadhaar_number`` as 12 digits by ``_valid_aadhaar``; each the
        journey's highest-confidence value). A PAN, or an Aadhaar, read on
        both copies and different says two people: never the same file;
      - mobile: the highest-confidence ``customer_phone``, as a real Indian
        mobile (``_normalize_mobile``: the last 10 digits, starting 6-9), equal;
      - model: both SKUs pinned -> the same ``product_skus.model_id``;
        otherwise the model names (the master's once pinned, the booking
        form's until then) are one family by ``generation_key``
        ("NEW THAR 2WD" is "THAR"; "THAR ROXX" is not);
      - booking reference number: a ``booking_reference_number`` read from a
        booking form / booking docket still on file, in common
        (``normalize_booking_reference``). Each dealership numbers its own
        bookings, so two journeys of different dealerships are never the
        same file. Two copies whose booking numbers were read on both and
        share none are two bookings, not a copy;
      - customer name (kept from 2026-10-31): the KYC name on the customer
        (``customers.legal_name``, VERIFIED), the same person by
        ``same_person`` -- a masked PAN name ("PXXXXJ KXXXR") by
        ``masked_matches``, two masked names by equality.
    When everything else agrees but the booking number was not read on one
    copy (or either), the copy is NOT deleted: the alert goes out and the copy
    is kept for review (KEPT_NO_BOOKING_REFERENCE). Until 2026-11-02 the same
    receipt number could stand in for the mobile; no longer -- all of the
    above must hold. Closed journeys (delivered, booking cancelled, duplicate
    booking, no delivery) are never paired. The DUPLICATE_BOOKING rule (one
    basis is enough, a TL task) is unchanged; this is the stricter check on
    top of it.

Which one is the copy (2026-11-04)
    The journey with fewer documents on file; on a tie the later
    ``created_at_utc``, ``journey_id`` breaking a tie -- the same order
    whichever journey's check runs, so exactly one side is ever the copy.
    Should the copy gain more documents before its delete, it is kept.
    The check runs in the NATIVE:BOOKING unit of either journey; when the
    older one is the last to have all its values, its own run flags the
    newer one.

What happens to it
    - An individual's PAN (4th character "P"): the new journey is deleted
      completely (Document Intelligence, R2, Audit Core), by the system, as
      its own SAME_FILE_DELETE work item after this check has committed --
      never inside the check's transaction. The alert is published once the
      delete is done.
    - A company, firm or any other PAN (4th character C, F, H, A, T, B, L,
      J, G ...): alert only, the journey is kept for review -- a fleet buyer
      can genuinely book two identical vehicles (product owner 2026-10-31).
    - A journey whose documents a reviewer already marked reviewed is kept
      too (the delete guard), and the alert says so.
    - A copy whose booking reference number was not read (on either side)
      is kept and alerted (KEPT_NO_BOOKING_REFERENCE). Once the booking
      numbers are read and agree, the check runs again and the copy is
      deleted after all (the kept alert is turned into a pending delete).
    - Every copy deleted keeps, on its ``p2_journey_deletions`` row, the PAN,
      the mobile, the Aadhaar masked to its last four digits ("XXXX XXXX
      1234" -- the full number is never stored), the booking reference
      number, the model and the copy kept: the "Deleted" tab of Duplicate
      bookings lists them (uc03_duplicate_bookings_report).

Safeguards (review of #594, 2026-10-31)
    - Product owner 2026-11-12: "i never put a rule of 48 hours". A same-outlet copy is
      acted on whenever it is found, however old; only the duplicate-across-outlets
      alert still looks at newly uploaded journeys (P2_SAME_FILE_WINDOW_HOURS), as an
      old pair would otherwise block both PCs' uploads at once.
    - Product owner 2026-11-12: "if it exactly same documents auto delete the booking".
      A copy whose every page is already on the kept journey (page fingerprint) is
      deleted even when the booking looks like a company's or its booking number was
      not read: identical pages cannot be a second vehicle.
    - Never deleted when anything says the booking is a company's or firm's:
      a non-individual PAN on any document on file of either journey, a
      GSTIN on either (not a sole proprietor's GSTIN built on the customer's
      own PAN, 2026-11-01), or a company / firm customer name.
    - Only values of documents still on file (ACTIVE evidence) count.
    - At delete time the earlier journey is locked and everything is checked
      again; a journey still receiving pages waits ten quiet minutes; the R2
      folders are swept once more after the delete; an unfinished system
      delete is finished; a journey removed another way publishes its alert.

Once per journey
    The alert row is the marker: (tenant, SAME_FILE_UPLOADED, new journey) is
    unique, so a recomputed check, or the two journeys' checks running at
    the same time, raise one alert.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.errors import ConflictError, NotFoundError
from audit_core.uc03_duplicate_booking_detection import (
    _buyer_gstin,
    _normalize_mobile,
    _placeholder,
    _valid_aadhaar,
    _valid_gstin,
    _valid_pan,
)
from audit_core.uc03_p2_names import (
    _is_company,
    is_firm_name,
    is_masked,
    masked_matches,
    person_tokens,
    same_person,
)
from audit_core.uc03_p2_runtime import enqueue_work
from audit_core.uc03_sku_words import generation_key

logger = structlog.get_logger(__name__)

ALERT_KIND = "SAME_FILE_UPLOADED"
CROSS_OUTLET_KIND = "DUPLICATE_ACROSS_OUTLETS"
CROSS_OUTLET_TITLE = "Duplicate booking across dealerships"
# 2026-11-04: another outlet of the same dealership -- uploads paused for both PCs.
SAME_DEALERSHIP_KIND = "DUPLICATE_SAME_DEALERSHIP"
SAME_DEALERSHIP_TITLE = "Duplicate booking at two outlets: uploads paused"
DELETE_WORK_TYPE = "SAME_FILE_DELETE"
ALERT_HOURS = 24
ALERT_TITLE = "Same file being uploaded"
# Only the duplicate-across-outlets alert looks at newly uploaded journeys alone (an old
# pair would block both PCs' uploads at once). The same-outlet copy has no window: product
# owner 2026-11-12, "i never put a rule of 48 hours" -- the window added in review of #594
# is gone for it.
SAME_FILE_WINDOW_HOURS = int(os.environ.get("P2_SAME_FILE_WINDOW_HOURS", "48"))
# A journey still receiving pages is not deleted under the PC's upload: the
# delete waits until nothing was uploaded to it for this long.
UPLOAD_QUIET_MINUTES = 10

# Outcomes, as the alert states them.
DELETED = "DELETED"
KEPT_NOT_INDIVIDUAL = "KEPT_NOT_INDIVIDUAL"
KEPT_REVIEWED = "KEPT_REVIEWED"
KEPT_OWN_DOCUMENTS = "KEPT_OWN_DOCUMENTS"  # the new file holds a document the earlier one does not
# Everything matches but the booking reference number was not read on one
# copy or both (product owner 2026-11-02): alert only, never deleted.
KEPT_NO_BOOKING_REFERENCE = "KEPT_NO_BOOKING_REFERENCE"
KEPT_CLOSED = "KEPT_CLOSED"
DELETE_FAILED = "DELETE_FAILED"
PENDING_DELETE = "PENDING_DELETE"
KEPT_CHANGED = "KEPT_CHANGED"  # re-checked at delete time: no longer the same file
REMOVED = "REMOVED"  # the new journey went another way before the system deleted it


class SameFileNoLongerHolds(RuntimeError):
    """Re-checked at delete time, the two journeys are no longer the same file."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UploadInProgress(RuntimeError):
    """Pages are still arriving on the new journey; try the delete later."""

    def __init__(self, delay_seconds: int) -> None:
        super().__init__(f"An upload to this journey is still in progress; deleting in {delay_seconds} s")
        self.delay_seconds = delay_seconds

# What a deletion record calls a copy deleted by this rule.
DELETION_KIND = "SAME_FILE"
_BOOKING_DOCUMENT_TYPES = ("booking_form", "booking_docket")

# The PAN's 4th character is the holder type; "P" is an individual.
_INDIVIDUAL_HOLDER = "P"

_CLOSED_SQL = """
    EXISTS (
      SELECT 1 FROM auditcore.journey_stage_states st
      WHERE st.tenant_id=j.tenant_id AND st.journey_id=j.journey_id
        AND ((st.stage_code='DELIVERY' AND st.business_completed_at_utc IS NOT NULL)
             OR (st.stage_code='BOOKING'
                 AND (st.business_status IN ('BOOKING_CANCELLED','DUPLICATE_BOOKING')
                      OR st.closure_disposition='NO_DELIVERY')))
    )
"""

# A value counts only while its document is on file (review of #594): a PAN
# or mobile read from a page since removed or replaced pairs nothing.
_ON_ACTIVE_DOCUMENT = """
    EXISTS (SELECT 1 FROM auditcore.evidence e
            WHERE e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
              AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE')
"""

_IDENTITY_SQL = f"""
    WITH ranked AS (
        SELECT f.journey_id, f.field_key, f.effective_value #>> '{{}}' AS value,
               ROW_NUMBER() OVER (
                   PARTITION BY f.journey_id, f.field_key
                   ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC
               ) AS row_rank
        FROM auditcore.journey_document_extracted_fields f
        WHERE f.tenant_id = :tenant_id AND f.journey_id = ANY(:journey_ids)
          AND f.field_key IN ('pan_number', 'aadhaar_number', 'customer_phone')
          AND f.effective_value IS NOT NULL
          AND {_ON_ACTIVE_DOCUMENT}
    )
    SELECT j.journey_id, j.created_at_utc, j.journey_reference,
           j.created_by_display_name AS pc_name, j.created_by_actor_id AS pc_actor_id,
           j.dealer_id, d.dealer_name, j.outlet_id, o.outlet_name,
           (SELECT r.value FROM ranked r
             WHERE r.journey_id = j.journey_id AND r.field_key = 'pan_number' AND r.row_rank = 1) AS pan,
           (SELECT r.value FROM ranked r
             WHERE r.journey_id = j.journey_id AND r.field_key = 'aadhaar_number' AND r.row_rank = 1) AS aadhaar,
           (SELECT r.value FROM ranked r
             WHERE r.journey_id = j.journey_id AND r.field_key = 'customer_phone' AND r.row_rank = 1) AS phone,
           CASE WHEN c.legal_name_status = 'VERIFIED' THEN c.legal_name END AS customer_name,
           s.model_id, jp.model_name_snapshot AS model_name, jp.variant_name_snapshot AS variant_name,
           (SELECT array_agg(f.effective_value #>> '{{}}'
                             ORDER BY f.confidence_score DESC NULLS LAST, f.updated_at_utc DESC)
              FROM auditcore.journey_document_extracted_fields f
             WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
               AND f.field_key = 'booking_reference_number'
               AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
               AND EXISTS (SELECT 1 FROM auditcore.evidence e
                           WHERE e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
                             AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
                             AND e.document_type_key IN ('booking_form', 'booking_docket'))) AS booking_refs,
           (SELECT array_agg(DISTINCT f.effective_value #>> '{{}}')
              FROM auditcore.journey_document_extracted_fields f
             WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
               AND f.field_key = 'receipt_number' AND f.source_document_type_key = 'dealer_receipt'
               AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
               AND {_ON_ACTIVE_DOCUMENT}) AS receipt_numbers,
           (SELECT array_agg(DISTINCT f.effective_value #>> '{{}}')
              FROM auditcore.journey_document_extracted_fields f
             WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
               AND f.field_key = 'invoice_number'
               AND f.source_document_type_key IN ('customer_invoice_dms', 'tax_invoice_tally')
               AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
               AND {_ON_ACTIVE_DOCUMENT}) AS invoice_numbers,
           (SELECT jc.vin_used FROM auditcore.journey_classifications jc
             WHERE jc.tenant_id = j.tenant_id AND jc.journey_id = j.journey_id) AS vin,
           {_CLOSED_SQL} AS is_closed
    FROM auditcore.journeys j
    JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
    JOIN auditcore.dealer_outlets o
      ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
    LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
    WHERE j.tenant_id = :tenant_id AND j.journey_id = ANY(:journey_ids)
"""

# Journeys of the Project whose extracted PAN or Aadhaar, on any document, is
# this one's (any outlet, any dealership: 2026-11-03, a duplicate booking across
# outlets): a narrowing only -- each is then compared on its own
# highest-confidence values.
_SAME_CUSTOMER_SQL = f"""
    SELECT DISTINCT f.journey_id
    FROM auditcore.journey_document_extracted_fields f
    WHERE f.tenant_id = :tenant_id AND f.journey_id <> :journey_id
      -- A value is needed to match a PAN or Aadhaar (both are never empty here: _valid_pan /
      -- _valid_aadhaar); saying so lets the lookup use the cleaned-up value indexes (0220).
      AND f.effective_value IS NOT NULL
      AND ((f.field_key = 'pan_number' AND CAST(:pan AS text) IS NOT NULL
            AND upper(regexp_replace(COALESCE(f.effective_value #>> '{{}}', ''), '[^A-Za-z0-9]', '', 'g')) = :pan)
        OR (f.field_key = 'aadhaar_number' AND CAST(:aadhaar AS text) IS NOT NULL
            AND regexp_replace(COALESCE(f.effective_value #>> '{{}}', ''), '[^0-9]', '', 'g') = :aadhaar))
      AND {_ON_ACTIVE_DOCUMENT}
"""

# Every PAN and GSTIN on the journeys' documents still on file.
_ACTIVE_TAX_IDS_SQL = f"""
    SELECT f.journey_id, f.field_key, f.effective_value #>> '{{}}' AS value
    FROM auditcore.journey_document_extracted_fields f
    WHERE f.tenant_id = :tenant_id AND f.journey_id = ANY(:journey_ids)
      AND f.field_key IN ('pan_number', 'gstin', 'buyer_gstin', 'customer_gstin')
      AND f.effective_value IS NOT NULL
      AND {_ON_ACTIVE_DOCUMENT}
"""


@dataclass(frozen=True)
class FileIdentity:
    journey_id: UUID
    created_at: datetime
    reference: str | None
    pc_name: str | None
    dealer_name: str | None
    outlet_name: str | None
    pan: str | None
    mobile: str | None
    name: str | None
    model_id: UUID | None
    model_name: str | None
    variant_name: str | None
    closed: bool
    # Who entered the journey (2026-10-31): their own screen beeps on the alert.
    pc_actor_id: str | None = None
    # 2026-11-02: the Aadhaar (12 digits) may stand for the PAN; the
    # dealership, as each numbers its own bookings; and the booking reference
    # numbers on its booking form / docket still on file, normalised
    # (``normalize_booking_reference``), with the one read with the highest
    # confidence as printed.
    aadhaar: str | None = None
    dealer_id: UUID | None = None
    booking_refs: frozenset[str] = frozenset()
    booking_ref_display: str | None = None
    # 2026-11-03: the same file is a mistake within one outlet only.
    outlet_id: UUID | None = None
    # 2026-11-12: the dealer receipt numbers on its receipts still on file, the numbers of
    # its vehicle invoices (DMS / Tally) and its agreed VIN -- the same sale (same_sale_fields).
    receipt_numbers: frozenset[str] = frozenset()
    invoice_numbers: frozenset[str] = frozenset()
    vin: str | None = None

    @property
    def model_key(self) -> str:
        return generation_key(self.model_name) if self.model_name and self.model_name.strip() else ""

    @property
    def complete(self) -> bool:
        """A PAN or an Aadhaar, the mobile, the customer name and the model --
        everything compared except the booking reference number, whose
        absence keeps the copy with an alert instead."""
        return bool((self.pan or self.aadhaar) and self.mobile and self.name and (self.model_id or self.model_key))

    @property
    def comparable(self) -> bool:
        return self.complete

    @property
    def order_key(self) -> tuple[datetime, str]:
        return (self.created_at, str(self.journey_id))


def _identities(connection: Connection, *, tenant_id: str, journey_ids: Iterable[UUID]) -> dict[UUID, FileIdentity]:
    ids = list(journey_ids)
    if not ids:
        return {}
    rows = connection.execute(
        text(_IDENTITY_SQL), {"tenant_id": tenant_id, "journey_ids": ids},
    ).mappings().all()
    out: dict[UUID, FileIdentity] = {}
    for row in rows:
        name = " ".join(str(row["customer_name"] or "").split()) or None
        if name and not person_tokens(name):
            name = None
        refs = [(raw, code) for raw in row["booking_refs"] or []
                if (code := normalize_booking_reference(raw))]
        out[UUID(str(row["journey_id"]))] = FileIdentity(
            journey_id=UUID(str(row["journey_id"])),
            created_at=row["created_at_utc"],
            reference=row["journey_reference"],
            pc_name=row["pc_name"],
            pc_actor_id=str(row["pc_actor_id"]) if row["pc_actor_id"] else None,
            dealer_name=row["dealer_name"],
            outlet_name=row["outlet_name"],
            pan=_valid_pan(row["pan"]),
            aadhaar=_valid_aadhaar(row["aadhaar"]),
            dealer_id=UUID(str(row["dealer_id"])) if row["dealer_id"] else None,
            outlet_id=UUID(str(row["outlet_id"])) if row["outlet_id"] else None,
            mobile=_normalize_mobile(row["phone"]),
            name=name,
            model_id=UUID(str(row["model_id"])) if row["model_id"] else None,
            model_name=(str(row["model_name"]).strip() or None) if row["model_name"] else None,
            variant_name=(str(row["variant_name"]).strip() or None) if row["variant_name"] else None,
            closed=bool(row["is_closed"]),
            booking_refs=frozenset(code for _, code in refs),
            booking_ref_display=" ".join(str(refs[0][0]).split()) if refs else None,
            receipt_numbers=_document_numbers(row["receipt_numbers"]),
            invoice_numbers=_document_numbers(row["invoice_numbers"]),
            vin=(str(row["vin"]).strip().upper() or None) if row["vin"] else None,
        )
    return out


_BOOKING_REF_TOKEN = re.compile(r"[A-Z]+|[0-9]+")


def normalize_booking_reference(value: Any) -> str | None:
    """A booking reference number in one spelling (2026-11-02): upper case,
    its letters and numbers only, each run of digits without its leading
    zeros, the runs joined by "-" -- "bk/0012", "BK-12", "BK 12" and "BK12"
    are all "BK-12"; "0000123" is "123". The runs stay apart, so "BK/12/3"
    and "BK/1/23" remain two numbers. None for a placeholder ("NA", "-",
    "0000"), for a value with no digit other than 0, or with fewer than three
    letters and digits -- too little to tell two bookings apart."""
    if value is None:
        return None
    raw = str(value).upper()
    if _placeholder(raw):
        return None
    tokens = _BOOKING_REF_TOKEN.findall(raw)
    if sum(len(t) for t in tokens) < 3:
        return None
    if not any(t.isdigit() and t.strip("0") for t in tokens):
        return None
    return "-".join((t.lstrip("0") or "0") if t.isdigit() else t for t in tokens)


# ── the comparisons ───────────────────────────────────────────────────────────
def same_name(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left_masked, right_masked = is_masked(left), is_masked(right)
    if left_masked and right_masked:
        return " ".join(left.upper().split()) == " ".join(right.upper().split())
    if left_masked:
        return masked_matches(left, right)
    if right_masked:
        return masked_matches(right, left)
    return same_person(left, right)


def same_model(left: FileIdentity, right: FileIdentity) -> bool:
    if left.model_id and right.model_id:
        return left.model_id == right.model_id
    return bool(left.model_key) and left.model_key == right.model_key


def _masked(value: str | None) -> str | None:
    if not value:
        return None
    return value if len(value) <= 4 else f"{'X' * (len(value) - 4)}{value[-4:]}"


def mask_aadhaar(value: Any) -> str | None:
    """An Aadhaar number as it may be shown or kept: its last four digits
    only, "XXXX XXXX 1234". The full number is never stored or shown."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return f"XXXX XXXX {digits[-4:]}" if len(digits) >= 4 else None


def booking_reference_shared(left: FileIdentity, right: FileIdentity) -> bool:
    """Both copies carry the same booking reference number."""
    return bool(left.booking_refs & right.booking_refs)


def same_outlet(left: FileIdentity, right: FileIdentity) -> bool:
    return not (left.outlet_id and right.outlet_id and left.outlet_id != right.outlet_id)


def same_customer(new: FileIdentity, existing: FileIdentity) -> tuple[bool, bool] | None:
    """(same PAN, same Aadhaar) when the two are one customer: either one equal, and
    neither read on both and different; None otherwise."""
    same_pan = bool(new.pan and new.pan == existing.pan)
    same_aadhaar = bool(new.aadhaar and new.aadhaar == existing.aadhaar)
    if not (same_pan or same_aadhaar):
        return None
    if (new.pan and existing.pan and not same_pan) or (new.aadhaar and existing.aadhaar and not same_aadhaar):
        return None
    return same_pan, same_aadhaar


def cross_outlet_fields(new: FileIdentity, existing: FileIdentity) -> list[dict[str, Any]] | None:
    """The fields when the two journeys are one customer's booking of one model at two
    different outlets (product owner 2026-11-03): the same PAN and/or Aadhaar, the same
    mobile and the same model -- no booking reference number (each dealership numbers its
    own) and no name (the PAN or Aadhaar is the person). None otherwise."""
    if same_outlet(new, existing):
        return None
    customer = same_customer(new, existing)
    if customer is None:
        return None
    if not (new.mobile and new.mobile == existing.mobile and (new.model_id or new.model_key)
            and same_model(new, existing)):
        return None
    same_pan, same_aadhaar = customer
    fields: list[dict[str, Any]] = []
    if same_pan:
        fields.append({"field": "pan", "label": "PAN", "newValue": _masked(new.pan),
                       "existingValue": _masked(existing.pan), "exact": True})
    if same_aadhaar:
        fields.append({"field": "aadhaar", "label": "Aadhaar", "newValue": mask_aadhaar(new.aadhaar),
                       "existingValue": mask_aadhaar(existing.aadhaar), "exact": True})
    fields += [
        {"field": "mobile", "label": "Mobile", "newValue": _masked(new.mobile),
         "existingValue": _masked(existing.mobile), "exact": True},
        {"field": "model", "label": "Model", "newValue": new.model_name, "existingValue": existing.model_name,
         "exact": bool(new.model_id and new.model_id == existing.model_id) or new.model_name == existing.model_name},
        {"field": "outlet", "label": "Outlet", "newValue": _place(new.dealer_name, new.outlet_name),
         "existingValue": _place(existing.dealer_name, existing.outlet_name), "exact": False},
    ]
    return fields


def matched_fields(new: FileIdentity, existing: FileIdentity) -> list[dict[str, Any]] | None:
    """The fields, as a reviewer should see them, when the two journeys are one
    booking entered twice at one outlet; None otherwise.

    Product owner 2026-11-02: the same PAN and/or the same Aadhaar (either
    one equal is enough; one read on both and different says two people),
    the same mobile, the same model, the same customer name (kept from
    2026-10-31) and the same booking reference number -- within one outlet
    (2026-11-03). Booking numbers read on both copies and none in common: two
    bookings, None. A booking number not read on one copy (or either) still
    returns the fields, with the booking reference row not exact: the caller
    then alerts and keeps the copy (KEPT_NO_BOOKING_REFERENCE), never deletes
    it. The 2026-11-04 rules were added to these, not in their place."""
    if not same_outlet(new, existing):  # another outlet: a duplicate booking across outlets
        return None
    if new.dealer_id and existing.dealer_id and new.dealer_id != existing.dealer_id:
        return None
    customer = same_customer(new, existing)
    if customer is None:
        return None
    if not (new.complete and existing.complete and new.mobile == existing.mobile
            and same_model(new, existing) and same_name(new.name, existing.name)):
        return None
    shared = sorted(new.booking_refs & existing.booking_refs)
    if new.booking_refs and existing.booking_refs and not shared:
        return None
    same_pan, same_aadhaar = customer
    fields: list[dict[str, Any]] = []
    if same_pan:
        fields.append({"field": "pan", "label": "PAN", "newValue": _masked(new.pan),
                       "existingValue": _masked(existing.pan), "exact": True})
    if same_aadhaar:
        fields.append({"field": "aadhaar", "label": "Aadhaar", "newValue": mask_aadhaar(new.aadhaar),
                       "existingValue": mask_aadhaar(existing.aadhaar), "exact": True})
    fields += [
        {"field": "mobile", "label": "Mobile", "newValue": _masked(new.mobile),
         "existingValue": _masked(existing.mobile), "exact": True},
        {"field": "model", "label": "Model", "newValue": new.model_name, "existingValue": existing.model_name,
         "exact": bool(new.model_id and new.model_id == existing.model_id) or new.model_name == existing.model_name},
        {"field": "name", "label": "Customer name", "newValue": new.name, "existingValue": existing.name,
         "exact": new.name == existing.name},
        {"field": "outlet", "label": "Outlet", "newValue": _place(new.dealer_name, new.outlet_name),
         "existingValue": _place(existing.dealer_name, existing.outlet_name), "exact": True},
        {"field": "bookingReference", "label": "Booking reference number",
         "newValue": new.booking_ref_display, "existingValue": existing.booking_ref_display,
         "exact": bool(shared)},
    ]
    return fields


def _document_numbers(values: Iterable[Any] | None) -> frozenset[str]:
    """Receipt / invoice numbers as compared: letters and digits only, upper case, at
    least six characters (a bare "1" or "R-2" identifies nothing)."""
    out = set()
    for value in values or []:
        code = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
        if len(code) >= 6:
            out.add(code)
    return frozenset(out)


def two_vehicles(left: FileIdentity, right: FileIdentity) -> bool:
    """The two journeys are for two vehicles: each has its VIN and they differ, or (VINs
    not both known) each has a vehicle invoice and they share no number."""
    if left.vin and right.vin:
        return left.vin != right.vin
    return bool(left.invoice_numbers and right.invoice_numbers
                and not left.invoice_numbers & right.invoice_numbers)


def same_sale_fields(new: FileIdentity, existing: FileIdentity) -> list[dict[str, Any]] | None:
    """The fields when the two journeys are one sale uploaded twice at one outlet: a
    booking file, then the delivery file with those documents again (product owner
    2026-11-12, AVINASH PATRA, UTK-PR-261103-001 and UTK-PR-261104-002: "if the same
    details are found but with additional documents then we will update the existing
    booking").

    The same PAN and/or Aadhaar and the same dealer receipt number on both: a dealership
    issues each receipt number once, so one payment is one sale. The mobile, the booking
    reference number, the model and the name are not required here -- on that pair the
    mobile and the booking number each misread by one digit on one copy, and a model
    changed between booking and invoice is common. Never two vehicles (two_vehicles).
    None otherwise; matched_fields is unchanged."""
    if not same_outlet(new, existing):
        return None
    if new.dealer_id and existing.dealer_id and new.dealer_id != existing.dealer_id:
        return None
    customer = same_customer(new, existing)
    if customer is None:
        return None
    shared = sorted(new.receipt_numbers & existing.receipt_numbers)
    if not shared or two_vehicles(new, existing):
        return None
    same_pan, same_aadhaar = customer
    fields: list[dict[str, Any]] = []
    if same_pan:
        fields.append({"field": "pan", "label": "PAN", "newValue": _masked(new.pan),
                       "existingValue": _masked(existing.pan), "exact": True})
    if same_aadhaar:
        fields.append({"field": "aadhaar", "label": "Aadhaar", "newValue": mask_aadhaar(new.aadhaar),
                       "existingValue": mask_aadhaar(existing.aadhaar), "exact": True})
    fields += [
        {"field": "receipt", "label": "Dealer receipt number", "newValue": shared[0],
         "existingValue": shared[0], "exact": True},
        {"field": "mobile", "label": "Mobile", "newValue": _masked(new.mobile),
         "existingValue": _masked(existing.mobile), "exact": bool(new.mobile and new.mobile == existing.mobile)},
        {"field": "model", "label": "Model", "newValue": new.model_name, "existingValue": existing.model_name,
         "exact": bool(new.model_id and new.model_id == existing.model_id) or new.model_name == existing.model_name},
        {"field": "name", "label": "Customer name", "newValue": new.name, "existingValue": existing.name,
         "exact": new.name == existing.name},
        {"field": "outlet", "label": "Outlet", "newValue": _place(new.dealer_name, new.outlet_name),
         "existingValue": _place(existing.dealer_name, existing.outlet_name), "exact": True},
        {"field": "bookingReference", "label": "Booking reference number",
         "newValue": new.booking_ref_display, "existingValue": existing.booking_ref_display,
         "exact": booking_reference_shared(new, existing)},
    ]
    return fields


def same_file_fields(new: FileIdentity, existing: FileIdentity) -> list[dict[str, Any]] | None:
    """One booking entered twice (matched_fields) or one sale uploaded twice (same_sale_fields);
    never two vehicles (different VINs, or vehicle invoices with no number in common)."""
    if two_vehicles(new, existing):
        return None
    return matched_fields(new, existing) or same_sale_fields(new, existing)


def booking_identified(new: FileIdentity, existing: FileIdentity) -> bool:
    """The copy is known to be the same booking: a booking number in common, or the same
    sale by its receipt -- otherwise it is kept and alerted (KEPT_NO_BOOKING_REFERENCE)."""
    return booking_reference_shared(new, existing) or same_sale_fields(new, existing) is not None


def document_counts(connection: Connection, *, tenant_id: str, journey_ids: Iterable[UUID]) -> dict[UUID, int]:
    """Documents on file (ACTIVE) per journey."""
    ids = list(journey_ids)
    return {UUID(str(j)): int(n) for j, n in connection.execute(
        text("SELECT journey_id, count(*) FROM auditcore.evidence WHERE tenant_id=:t AND journey_id = ANY(:ids) "
             "AND association_status='ACTIVE' GROUP BY journey_id"),
        {"t": tenant_id, "ids": ids},
    ).all()}


def _no_booking_reference_reason(new: FileIdentity, existing: FileIdentity) -> str:
    if not new.booking_refs and not existing.booking_refs:
        where = "either copy"
    elif not new.booking_refs:
        where = f"the new file {new.reference or ''}".rstrip()
    else:
        where = existing.reference or "the earlier journey"
    return f"the booking reference number was not read on {where}"


def is_individual_pan(pan: str | None) -> bool:
    return bool(pan) and len(pan) == 10 and pan[3] == _INDIVIDUAL_HOLDER


# Words of a business's name not already in uc03_p2_names' company / firm words.
_BUSINESS_WORDS = frozenset({"CO", "AGENCIES", "CONSTRUCTION", "INFRA", "TRADERS", "SERVICES"})


def looks_like_company(name: str | None) -> bool:
    """A customer name that is a company's or a firm's ("M/S ...", "... PVT
    LTD", "... ENTERPRISES", "... & CO.")."""
    if not name:
        return False
    words = set("".join(ch if ch.isalnum() else " " for ch in name.upper()).split())
    return _is_company(name) or is_firm_name(name) or bool(words & _BUSINESS_WORDS)


def business_reason(connection: Connection, *, tenant_id: str, files: list[FileIdentity]) -> str | None:
    """Why this booking may be a company's or firm's although the PAN read as
    the customer's is an individual's (review of #594: a company buying with
    its signatory's personal PAN). None when nothing says so. Then the new
    file is never deleted, only alerted and kept for review."""
    for identity in files:
        # A copy matched on its Aadhaar may carry no PAN: an Aadhaar is an
        # individual's, so only a PAN read and not an individual's counts.
        if identity.pan and not is_individual_pan(identity.pan):
            return "the PAN is not an individual's"
    for identity in files:
        if looks_like_company(identity.name):
            return "the customer name is a company's or firm's"
    rows = connection.execute(
        text(_ACTIVE_TAX_IDS_SQL), {"tenant_id": tenant_id, "journey_ids": [f.journey_id for f in files]},
    ).mappings().all()
    for row in rows:
        if row["field_key"] == "pan_number":
            pan = _valid_pan(row["value"])
            if pan and not is_individual_pan(pan):
                return "a company or firm PAN is on file"
    # A sole proprietor's GSTIN is built on their own personal PAN (2026-11-01:
    # Prahallad Behera's 21BYXPB9740G1Z8 on PAN BYXPB9740G; Binapani Panda,
    # Bishnu Charan Samal): it does not make the customer a company, so it is
    # not a reason to keep the copy.
    from audit_core.uc03_p2_file_identity import gstin_pan

    own = {identity.pan for identity in files if identity.pan}

    def a_business_gstin(value: Any) -> bool:
        return _valid_gstin(value) and gstin_pan(value) not in own

    for row in rows:
        if row["field_key"] != "pan_number" and a_business_gstin(row["value"]):
            return "a GSTIN is on file"
    for identity in files:
        if a_business_gstin(_buyer_gstin(connection, tenant_id=tenant_id, journey_id=identity.journey_id)):
            return "a GSTIN is on file"
    return None


# ── the alert ─────────────────────────────────────────────────────────────────
def _place(dealer: str | None, outlet: str | None) -> str:
    parts = [p for p in (dealer, outlet) if p and p.strip()]
    if len(parts) == 2 and parts[0].strip().lower() == parts[1].strip().lower():
        parts = parts[:1]
    return "/".join(parts) or "unknown dealership"


def alert_body(payload: dict[str, Any], outcome: str) -> str:
    new = payload.get("newJourney") or {}
    existing = payload.get("existingJourney") or {}
    who = (f"{payload.get('customerName') or 'Unknown customer'}, {payload.get('model') or 'unknown model'}, "
           f"PC {new.get('pcName') or 'unknown'}, {_place(new.get('dealerName'), new.get('outletName'))}")
    ref = new.get("reference") or "the new journey"
    kept = payload.get("keptReason") or "the PAN is not an individual's"
    labels = [str(f.get("label") or "").replace("Customer name", "customer name").replace("Receipt number",
              "receipt number").replace("Booking reference number", "booking reference number")
              .replace("Mobile", "mobile").replace("Model", "model")
              for f in payload.get("matchedFields") or []
              if f.get("field") != "bookingReference" or f.get("exact")] or ["PAN", "mobile", "customer name", "model"]
    on = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
    matches = (f"It matches {existing.get('reference') or 'an earlier journey'} "
               f"(PC {existing.get('pcName') or 'unknown'}, "
               f"{_place(existing.get('dealerName'), existing.get('outletName'))}) "
               f"on {on}.")
    what = {
        DELETED: (f"The new file {ref} was deleted"
                  + (f" after its {', '.join(payload['carriedDocuments']).replace('_', ' ')} "
                     f"{'was' if len(payload['carriedDocuments']) == 1 else 'were'} added to "
                     f"{existing.get('reference') or 'the earlier journey'}."
                     if payload.get("carriedDocuments") else ".")),
        PENDING_DELETE: f"The new file {ref} is being deleted.",
        KEPT_NOT_INDIVIDUAL: (f"The new file {ref} was kept for review: {kept} "
                              "(a company or firm may book two identical vehicles)."),
        KEPT_CHANGED: (f"The new file {ref} was kept for review: "
                       f"{payload.get('keptReason') or 'it no longer matches'}."),
        REMOVED: f"The new file {ref} has been removed.",
        KEPT_REVIEWED: f"The new file {ref} was kept for review: its documents are already marked reviewed.",
        KEPT_OWN_DOCUMENTS: (f"The new file {ref} was kept for review: it holds "
                             f"{payload.get('keptReason') or 'documents the earlier journey does not'}."),
        KEPT_CLOSED: f"The new file {ref} was kept for review: it is already closed.",
        KEPT_NO_BOOKING_REFERENCE: (f"The new file {ref} was kept for review: "
                                    f"{payload.get('keptReason') or 'the booking reference number was not read'}."),
        DELETE_FAILED: f"The new file {ref} could not be deleted; the Team Lead has been told.",
    }.get(outcome, f"The new file is {ref}.")
    return f"{ALERT_TITLE} — {who}. {what} {matches}"


def _journey_view(identity: FileIdentity) -> dict[str, Any]:
    return {
        "journeyId": str(identity.journey_id),
        "reference": identity.reference,
        "pcName": identity.pc_name,
        "pcActorId": identity.pc_actor_id,
        "dealerName": identity.dealer_name,
        "outletName": identity.outlet_name,
        "createdAtUtc": identity.created_at.isoformat() if identity.created_at else None,
    }


def publish_alert(connection: Connection, *, tenant_id: str, journey_id: UUID, outcome: str,
                  kept_reason: str | None = None) -> bool:
    """Publish the same-file alert of a journey with its outcome (for 24 hours).
    True when a PENDING alert was published; an alert already published is
    left as it is."""
    row = connection.execute(
        text(
            "SELECT alert_id, payload FROM auditcore.p2_tenant_alerts "
            "WHERE tenant_id=:t AND alert_kind=:k AND subject_journey_id=:j AND alert_status='PENDING' "
            "FOR UPDATE"
        ),
        {"t": tenant_id, "k": ALERT_KIND, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return False
    payload = {**dict(row["payload"] or {}), "outcome": outcome}
    if kept_reason:
        payload["keptReason"] = kept_reason
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tenant_alerts
            SET alert_status='PUBLISHED', outcome=:outcome, body=:body, payload=CAST(:payload AS jsonb),
                published_at_utc=now(), expires_at_utc=now() + (:hours * interval '1 hour'), updated_at_utc=now()
            WHERE tenant_id=:t AND alert_id=:a
            """
        ),
        {"t": tenant_id, "a": row["alert_id"], "outcome": outcome, "body": alert_body(payload, outcome),
         "payload": json.dumps(payload, default=str), "hours": ALERT_HOURS},
    )
    logger.info("p2_same_file_alert_published", tenant_id=tenant_id, journey_id=str(journey_id), outcome=outcome)
    return True


def close_pending_alert(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """A journey deleted by any path while its same-file alert waits: the
    alert goes out as removed (its delete item went with the journey)."""
    return publish_alert(connection, tenant_id=tenant_id, journey_id=journey_id, outcome=REMOVED)


def documents_only_on(connection: Connection, *, tenant_id: str, new_journey_id: UUID,
                      existing_journey_id: UUID) -> list[str]:
    """The kinds of document on file on the new journey and on none of the
    earlier one's (2026-11-01): deleting it would lose them -- Sarbeswar
    Pradhan's Tally invoice was only on his second booking, Susama Naik's
    booking form only on her first. A dealer and a payment receipt are one
    kind, as are a booking form and a docket."""
    from audit_core.uc03_document_capture_v2 import _canonical_document_type

    rows = connection.execute(
        text("SELECT journey_id, document_type_key FROM auditcore.evidence WHERE tenant_id=:t "
             "AND journey_id = ANY(:ids) AND association_status='ACTIVE'"),
        {"t": tenant_id, "ids": [new_journey_id, existing_journey_id]},
    ).all()

    def kind(key: Any) -> str:
        canonical = _canonical_document_type(str(key or ""))
        return "booking_form" if canonical in {"booking_form", "booking_docket"} else canonical

    new_kinds = {kind(k) for j, k in rows if UUID(str(j)) == new_journey_id}
    existing_kinds = {kind(k) for j, k in rows if UUID(str(j)) == existing_journey_id}
    return sorted(new_kinds - existing_kinds)


# Receipts: a customer can have several, so one is carried when its number is new.
_RECEIPT_KINDS = frozenset({"dealer_receipt", "payment_receipt"})
# A page not typed yet is carried: nothing says the earlier journey has it.
_UNTYPED_KINDS = frozenset({"document", "unknown", "other", "unclassified"})
# The customer's own files (file identity): only their documents are carried to the earlier journey.
_CARRIED_IDENTITY = "BELONGS"
_GONE_UNITS = ("CANCELLED", "MERGED")


@dataclass(frozen=True)
class CarryPlan:
    """What the copy holds that the earlier journey does not, by page fingerprint: the
    document units to copy, the kinds they are (for the alert), and a reason to keep the
    copy instead when some of it is not settled as the customer's."""
    kinds: tuple[str, ...]
    units: tuple[dict[str, Any], ...]
    unsure: str | None
    # Every page of the copy is on the earlier journey already, by fingerprint (2026-11-12).
    identical: bool = False


def _unit_pages(unit: dict[str, Any]) -> list[int]:
    return [int(p) for p in (unit["page_numbers"] or [unit["page_number"]]) if p is not None]


def carry_plan(connection: Connection, *, tenant_id: str, new: FileIdentity, existing: FileIdentity) -> CarryPlan:
    """Product owner 2026-11-03: "if we are having fingerprint scan then it's better to use it
    and make the system more reliable and avoid any loss of document". A page of the copy is
    the earlier journey's already when a page with the same fingerprint (page_sha256, the
    SHA-256 of the split page) was uploaded to it; every other page is new. Each document
    unit of the copy holding a new page is carried whole (a multi-page document stays one).
    Kept instead, never deleted, when a new page's file is not judged the customer's
    (UNKNOWN, or not judged yet), when the copy holds another customer's file (FOREIGN),
    or when a new page has no stored unit to copy yet."""
    ref = existing.reference or "the earlier journey"
    on_existing = {str(sha) for sha in connection.execute(
        text("SELECT DISTINCT page_sha256 FROM auditcore.p2_document_queue WHERE tenant_id=:t AND journey_id=:j "
             "AND unit_kind='PAGE' AND page_sha256 IS NOT NULL AND queue_status <> 'CANCELLED'"),
        {"t": tenant_id, "j": existing.journey_id},
    ).scalars().all()}
    rows = [dict(r) for r in connection.execute(
        text(
            """
            SELECT q.queue_id, q.batch_id, q.unit_kind, q.page_number, q.page_numbers, q.page_sha256, q.di_document_id,
                   q.page_object_key, q.queue_status, q.classified_document_type, q.merged_into_queue_id,
                   b.original_filename, b.content_type, b.identity_status, b.batch_status, b.uploaded_by_actor_id,
                   (SELECT e.document_type_key FROM auditcore.evidence e
                     WHERE e.tenant_id=q.tenant_id AND e.journey_id=q.journey_id AND e.di_document_id=q.di_document_id
                     ORDER BY (e.association_status='ACTIVE') DESC LIMIT 1) AS document_type_key
            FROM auditcore.p2_document_queue q
            JOIN auditcore.p2_upload_batches b ON b.tenant_id=q.tenant_id AND b.batch_id=q.batch_id
            WHERE q.tenant_id=:t AND q.journey_id=:j AND q.queue_status <> 'CANCELLED'
              AND b.batch_status <> 'CANCELLED'
            """
        ),
        {"t": tenant_id, "j": new.journey_id},
    ).mappings().all()]
    if any(r["identity_status"] == "FOREIGN" for r in rows):
        return CarryPlan(kinds=(), units=(), unsure=f"it holds another customer's file, which {ref} must not get")
    pages = [r for r in rows if r["unit_kind"] == "PAGE"]
    fresh = [p for p in pages if not p["page_sha256"] or str(p["page_sha256"]) not in on_existing]
    if not fresh:
        return CarryPlan(kinds=(), units=(), unsure=None, identical=bool(pages))  # the same set of documents
    unsettled = [p for p in fresh if p["identity_status"] != _CARRIED_IDENTITY]
    if unsettled:
        return CarryPlan(kinds=(), units=(), unsure=(
            f"{len(unsettled)} page(s) {ref} does not have, not yet settled as the customer's"))
    # The unit that holds each new page: the document it was merged into, or the page itself.
    units: dict[str, dict[str, Any]] = {}
    by_id = {str(r["queue_id"]): r for r in rows}
    for page in fresh:
        unit = by_id.get(str(page["merged_into_queue_id"])) if page["merged_into_queue_id"] else page
        if unit is None or unit["queue_status"] in _GONE_UNITS or not unit["page_object_key"]:
            return CarryPlan(kinds=(), units=(), unsure=(
                f"page {page['page_number']} of {page['original_filename'] or 'a file'}, which {ref} does not have, "
                "has no stored document to copy yet"))
        units[str(unit["queue_id"])] = {**unit, "document_type_key": unit["document_type_key"]
                                        or unit["classified_document_type"] or "document"}
    # Only what the earlier journey does not have (product owner 2026-11-12: "add additional
    # documents"): a PC re-photographs the PAN, booking docket and receipt for the delivery
    # file, so those pages differ by fingerprint yet are documents the journey already holds.
    # A kind it already holds is not copied again; a receipt is, when its number is new.
    held = {str(k) for k in connection.execute(
        text("SELECT DISTINCT document_type_key FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
             "AND association_status='ACTIVE' AND document_type_key IS NOT NULL"),
        {"t": tenant_id, "j": existing.journey_id},
    ).scalars().all()}
    carried: dict[str, dict[str, Any]] = {}
    for key, unit in units.items():
        kind = str(unit["document_type_key"])
        if kind in _RECEIPT_KINDS:
            numbers = _document_numbers(connection.execute(
                text("SELECT f.effective_value #>> '{}' FROM auditcore.journey_document_extracted_fields f "
                     "WHERE f.tenant_id=:t AND f.journey_id=:j AND f.di_document_id=:d AND f.field_key='receipt_number'"),
                {"t": tenant_id, "j": new.journey_id, "d": unit["di_document_id"]},
            ).scalars().all()) if unit["di_document_id"] else frozenset()
            if numbers and numbers <= existing.receipt_numbers:
                continue
        elif kind in held and kind not in _UNTYPED_KINDS:
            continue
        carried[key] = unit
    kinds = tuple(sorted({str(u["document_type_key"]) for u in carried.values()}))
    return CarryPlan(kinds=kinds, units=tuple(carried.values()), unsure=None)


def _own_documents_reason(connection: Connection, *, tenant_id: str, new: FileIdentity,
                          existing: FileIdentity) -> str | None:
    """Why the copy must be kept for what it alone holds: only when some of it is not settled
    as the customer's; what is the customer's is carried to the earlier journey (2026-11-03)."""
    return carry_plan(connection, tenant_id=tenant_id, new=new, existing=existing).unsure


# Product owner 2026-11-12: "maintain a count of duplicate uploads for each PC from now
# onwards.. one which are deleted.. it should be going in the daily tracker of PC and TL /PM
# executive". Counted from that message on, by the day the copy was uploaded.
DUPLICATE_UPLOADS_COUNTED_FROM = datetime(2026, 11, 12, 11, 45, tzinfo=UTC)


def duplicate_uploads_deleted(connection: Connection, *, tenant_id: str, start: datetime, end: datetime,
                              pc_actor_id: str | None = None) -> list[dict[str, Any]]:
    """The copies deleted as the same file (or the same sale) uploaded again, by the journey
    they were: outlet, the PC who uploaded it and when (journey_created_at_utc), uploaded in
    [start, end) and not before DUPLICATE_UPLOADS_COUNTED_FROM. The Project Tracker and My
    Tracker count these under the PC."""
    rows = connection.execute(
        text(
            """
            SELECT d.journey_id, d.outlet_id, d.pc_actor_id, d.journey_created_at_utc AS uploaded_at
            FROM auditcore.p2_journey_deletions d
            WHERE d.tenant_id = :t AND d.deletion_kind = :kind AND d.deletion_status = 'COMPLETED'
              AND d.journey_created_at_utc >= GREATEST(CAST(:s AS timestamptz), CAST(:from_ AS timestamptz))
              AND d.journey_created_at_utc < :e
              AND (CAST(:pc AS text) IS NULL OR d.pc_actor_id = :pc)
            """
        ),
        {"t": tenant_id, "kind": DELETION_KIND, "s": start, "e": end, "from_": DUPLICATE_UPLOADS_COUNTED_FROM,
         "pc": pc_actor_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def identical_documents(connection: Connection, *, tenant_id: str, new: FileIdentity,
                        existing: FileIdentity) -> bool:
    """Every page of the copy is already on the kept journey, by page fingerprint: exactly
    the same documents (product owner 2026-11-12). A copy with no page at all is not."""
    has_pages = connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM auditcore.p2_document_queue WHERE tenant_id=:t AND journey_id=:j "
             "AND unit_kind='PAGE' AND queue_status <> 'CANCELLED')"),
        {"t": tenant_id, "j": new.journey_id},
    ).scalar_one()
    if not has_pages:
        return False
    return carry_plan(connection, tenant_id=tenant_id, new=new, existing=existing).identical


def carry_documents(engine: Engine, *, tenant_id: str, new_journey_id: UUID, existing_journey_id: UUID) -> list[str]:
    """Copy the documents only the copy holds to the earlier journey, as new uploads there
    (uc03_duplicate_merge._copy_unit; idempotent: one fixed upload per unit). Returns the
    kinds carried; nothing when there is nothing to carry or it is not all the customer's."""
    from audit_core.db import set_tenant_context
    from audit_core.uc03_duplicate_merge import _copy_unit

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        files = _identities(connection, tenant_id=tenant_id, journey_ids=[new_journey_id, existing_journey_id])
        new, existing = files.get(new_journey_id), files.get(existing_journey_id)
        if new is None or existing is None:
            return []
        plan = carry_plan(connection, tenant_id=tenant_id, new=new, existing=existing)
        keep = connection.execute(
            text("SELECT journey_id, journey_reference, created_by_actor_id FROM auditcore.journeys "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"t": tenant_id, "j": existing_journey_id},
        ).mappings().one()
    if plan.unsure or not plan.units:
        return []
    copy = {"journey_id": new_journey_id, "journey_reference": new.reference}
    now = datetime.now(UTC)
    for unit in plan.units:
        # Uploaded on the earlier journey by its PC; by whoever uploaded the page when it has none.
        uploader = keep["created_by_actor_id"] or unit.get("uploaded_by_actor_id")
        _copy_unit(engine, tenant_id, {**dict(keep), "created_by_actor_id": uploader}, copy, unit,
                   tag=f"same-file:{new_journey_id}", now=now)
    logger.info("p2_same_file_documents_carried", tenant_id=tenant_id, journey_id=str(new_journey_id),
                existing_journey_id=str(existing_journey_id), kinds=list(plan.kinds), units=len(plan.units))
    return list(plan.kinds)


def _reviewed(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    return connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews "
             "WHERE tenant_id=:t AND journey_id=:j)"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()


def _raise(
    connection: Connection, *, tenant_id: str, new: FileIdentity, existing: FileIdentity,
    fields: list[dict[str, Any]], correlation_id: str,
) -> str | None:
    """The alert for one new file, and its delete queued; None when this
    file was already handled (the marker)."""
    kept_reason = business_reason(connection, tenant_id=tenant_id, files=[new, existing])
    plan = CarryPlan(kinds=(), units=(), unsure=None)
    if _reviewed(connection, tenant_id=tenant_id, journey_id=new.journey_id):
        outcome, kept_reason = KEPT_REVIEWED, None
    elif identical_documents(connection, tenant_id=tenant_id, new=new, existing=existing):
        # Exactly the same documents: deleted whatever else is said (2026-11-12).
        outcome, kept_reason = PENDING_DELETE, None
    elif kept_reason:
        outcome = KEPT_NOT_INDIVIDUAL
    elif not booking_identified(new, existing):
        outcome, kept_reason = KEPT_NO_BOOKING_REFERENCE, _no_booking_reference_reason(new, existing)
    elif (plan := carry_plan(connection, tenant_id=tenant_id, new=new, existing=existing)).unsure:
        outcome, kept_reason = KEPT_OWN_DOCUMENTS, plan.unsure
    else:
        outcome = PENDING_DELETE
    payload = {
        "customerName": new.name,
        "model": new.model_name,
        "variant": new.variant_name,
        "newJourney": _journey_view(new),
        "existingJourney": _journey_view(existing),
        "matchedFields": fields,
        "outcome": outcome,
        **({"keptReason": kept_reason} if kept_reason else {}),
        # The documents only the copy holds, carried to the earlier journey before the delete.
        **({"carryDocuments": list(plan.kinds)} if outcome == PENDING_DELETE and plan.kinds else {}),
    }
    publish_now = outcome != PENDING_DELETE
    alert_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_tenant_alerts (
                tenant_id, alert_kind, subject_journey_id, subject_journey_reference, related_journey_id,
                alert_status, outcome, title, body, payload, published_at_utc, expires_at_utc
            ) VALUES (
                :t, :k, :j, :ref, :related, :status, :outcome, :title, :body, CAST(:payload AS jsonb),
                CASE WHEN :publish THEN now() END,
                CASE WHEN :publish THEN now() + (:hours * interval '1 hour') END
            )
            ON CONFLICT (tenant_id, alert_kind, subject_journey_id) DO NOTHING
            RETURNING alert_id
            """
        ),
        {"t": tenant_id, "k": ALERT_KIND, "j": new.journey_id, "ref": new.reference,
         "related": existing.journey_id, "status": "PUBLISHED" if publish_now else "PENDING",
         "outcome": outcome, "title": ALERT_TITLE, "body": alert_body(payload, outcome),
         "payload": json.dumps(payload, default=str), "publish": publish_now, "hours": ALERT_HOURS},
    ).scalar_one_or_none()
    if alert_id is None and outcome == PENDING_DELETE:
        # Kept earlier only because a booking number was not yet read (now read and the
        # same on both), or for documents it alone held (now carried): deleted after all.
        alert_id = connection.execute(
            text(
                """
                UPDATE auditcore.p2_tenant_alerts
                SET alert_status='PENDING', outcome=:outcome, body=:body, payload=CAST(:payload AS jsonb),
                    related_journey_id=:related, published_at_utc=NULL, expires_at_utc=NULL, updated_at_utc=now()
                WHERE tenant_id=:t AND alert_kind=:k AND subject_journey_id=:j AND outcome = ANY(:was)
                RETURNING alert_id
                """
            ),
            {"t": tenant_id, "k": ALERT_KIND, "j": new.journey_id, "related": existing.journey_id,
             "outcome": outcome, "body": alert_body(payload, outcome), "payload": json.dumps(payload, default=str),
             # 2026-11-03: kept for what it alone held, now carried to the earlier journey;
             # 2026-11-12: kept as a company's, or changed at delete time, and now found to be
             # exactly the same documents (or the same again).
             "was": [KEPT_NO_BOOKING_REFERENCE, KEPT_OWN_DOCUMENTS, KEPT_NOT_INDIVIDUAL, KEPT_CHANGED]},
        ).scalar_one_or_none()
    if alert_id is None:
        return None
    if outcome == PENDING_DELETE:
        enqueue_work(
            connection, tenant_id=tenant_id, journey_id=new.journey_id, work_type=DELETE_WORK_TYPE,
            work_key=f"same-file:{new.journey_id}",
            payload={"alertId": str(alert_id), "existingJourneyId": str(existing.journey_id),
                     "existingReference": existing.reference},
            correlation_id=correlation_id,
        )
    logger.info(
        "p2_same_file_detected", tenant_id=tenant_id, journey_id=str(new.journey_id),
        journey_reference=new.reference, existing_journey_id=str(existing.journey_id),
        existing_reference=existing.reference, outcome=outcome,
    )
    return outcome


def cross_outlet_body(payload: dict[str, Any], *, title: str | None = None) -> str:
    new = payload.get("newJourney") or {}
    existing = payload.get("existingJourney") or {}
    who = f"{payload.get('customerName') or 'Unknown customer'}, {payload.get('model') or 'unknown model'}"
    labels = [str(f.get("label") or "") for f in payload.get("matchedFields") or [] if f.get("field") != "outlet"]
    on = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
    blocked = (" Uploads by both PCs are paused until a Reviewer deletes the duplicate booking."
               if payload.get("uploadsBlocked") else "")
    return (f"{title or CROSS_OUTLET_TITLE} — {who}. {new.get('reference') or 'A new journey'} "
            f"(PC {new.get('pcName') or 'unknown'}, {_place(new.get('dealerName'), new.get('outletName'))}) "
            f"and {existing.get('reference') or 'an earlier journey'} "
            f"(PC {existing.get('pcName') or 'unknown'}, {_place(existing.get('dealerName'), existing.get('outletName'))}) "
            f"are the same customer on {on}. Nothing was deleted.{blocked}")


def _raise_cross_outlet(connection: Connection, *, tenant_id: str, new: FileIdentity, existing: FileIdentity,
                        fields: list[dict[str, Any]], same_dealership: bool = False) -> bool:
    """The deep-red alert for a duplicate booking across outlets, once per newer journey.

    Product owner 2026-11-04:
    - another DEALERSHIP: a big red alert flashing on every screen of the Project, and a
      sound for the PCs of both bookings (DUPLICATE_ACROSS_OUTLETS, no audience);
    - another outlet of the SAME dealership: the PCs of both bookings may not upload until
      a Reviewer deletes the duplicate (DUPLICATE_SAME_DEALERSHIP, uc03_p2_upload_pause);
      the alert is for the PCs, Reviewers and Team Leads of both outlets."""
    kind = SAME_DEALERSHIP_KIND if same_dealership else CROSS_OUTLET_KIND
    title = SAME_DEALERSHIP_TITLE if same_dealership else CROSS_OUTLET_TITLE
    payload = {
        "customerName": new.name or existing.name,
        "model": new.model_name or existing.model_name,
        "variant": new.variant_name,
        "newJourney": {**_journey_view(new), "outletId": str(new.outlet_id) if new.outlet_id else None},
        "existingJourney": {**_journey_view(existing),
                            "outletId": str(existing.outlet_id) if existing.outlet_id else None},
        "matchedFields": fields,
        "severity": "CRITICAL",
        **({"audienceOutletIds": sorted({str(o) for o in (new.outlet_id, existing.outlet_id) if o}),
            "audienceRoles": ["PC", "REVIEWER", "TL"], "uploadsBlocked": True} if same_dealership else {}),
    }
    alert_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_tenant_alerts (
                tenant_id, alert_kind, subject_journey_id, subject_journey_reference, related_journey_id,
                alert_status, outcome, title, body, payload, published_at_utc, expires_at_utc
            ) VALUES (
                :t, :k, :j, :ref, :related, 'PUBLISHED', 'FLAGGED', :title, :body, CAST(:payload AS jsonb),
                now(), now() + (:hours * interval '1 hour')
            )
            ON CONFLICT (tenant_id, alert_kind, subject_journey_id) DO NOTHING
            RETURNING alert_id
            """
        ),
        {"t": tenant_id, "k": kind, "j": new.journey_id, "ref": new.reference,
         "related": existing.journey_id, "title": title, "body": cross_outlet_body(payload, title=title),
         "payload": json.dumps(payload, default=str), "hours": ALERT_HOURS},
    ).scalar_one_or_none()
    if alert_id is not None:
        logger.info("p2_duplicate_across_outlets", tenant_id=tenant_id, journey_id=str(new.journey_id),
                    journey_reference=new.reference, existing_journey_id=str(existing.journey_id),
                    existing_reference=existing.reference)
    return alert_id is not None


def _detect(connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str) -> dict[str, Any]:
    me = _identities(connection, tenant_id=tenant_id, journey_ids=[journey_id]).get(journey_id)
    # The customer, mobile and model are enough to look (a duplicate across outlets needs no
    # name); the same file still needs everything (matched_fields).
    # A journey known only by its PAN / Aadhaar and receipt can still be the same sale.
    if me is None or me.closed or not (me.pan or me.aadhaar):
        return {"examined": 0, "raised": 0}
    candidate_ids = [
        UUID(str(v)) for v in connection.execute(
            text(_SAME_CUSTOMER_SQL),
            {"tenant_id": tenant_id, "journey_id": journey_id, "pan": me.pan, "aadhaar": me.aadhaar},
        ).scalars().all()
    ]
    others = _identities(connection, tenant_id=tenant_id, journey_ids=candidate_ids)
    # A journey being read for a PC's bulk upload is left to BULK_SETTLE (2026-11-03): it
    # joins the customer's booking quietly instead of alerting the Project.
    from audit_core.uc03_p2_bulk_upload import reading_for_bulk

    bulk = reading_for_bulk(connection, tenant_id=tenant_id, journey_ids=[journey_id, *others])
    if journey_id in bulk:
        return {"examined": len(others), "raised": 0, "acrossOutlets": 0, "bulk": True}
    others = {k: v for k, v in others.items() if k not in bulk}
    cutoff = connection.execute(
        text("SELECT now() - (:hours * interval '1 hour')"), {"hours": SAME_FILE_WINDOW_HOURS},
    ).scalar_one()
    # Another outlet: a duplicate booking across outlets, alert only (2026-11-03). The newer
    # journey is named against the earliest booking at another outlet.
    across = [o for o in others.values() if not o.closed and cross_outlet_fields(me, o) is not None]
    flagged = 0
    if across:
        pair = sorted([me, *across], key=lambda identity: identity.order_key)
        earliest = pair[0]
        for newer in pair[1:]:
            if newer.created_at < cutoff or same_outlet(newer, earliest):
                continue
            fields = cross_outlet_fields(newer, earliest)
            if not fields:
                continue
            # Product owner 2026-11-04: at another outlet of the same dealership the PCs of both
            # stop uploading until a Reviewer deletes the duplicate; at another dealership a big
            # red alert on every screen.
            same_dealer = bool(newer.dealer_id and newer.dealer_id == earliest.dealer_id)
            if _raise_cross_outlet(connection, tenant_id=tenant_id, new=newer, existing=earliest, fields=fields,
                                   same_dealership=same_dealer):
                flagged += 1
    matches = [o for o in others.values() if not o.closed and same_file_fields(me, o) is not None]
    if not matches:
        return {"examined": len(others), "raised": 0, "acrossOutlets": flagged}
    group = [me, *matches]
    # Product owner 2026-11-04: the journey with more documents is kept (the earlier on a tie);
    # every other one is the copy, deleted once what only it holds is carried over.
    counts = document_counts(connection, tenant_id=tenant_id, journey_ids=[g.journey_id for g in group])
    original = min(group, key=lambda identity: (-counts.get(identity.journey_id, 0), identity.order_key))
    outcomes: list[str] = []
    for new in sorted(group, key=lambda identity: identity.order_key):
        if new is original:
            continue
        fields = same_file_fields(new, original)
        if fields is None:  # matched this journey, not the oldest one
            continue
        outcome = _raise(connection, tenant_id=tenant_id, new=new, existing=original, fields=fields,
                         correlation_id=correlation_id)
        if outcome is not None:
            outcomes.append(outcome)
    return {"examined": len(others), "raised": len(outcomes), "outcomes": outcomes, "acrossOutlets": flagged}


def sync_same_file_detection(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str,
) -> dict[str, Any]:
    """Look for an earlier journey that is the same file as this one (or a
    later one this one makes a copy of); alert and queue the delete. Never
    raises: a failure is logged and leaves the unit's own work untouched."""
    try:
        with connection.begin_nested():
            return _detect(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id)
    except Exception:
        logger.warning("p2_same_file_detection_failed", tenant_id=tenant_id, journey_id=str(journey_id),
                       exc_info=True)
        return {"error": True}


def recheck_pairs_of(connection: Connection, *, tenant_id: str, deleted_journey_id: UUID,
                     correlation_id: str | None) -> int:
    """After a journey is deleted: the journeys whose open DUPLICATE_BOOKING
    finding names it as their original are checked again at once (forced),
    so the finding -- and its Team Lead task -- closes instead of pointing
    at a journey that is gone. Runs in the delete's own transaction."""
    from audit_core.uc03_p2_controls import request_control_evaluation

    journeys = connection.execute(
        text("SELECT DISTINCT journey_id FROM auditcore.audit_findings WHERE tenant_id=:t "
             "AND finding_type_code='DUPLICATE_BOOKING' AND rule_key=:key "
             "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
        {"t": tenant_id, "key": f"DUPLICATE_BOOKING:{deleted_journey_id}"},
    ).scalars().all()
    for journey_id in journeys:
        request_control_evaluation(connection, tenant_id=tenant_id, journey_id=UUID(str(journey_id)),
                                   correlation_id=correlation_id, delay_seconds=0, force=True)
    return len(journeys)


# ── the delete, as its own work item ──────────────────────────────────────────
def recheck_before_delete(
    connection: Connection, *, tenant_id: str, new_journey_id: UUID, existing_journey_id: UUID | None,
    existing_reference: str | None,
) -> None:
    """At delete time, in the deletion record's transaction (review of #594):
    the earlier journey is locked and must still exist and be open, the two
    must still agree on all four values, and nothing may say the booking is a
    company's, and (2026-11-02) the booking reference number must be read on
    both and the same. Raises SameFileNoLongerHolds with the reason,
    NotFoundError when the new journey itself is gone."""
    ref = existing_reference or "the earlier journey"
    if existing_journey_id is None:
        raise SameFileNoLongerHolds("the earlier journey is not known")
    locked = connection.execute(
        text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j FOR UPDATE"),
        {"t": tenant_id, "j": existing_journey_id},
    ).scalar_one_or_none()
    files = _identities(connection, tenant_id=tenant_id, journey_ids=[new_journey_id, existing_journey_id])
    new = files.get(new_journey_id)
    if new is None:
        raise NotFoundError(error_code="VAC-NF-005", title="Journey not found", detail="The new journey is gone.")
    existing = files.get(existing_journey_id) if locked is not None else None
    if existing is None:
        raise SameFileNoLongerHolds(f"{ref} no longer exists")
    if existing.closed:
        raise SameFileNoLongerHolds(f"{ref} is closed")
    if new.closed:
        raise SameFileNoLongerHolds("it is already closed")
    if same_file_fields(new, existing) is None:
        raise SameFileNoLongerHolds(
            f"its PAN / Aadhaar, mobile, model, customer name, booking reference number or receipt "
            f"no longer match {ref}")
    counts = document_counts(connection, tenant_id=tenant_id, journey_ids=[new_journey_id, existing_journey_id])
    if counts.get(new_journey_id, 0) > counts.get(existing_journey_id, 0):
        # the copy now holds more documents (pages still arriving): the other one is the copy
        raise SameFileNoLongerHolds(f"it now holds more documents than {ref}")
    if identical_documents(connection, tenant_id=tenant_id, new=new, existing=existing):
        return  # exactly the same documents (2026-11-12)
    if not booking_identified(new, existing):
        raise SameFileNoLongerHolds(_no_booking_reference_reason(new, existing))
    reason = business_reason(connection, tenant_id=tenant_id, files=[new, existing]) or _own_documents_reason(
        connection, tenant_id=tenant_id, new=new, existing=existing)
    if reason:
        raise SameFileNoLongerHolds(reason)


def deletion_details(connection: Connection, *, tenant_id: str, new_journey_id: UUID,
                     existing_journey_id: UUID | None) -> dict[str, Any]:
    """What the deletion record keeps of a copy deleted by this rule
    (product owner 2026-11-02), read in the record's transaction before any
    of the journey's facts go: the PAN and mobile in full, the Aadhaar masked
    to its last four digits (never the full number), the booking reference
    number as read, the model, and the copy kept."""
    files = _identities(connection, tenant_id=tenant_id,
                        journey_ids=[j for j in (new_journey_id, existing_journey_id) if j])
    new = files.get(new_journey_id)
    existing = files.get(existing_journey_id) if existing_journey_id else None
    return {
        "deletion_kind": DELETION_KIND,
        "customer_pan": new.pan if new else None,
        "customer_aadhaar_masked": mask_aadhaar(new.aadhaar) if new and new.aadhaar else None,
        "customer_mobile": new.mobile if new else None,
        "booking_reference": new.booking_ref_display if new else None,
        "model_name": new.model_name if new else None,
        "kept_journey_id": existing_journey_id,
        "kept_journey_reference": existing.reference if existing else None,
    }


def _upload_wait_seconds(connection: Connection, *, tenant_id: str, journey_id: UUID) -> int:
    """Seconds until nothing was uploaded to the journey for UPLOAD_QUIET_MINUTES; 0 when quiet."""
    remaining = connection.execute(
        text(
            """
            SELECT EXTRACT(EPOCH FROM (GREATEST(
                     (SELECT max(created_at_utc) FROM auditcore.p2_upload_batches
                       WHERE tenant_id=:t AND journey_id=:j),
                     (SELECT max(created_at_utc) FROM auditcore.p2_document_queue
                       WHERE tenant_id=:t AND journey_id=:j))
                   + (:minutes * interval '1 minute') - now()))
            """
        ),
        {"t": tenant_id, "j": journey_id, "minutes": UPLOAD_QUIET_MINUTES},
    ).scalar_one_or_none()
    return max(0, int(float(remaining))) if remaining is not None else 0


def run_same_file_delete(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    security_client: Any,
    v2_client: Any,
    storage: Any,
    before_record: Callable[[Connection], None] | None = None,
) -> str:
    """Delete the new file and publish its alert. Returns DELETED, GONE (the
    journey was already removed; the alert goes out as removed),
    KEPT_REVIEWED / KEPT_CLOSED (the delete guard refused it), KEPT_CHANGED
    (re-checked, no longer the same file) or NOTHING (no pending alert:
    already handled). Raises UploadInProgress while pages are still
    arriving, and a technical failure for the work item to be retried."""
    from audit_core.db import set_tenant_context
    from audit_core.uc03_p2_journey_delete import system_delete_journey

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        alert = connection.execute(
            text("SELECT payload, related_journey_id FROM auditcore.p2_tenant_alerts WHERE tenant_id=:t "
                 "AND alert_kind=:k AND subject_journey_id=:j AND alert_status='PENDING'"),
            {"t": tenant_id, "k": ALERT_KIND, "j": journey_id},
        ).mappings().one_or_none()
        wait = _upload_wait_seconds(connection, tenant_id=tenant_id, journey_id=journey_id) if alert else 0
    if alert is None:
        return "NOTHING"
    if wait > 0:
        raise UploadInProgress(wait + 30)
    existing_ref = (dict(alert["payload"] or {}).get("existingJourney") or {}).get("reference")
    existing_id = UUID(str(alert["related_journey_id"])) if alert["related_journey_id"] else None
    reason = (f"Same file uploaded again: matches {existing_ref or 'an earlier journey'} on PAN / Aadhaar, "
              "mobile, model, customer name and booking reference number")

    def publish(outcome: str, kept_reason: str | None = None) -> Callable[[Connection], None]:
        return lambda connection: publish_alert(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                outcome=outcome, kept_reason=kept_reason)

    def deleted(connection: Connection) -> None:
        publish(DELETED)(connection)
        recheck_pairs_of(connection, tenant_id=tenant_id, deleted_journey_id=journey_id,
                         correlation_id=correlation_id)

    carried: list[str] = []  # the kinds carried, for the alert

    def check(connection: Connection, continuing: bool) -> None:
        if before_record is not None:
            before_record(connection)
        if continuing:
            # Part of it is already gone: finish it, whatever has changed.
            return
        recheck_before_delete(connection, tenant_id=tenant_id, new_journey_id=journey_id,
                              existing_journey_id=existing_id, existing_reference=existing_ref)

    kept_reason: str | None = None
    try:
        # 2026-11-03: what only the copy holds goes to the earlier journey first -- once the
        # pair still holds without it (nothing is copied for a copy that will be kept).
        if existing_id is not None:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                recheck_before_delete(connection, tenant_id=tenant_id, new_journey_id=journey_id,
                                      existing_journey_id=existing_id, existing_reference=existing_ref)
            carried = carry_documents(engine, tenant_id=tenant_id, new_journey_id=journey_id,
                                      existing_journey_id=existing_id)
            if carried:
                with engine.begin() as connection:
                    set_tenant_context(connection, tenant_id)
                    connection.execute(
                        text("UPDATE auditcore.p2_tenant_alerts SET payload = payload || CAST(:extra AS jsonb), "
                             "updated_at_utc=now() WHERE tenant_id=:t AND alert_kind=:k AND subject_journey_id=:j"),
                        {"extra": json.dumps({"carriedDocuments": carried}), "t": tenant_id, "k": ALERT_KIND,
                         "j": journey_id},
                    )
        system_delete_journey(
            engine, tenant_id=tenant_id, journey_id=journey_id, reason=reason, correlation_id=correlation_id,
            security_client=security_client, v2_client=v2_client, storage=storage,
            on_deleted=deleted, before_record=check,
            record_details=lambda connection: deletion_details(
                connection, tenant_id=tenant_id, new_journey_id=journey_id, existing_journey_id=existing_id),
        )
        return DELETED
    except NotFoundError:
        outcome, result = REMOVED, "GONE"
    except SameFileNoLongerHolds as exc:
        outcome, result, kept_reason = KEPT_CHANGED, KEPT_CHANGED, exc.reason
    except ConflictError as exc:
        outcome = KEPT_REVIEWED if "reviewed" in exc.title.lower() else KEPT_CLOSED
        result = outcome
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        publish(outcome, kept_reason)(connection)
    logger.info("p2_same_file_delete_settled", tenant_id=tenant_id, journey_id=str(journey_id), outcome=result,
                kept_reason=kept_reason)
    return result
