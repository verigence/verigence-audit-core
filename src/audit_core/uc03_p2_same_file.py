"""The same file uploaded again (product owner, 2026-10-31).

    "When ever a PC enters a Journey which has existing PAN, Mobile No,
    Customer Name, Model same, Pls raise an Alert across all PC telling Same
    File Being Uploaded. Give the name of customer, Model, PC Name and
    Dealership, and delete the journey completely. This alert should be seen
    by all team on their respective screens, if they are active."

What is "the same file"
    Two journeys of one Project (any dealership) that agree on ALL FOUR:
      - PAN: the journey's highest-confidence ``pan_number``, in PAN shape
        (``_valid_pan``), equal;
      - mobile: the highest-confidence ``customer_phone``, as a real Indian
        mobile (``_normalize_mobile``: the last 10 digits, starting 6-9), equal;
      - customer name: the KYC name on the customer (``customers.legal_name``,
        VERIFIED), the same person by ``same_person`` -- a masked PAN name
        ("PXXXXJ KXXXR") by ``masked_matches``, two masked names by equality;
      - model: both SKUs pinned -> the same ``product_skus.model_id``;
        otherwise the model names (the master's once pinned, the booking
        form's until then) are one family by ``generation_key``
        ("NEW THAR 2WD" is "THAR"; "THAR ROXX" is not).
    Or (product owner 2026-11-01) the same PAN and customer name and the same
    receipt number on a receipt of both, the model not differing (it may be
    unidentified on one): two separate bookings never share a receipt.
    A journey missing any of the four, and with no receipt number, is not compared. Closed journeys
    (delivered, booking cancelled, duplicate booking, no delivery) are never
    paired. The DUPLICATE_BOOKING rule (one basis is enough, a TL task) is
    unchanged; this is the stricter, all-four check on top of it.

Which one is the new file
    The later ``created_at_utc``, ``journey_id`` breaking a tie -- the same
    order whichever journey's check runs, so exactly one side is ever "new".
    The check runs in the NATIVE:BOOKING unit of either journey; when the
    older one is the last to have all four values, its own run flags the
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

Safeguards (review of #594, 2026-10-31)
    - Only a new file created within P2_SAME_FILE_WINDOW_HOURS (48) is acted
      on; older pairs stay with DUPLICATE_BOOKING and the purge job.
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.errors import ConflictError, NotFoundError
from audit_core.uc03_duplicate_booking_detection import (
    _buyer_gstin,
    _normalize_mobile,
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
DELETE_WORK_TYPE = "SAME_FILE_DELETE"
ALERT_HOURS = 24
ALERT_TITLE = "Same file being uploaded"
# Only a new file uploaded within this window is acted on (review of #594,
# 2026-10-31): the nightly forced re-run of every booking's checks must not
# delete older pairs -- those stay with the DUPLICATE_BOOKING rule and the
# product owner's own list (DUPLICATE_JOURNEY_PURGE).
SAME_FILE_WINDOW_HOURS = int(os.environ.get("P2_SAME_FILE_WINDOW_HOURS", "48"))
# A journey still receiving pages is not deleted under the PC's upload: the
# delete waits until nothing was uploaded to it for this long.
UPLOAD_QUIET_MINUTES = 10

# Outcomes, as the alert states them.
DELETED = "DELETED"
KEPT_NOT_INDIVIDUAL = "KEPT_NOT_INDIVIDUAL"
KEPT_REVIEWED = "KEPT_REVIEWED"
KEPT_OWN_DOCUMENTS = "KEPT_OWN_DOCUMENTS"  # the new file holds a document the earlier one does not
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
          AND f.field_key IN ('pan_number', 'customer_phone')
          AND f.effective_value IS NOT NULL
          AND {_ON_ACTIVE_DOCUMENT}
    )
    SELECT j.journey_id, j.created_at_utc, j.journey_reference,
           j.created_by_display_name AS pc_name, j.created_by_actor_id AS pc_actor_id,
           d.dealer_name, o.outlet_name,
           (SELECT r.value FROM ranked r
             WHERE r.journey_id = j.journey_id AND r.field_key = 'pan_number' AND r.row_rank = 1) AS pan,
           (SELECT r.value FROM ranked r
             WHERE r.journey_id = j.journey_id AND r.field_key = 'customer_phone' AND r.row_rank = 1) AS phone,
           CASE WHEN c.legal_name_status = 'VERIFIED' THEN c.legal_name END AS customer_name,
           s.model_id, jp.model_name_snapshot AS model_name, jp.variant_name_snapshot AS variant_name,
           (SELECT array_agg(DISTINCT f.effective_value #>> '{{}}')
              FROM auditcore.journey_document_extracted_fields f
             WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id AND f.field_key = 'receipt_number'
               AND f.source_document_type_key IN ('dealer_receipt', 'payment_receipt')
               AND f.effective_value IS NOT NULL AND {_ON_ACTIVE_DOCUMENT}) AS receipts,
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

# Journeys whose extracted PAN, on any document, is this PAN: a narrowing
# only -- each is then compared on its own highest-confidence PAN.
_SAME_PAN_SQL = f"""
    SELECT DISTINCT f.journey_id
    FROM auditcore.journey_document_extracted_fields f
    WHERE f.tenant_id = :tenant_id AND f.field_key = 'pan_number' AND f.journey_id <> :journey_id
      AND upper(regexp_replace(COALESCE(f.effective_value #>> '{{}}', ''), '[^A-Za-z0-9]', '', 'g')) = :pan
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
    # The receipt numbers on its receipts still on file (2026-11-01): the same
    # receipt on two bookings is the same booking entered twice.
    receipts: frozenset[str] = frozenset()

    @property
    def model_key(self) -> str:
        return generation_key(self.model_name) if self.model_name and self.model_name.strip() else ""

    @property
    def complete(self) -> bool:
        return bool(self.pan and self.mobile and self.name and (self.model_id or self.model_key))

    @property
    def comparable(self) -> bool:
        """All four values, or a PAN, a customer name and a receipt number."""
        return self.complete or bool(self.pan and self.name and self.receipts)

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
        out[UUID(str(row["journey_id"]))] = FileIdentity(
            journey_id=UUID(str(row["journey_id"])),
            created_at=row["created_at_utc"],
            reference=row["journey_reference"],
            pc_name=row["pc_name"],
            pc_actor_id=str(row["pc_actor_id"]) if row["pc_actor_id"] else None,
            dealer_name=row["dealer_name"],
            outlet_name=row["outlet_name"],
            pan=_valid_pan(row["pan"]),
            mobile=_normalize_mobile(row["phone"]),
            name=name,
            model_id=UUID(str(row["model_id"])) if row["model_id"] else None,
            model_name=(str(row["model_name"]).strip() or None) if row["model_name"] else None,
            variant_name=(str(row["variant_name"]).strip() or None) if row["variant_name"] else None,
            closed=bool(row["is_closed"]),
            receipts=frozenset(r for r in (normalize_receipt(v) for v in row["receipts"] or []) if r),
        )
    return out


def normalize_receipt(value: Any) -> str | None:
    """A receipt number as printed, upper case without spaces; None for a
    value too short or without a digit to tell receipts apart."""
    code = "".join(str(value or "").upper().split())
    return code if len(code) >= 4 and any(ch.isdigit() for ch in code) else None


# ── the four comparisons ──────────────────────────────────────────────────────
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


def _models_differ(left: FileIdentity, right: FileIdentity) -> bool:
    """Both carry a model and it is not the same one."""
    known = (left.model_id or left.model_key) and (right.model_id or right.model_key)
    return bool(known) and not same_model(left, right)


def matched_fields(new: FileIdentity, existing: FileIdentity) -> list[dict[str, Any]] | None:
    """The fields, as a reviewer should see them, when the two files are the
    same booking; None otherwise.

    The same file is (a) all four -- PAN, mobile, customer name and model --
    or (b) since 2026-11-01, the same PAN and customer name and the same
    receipt number on both, the model not differing (product owner
    2026-11-01). Two separate bookings never share a receipt; a booking
    entered twice does, even when the mobile was read on one copy only or the
    model not identified on one (13 such pairs found on 2026-11-01, among them
    VJ2610-000061 / UTK-SB-261019-001 and UTK-RM-261024-008 / UTK-RM-261031-002)."""
    if not (new.pan and new.pan == existing.pan and same_name(new.name, existing.name)):
        return None
    pan = {"field": "pan", "label": "PAN", "newValue": _masked(new.pan), "existingValue": _masked(existing.pan),
           "exact": True}
    name = {"field": "name", "label": "Customer name", "newValue": new.name, "existingValue": existing.name,
            "exact": new.name == existing.name}
    model = {"field": "model", "label": "Model", "newValue": new.model_name, "existingValue": existing.model_name,
             "exact": bool(new.model_id and new.model_id == existing.model_id) or new.model_name == existing.model_name}
    mobile = {"field": "mobile", "label": "Mobile", "newValue": _masked(new.mobile),
              "existingValue": _masked(existing.mobile), "exact": True}
    if new.complete and existing.complete and new.mobile == existing.mobile and same_model(new, existing):
        return [pan, mobile, name, model]
    shared = sorted(new.receipts & existing.receipts)
    if not shared or _models_differ(new, existing):
        return None
    receipt = {"field": "receipt", "label": "Receipt number", "newValue": ", ".join(shared),
               "existingValue": ", ".join(shared), "exact": True}
    fields = [pan, name, receipt]
    if new.mobile and new.mobile == existing.mobile:
        fields.insert(1, mobile)
    if new.model_name or existing.model_name:
        fields.append(model)
    return fields


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
        if not is_individual_pan(identity.pan):
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
              "receipt number").replace("Mobile", "mobile").replace("Model", "model")
              for f in payload.get("matchedFields") or []] or ["PAN", "mobile", "customer name", "model"]
    on = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
    matches = (f"It matches {existing.get('reference') or 'an earlier journey'} "
               f"(PC {existing.get('pcName') or 'unknown'}, "
               f"{_place(existing.get('dealerName'), existing.get('outletName'))}) "
               f"on {on}.")
    what = {
        DELETED: f"The new file {ref} was deleted.",
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


def _own_documents_reason(connection: Connection, *, tenant_id: str, new: FileIdentity,
                          existing: FileIdentity) -> str | None:
    only = documents_only_on(connection, tenant_id=tenant_id, new_journey_id=new.journey_id,
                             existing_journey_id=existing.journey_id)
    if not only:
        return None
    return f"a document {existing.reference or 'the earlier journey'} does not: {', '.join(only).replace('_', ' ')}"


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
    if kept_reason:
        outcome = KEPT_NOT_INDIVIDUAL
    elif _reviewed(connection, tenant_id=tenant_id, journey_id=new.journey_id):
        outcome = KEPT_REVIEWED
    elif (own := _own_documents_reason(connection, tenant_id=tenant_id, new=new, existing=existing)):
        outcome, kept_reason = KEPT_OWN_DOCUMENTS, own
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


def _detect(connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str) -> dict[str, Any]:
    me = _identities(connection, tenant_id=tenant_id, journey_ids=[journey_id]).get(journey_id)
    if me is None or me.closed or not me.comparable:
        return {"examined": 0, "raised": 0}
    candidate_ids = [
        UUID(str(v)) for v in connection.execute(
            text(_SAME_PAN_SQL), {"tenant_id": tenant_id, "journey_id": journey_id, "pan": me.pan},
        ).scalars().all()
    ]
    others = _identities(connection, tenant_id=tenant_id, journey_ids=candidate_ids)
    matches = [o for o in others.values() if not o.closed and matched_fields(me, o) is not None]
    if not matches:
        return {"examined": len(others), "raised": 0}
    group = [me, *matches]
    original = min(group, key=lambda identity: identity.order_key)
    cutoff = connection.execute(
        text("SELECT now() - (:hours * interval '1 hour')"), {"hours": SAME_FILE_WINDOW_HOURS},
    ).scalar_one()
    outcomes: list[str] = []
    for new in sorted(group, key=lambda identity: identity.order_key):
        if new is original:
            continue
        if new.created_at < cutoff:
            # Not a file just uploaded again: left to DUPLICATE_BOOKING.
            continue
        fields = matched_fields(new, original)
        if fields is None:  # matched this journey, not the oldest one
            continue
        outcome = _raise(connection, tenant_id=tenant_id, new=new, existing=original, fields=fields,
                         correlation_id=correlation_id)
        if outcome is not None:
            outcomes.append(outcome)
    return {"examined": len(others), "raised": len(outcomes), "outcomes": outcomes}


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
    company's. Raises SameFileNoLongerHolds with the reason, NotFoundError
    when the new journey itself is gone."""
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
    if matched_fields(new, existing) is None:
        raise SameFileNoLongerHolds(f"its PAN, customer name, mobile, model or receipt no longer match {ref}")
    reason = business_reason(connection, tenant_id=tenant_id, files=[new, existing]) or _own_documents_reason(
        connection, tenant_id=tenant_id, new=new, existing=existing)
    if reason:
        raise SameFileNoLongerHolds(reason)


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
    reason = f"Same file uploaded again: matches {existing_ref or 'an earlier journey'} on PAN, mobile, name and model"

    def publish(outcome: str, kept_reason: str | None = None) -> Callable[[Connection], None]:
        return lambda connection: publish_alert(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                outcome=outcome, kept_reason=kept_reason)

    def deleted(connection: Connection) -> None:
        publish(DELETED)(connection)
        recheck_pairs_of(connection, tenant_id=tenant_id, deleted_journey_id=journey_id,
                         correlation_id=correlation_id)

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
        system_delete_journey(
            engine, tenant_id=tenant_id, journey_id=journey_id, reason=reason, correlation_id=correlation_id,
            security_client=security_client, v2_client=v2_client, storage=storage,
            on_deleted=deleted, before_record=check,
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
