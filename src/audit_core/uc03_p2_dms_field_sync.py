"""DMS dumps correct what the Journey's papers were read as (product owner 2026-11-23).

"when we have delivery and booking information from DMS can we check and correct the wrong
information captured in the Journey": the sales consultant, the OTF number, the enquiry number
and the PIN code from the booking dump; the DMS invoice number and the chassis / VIN from the
delivery dump. The DMS model and variant are not used ("ignore DMS Model"): the DMS names
them as codes the price list does not use. The dates have their own path (uc03_p2_dms_dumps,
_sure_match_sync) and are left as they are.

Only a match the product owner trusts corrects anything:
- booking dump: the OTF and the customer's name agree, and the mobile or the model too;
- delivery dump: the OTF, the model and the customer's name agree (a dump with no OTF column
  corrects nothing).

A value is corrected only where a document of the Journey was read with one, through the field
correction path: the earlier value kept in the change log (DMS_SYNC), the uploader named. A
newer dump of the same kind leaves the Journey to that dump. Reads in plan_field_fixes, writes
in apply_field_fixes.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_enquiry_number import enquiry_key
from audit_core.uc03_p2_names import same_person

logger = structlog.get_logger(__name__)

BOOKING, DELIVERY = "BOOKING", "DELIVERY"
BOOKING_DOCS = ("booking_form", "booking_docket")


def _alnum(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _raw(row: dict[str, Any], *names: str) -> Any:
    raw = row.get("raw") or {}
    if not isinstance(raw, dict):
        return None
    for name in names:
        if (value := raw.get(name)) not in (None, ""):
            return value
    return None


def same_vehicle(a: Any, b: Any) -> bool:
    """The same model however it is spaced or extended ("SCORPIO-N" / "Scorpio N" / "SCORPION
    Z8L D", "XUV3XO" / "3XO"): one name within the other, letters and digits only. Stricter than
    matching's same_model, which takes a word in common ("XUV 3XO" and "XUV 7XO")."""
    x, y = _alnum(a), _alnum(b)
    if not x or not y:
        return False
    shorter, longer = sorted((x, y), key=len)
    return len(shorter) >= 3 and shorter in longer


def _otf_agrees(row: dict[str, Any], journey: dict[str, Any]) -> bool:
    from audit_core.uc03_p2_dms_dumps import booking_keys

    return bool(booking_keys(row.get("booking_number"), row.get("dms_booking_number")) & (journey.get("refs") or set()))


def trusted(kind: str, row: dict[str, Any], journey: dict[str, Any] | None) -> bool:
    """A match good enough to correct the Journey's papers by (product owner 2026-11-23: "for
    booking it is OTF and customer name minimum, with mobile number matching is +, or model
    matching is + for delivery it is booking OTF, model, customer name at minimum")."""
    from audit_core.uc03_p2_dms_dumps import mobile_key

    if journey is None or not _otf_agrees(row, journey):
        return False
    name = row.get("customer_name")
    if not (name and any(same_person(name, n) for n in journey.get("names") or ())):
        return False
    model = same_vehicle(row.get("model"), journey.get("model"))
    if kind == BOOKING:
        return model or mobile_key(row.get("mobile")) in (journey.get("mobiles") or set())
    return model


# --------------------------------------------------------------------------- the fields

@dataclass(frozen=True)
class FieldRule:
    """One field the DMS corrects: where it is read, what the dump gives, and whether a read
    value already says the same."""
    field_key: str
    documents: tuple[str, ...]
    dms: Callable[[dict[str, Any]], str | None]
    agrees: Callable[[str, str], bool]
    # The value to write over a read one (the OTF: the DMS number of the same kind).
    target: Callable[[str, dict[str, Any]], str | None] = lambda read, dms: dms["value"]


def _text(value: Any) -> str | None:
    out = re.sub(r"\s+", " ", str(value or "")).strip()
    return out or None


def _consultant(row: dict[str, Any]) -> str | None:
    return _text(row.get("consultant"))


def _pin(row: dict[str, Any]) -> str | None:
    digits = re.sub(r"\D", "", str(_raw(row, "CUSTOMER MAILINGPOSTALCODE") or ""))
    return digits if re.fullmatch(r"[1-9]\d{5}", digits) else None


def _pin_agrees(read: str, dms: str) -> bool:
    return re.sub(r"\D", "", read) == dms


def _enquiry(row: dict[str, Any]) -> str | None:
    value = _text(_raw(row, "ENQUIRY NUMBER", "ENQUIRY NO"))
    return value if enquiry_key(value) else None


def _enquiry_agrees(read: str, dms: str) -> bool:
    return enquiry_key(read) == enquiry_key(dms)


def _otf(row: dict[str, Any]) -> str | None:
    return _text(row.get("booking_number")) or _text(row.get("dms_booking_number"))


def otf_target(read: str, row: dict[str, Any]) -> str | None:
    """The DMS number of the kind the paper carries: a B-number read ("77700040") becomes the
    DMS's B-number, an OTF read ("019876", "OTF 31415") the DMS's full OTF number. A read value
    that is neither stays as it is."""
    digits = re.sub(r"\D", "", read).lstrip("0")
    b, otf = _text(row.get("booking_number")), _text(row.get("dms_booking_number"))
    if b and digits and re.sub(r"\D", "", b).lstrip("0") == digits:
        return b
    if otf:
        bare = _alnum(otf).removeprefix("OTF")
        tail = re.search(r"(\d{4,})$", bare)
        if digits and (_alnum(read).removeprefix("OTF") == bare
                       or (tail is not None and tail.group(1).lstrip("0") == digits)):
            return otf
    return None


def _otf_agrees_read(read: str, dms: str) -> bool:  # decided by otf_target alone
    return False


# The papers that print the chassis / VIN: uc03_p2_dms_dumps.CHASSIS_DOCUMENT_TYPES (that module
# imports this one; a test keeps the two the same).
CHASSIS_DOCS = (
    "customer_invoice_dms", "customer_invoice_tally", "customer_ledger", "form_21", "vehicle_rc", "gate_pass",
    "tax_invoice_tally", "accessory_invoice_dms", "accessory_invoice_tally", "insurance_cover", "rto_challan",
    "ew_invoice", "rsa_invoice", "delivery_order_cover",
)


def _chassis(row: dict[str, Any]) -> str | None:
    value = _alnum(row.get("chassis"))
    return value if len(value) >= 6 else None


def _chassis_agrees(read: str, dms: str) -> bool:
    """The DMS's chassis is the full VIN (17 characters) or its last characters (Aditya's sheet
    prints the last 8): the paper agrees when it ends with them."""
    r, d = _alnum(read), _alnum(dms)
    return r == d if len(d) >= 17 else r.endswith(d)


def vin_target(read: str, dms: str) -> str | None:
    """The VIN to write: the DMS's when it gives the full VIN; when it gives only the last
    characters, the paper's VIN with those characters replaced -- only on a full 17-character
    VIN read, else nothing."""
    r, d = _alnum(read), _alnum(dms)
    if len(d) >= 17:
        return d
    if len(r) == 17 and 6 <= len(d) < 17:
        return r[:17 - len(d)] + d
    return None


def _invoice(row: dict[str, Any]) -> str | None:
    value = _text(row.get("invoice_number"))
    return value if value and len(_alnum(value)) >= 5 else None


def _invoice_agrees(read: str, dms: str) -> bool:
    return _alnum(read) == _alnum(dms)


RULES: dict[str, tuple[FieldRule, ...]] = {
    BOOKING: (
        FieldRule("sales_person", BOOKING_DOCS, _consultant, lambda r, d: same_person(r, d)),
        FieldRule("booking_reference_number", BOOKING_DOCS, _otf, _otf_agrees_read,
                  target=lambda read, dms: otf_target(read, dms["row"])),
        FieldRule("enquiry_number", BOOKING_DOCS, _enquiry, _enquiry_agrees),
        # The PIN is read from the address KYC (product owner 2026-11-23).
        FieldRule("address_pincode", ("aadhaar",), _pin, _pin_agrees),
        FieldRule("pin_code", ("customer_kyc",), _pin, _pin_agrees),
    ),
    # The sales consultant is the booking dump's alone (product owner 2026-11-23: "from delivery
    # dump we dont need sales consultant name ... we need to verify DMS invoice number and
    # chasis/VIN number").
    DELIVERY: (
        FieldRule("invoice_number", ("customer_invoice_dms",), _invoice, _invoice_agrees),
        FieldRule("vin_number", CHASSIS_DOCS, _chassis, _chassis_agrees,
                  target=lambda read, dms: vin_target(read, dms["value"])),
        FieldRule("chassis_number", CHASSIS_DOCS, _chassis, _chassis_agrees,
                  target=lambda read, dms: vin_target(read, dms["value"])),
    ),
}

_COPIES_SQL = """
    SELECT DISTINCT ON (f.journey_id, f.di_document_id, f.field_key)
           CAST(f.journey_id AS text) AS journey_id, CAST(f.di_document_id AS text) AS document_id,
           COALESCE(e.document_type_key, f.source_document_type_key) AS doc, f.field_key,
           f.source_canonical_field_id, f.source_fact_version, f.effective_value #>> '{}' AS value
    FROM auditcore.journey_document_extracted_fields f
    JOIN auditcore.evidence e
      ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
     AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
    WHERE f.tenant_id = :t AND CAST(f.journey_id AS text) = ANY(:js) AND f.field_key = ANY(:fks)
      AND COALESCE(e.document_type_key, f.source_document_type_key) = ANY(:docs)
      AND f.source_canonical_field_id IS NOT NULL AND f.source_fact_version IS NOT NULL
    ORDER BY f.journey_id, f.di_document_id, f.field_key, f.source_fact_version DESC NULLS LAST,
             f.updated_at_utc DESC
"""


def plan_field_fixes(connection: Connection, tenant_id: str, kind: str,
                     rows: list[tuple[dict[str, Any], str]], updates: dict[int, dict[str, Any]],
                     newer: set[str]) -> list[dict[str, Any]]:
    """The corrections the trusted rows call for. Reads only; each row's date_sync gets the
    fields it would correct ("fields")."""
    rules = RULES.get(kind, ())
    if not rows or not rules:
        return []
    journeys = sorted({jid for _, jid in rows} - newer)
    copies: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    if journeys:
        for r in connection.execute(text(_COPIES_SQL), {
                "t": tenant_id, "js": journeys, "fks": sorted({x.field_key for x in rules}),
                "docs": sorted({d for x in rules for d in x.documents})}).mappings():
            copies[(r["journey_id"], r["field_key"])].append(dict(r))
    fixes: list[dict[str, Any]] = []
    for row, jid in rows:
        if jid in newer:
            continue
        noted: list[dict[str, Any]] = []
        for rule in rules:
            value = rule.dms(row)
            if not value:
                continue
            for copy in copies.get((jid, rule.field_key), []):
                read = _text(copy["value"])
                if copy["doc"] not in rule.documents or not read or rule.agrees(read, value):
                    continue
                new = rule.target(read, {"value": value, "row": row})
                if not new or _alnum(new) == _alnum(read):  # spacing and punctuation aside
                    continue
                noted.append({"field": rule.field_key, "was": read, "dms": new, "status": "TO_APPLY"})
                fixes.append({"journeyId": jid, "documentId": copy["document_id"], "documentType": copy["doc"],
                              "canonicalFieldId": copy["source_canonical_field_id"],
                              "sourceFactVersion": copy["source_fact_version"], "fieldKey": rule.field_key,
                              "was": read, "dms": new, "rowNo": row["row_no"]})
        if noted:
            u = updates[row["row_no"]]
            u["ds"] = {**(u["ds"] or {}), "fields": noted}
    return fixes


def apply_field_fixes(connection: Connection, *, tenant_id: str, dump: dict[str, Any],
                      fixes: list[dict[str, Any]], updates: dict[int, dict[str, Any]]) -> int:
    """The planned corrections written through the field correction path (logged DMS_SYNC, the
    uploader named); the Journeys' facts noted as changed so their checks settle."""
    from fastapi import HTTPException

    from audit_core.uc03_p2_documents import apply_p2_field_correction
    from audit_core.uc03_p2_runtime import note_facts_changed

    word = "booking" if dump["dump_kind"] == BOOKING else "delivery"
    remarks = (f"As per the DMS {word} dump of {dump['business_date']:%d %b %Y} ({dump['file_name']}), "
               f"uploaded by {dump['uploaded_by_name'] or dump['uploaded_by']}")
    applied, changed = 0, set()
    for fix in fixes:
        status, reason = "APPLIED", None
        try:
            with connection.begin_nested():
                apply_p2_field_correction(
                    connection, tenant_id=tenant_id, journey_id=UUID(fix["journeyId"]),
                    document_id=UUID(fix["documentId"]), field_key=fix["fieldKey"],
                    canonical_field_id=fix["canonicalFieldId"], source_fact_version=int(fix["sourceFactVersion"]),
                    new_value=fix["dms"], actor_id=str(dump["uploaded_by"]), actor_role_code="DMS_SYNC",
                    remarks=f"{remarks}, row {fix['rowNo']}", change_reason="DMS_SYNC")
        except HTTPException as error:
            status, reason = "NOT_APPLIED", str(error.detail)[:300]
        except Exception as error:  # noqa: BLE001 -- one Journey's value must not fail the upload
            logger.warning("dms_field_sync_failed", journey_id=fix["journeyId"], field=fix["fieldKey"],
                           error=str(error)[:300])
            status, reason = "NOT_APPLIED", type(error).__name__
        else:
            applied += 1
            changed.add(fix["journeyId"])
        for noted in (updates[fix["rowNo"]].get("ds") or {}).get("fields", []):
            if noted["field"] == fix["fieldKey"] and noted["was"] == fix["was"]:
                noted["status"] = status
                if reason:
                    noted["reason"] = reason
    for jid in sorted(changed):
        note_facts_changed(connection, tenant_id=tenant_id, journey_id=UUID(jid), reason="DMS_FIELD_SYNC")
    return applied


__all__ = ["CHASSIS_DOCS", "RULES", "apply_field_fixes", "otf_target", "plan_field_fixes", "same_vehicle",
           "trusted", "vin_target"]
