"""uc03_model_resolution.py — name a booking's SKU in the OEM price masters
from the words its documents print, or raise ``MODEL_NOT_IDENTIFIED`` for the
PC (product owner rules 2026-10-20, R3 and R4).

The ladder, run per document (the booking form; each customer vehicle
invoice on its own -- never the OEM's wholesale invoice; never a model from
one document with a variant or price from another):

  0. The price list in force on the Journey's pricing date
     (``journey_pricing_date``: the date the Journey is priced on, else the
     booking form's date, else the booking date entered by hand -- never
     today). No date, no resolution.
  1. Category first (``uc03_sku_words``): EV or ICE, Private or Commercial.
     Uncertain -> a PC task, nothing pinned.
  2. Words: model + trim + variant after the spelling normalisation; the
     distinguishing words must match exactly in both directions. Exactly
     one price-master row is the word-identified SKU; anything else is a PC
     task with a shortlist (the shortlist is only *ordered* by closeness,
     never decided by it).
  3. Price check (booking): the word-identified SKU's master EX_SHOWROOM
     against the booking form's ex-showroom, within two rupees. Equal ->
     pinned as AUTO_MATCHED. Different, or no booking price -> pinned as
     AUTO_PRICE_MISMATCH (so the deal still shows against it) and a PC task
     "Confirm the model and price: master X vs booking Y" (2026-10-21).
  4. Invoice: when one customer vehicle invoice's words name exactly one SKU
     and its ex-showroom (rule R1: printed selling price before discount +
     GST at the invoice's printed rate) equals that SKU's master price, the
     invoice sets the SKU (AUTO_INVOICE_MATCHED). It replaces an automatic
     pin; a SKU a PC or Team Lead set is never overwritten -- a Team Lead
     task asks instead. Invoice words naming one SKU at a different price ->
     a PC task. Several vehicle invoices naming different SKUs -> a PC task.

Price never picks a variant, fuel, gearbox, Private/Commercial or EV/ICE.
Automatic pins are TENTATIVE (``journey_products.match_basis`` says how they
were made, ``match_evidence`` keeps the document, words and prices compared)
and later evidence or a direct PC pick replaces them; a PC or TL decision is
CONFIRMED and changes only through a Team Lead reviewed proposal.

Idempotent, never raises (the sync entry points run inside a savepoint).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_db_transient import raise_if_worker_transient
from audit_core.uc03_delivery_commands import _machine_flag
from audit_core.uc03_manual_verification import _resolve_finding
from audit_core.uc03_masters_alignment import registration_basis
from audit_core.uc03_model_attribute_matching import (
    _decompose,
    _master_attributes,
    normalized_model_key,
)
from audit_core.uc03_sku_candidates import (
    _label_similarity,
    _price_plan_for_journey,
    journey_pricing_date,
)
from audit_core.uc03_sku_words import (
    WordResult,
    clear_suggestion,
    identify,
    order_shortlist,
    price_check,
    row_registration,
)
from audit_core.workflow import create_workflow_task

logger = logging.getLogger(__name__)
# One structured line per resolution attempt (Railway shows structlog lines;
# the stdlib logger above is kept for the existing tracebacks).
_outcome_log = structlog.get_logger(__name__)

_FINDING_TYPE = "MODEL_NOT_IDENTIFIED"
_RULE_KEY = "MODEL_NOT_IDENTIFIED:BOOKING"
_STAGE = "BOOKING"
_EX_SHOWROOM_COMPONENT = "EX_SHOWROOM"

# How a pin was made (selection_method); match_basis below says what it is.
_SELECTION_METHOD = "MODEL_RESOLUTION_WORDS_V2"  # the booking form's words
_INVOICE_SELECTION_METHOD = "MODEL_RESOLUTION_INVOICE_V2"  # a vehicle invoice's words + price
_PC_PICK_METHOD = "MODEL_RESOLUTION_PC_PICK_V1"
_CORRECTION_SELECTION_METHOD = "MODEL_SELECTION_CORRECTION_V1"
_INVOICE_TL_METHOD = "MODEL_RESOLUTION_INVOICE_TL_V1"  # a Team Lead applied the invoice's SKU

# journey_products.match_basis (migration 0152). Automatic pins are
# selection_status TENTATIVE and replaceable; PC / TL decisions CONFIRMED.
BASIS_AUTO_MATCHED = "AUTO_MATCHED"
BASIS_AUTO_PRICE_MISMATCH = "AUTO_PRICE_MISMATCH"
BASIS_AUTO_INVOICE = "AUTO_INVOICE_MATCHED"
BASIS_AUTO_LEGACY = "AUTO_LEGACY"
BASIS_AUTO_UNCONFIRMED = "AUTO_UNCONFIRMED"
BASIS_PC = "PC_CONFIRMED"
BASIS_TL = "TL_APPROVED"
AUTOMATIC_BASES = frozenset({BASIS_AUTO_MATCHED, BASIS_AUTO_PRICE_MISMATCH, BASIS_AUTO_INVOICE, BASIS_AUTO_LEGACY,
                             BASIS_AUTO_UNCONFIRMED})
HUMAN_BASES = frozenset({BASIS_PC, BASIS_TL})
# Plain words for the Deal screen (2026-10-20: automatic matches are not
# "Confirmed"; only a PC / TL decision is).
BASIS_LABELS = {
    BASIS_AUTO_MATCHED: "Matched automatically by its words; booking price agrees",
    BASIS_AUTO_PRICE_MISMATCH: "Matched automatically by its words; price differs from the booking -- PC to set",
    BASIS_AUTO_INVOICE: "Set by the vehicle invoice (words and price agree)",
    BASIS_AUTO_LEGACY: "Matched automatically before 20 Oct 2026 -- to be re-checked",
    BASIS_AUTO_UNCONFIRMED: "Matched automatically, but the documents do not name it -- PC to confirm or set",
    BASIS_PC: "Confirmed by the PC",
    BASIS_TL: "Approved by the Team Lead",
}

# The booking form (and docket) names the booked vehicle and its quoted
# ex-showroom price.
_BOOKING_DOCUMENT_TYPES = ("booking_form", "booking_docket")
_BOOKING_FIELD_KEYS = (
    "vehicle_model", "vehicle_variant", "vehicle_color", "sku_code", "ex_showroom_price", "registration_type",
)
# The customer's vehicle invoices: each one on its own (rule R2). The OEM's
# wholesale invoice bills the dealer, never the customer, and is not read
# (2026-10-20; it used to be). "tax_invoice_dms" / "tax_invoice" are the
# DMS / generic spellings the rest of the code base already reads as
# invoices.
_VEHICLE_INVOICE_TYPES = (
    "customer_invoice_dms", "customer_invoice_dms_v2", "tax_invoice_dms", "tax_invoice_tally", "tax_invoice",
    "invoice_generic",
)
_RETAIL_INVOICE_TYPES = frozenset({"customer_invoice_dms", "customer_invoice_dms_v2", "tax_invoice_dms",
                                   "tax_invoice_tally", "tax_invoice"})
# An invoice that bills something else (accessories, a warranty...) never
# names the vehicle, whatever its model box says.
_NON_VEHICLE_PURPOSES = frozenset({"ACCESSORY", "EXTENDED_WARRANTY", "RSA", "INSURANCE", "VEHICLE_WHOLESALE",
                                   "SERVICE", "FASTAG"})
_INVOICE_FIELD_KEYS = (
    "sku_code", "model_name_raw", "vehicle_description_raw", "variant_raw", "vehicle_color",
    "invoice_purpose", "invoice_nature", "invoice_number", "invoice_date", "vin_number", "chassis_number",
    "gross_amount_before_discount", "invoice_discount_amount", "taxable_amount",
    "cgst_rate", "sgst_rate", "igst_rate", "cgst_amount", "sgst_amount", "igst_amount", "cess_amount",
    "tcs_amount", "round_off_amount", "grand_total_amount", "line_items",
)

# A SKU a person set that the vehicle invoice disagrees with (kept for the
# findings raised before 2026-10-20; they close when the two agree).
_SKU_MISMATCH_FINDING_TYPE = "INVOICE_SKU_MISMATCH"
_SKU_MISMATCH_RULE_KEY = "INVOICE_SKU_MISMATCH"
_SKU_MISMATCH_WORKFLOW_TYPE = "UC03_INVOICE_SKU_MISMATCH"
_SKU_MISMATCH_TASK_TYPE = "INVOICE_SKU_MISMATCH_REASON"

_CATALOGUE_STAGE = "CATALOGUE_SEARCH"
# What each word-identification outcome is called on findings and in logs.
_STAGE_FOR_STATUS = {
    "IDENTIFIED": "WORDS",
    "AMBIGUOUS": "WORDS_AMBIGUOUS",
    "REGISTRATION_UNKNOWN": "REGISTRATION_UNKNOWN",
    "CATEGORY_UNCERTAIN": "CATEGORY_UNCERTAIN",
    "CATEGORY_CONFLICT": "CATEGORY_CONFLICT",
    "UNRECOGNISED_WORD": "WORDS_UNRECOGNISED",
    "NO_MATCH": "WORDS_NO_MATCH",
    "NO_MODEL": _CATALOGUE_STAGE,
    "NO_TEXT": "NONE",
}


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None




# ── reading the model text the way the price list names it ─────────────────────
_BRAND_PREFIX = re.compile(r"^\s*(?:MAHINDRA\s*(?:&|AND)\s*MAHINDRA|M\s*&\s*M|MAHINDRA)\b[\s/\-:.,]*", re.IGNORECASE)


def clean_model_text(value: str | None) -> str | None:
    """The model as printed, without the OEM's own name in front ("MAHINDRA
    BOLERO NEO+", "MAHINDRA & MAHINDRA MAXX HD", "MAHINDRA/BOLERO/MAXX PUP
    HD ...": DMS documents print it) and with "/" read as a space. Nothing
    else is changed (2026-10-20)."""
    if value is None:
        return None
    text_value = str(value)
    while True:
        stripped = _BRAND_PREFIX.sub("", text_value, count=1)
        if stripped == text_value:
            break
        text_value = stripped
    text_value = re.sub(r"\s+", " ", text_value.replace("/", " ")).strip()
    return text_value or None


# Registration type as DI reads it (values seen 2026-10-20: Personal,
# Private, Motor Car, Commercial, Fresh Permit, NEW, "New Registration (RTO
# Side)"). Only a stated private or commercial registration decides; the
# rest say nothing about it.
_COMMERCIAL_REGISTRATION = ("COMMERCIAL", "TRANSPORT", "TAXI", "GOODS", "PERMIT", "YELLOW")
_PRIVATE_REGISTRATION = ("PERSONAL", "PRIVATE", "MOTOR CAR", "NON TRANSPORT", "NON-TRANSPORT", "WHITE")


def registration_category(value: str | None) -> str | None:
    token = str(value or "").strip().upper()
    if not token:
        return None
    if any(word in token for word in ("NON TRANSPORT", "NON-TRANSPORT")):
        return "PRIVATE"
    if any(word in token for word in _COMMERCIAL_REGISTRATION):
        return "COMMERCIAL"
    if any(word in token for word in _PRIVATE_REGISTRATION):
        return "PRIVATE"
    return None


_row_registration = row_registration


def _rows_for_registration(rows: list[dict[str, Any]], category: str | None) -> list[dict[str, Any]]:
    """Drop the rows priced for the other registration when the Journey's
    registration type says private or commercial (2026-10-20)."""
    if category is None:
        return rows
    return [r for r in rows if _row_registration(r) in (None, category)]


# ── the price list's rows ─────────────────────────────────────────────────────
def _sku_rows_for_version(
    connection: Connection, *, tenant_id: str, price_list_version_id: UUID
) -> list[dict[str, Any]]:
    """SKU rows with on-road totals and the master ex-showroom.

    An OEM price row carries both REGISTRATION_INDIVIDUAL and
    REGISTRATION_CORPORATE components, so a naive SUM double-counts
    registration: ``master_total_individual`` excludes the corporate line,
    ``master_total_corporate`` the individual one (shown on the PC's
    shortlist; never used to choose a SKU).

    ``fuel_powertrain`` / ``transmission`` / ``drive`` / ``seater`` / ``trim``
    are the variant's structured columns; ``category`` / ``segment_code`` say
    whether it is electric (rule R4)."""
    rows = connection.execute(
        text(
            """
            SELECT s.product_sku_id,
                   s.sku_code,
                   pm.model_name,
                   pv.variant_name,
                   c.colour_name,
                   pv.fuel_powertrain,
                   pv.transmission,
                   pv.attributes ->> 'drive'    AS drive,
                   pv.attributes ->> 'seater'   AS seater,
                   pv.attributes ->> 'trim'     AS trim,
                   pv.attributes ->> 'category' AS category,
                   seg.segment_code,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key <> 'REGISTRATION_CORPORATE')
                                                                                AS master_total_individual,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key <> 'REGISTRATION_INDIVIDUAL')
                                                                                AS master_total_corporate,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key = :exkey)
                                                                                AS master_ex_showroom
            FROM auditcore.price_list_items pli
            JOIN auditcore.product_skus s      ON s.product_sku_id = pli.product_sku_id
            JOIN auditcore.product_models pm   ON pm.model_id  = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            LEFT JOIN auditcore.colours c      ON c.colour_id  = s.colour_id
            LEFT JOIN auditcore.segments seg   ON seg.segment_id = pm.segment_id
            WHERE pli.tenant_id = :tenant_id
              AND pli.price_list_version_id = :plv
              AND s.is_active = true
              AND pm.is_active = true
              AND pv.is_active = true
            GROUP BY s.product_sku_id, s.sku_code, pm.model_name, pv.variant_name, c.colour_name,
                     pv.fuel_powertrain, pv.transmission,
                     pv.attributes ->> 'drive', pv.attributes ->> 'seater', pv.attributes ->> 'trim',
                     pv.attributes ->> 'category', seg.segment_code
            ORDER BY pm.model_name, pv.variant_name, s.sku_code
            """
        ),
        {"tenant_id": tenant_id, "plv": price_list_version_id, "exkey": _EX_SHOWROOM_COMPONENT},
    ).mappings().all()
    return [dict(row) for row in rows]


def _current_rows(connection: Connection, *, tenant_id: str, journey_id: UUID,
                  on: date | None = None) -> dict[str, Any]:
    """The price list in force on the Journey's pricing date and its SKU rows,
    or ``{"skipped": True, "reason": ...}``."""
    on = on or journey_pricing_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if on is None:
        return {"skipped": True, "reason": "no_pricing_date"}
    try:
        plan = _price_plan_for_journey(connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=on)
    except AuditCoreError:  # no effective price list yet
        return {"skipped": True, "reason": "no_effective_price_list"}
    rows = _sku_rows_for_version(connection, tenant_id=tenant_id, price_list_version_id=plan["price_list_version_id"])
    if not rows:
        return {"skipped": True, "reason": "empty_price_list"}
    return {"plan": plan, "rows": rows, "on": on}


_INVOICE_DATE_FALLBACK_REASONS = ("no_pricing_date", "no_effective_price_list")


def _rows_on_invoice_date(
    connection: Connection, *, tenant_id: str, journey_id: UUID, invoices: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The price list in force on a vehicle invoice's own date, for a Journey
    whose own pricing date gives none. The earliest invoice date that has a
    list in force is used, so a later amended invoice cannot move the deal to
    a newer book. None when no invoice carries a date, or none of them has a
    list in force either."""
    dates = sorted({inv["invoiceDate"] for inv in invoices if inv.get("invoiceDate")})
    for on in dates:
        try:
            plan = _price_plan_for_journey(connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=on)
        except AuditCoreError:
            continue
        rows = _sku_rows_for_version(connection, tenant_id=tenant_id,
                                     price_list_version_id=plan["price_list_version_id"])
        if rows:
            return {"plan": plan, "rows": rows, "on": on, "pricingDateFrom": "INVOICE"}
    return None


def _plan_label(plan: dict[str, Any]) -> str:
    name = plan.get("price_list_name") or plan.get("price_list_code") or "Price list"
    version = plan.get("version_no")
    start = plan.get("effective_from")
    return f"{name}" + (f" v{version}" if version is not None else "") + (f" (from {start})" if start else "")


# ── what is on the Journey now ────────────────────────────────────────────────
def _pin_state(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT jp.product_sku_id, jp.selection_status, jp.selection_method, jp.match_basis,
                   jp.match_evidence, s.sku_code,
                   NULLIF(TRIM(jp.model_name_snapshot), '')   AS model_name,
                   NULLIF(TRIM(jp.variant_name_snapshot), '') AS variant_name,
                   NULLIF(TRIM(jp.colour_name_snapshot), '')  AS colour_name
            FROM auditcore.journey_products jp
            LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
            WHERE jp.tenant_id = :t AND jp.journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    return dict(row) if row else {
        "product_sku_id": None, "selection_status": None, "selection_method": None, "match_basis": None,
        "match_evidence": None, "sku_code": None, "model_name": None, "variant_name": None, "colour_name": None,
    }


def is_human_decision(state: dict[str, Any]) -> bool:
    """A SKU a PC or Team Lead set (CONFIRMED). Automatic pins are TENTATIVE
    since migration 0152; a CONFIRMED row from before it that the resolver
    made is AUTO_LEGACY there."""
    return state.get("product_sku_id") is not None and state.get("selection_status") == "CONFIRMED"


def _journey_registration(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The Journey's registration: Private or Commercial decides between the
    two SKUs of a pickup (rule R4). Several registration records that say
    different things leave it unknown -- never "the newest wins" (review
    471 S6)."""
    rows = connection.execute(
        text(
            """
            SELECT cu.customer_type_code, rr.registration_type_code, rr.registration_category_code
            FROM auditcore.journeys j
            LEFT JOIN auditcore.customers cu
              ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
            LEFT JOIN auditcore.registration_records rr
              ON rr.tenant_id = j.tenant_id AND rr.journey_id = j.journey_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            ORDER BY rr.updated_at_utc DESC NULLS LAST
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    reg = dict(rows[0]) if rows else {}
    categories = {
        registration_category(r.get("registration_type_code")) or registration_category(r.get("registration_category_code"))
        for r in rows
    } - {None}
    return {
        "basis": registration_basis(
            customer_type_code=reg.get("customer_type_code"),
            registration_type_code=reg.get("registration_type_code"),
        ),
        "category": next(iter(categories)) if len(categories) == 1 else None,
        "conflict": len(categories) > 1,
    }


# ── one document at a time ────────────────────────────────────────────────────
def _plain(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _document_fields(
    connection: Connection, *, tenant_id: str, journey_id: UUID, types: tuple[str, ...], keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Every document of these types with its fields as the PC left them
    (the corrected value wins), newest document first. A voided, superseded
    or unlinked document stops counting (rule R2)."""
    rows = connection.execute(
        text(
            """
            SELECT f.di_document_id, f.source_document_type_key, f.field_key, f.effective_value,
                   f.updated_at_utc
            FROM auditcore.journey_document_extracted_fields f
            LEFT JOIN auditcore.evidence e
              ON e.tenant_id = f.tenant_id AND e.di_document_id = f.di_document_id
            WHERE f.tenant_id = :t AND f.journey_id = :j
              AND f.source_document_type_key = ANY(:types)
              AND f.field_key = ANY(:keys)
              AND f.effective_value IS NOT NULL
              AND f.effective_value NOT IN ('null'::jsonb, '""'::jsonb)
              AND (e.evidence_id IS NULL OR e.association_status = 'ACTIVE')
            ORDER BY f.updated_at_utc, f.extracted_field_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": list(types), "keys": list(keys)},
    ).mappings().all()
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc = documents.setdefault(str(row["di_document_id"]), {
            "documentId": str(row["di_document_id"]), "documentType": str(row["source_document_type_key"]),
            "updatedAt": row["updated_at_utc"], "fields": {},
        })
        value = _plain(row["effective_value"])
        if value is not None:
            doc["fields"][str(row["field_key"])] = value  # the latest reading of a field wins
        doc["updatedAt"] = max(doc["updatedAt"], row["updated_at_utc"])
    return sorted(documents.values(), key=lambda d: d["updatedAt"], reverse=True)


def _reviewed_overlay(
    connection: Connection, *, tenant_id: str, journey_id: UUID, table: str, document_id: str, keys: tuple[str, ...],
) -> dict[str, Any]:
    """The PC-reviewed copy of the same document (booking_form_review_values /
    invoice_review_values), when there is one."""
    columns = connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'auditcore' AND table_name = :table"
        ),
        {"table": table},
    ).scalars().all()
    wanted = [k for k in keys if k in set(columns)]
    if not wanted:
        return {}
    row = connection.execute(
        text(
            f"SELECT {', '.join(wanted)} FROM auditcore.{table} "
            "WHERE tenant_id = :t AND journey_id = :j AND source_di_document_id = CAST(:d AS uuid)"
        ),
        {"t": tenant_id, "j": journey_id, "d": document_id},
    ).mappings().one_or_none()
    return {k: v for k, v in (dict(row) if row else {}).items() if _plain(v) is not None}


def _booking_doc_reading(fields: dict[str, Any], *, document_id: str | None, document_type: str,
                         updated_at: Any = None) -> dict[str, Any]:
    return {
        "source": "BOOKING", "documentId": document_id, "documentType": document_type,
        "model": clean_model_text(fields.get("vehicle_model")), "variant": _plain(fields.get("vehicle_variant")),
        "colour": _plain(fields.get("vehicle_color")), "skuCode": _plain(fields.get("sku_code")),
        "exShowroom": _money(fields.get("ex_showroom_price")),
        "registration": registration_category(fields.get("registration_type")),
        "updatedAt": updated_at,
    }


def _booking_readings(connection: Connection, *, tenant_id: str, journey_id: UUID,
                      state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every booking form that names the vehicle, each read on its own (its
    words and its quoted ex-showroom), newest first; a docket only when there
    is no form. Journeys captured by hand, with no booking form on file, fall
    back to the reviewed booking values and then to the model typed on the
    Journey -- or, once a SKU was pinned from that typed model, to the words
    the pin recorded (the snapshot then holds the price master's own names,
    and reading those back would confirm the pin by itself)."""
    documents = _document_fields(
        connection, tenant_id=tenant_id, journey_id=journey_id,
        types=_BOOKING_DOCUMENT_TYPES, keys=_BOOKING_FIELD_KEYS,
    )
    documents = [d for d in documents if d["fields"].get("vehicle_model") or d["fields"].get("vehicle_variant")
                 or d["fields"].get("sku_code")]
    if documents:
        forms = [d for d in documents if d["documentType"] == "booking_form"] or documents
        readings = []
        for doc in forms:
            fields = {**doc["fields"], **_reviewed_overlay(
                connection, tenant_id=tenant_id, journey_id=journey_id, table="booking_form_review_values",
                document_id=doc["documentId"], keys=_BOOKING_FIELD_KEYS,
            )}
            readings.append(_booking_doc_reading(fields, document_id=doc["documentId"],
                                                 document_type=doc["documentType"], updated_at=doc["updatedAt"]))
        return readings
    reviewed = connection.execute(
        text(
            """
            SELECT source_di_document_id, vehicle_model, vehicle_variant, vehicle_color, sku_code,
                   ex_showroom_price, registration_type
            FROM auditcore.booking_form_review_values
            WHERE tenant_id = :t AND journey_id = :j
            ORDER BY reviewed_at_utc DESC NULLS LAST
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    named = next((r for r in reviewed if _plain(r["vehicle_model"]) or _plain(r["sku_code"])), None)
    if named is not None:
        return [_booking_doc_reading(dict(named), document_id=str(named["source_di_document_id"]),
                                     document_type="booking_form")]
    state = state or _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    evidence = state.get("match_evidence") if isinstance(state.get("match_evidence"), dict) else {}
    if state.get("product_sku_id") is not None:
        if evidence.get("documentType") == "journey" and evidence.get("modelText"):
            return [{
                "source": "BOOKING", "documentId": None, "documentType": "journey",
                "model": clean_model_text(evidence.get("modelText")), "variant": _plain(evidence.get("variantText")),
                "colour": state.get("colour_name"), "skuCode": None,
                "exShowroom": _money(evidence.get("documentExShowroom")), "registration": None,
            }]
        return []
    if not state.get("model_name"):
        return []
    # The model typed on the Journey (manual capture). Its quoted price is the
    # reviewed booking form's, else the Journey's ex-showroom line when no
    # invoice wrote it.
    price = next((_money(r["ex_showroom_price"]) for r in reviewed if r["ex_showroom_price"] is not None), None)
    if price is None:
        line = connection.execute(
            text(
                """
                SELECT actual_amount, source_reference FROM auditcore.commercial_lines
                WHERE tenant_id = :t AND journey_id = :j AND component_key = 'ex_showroom_price'
                  AND actual_amount IS NOT NULL
                """
            ),
            {"t": tenant_id, "j": journey_id},
        ).mappings().one_or_none()
        source = str((line or {}).get("source_reference") or "").split(":", 1)[0].strip().lower()
        if line is not None and "invoice" not in source:
            price = _money(line["actual_amount"])
    return [{
        "source": "BOOKING", "documentId": None, "documentType": "journey",
        "model": clean_model_text(state["model_name"]), "variant": state.get("variant_name"),
        "colour": state.get("colour_name"), "skuCode": None, "exShowroom": price, "registration": None,
    }]


def _cancelled_invoice_ids(connection: Connection, *, tenant_id: str, journey_id: UUID) -> set[str]:
    """Vehicle invoices a credit note reverses in full (PR #470's
    ``select_invoices``): cancelled, so they name no vehicle any more. One
    still waiting for the PC's "does this credit note cancel it?" counts."""
    from audit_core.uc03_invoice_materialization import (
        load_invoice_readings,
        select_invoices,
    )

    selection = select_invoices(load_invoice_readings(connection, tenant_id=tenant_id, journey_id=journey_id))
    return {str(copy["documentId"]) for group in selection.get("cancelled") or []
            for copy in group.get("copies") or [] if copy.get("documentId")}


def _vehicle_invoice_readings(connection: Connection, *, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    """Each customer vehicle invoice on its own: what it names (model,
    variant, printed SKU code, colour) and its ex-showroom by rule R1, all
    from that one invoice. A credit note, an accessory / warranty / RSA
    invoice, the OEM's wholesale invoice, or a vehicle invoice a credit note
    cancelled is not a vehicle invoice."""
    readings: list[dict[str, Any]] = []
    documents = _document_fields(
        connection, tenant_id=tenant_id, journey_id=journey_id, types=_VEHICLE_INVOICE_TYPES,
        keys=_INVOICE_FIELD_KEYS,
    )
    cancelled = _cancelled_invoice_ids(connection, tenant_id=tenant_id, journey_id=journey_id) if documents else set()
    for doc in documents:
        if doc["documentId"] in cancelled:
            continue
        fields = {**doc["fields"], **_reviewed_overlay(
            connection, tenant_id=tenant_id, journey_id=journey_id, table="invoice_review_values",
            document_id=doc["documentId"], keys=_INVOICE_FIELD_KEYS,
        )}
        purpose = str(fields.get("invoice_purpose") or "").strip().upper()
        nature = str(fields.get("invoice_nature") or "").strip().upper()
        if purpose in _NON_VEHICLE_PURPOSES or nature in {"CREDIT_NOTE", "DEBIT_NOTE"}:
            continue
        if doc["documentType"] == "invoice_generic" and purpose not in ("", "VEHICLE_SALE"):
            continue
        model = clean_model_text(fields.get("model_name_raw") or fields.get("vehicle_description_raw"))
        sku_code = _plain(fields.get("sku_code"))
        if model is None and sku_code is None:
            continue
        ex_showroom, basis = invoice_ex_showroom(fields, doc["documentType"])
        readings.append({
            "source": "INVOICE", "documentId": doc["documentId"], "documentType": doc["documentType"],
            "invoiceNumber": _plain(fields.get("invoice_number")),
            "retail": doc["documentType"] in _RETAIL_INVOICE_TYPES,
            "model": model, "variant": _plain(fields.get("variant_raw")),
            "colour": _plain(fields.get("vehicle_color")), "skuCode": sku_code,
            "exShowroom": ex_showroom, "exShowroomBasis": basis, "gstRate": basis.get("gstRatePercent"),
            # The date the invoice prints. Used only when the Journey itself
            # has no usable pricing date (2026-10-23) -- see _evaluate.
            "invoiceDate": _invoice_date(fields.get("invoice_date")),
            "updatedAt": doc["updatedAt"],
        })
    return readings


def _invoice_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        from audit_core.uc03_p2_dates import parse_extracted_date

        return parse_extracted_date(value)


# ── rule R1: the invoice's ex-showroom ────────────────────────────────────────
# A rupee amount the way dealers write it: a plain number, Indian (8,52,000) or
# western (852,000) thousands separators, optionally behind "Rs." / "Rs" / "INR" /
# the rupee sign and followed by "/-". Nothing else is a price (no scope of error).
_MONEY_PREFIX = re.compile(r"^(?:rs\.?|inr|\u20b9)\s*", re.IGNORECASE)
_MONEY_SUFFIX = re.compile(r"\s*/-$")
_MONEY_NUMBER = re.compile(
    r"^-?(?:\d+|\d{1,2}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$"
)


def _money(value: Any) -> Decimal | None:
    """A price as a Decimal, or None when it is not read. Strict: text that is
    not exactly an amount ("852000.00 (incl. GST)", "8.52 lakh", "8,52,00",
    "Rs. 1.2.3") is NOT a price and gives None -- never a guess from the digits
    in it (the old version stripped every letter, so "Rs. 8,52,000" read as
    0.852)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        return number if number.is_finite() else None
    text_value = _MONEY_SUFFIX.sub("", _MONEY_PREFIX.sub("", str(value).strip(), count=1), count=1).strip()
    if not _MONEY_NUMBER.fullmatch(text_value):
        return None
    return Decimal(text_value.replace(",", ""))


def invoice_ex_showroom(fields: dict[str, Any], document_type: str = "customer_invoice_dms",
                        ) -> tuple[Decimal | None, dict[str, Any]]:
    """Rule R1 (2026-10-20) for one vehicle invoice: its printed selling
    price (before any discount) times (1 + its printed GST rate). One
    implementation for the Deal sheet and the SKU check: PR #470's
    ``uc03_invoice_materialization.vehicle_invoice_ex_showroom`` (review 471
    S5 -- discounts printed negative or only as lines, the vehicle line's own
    discount, add-on lines sharing the invoice). Returns (ex-showroom or None,
    how it was read: the GST rate in percent for the category gate, and the
    issues that left it unread)."""
    from audit_core.uc03_invoice_materialization import vehicle_invoice_ex_showroom

    reading = vehicle_invoice_ex_showroom(dict(fields), document_type)
    rate = reading.get("rate")
    basis: dict[str, Any] = {
        "method": "R1_VEHICLE_INVOICE_EX_SHOWROOM",
        "sellingPrice": reading.get("sellingPrice"),
        "sellingPriceSource": reading.get("sellingPriceSource"),
        "gstRatePercent": (Decimal(str(rate)) * 100).quantize(Decimal("0.01")) if rate is not None else None,
    }
    issues = [i.get("code") for i in reading.get("issues") or [] if isinstance(i, dict)]
    if issues:
        basis["issues"] = issues
    ex = reading.get("exShowroom")
    if ex is None:
        basis["reason"] = " ".join(str(i.get("message")) for i in reading.get("issues") or []
                                    if isinstance(i, dict) and i.get("message")) or "ex-showroom not read"
    return (Decimal(str(ex)) if ex is not None else None), basis


def _resolution_inputs(
    connection: Connection, *, tenant_id: str, journey_id: UUID, require_model: bool = True
) -> dict[str, Any] | None:
    """The Journey's current vehicle and registration (the model-selection
    correction flow reads it). None when there is no vehicle row, or no model
    on it and ``require_model``."""
    state = _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    has_row = connection.execute(
        text("SELECT 1 FROM auditcore.journey_products WHERE tenant_id = :t AND journey_id = :j"),
        {"t": tenant_id, "j": journey_id},
    ).first() is not None
    if require_model and (not has_row or state["model_name"] is None):
        return None
    registration = _journey_registration(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {
        "product_sku_id": state["product_sku_id"],
        "selection_status": state["selection_status"],
        "match_basis": state["match_basis"],
        "model_name": clean_model_text(state["model_name"]),
        "variant_name": state["variant_name"],
        "colour_name": state["colour_name"],
        "registration_basis": registration["basis"],
        "registration_category": registration["category"],
    }




def _oem_code_for_tenant(connection: Connection, *, tenant_id: str) -> str | None:
    return connection.execute(
        text(
            """
            SELECT o.oem_code
            FROM auditcore.projects p
            JOIN auditcore.oems o ON o.oem_id = p.oem_id
            WHERE p.tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()


def _oem_model_aliases(connection: Connection, *, oem_code: str) -> list[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT alias_text, canonical_model_name FROM auditcore.oem_model_aliases "
            "WHERE oem_code = :oem_code"
        ),
        {"oem_code": oem_code},
    ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


# ── matching ──────────────────────────────────────────────────────────────────
def _narrow(rows: list[dict[str, Any]], *, variant: str | None, colour: str | None) -> list[dict[str, Any]]:
    if len(rows) > 1 and variant:
        v = [r for r in rows if r["variant_name"] and _label_similarity(variant, str(r["variant_name"])) == Decimal(1)]
        if v:
            rows = v
    if len(rows) > 1 and colour:
        c = [r for r in rows if r["colour_name"] and _label_similarity(colour, str(r["colour_name"])) == Decimal(1)]
        if c:
            rows = c
    return rows


def _master_total(row: dict[str, Any], basis: str) -> Decimal | None:
    key = "master_total_corporate" if basis == "CORPORATE" else "master_total_individual"
    return _to_decimal(row.get(key))



def _model_identity() -> tuple[Any, dict[str, str]]:
    """The price masters' own model identity (2026-10-20, PR #468): one
    vehicle model whatever a month's list called it, the Bolero family split
    by series, "SCORPIO-N" the new Scorpio N; and the successor a spelling
    names once its model has left the book ("SCORPIO N" on an October
    date). The resolver uses the same identity the books are loaded with,
    so a renamed model is found under either spelling on either date."""
    try:
        from audit_core import oem_price_masters as masters
    except ImportError:  # pragma: no cover
        return None, {}
    identity = getattr(masters, "price_list_model_key", None)
    return identity, dict(getattr(masters, "_SUCCESSOR_IDENTITY", {}) or {})


# ── identification on one document ────────────────────────────────────────────
def _identify(
    rows: list[dict[str, Any]], reading: dict[str, Any], *, aliases: list[tuple[str, str]],
    registration: str | None,
) -> WordResult:
    """The word-identified SKU of one document (``uc03_sku_words.identify``).
    A SKU code the document prints that is exactly one row of the list names
    it too. A colour the same document prints settles rows that differ only
    by colour."""
    code = reading.get("skuCode")
    if code:
        by_code = [r for r in rows if str(r["sku_code"]).strip().casefold() == str(code).strip().casefold()]
        if len(by_code) == 1:
            return WordResult(status="IDENTIFIED", row=by_code[0], candidates=by_code,
                              detail={"skuCode": code, "via": "SKU_CODE"})
    identity, successors = _model_identity()
    result = identify(
        rows=rows, model_text=reading.get("model"), variant_text=reading.get("variant"), aliases=aliases,
        registration=registration or reading.get("registration"),
        gst_rate=_money(reading.get("gstRate")), identity=identity, successors=successors,
    )
    if result.status == "AMBIGUOUS" and reading.get("colour") and result.candidates:
        same_colour = _narrow(result.candidates, variant=None, colour=reading["colour"])
        if len(same_colour) == 1 and len(same_colour) < len(result.candidates):
            result.status, result.row, result.candidates = "IDENTIFIED", same_colour[0], same_colour
            result.detail["colour"] = reading["colour"]
    if result.status == "AMBIGUOUS":
        _suggest_nearest_model_by_price(result, reading)
    return result


def _suggest_nearest_model_by_price(result: WordResult, reading: dict[str, Any]) -> None:
    """When the words fit SKUs of SEVERAL MODELS, the document's own
    ex-showroom names which model it is (2026-10-23 product owner: price is
    only for when multiple models come in).

    The price narrows the candidates to that one model and the model is
    recorded as the suggestion; it never pins the SKU and never picks a
    variant, fuel, gearbox, registration or EV/ICE within a model -- that
    stays the rule stated at the top of this module. Still ambiguous after
    the narrowing means the task now lists one model's variants instead of
    several models', and the PC confirms.
    """
    offered = _to_decimal(reading.get("exShowroom"))
    if offered is None or not result.candidates:
        return
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in result.candidates:
        by_model.setdefault(str(row.get("model_name") or ""), []).append(row)
    if len(by_model) < 2:
        return  # one model already: the price has no model to choose between
    best: tuple[Any, str] | None = None
    for model_name, rows in by_model.items():
        prices = [_to_decimal(r.get("master_ex_showroom")) for r in rows]
        gaps = [abs(price - offered) for price in prices if price is not None]
        if not gaps:
            continue
        nearest = min(gaps)
        if best is None or nearest < best[0]:
            best = (nearest, model_name)
    if best is None:
        return
    # A tie between two models on the same gap settles nothing.
    gap, model_name = best
    ties = 0
    for rows in by_model.values():
        prices = [_to_decimal(r.get("master_ex_showroom")) for r in rows]
        gaps = [abs(price - offered) for price in prices if price is not None]
        if gaps and min(gaps) == gap:
            ties += 1
    if ties != 1:
        return
    result.candidates = by_model[model_name]
    result.detail["priceNearestModel"] = model_name
    result.detail["priceOffered"] = str(offered)
    result.detail["priceGap"] = str(gap)
    if len(result.candidates) == 1:
        # One model, one SKU: still a suggestion, not a pin -- the PC confirms
        # (product owner: "find out a nearest match model and then raise a
        # Task for PC to confirm").
        result.detail["priceNearestSku"] = result.candidates[0].get("sku_code")


def _evidence(
    reading: dict[str, Any], result: WordResult, *, check: str | None, plan: dict[str, Any], on: date,
) -> dict[str, Any]:
    """What the pin rests on: the document, its words, the prices compared."""
    row = result.row or {}
    master = _to_decimal(row.get("master_ex_showroom"))
    offered = reading.get("exShowroom")
    return {
        "source": reading.get("source"),
        "documentId": reading.get("documentId"),
        "documentType": reading.get("documentType"),
        "invoiceNumber": reading.get("invoiceNumber"),
        "modelText": reading.get("model"),
        "variantText": reading.get("variant"),
        "skuCodeText": reading.get("skuCode"),
        "words": result.detail.get("words"),
        "stated": result.detail.get("stated"),
        "category": result.detail.get("category") or result.category,
        "skuCode": row.get("sku_code"),
        "masterExShowroom": str(master) if master is not None else None,
        "documentExShowroom": str(offered) if offered is not None else None,
        "difference": str(offered - master) if offered is not None and master is not None else None,
        "priceCheck": check,
        "exShowroomBasis": {k: str(v) for k, v in (reading.get("exShowroomBasis") or {}).items()},
        "priceListVersionId": str(plan.get("price_list_version_id")) if plan.get("price_list_version_id") else None,
        "priceList": _plan_label(plan),
        "pricingDate": on.isoformat(),
        "decidedAtUtc": datetime.now(UTC).isoformat(),
    }


# ── persistence ───────────────────────────────────────────────────────────────
_PIN_SQL = """
    INSERT INTO auditcore.journey_products (
        tenant_id, journey_id, product_sku_id,
        model_code_snapshot, model_name_snapshot,
        variant_code_snapshot, variant_name_snapshot,
        colour_code_snapshot, colour_name_snapshot,
        selection_source, selection_status, selection_method, match_basis, match_evidence
    )
    SELECT
        :tenant_id, :journey_id, s.product_sku_id,
        pm.model_code, pm.model_name,
        pv.variant_code, pv.variant_name,
        c.colour_code, c.colour_name,
        'EVIDENCE', :status, :method, :basis, CAST(:evidence AS jsonb)
    FROM auditcore.product_skus s
    JOIN auditcore.product_models pm ON pm.model_id = s.model_id
    JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
    LEFT JOIN auditcore.colours c ON c.colour_id = s.colour_id
    WHERE s.product_sku_id = :product_sku_id
    ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
        product_sku_id = EXCLUDED.product_sku_id,
        model_code_snapshot = EXCLUDED.model_code_snapshot,
        model_name_snapshot = EXCLUDED.model_name_snapshot,
        variant_code_snapshot = EXCLUDED.variant_code_snapshot,
        variant_name_snapshot = EXCLUDED.variant_name_snapshot,
        -- A price-list SKU without a colour keeps the colour the
        -- documents gave (2026-10-20: pinning used to blank it).
        colour_code_snapshot = COALESCE(EXCLUDED.colour_code_snapshot,
                                        auditcore.journey_products.colour_code_snapshot),
        colour_name_snapshot = COALESCE(EXCLUDED.colour_name_snapshot,
                                        auditcore.journey_products.colour_name_snapshot),
        selection_source = 'EVIDENCE',
        selection_status = EXCLUDED.selection_status,
        selection_method = EXCLUDED.selection_method,
        match_basis = EXCLUDED.match_basis,
        match_evidence = EXCLUDED.match_evidence,
        updated_at_utc = now()
"""


def _note_sku_fact_change(connection: Connection, *, tenant_id: str, journey_id: UUID, reason: str) -> None:
    """A pinned SKU is a fact change, and until 2026-10-24 it was the one fact
    change that told nothing downstream.

    The SKU chooses the price-list row, so pinning it rewrites every standard
    amount on the deal and every discount the standard entitles. Writing
    ``journey_products`` alone left the Journey's ``fact_version`` where it was,
    so the Journey list's cached variance and the controls' own
    ``evaluated_fact_version`` both still believed themselves current:
    NIRUPAMA KHUNTIA read +2,71,181 on the list against -9,407 on the Deal tab,
    from the same field, because a resolution run had re-pinned her SKU
    overnight. Requesting the recompute here puts a pin on the same footing as
    every other fact change, and it fires only when a pin was actually written.

    Guarded on the Phase 2 runtime row already existing, because
    ``p2_request_stage_recompute`` would otherwise create one and a Phase 1
    journey would start reading as Phase 2 in the list.
    """
    phase2 = connection.execute(
        text(
            "SELECT 1 FROM auditcore.p2_journey_runtime "
            "WHERE tenant_id = :t AND journey_id = :j"
        ),
        {"t": tenant_id, "j": journey_id},
    ).first()
    if phase2 is None:
        return
    from audit_core.uc03_p2_runtime import note_facts_changed

    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id, reason=reason)


def _pin_sku(
    connection: Connection, *, tenant_id: str, journey_id: UUID, product_sku_id: UUID,
    method: str = _SELECTION_METHOD, basis: str = BASIS_AUTO_MATCHED, evidence: dict[str, Any] | None = None,
) -> bool:
    """An automatic pin (TENTATIVE, with how it was made and on what
    evidence) and the Journey's vehicle snapshot from the price master. Never
    touches a SKU a PC or Team Lead set. Returns whether it was written."""
    written = connection.execute(
        text(_PIN_SQL + " WHERE auditcore.journey_products.selection_status IS DISTINCT FROM 'CONFIRMED' "
             "RETURNING product_sku_id"),
        {
            "tenant_id": tenant_id, "journey_id": journey_id, "product_sku_id": product_sku_id,
            "status": "TENTATIVE", "method": method, "basis": basis,
            "evidence": json.dumps(evidence or {}, default=str),
        },
    ).first() is not None
    if written:
        _close_sku_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
        _note_sku_fact_change(connection, tenant_id=tenant_id, journey_id=journey_id,
                              reason="sku_pinned_automatically")
    return written


def _pin_human(
    connection: Connection, *, tenant_id: str, journey_id: UUID, product_sku_id: UUID, method: str, basis: str,
    evidence: dict[str, Any] | None = None, task_id: Any = None, actor_id: str | None = None,
) -> None:
    """A PC's or Team Lead's decision (CONFIRMED): replaces whatever is on
    the Journey."""
    connection.execute(
        text(_PIN_SQL),
        {
            "tenant_id": tenant_id, "journey_id": journey_id, "product_sku_id": product_sku_id,
            "status": "CONFIRMED", "method": method, "basis": basis,
            "evidence": json.dumps(evidence or {}, default=str),
        },
    )
    _close_sku_tasks(connection, tenant_id=tenant_id, journey_id=journey_id)
    _note_sku_fact_change(connection, tenant_id=tenant_id, journey_id=journey_id,
                          reason="sku_set_by_reviewer")
    from audit_core.uc03_sku_tasks import resolve_automatic_match_tasks

    resolve_automatic_match_tasks(connection, tenant_id=tenant_id, journey_id=journey_id,
                                  reason=f"SKU set by {'the Team Lead' if basis == BASIS_TL else 'the PC'}",
                                  skip_task_id=task_id, actor_id=actor_id)


def _update_match(
    connection: Connection, *, tenant_id: str, journey_id: UUID, basis: str, evidence: dict[str, Any],
) -> None:
    """Same automatic SKU, newer evidence (a price corrected on the form)."""
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_products
            SET match_basis = :basis, match_evidence = CAST(:evidence AS jsonb), updated_at_utc = now()
            WHERE tenant_id = :t AND journey_id = :j AND selection_status IS DISTINCT FROM 'CONFIRMED'
            """
        ),
        {"t": tenant_id, "j": journey_id, "basis": basis, "evidence": json.dumps(evidence, default=str)},
    )


def _close_sku_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    from audit_core.uc03_sku_tasks import resolve_sku_tasks

    sku_code = connection.execute(
        text(
            "SELECT s.sku_code FROM auditcore.journey_products jp "
            "JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id "
            "WHERE jp.tenant_id = :t AND jp.journey_id = :j"
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one_or_none()
    if sku_code is not None:
        resolve_sku_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, sku_code=str(sku_code))




def _resolve_open_flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    actor_id: str | None = None,
) -> int:
    """Resolve any open MODEL_NOT_IDENTIFIED finding for this journey.

    Never needs to touch the linked Task itself: ``sync_finding_work_item``'s
    trigger (migration 0098) already cancels every still-open Task for a
    finding the instant its ``finding_status`` flips to RESOLVED/VOIDED, as
    a codebase-wide guardrail covering every resolve path, not just this
    one -- adding a second, Python-side completion here would only race
    that trigger and lose (confirmed: the trigger fires synchronously as
    part of the very UPDATE below, before any later statement in this same
    transaction runs).

    ``actor_id`` -- set only by the PC-driven confirm path (the automatic
    match keeps this NULL/SYSTEM) -- records who actually resolved it in
    the finding's own event history, instead of every resolution reading
    as an anonymous system action.
    """
    open_ids = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND finding_type_code = :ft
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "ft": _FINDING_TYPE},
    ).scalars().all()
    for finding_id in open_ids:
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_findings
                SET finding_status = 'RESOLVED', disposition = 'FIXED',
                    resolved_at_utc = now(), updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND audit_finding_id = :fid
                  AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "fid": finding_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code,
                    event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
                ) VALUES (
                    :tenant_id, :fid, :journey_id, :stage,
                    'RESOLVED', :actor_id, :actor_role, CAST(:payload AS jsonb), :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "fid": finding_id,
                "journey_id": journey_id,
                "stage": _STAGE,
                "actor_id": actor_id,
                "actor_role": "HUMAN" if actor_id else "SYSTEM",
                "payload": json.dumps({"disposition": "FIXED", "note": "Model resolved to a single SKU."}),
                "correlation_id": correlation_id,
            },
        )
    return len(open_ids)


def _refresh_open_flag(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    finding_id: UUID,
    title: str,
    description: str,
    safe_payload: dict[str, Any],
    task_payload_extra: dict[str, Any],
    correlation_id: str,
) -> bool:
    """Decision 2026-10-01: an open MODEL_NOT_IDENTIFIED finding follows the
    latest run. ``_machine_flag`` leaves an existing open finding untouched,
    so a finding first raised from an invoice's text kept that wording and
    an empty shortlist for ever, however many Booking Form reads and
    re-syncs came after (Sonali Das on DEV: "did not match any SKU" while
    the resolver held eleven). When the wording changed, the finding's
    title and description are rewritten (the Phase 2 task refreshes from
    them), the new inputs and shortlist are recorded as a REFRESHED event,
    and the open self-serve task's candidates are replaced. Returns whether
    anything changed."""
    current = connection.execute(
        text(
            """
            SELECT title, description FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND audit_finding_id=:fid
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"tenant_id": tenant_id, "fid": finding_id},
    ).mappings().one_or_none()
    if current is None or (current["title"] == title and current["description"] == description):
        return False
    connection.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET title=:title, description=:description, updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND audit_finding_id=:fid
            """
        ),
        {"tenant_id": tenant_id, "fid": finding_id, "title": title, "description": description},
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
            ) VALUES (
                :tenant_id, :fid, :journey_id, :stage,
                'REFRESHED', NULL, 'SYSTEM', CAST(:payload AS jsonb), :correlation_id
            )
            """
        ),
        {
            "tenant_id": tenant_id, "fid": finding_id, "journey_id": journey_id, "stage": _STAGE,
            "payload": json.dumps({**safe_payload, "ruleKey": _RULE_KEY, "originKind": "MACHINE"}, default=str),
            "correlation_id": correlation_id,
        },
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.workflow_tasks
            SET task_payload = COALESCE(task_payload, '{}'::jsonb) || CAST(:extra AS jsonb), updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND related_finding_id=:fid
              AND task_status NOT IN ('COMPLETED', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
            """
        ),
        {"tenant_id": tenant_id, "fid": finding_id, "extra": json.dumps(task_payload_extra, default=str)},
    )
    logger.info(
        "model_resolution_finding_refreshed tenant=%s journey=%s finding=%s stage=%s candidates=%s",
        tenant_id, journey_id, finding_id, safe_payload.get("matchStage"), safe_payload.get("candidateCount"),
    )
    return True


def _run_deal_reconciliation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    raise_findings: bool = True,
) -> None:
    """Materialise price + discount standards for a resolved SKU (never raises)."""
    from audit_core.uc03_deal_reconciliation import sync_deal_reconciliation

    sync_deal_reconciliation(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        correlation_id=correlation_id,
        raise_findings=raise_findings,
    )




# ── catalogue search: the model name itself never resolved ──────────────────
# Handwriting read as a neighbouring nameplate ("XUV3XO AX7L EV" extracted as
# "XUV300" / "W7L EV" at 92% confidence, a real DEV journey) resolves to no
# model at all above: every fallback so far runs *inside* an already
# resolved model, and the exact/alias model match is deliberately never
# fuzzy. Rather than stop with an empty shortlist, score every SKU of the
# effective price list on the evidence the paperwork does carry: the stated
# fuel/transmission/drive/seater (a hard filter, an "EV" form never lands on
# a diesel), an exact price, the model name with the usual OCR confusions
# folded (O/0, I/1, S/5, B/8, Z/2) and the trim. Pins only a clear winner;
# otherwise the top few go to the PC as the shortlist to pick from.
_CATALOGUE_CONFUSION_FOLD = str.maketrans({"O": "0", "I": "1", "S": "5", "B": "8", "Z": "2", "Q": "0"})
_CATALOGUE_MODEL_FLOOR = 0.6  # below this the nameplate is simply not this one
_CATALOGUE_PIN_MODEL = 0.8
_CATALOGUE_PIN_TRIM = 0.7
_CATALOGUE_PIN_MARGIN = 0.15
_CATALOGUE_SHORTLIST = 5
_CATALOGUE_MODEL_WEIGHT = 0.5
_CATALOGUE_TRIM_WEIGHT = 0.3
_CATALOGUE_ATTR_WEIGHT = 0.2
_CATALOGUE_ATTR_KEYS = ("fuel", "transmission", "drive", "seater")


def _catalogue_key(text_value: str | None, *, oem_code: str | None) -> str:
    """Glued, upper-cased text with the OEM's fuel/transmission/drive/seater
    tokens removed and OCR look-alike characters folded, so "XUV 3XO EV" and
    a hand-written "XUV300" compare on the nameplate alone."""
    decomposed = _decompose(text_value, oem_code=oem_code) if oem_code else None
    key = decomposed.trim_key if decomposed is not None else normalized_model_key(text_value)
    return key.translate(_CATALOGUE_CONFUSION_FOLD)


def _catalogue_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()



def _catalogue_search(
    rows: list[dict[str, Any]], reading: dict[str, Any], *, oem_code: str | None, with_scores: bool = False,
) -> Any:
    """The document's model text names no model of the list (handwriting
    read as a neighbouring nameplate: "XUV3XO AX7L EV" read as "XUV300" /
    "W7L EV"). Score every SKU on what the paperwork does say -- the model
    name with OCR look-alikes folded (O/0, I/1, S/5, B/8, Z/2), the trim, and
    the stated fuel / gearbox / drive / seats (a hard filter: an "EV" form
    never lands on a diesel) -- and return the closest few for the PC.
    Never a pin (2026-10-20: the price no longer settles it either; it only
    breaks a tie in the order)."""
    model_text = str(reading.get("model") or "")
    variant_text = str(reading.get("variant") or "")
    combined = " ".join(part for part in (model_text, variant_text) if part)
    stated = _decompose(combined, oem_code=oem_code) if oem_code else None
    model_key = _catalogue_key(model_text, oem_code=oem_code)
    full_key = _catalogue_key(combined, oem_code=oem_code)
    if not model_key:
        return ([], []) if with_scores else []
    offered = reading.get("exShowroom")
    scored: list[tuple[float, Decimal, dict[str, Any]]] = []
    for row in rows:
        master = _master_attributes(row, oem_code=oem_code) if oem_code else None
        if stated is not None and master is not None and any(
            getattr(stated, key) and getattr(master, key) and getattr(stated, key) != getattr(master, key)
            for key in _CATALOGUE_ATTR_KEYS
        ):
            continue
        master_key = _catalogue_key(str(row["model_name"]), oem_code=oem_code)
        model_sim = max(
            _catalogue_similarity(master_key, model_key[: len(master_key)]),
            _catalogue_similarity(master_key, model_key),
        )
        if model_sim < _CATALOGUE_MODEL_FLOOR:
            continue
        remainder = full_key[len(master_key):] if len(full_key) > len(master_key) else ""
        master_trim = (master.trim_key if master is not None else "").translate(_CATALOGUE_CONFUSION_FOLD)
        trim_sim = _catalogue_similarity(remainder, master_trim)
        confirmed = (
            sum(
                1 for key in _CATALOGUE_ATTR_KEYS
                if getattr(stated, key) and getattr(master, key) and getattr(stated, key) == getattr(master, key)
            )
            if stated is not None and master is not None
            else 0
        )
        score = (
            _CATALOGUE_MODEL_WEIGHT * model_sim
            + _CATALOGUE_TRIM_WEIGHT * trim_sim
            + _CATALOGUE_ATTR_WEIGHT * (confirmed / len(_CATALOGUE_ATTR_KEYS))
        )
        master_ex = _to_decimal(row.get("master_ex_showroom"))
        gap = abs(master_ex - offered) if master_ex is not None and offered is not None else Decimal("1e12")
        scored.append((score, gap, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if with_scores:
        return [row for *_, row in scored[:_CATALOGUE_SHORTLIST]], [score for score, *_ in scored[:2]]
    return [row for *_, row in scored[:_CATALOGUE_SHORTLIST]]




def check_invoice_sku_mismatch(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    confirmed_sku_id: UUID,
    invoice_sku_id: UUID,
    invoice_sku_code: str,
    correlation_id: str,
    invoice_label: str | None = None,
) -> None:
    """The Delivery invoice's own implied vehicle disagrees with the SKU
    already on the journey (CONFIRMED or merely tentative) -- raise a finding
    and ask the PC for the reason, per explicit instruction, rather than
    silently pinning over it or silently ignoring the disagreement. Nothing
    here changes ``journey_products`` on its own; a Team Lead adjudicates the
    finding once PC has explained (the usual ACKNOWLEDGE / CONFIRM_BREACH /
    MARK_FALSE_POSITIVE verdicts). Self-heals: resolves once the invoice's
    implied SKU matches the confirmed one again.
    """
    if invoice_sku_id == confirmed_sku_id:
        finding_id = connection.execute(
            text(
                """
                SELECT audit_finding_id FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND rule_key=:rule_key AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "rule_key": _SKU_MISMATCH_RULE_KEY},
        ).scalar_one_or_none()
        if finding_id is not None:
            _resolve_finding(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code="DELIVERY",
                finding_id=finding_id,
                actor_id=None,
                correlation_id=correlation_id,
                note="The invoice's vehicle now matches the confirmed SKU.",
            )
        return

    confirmed_code = connection.execute(
        text("SELECT sku_code FROM auditcore.product_skus WHERE product_sku_id=:id"),
        {"id": confirmed_sku_id},
    ).scalar_one_or_none()

    finding_id = _machine_flag(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
        rule_key=_SKU_MISMATCH_RULE_KEY,
        finding_type=_SKU_MISMATCH_FINDING_TYPE,
        severity="HIGH",
        title="Invoice shows a different vehicle than the confirmed SKU",
        description=(
            f"The confirmed vehicle is {confirmed_code}, but the Delivery invoice implies "
            f"{invoice_label or invoice_sku_code}. A PC must explain the reason for the change."
        ),
        correlation_id=correlation_id,
        safe_payload={
            "confirmedSkuCode": confirmed_code,
            "invoiceSkuCode": invoice_sku_code,
            "invoiceVehicle": invoice_label,
        },
        blocking_completion=False,
    )
    open_task_count = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.workflow_tasks
            WHERE tenant_id=:tenant_id AND related_finding_id=:fid
              AND task_status NOT IN ('COMPLETED', 'CANCELLED', 'FAILED', 'DEAD_LETTER')
            """
        ),
        {"tenant_id": tenant_id, "fid": finding_id},
    ).scalar_one()
    if open_task_count == 0:
        create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type=_SKU_MISMATCH_WORKFLOW_TYPE,
            process_area="DELIVERY",
            task_type=_SKU_MISMATCH_TASK_TYPE,
            assigned_role_code="PC",
            related_finding_id=finding_id,
            severity="HIGH",
            task_payload={
                "ruleKey": _SKU_MISMATCH_RULE_KEY,
                "findingId": str(finding_id),
                "confirmedSkuCode": confirmed_code,
                "invoiceSkuCode": invoice_sku_code,
            },
            effect_key=f"task:{finding_id}:invoice-sku-mismatch",
            correlation_id=correlation_id,
        )



# ── a model in another month's price list ──────────────────────────────────────
_OTHER_LIST_VERSIONS = 12
_OTHER_LIST_HITS = 3


def _model_rows_by_name(rows: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    """Rows whose model name is the text, or the start of it ("XUV 3XO" in
    "XUV 3XO AX5 L 1.2 TGDi MT"); the longest such name wins."""
    key = normalized_model_key(model)
    if not key:
        return []
    best: list[dict[str, Any]] = []
    best_len = 0
    for row in rows:
        name = normalized_model_key(str(row["model_name"] or ""))
        if len(name) < 3 or not key.startswith(name):
            continue
        if len(name) > best_len:
            best, best_len = [row], len(name)
        elif len(name) == best_len:
            best.append(row)
    return best


def _other_price_list_hits(
    connection: Connection, *, tenant_id: str, inputs: dict[str, Any], current_version_id: Any,
) -> list[dict[str, Any]]:
    """Model names change between months ("XUV300" became "XUV 3XO"). When
    the Journey's own price list does not hold the model, say which other
    published price list does -- for the PC and TL to see; nothing is
    pinned from it, the Journey stays priced on its own month's list."""
    model = inputs.get("model_name")
    if not model:
        return []
    if current_version_id is not None:
        current_rows = _sku_rows_for_version(
            connection, tenant_id=tenant_id, price_list_version_id=current_version_id
        )
        if _model_rows_by_name(current_rows, model):
            # The Journey's own list has this model: it is the variant that
            # did not match, not the month (2026-10-20, journey 9d1c307c).
            return []
    versions = connection.execute(
        text(
            """
            SELECT plv.price_list_version_id, pl.price_list_name, pl.price_list_code,
                   plv.version_no, plv.effective_from, plv.effective_to
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
            WHERE plv.tenant_id = :tenant_id AND plv.lifecycle_status = 'PUBLISHED'
              AND plv.price_list_version_id IS DISTINCT FROM :current
            ORDER BY plv.effective_from DESC, plv.version_no DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "current": current_version_id, "limit": _OTHER_LIST_VERSIONS},
    ).mappings().all()
    hits: list[dict[str, Any]] = []
    for version in versions:
        rows = _sku_rows_for_version(
            connection, tenant_id=tenant_id, price_list_version_id=version["price_list_version_id"]
        )
        found = _narrow(_model_rows_by_name(rows, model), variant=inputs.get("variant_name"), colour=None)
        if not found:
            continue
        hits.append({
            "priceListVersionId": str(version["price_list_version_id"]),
            "priceList": _plan_label(dict(version)),
            "effectiveFrom": str(version["effective_from"]) if version["effective_from"] else None,
            "effectiveTo": str(version["effective_to"]) if version["effective_to"] else None,
            "modelName": str(found[0]["model_name"]),
            "skuCount": len(found),
            "skuCodes": [str(r["sku_code"]) for r in found[:5]],
        })
        if len(hits) >= _OTHER_LIST_HITS:
            break
    return hits




def _apply_invoice_colour(connection: Connection, *, tenant_id: str, journey_id: UUID, colour: str | None) -> None:
    """Rule 2026-10-20: the DMS / Tally invoice decides the vehicle's model,
    variant, transmission -- and colour. The price list carries no colours,
    so the colour lives on the Journey's vehicle; the invoiced one replaces
    a different booked one, and the Team Lead is told."""
    if not colour:
        return
    row = connection.execute(
        text(
            "SELECT colour_name_snapshot FROM auditcore.journey_products "
            "WHERE tenant_id = :t AND journey_id = :j"
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return
    booked = row["colour_name_snapshot"]
    if booked and normalized_model_key(booked) == normalized_model_key(colour):
        return
    before = _vehicle_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            "UPDATE auditcore.journey_products SET colour_name_snapshot = :c, updated_at_utc = now() "
            "WHERE tenant_id = :t AND journey_id = :j"
        ),
        {"c": colour[:200], "t": tenant_id, "j": journey_id},
    )
    if booked:
        from audit_core.uc03_sku_tasks import raise_model_changed_observation

        after = _vehicle_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
        after["skuId"] = after.get("skuId") or "colour"
        raise_model_changed_observation(connection, tenant_id=tenant_id, journey_id=journey_id,
                                        before=before, after={**after, "skuId": f"{after['skuId']}:{colour}"})


def _vehicle_snapshot(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT jp.product_sku_id, s.sku_code, jp.model_name_snapshot AS model,
                   jp.variant_name_snapshot AS variant, jp.colour_name_snapshot AS colour
            FROM auditcore.journey_products jp
            LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
            WHERE jp.tenant_id = :t AND jp.journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none() or {}
    return {
        "skuId": str(row.get("product_sku_id")) if row.get("product_sku_id") else None,
        "skuCode": row.get("sku_code"), "model": row.get("model"), "variant": row.get("variant"),
        "colour": row.get("colour"),
    }




def _format_candidate_line(row: dict[str, Any], basis: str) -> str:
    label = str(row["model_name"])
    if row.get("variant_name"):
        label += f" {row['variant_name']}"
    if row.get("colour_name"):
        label += f" ({row['colour_name']})"
    ex_showroom = _to_decimal(row.get("master_ex_showroom"))
    total = _master_total(row, basis)
    price_bits = []
    if ex_showroom is not None:
        price_bits.append(f"ex-showroom {ex_showroom:,.2f}")
    if total is not None:
        price_bits.append(f"on-road total {total:,.2f}")
    price_text = f" — {', '.join(price_bits)}" if price_bits else ""
    return f"{row['sku_code']} · {label}{price_text}"


def _candidate_task_payload(
    inputs: dict[str, Any], matched: list[dict[str, Any]], *, multiple: bool, stage: str = "",
    source: str = "BOOKING", other_lists: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The PC-facing shortlist for the auto-spawned Task: business-readable
    comment text naming each candidate model/variant/colour with its
    ex-showroom price, plus the same list structured for a UI picker."""
    basis = inputs["registration_basis"]
    shortlist = matched[:5]
    text_of = "vehicle invoice" if source == "INVOICE" else "booking"
    scan = "vehicle invoice" if source == "INVOICE" else "Booking Form"
    if stage == _CATALOGUE_STAGE:
        intro = (
            f"The {text_of} model text did not match any SKU name in the current price masters "
            "(the scan may have been misread). The closest SKUs on fuel, price, model and trim "
            f"are listed. Open the scanned {scan} on the Journey Documents page and "
            "select the one that matches it:"
        )
    elif multiple:
        intro = (
            f"The {text_of} model text matched {len(matched)} possible vehicle SKUs and "
            f"could not be narrowed to one automatically. Open the scanned {scan} "
            "on the Journey Documents page and select the SKU that matches it:"
        )
    else:
        intro = (
            f"The {text_of} model text did not match any SKU in the current price masters. "
            f"Open the scanned {scan} on the Journey Documents page, check the exact "
            "model/variant/colour printed on it, and select the matching SKU there."
        )
    if other_lists:
        intro = (
            "This model is not in the Journey's own price list but is in "
            + "; ".join(f"{h['priceList']} as '{h['modelName']}'" for h in other_lists)
            + ". " + intro
        )
    lines = [f"{i}. {_format_candidate_line(r, basis)}" for i, r in enumerate(shortlist, start=1)]
    comment = intro if not lines else intro + "\n" + "\n".join(lines)
    return {
        "comment": comment,
        "candidates": [
            {
                "productSkuId": str(r["product_sku_id"]),
                "skuCode": r["sku_code"],
                "modelName": r["model_name"],
                "variantName": r["variant_name"],
                "colourName": r["colour_name"],
                "exShowroomPrice": (
                    str(_to_decimal(r.get("master_ex_showroom")))
                    if _to_decimal(r.get("master_ex_showroom")) is not None
                    else None
                ),
                "totalPrice": str(_master_total(r, basis)) if _master_total(r, basis) is not None else None,
            }
            for r in shortlist
        ],
    }



# ── the ladder over a Journey's documents ─────────────────────────────────────
def _evaluate(
    connection: Connection, *, tenant_id: str, journey_id: UUID, state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read every document that names the vehicle and identify the SKU on
    each one by its own words. No side effects."""
    state = state or _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    bookings = _booking_readings(connection, tenant_id=tenant_id, journey_id=journey_id, state=state)
    invoices = _vehicle_invoice_readings(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not bookings and not invoices:
        # No vehicle model read off the Booking Form, no vehicle invoice
        # naming one.
        return {"skipped": True, "reason": "no_vehicle_model_read"}
    current = _current_rows(connection, tenant_id=tenant_id, journey_id=journey_id)
    if current.get("skipped") and current.get("reason") in _INVOICE_DATE_FALLBACK_REASONS:
        # 2026-10-23: a Journey with no booking date, or whose date has no
        # price list in force, was skipped outright -- the booking form and
        # the invoices had already been read on the lines above and were
        # thrown away, because word matching needs price-list rows to match
        # AGAINST. Seven live Journeys sat unresolved this way while their own
        # customer invoice named the vehicle (UTK-SA-261023-003 reads "Access"
        # on the booking and "THAR ROXX MX1 D MT RWD STL BLK" on the invoice).
        #
        # The invoice is the source of truth once uploaded, so its own printed
        # date picks the price list when the Journey has none to offer. The
        # Journey's pricing date is NOT changed by this: only the list this
        # resolution reads from, and the evidence records that it did so.
        fallback = _rows_on_invoice_date(connection, tenant_id=tenant_id, journey_id=journey_id, invoices=invoices)
        if fallback is not None:
            current = fallback
    if current.get("skipped"):
        return current
    rows = current["rows"]
    oem_code = _oem_code_for_tenant(connection, tenant_id=tenant_id)
    aliases = _oem_model_aliases(connection, oem_code=oem_code) if oem_code else []
    registration = _journey_registration(connection, tenant_id=tenant_id, journey_id=journey_id)
    booking_results = [
        (b, _identify(rows, b, aliases=aliases, registration=registration["category"])) for b in bookings
    ]
    invoice_results = [
        (inv, _identify(rows, inv, aliases=aliases, registration=registration["category"])) for inv in invoices
    ]
    # Several booking forms are compared like the invoices (review 471 S6):
    # the ones whose words name one SKU decide; naming different SKUs is a
    # PC task, never "the newest wins".
    named = [(b, r) for b, r in booking_results if r.identified]
    booking_disagree = len({str(r.row["product_sku_id"]) for _, r in named}) > 1
    booking, booking_result = (
        named[0] if named and not booking_disagree
        else booking_results[0] if booking_results else (None, None)
    )
    return {
        "plan": current["plan"], "rows": rows, "on": current["on"], "oemCode": oem_code, "aliases": aliases,
        "registration": registration, "state": state, "booking": booking, "bookingResult": booking_result,
        "bookings": booking_results, "bookingDisagree": named if booking_disagree else None,
        "invoices": invoice_results,
    }


def _shortlist(ev: dict[str, Any]) -> dict[str, Any] | None:
    """The document to report and the PC's shortlist when no document's
    words name one SKU: a vehicle invoice first (the source of truth), then
    the booking form. The shortlist is ordered by shared words, then by the
    nearest master price -- ordering only, nothing is picked from it; a
    one-click suggestion is offered only when the first row shares more of
    the document's words than the second (review 471 S8)."""
    options = [*ev["invoices"]]
    if ev["booking"] is not None:
        options.append((ev["booking"], ev["bookingResult"]))
    if not options:
        return None
    informative = [o for o in options if o[1].candidates or o[1].model_rows]
    reading, result = (informative or options)[0]
    stage = _STAGE_FOR_STATUS.get(result.status, result.status)
    if result.status == "NO_MODEL":
        matched, scores = _catalogue_search(ev["rows"], reading, oem_code=ev["oemCode"], with_scores=True)
        stage = _CATALOGUE_STAGE if matched else "NONE"
        # Only a clear best spelling is offered: a tie the price would break
        # shows the shortlist instead (review 471 S8).
        suggestion = matched[0] if matched and (len(scores) < 2 or scores[0] > scores[1]) else None
    else:
        pool = result.candidates if len(result.candidates) > 1 else result.model_rows
        matched = order_shortlist(pool, model_text=reading.get("model"), variant_text=reading.get("variant"),
                                  offered_ex_showroom=reading.get("exShowroom"))
        suggestion = (
            clear_suggestion(matched, model_text=reading.get("model"), variant_text=reading.get("variant"))
            if result.status in {"NO_MATCH", "UNRECOGNISED_WORD"} else None
        )
    return {"reading": reading, "result": result, "matched": matched, "stage": stage,
            "guess": suggestion is not None, "suggestion": suggestion, "source": reading["source"]}


def _document_item(reading: dict[str, Any], res: WordResult) -> dict[str, Any]:
    return {
        "documentId": reading.get("documentId"), "documentType": reading.get("documentType"),
        "invoiceNumber": reading.get("invoiceNumber"), "skuId": str(res.row["product_sku_id"]),
        "skuCode": res.row["sku_code"],
        "vehicle": f"{res.row['model_name']} {res.row.get('variant_name') or ''}".strip(),
    }


def _invoice_analysis(ev: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """What the vehicle invoices say (rule R3), without writing anything:

    * ``target`` -- one invoice's words name exactly one SKU and its R1
      ex-showroom equals that SKU's master price: the invoice sets the SKU;
    * ``disagree`` -- invoices naming different SKUs (none sets it);
    * ``priceTasks`` -- the invoices naming the one SKU at a different, or
      unread, price (each a PC task), and ``fallback`` the newest of them;
    * ``review`` -- with a PC / TL SKU on the Journey: invoices naming a
      different SKU at its master price (a Team Lead task). A human decision
      raises no price or disagreement task for itself (review 471 B4)."""
    named = [(inv, res) for inv, res in ev["invoices"] if res.identified]
    by_sku: dict[str, list[tuple[dict[str, Any], WordResult]]] = {}
    for inv, res in named:
        by_sku.setdefault(str(res.row["product_sku_id"]), []).append((inv, res))
    out: dict[str, Any] = {"named": named, "target": None, "disagree": None, "priceTasks": [], "fallback": None,
                           "review": []}

    def checks(items: list[tuple[dict[str, Any], WordResult]]) -> list[tuple[dict[str, Any], WordResult, str]]:
        master = _to_decimal(items[0][1].row.get("master_ex_showroom"))
        return [(inv, res, price_check(master, inv.get("exShowroom"))) for inv, res in items]

    if is_human_decision(state):
        for sku_id, items in by_sku.items():
            if sku_id == str(state["product_sku_id"]):
                continue
            agreeing = [(inv, res) for inv, res, check in checks(items) if check == "MATCH"]
            if agreeing:
                out["review"].append(agreeing[0])
        return out
    if len(by_sku) > 1:
        out["disagree"] = [_document_item(inv, res) for inv, res in named]
        return out
    if not by_sku:
        return out
    checked = checks(next(iter(by_sku.values())))
    agreeing = [(inv, res) for inv, res, check in checked if check == "MATCH"]
    if agreeing:
        out["target"] = {"reading": agreeing[0][0], "result": agreeing[0][1]}
        return out
    out["priceTasks"] = checked
    inv, res, check = checked[0]  # the newest invoice naming the SKU
    out["fallback"] = {"reading": inv, "result": res, "check": check}
    return out


def _legacy(state: dict[str, Any]) -> bool:
    """A pin made by the resolver before the 2026-10-20 rules (it may have
    been chosen by price): only the ``reresolve`` run re-checks it."""
    return (state.get("product_sku_id") is not None and not is_human_decision(state)
            and state.get("match_basis") in (None, BASIS_AUTO_LEGACY))


_SKIP_WHY = {
    "no_vehicle_model_read": "no booking form or vehicle invoice on file names the vehicle",
    "no_pricing_date": "the Journey has no pricing date (no booking date read or entered)",
    "no_effective_price_list": "no price list is in force on the Journey's pricing date",
}


def _why_not_named(ev: dict[str, Any]) -> str:
    """Why the documents do not name the pinned SKU, for the PC."""
    parts = []
    booking, res = ev.get("booking"), ev.get("bookingResult")
    if ev.get("bookingDisagree"):
        parts.append("the booking forms name different vehicles")
    elif booking is not None and res is not None:
        read = " / ".join(v for v in (booking.get("model"), booking.get("variant")) if v) or "-"
        reason = _REASON_TEXT.get(res.status, "does not name one SKU").format(
            n=len(res.candidates), model=res.detail.get("model") or booking.get("model"),
            unrecognised=", ".join(f"'{w}'" for w in res.detail.get("unrecognised") or []) or "a word")
        parts.append(f"the booking form reads '{read}': {reason}")
    else:
        parts.append("no booking form names the vehicle")
    named = [(inv, r) for inv, r in ev.get("invoices") or [] if r.identified]
    if named:
        parts.append("the vehicle invoice names " + ", ".join(sorted({r.row["sku_code"] for _, r in named})))
    return "; ".join(parts)


def _decide(ev: dict[str, Any], state: dict[str, Any], *, recheck_legacy: bool) -> dict[str, Any]:
    """The ladder's decision for one Journey, without writing anything (the
    dry run shows exactly this). Actions:

    INVOICE_SET -- a vehicle invoice's words and price name one SKU;
    KEEP_HUMAN -- a PC / TL SKU stays (Team Lead review tasks may be due);
    BOOKING_PIN -- the booking form's words name one SKU (price checked);
    INVOICE_WORDS_PIN -- no SKU yet, an invoice's words name one at a
      different price: pinned AUTO_PRICE_MISMATCH with its PC task;
    KEEP -- the automatic SKU stays as it is (an invoice still names it, or
      a pre-rule pin outside the reresolve run, or nothing to read);
    UNCONFIRMED -- an automatic SKU the documents do not name: it is KEPT
      (never cleared: review 471 B2), marked AUTO_UNCONFIRMED, and the PC
      gets "SKU not clearly identified from the documents";
    NOT_IDENTIFIED -- no SKU and none named: MODEL_NOT_IDENTIFIED;
    SKIP -- nothing to resolve."""
    has_sku = state.get("product_sku_id") is not None
    human = is_human_decision(state)
    legacy = _legacy(state)
    if ev.get("skipped"):
        if has_sku and not human and legacy and recheck_legacy:
            return {"action": "UNCONFIRMED", "why": _SKIP_WHY.get(ev.get("reason"), str(ev.get("reason"))),
                    "invoice": None, "skipped": ev.get("reason")}
        return {"action": "KEEP" if has_sku else "SKIP", "invoice": None, "skipped": ev.get("reason"),
                "keepReason": "nothing to read" if has_sku else None}
    inv = _invoice_analysis(ev, state)
    plan: dict[str, Any] = {"invoice": inv}
    if human:
        return {**plan, "action": "KEEP_HUMAN"}
    if inv["target"] is not None:
        return {**plan, "action": "INVOICE_SET", "target": inv["target"]}
    res = ev["bookingResult"]
    if res is not None and res.identified and not ev.get("bookingDisagree"):
        master = _to_decimal(res.row.get("master_ex_showroom"))
        check = price_check(master, ev["booking"].get("exShowroom"))
        return {**plan, "action": "BOOKING_PIN", "check": check,
                "basis": BASIS_AUTO_MATCHED if check == "MATCH" else BASIS_AUTO_PRICE_MISMATCH}
    if has_sku:
        fallback = inv.get("fallback")
        if fallback is not None and str(fallback["result"].row["product_sku_id"]) == str(state["product_sku_id"]):
            return {**plan, "action": "KEEP", "keepReason": "a vehicle invoice's words name it", "named": True}
        if legacy and not recheck_legacy:
            return {**plan, "action": "KEEP", "keepReason": "pinned before the 2026-10-20 rules (reresolve re-checks it)"}
        return {**plan, "action": "UNCONFIRMED", "why": _why_not_named(ev)}
    if inv.get("fallback"):
        return {**plan, "action": "INVOICE_WORDS_PIN", "fallback": inv["fallback"]}
    return {**plan, "action": "NOT_IDENTIFIED"}


def _proposal(ev: dict[str, Any], state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """The decision in words, for the dry run and the run items: old SKU ->
    proposed SKU, basis, the document's words and prices, the task due."""
    action = plan["action"]
    row: dict[str, Any] | None = None
    reading: dict[str, Any] | None = None
    basis = state.get("match_basis")
    check = None
    if action == "INVOICE_SET":
        reading, row, basis, check = plan["target"]["reading"], plan["target"]["result"].row, BASIS_AUTO_INVOICE, "MATCH"
    elif action == "BOOKING_PIN":
        reading, row, basis, check = ev["booking"], ev["bookingResult"].row, plan["basis"], plan["check"]
    elif action == "INVOICE_WORDS_PIN":
        fb = plan["fallback"]
        reading, row, basis, check = fb["reading"], fb["result"].row, BASIS_AUTO_PRICE_MISMATCH, fb["check"]
    elif action == "UNCONFIRMED":
        basis = BASIS_AUTO_UNCONFIRMED
    tasks: list[str] = []
    inv = plan.get("invoice") or {}
    if inv.get("disagree"):
        tasks.append("SKU_INVOICES_DISAGREE")
    if action == "KEEP_HUMAN":
        if _booking_forms_differing(ev, state):
            tasks.append("SKU_BOOKING_FORM_DIFFERS")
    elif ev.get("bookingDisagree"):
        tasks.append("SKU_BOOKING_FORMS_DISAGREE")
    if action == "SKIP":
        tasks.append({"no_effective_price_list": "PRICE_MASTER_NOT_IN_FORCE",
                      "no_pricing_date": "PC_BOOKING_DATE_MISSING or SKU_NOT_SET"}.get(
                          str(plan.get("skipped")), "SKU_NOT_SET"))
    tasks += ["SKU_PRICE_MISMATCH (invoice)"] * len(inv.get("priceTasks") or [])
    if inv.get("review"):
        tasks.append("SKU_INVOICE_REVIEW")
    if action == "BOOKING_PIN" and basis == BASIS_AUTO_PRICE_MISMATCH:
        tasks.append("SKU_PRICE_MISMATCH (booking)")
    if action == "UNCONFIRMED" and not _other_pc_question(ev, plan):
        tasks.append("SKU_NOT_CONFIRMED")
    if action == "NOT_IDENTIFIED":
        tasks.append("MODEL_NOT_IDENTIFIED")
    master = _to_decimal((row or {}).get("master_ex_showroom"))
    offered = (reading or {}).get("exShowroom")
    new_sku = row["sku_code"] if row is not None else state.get("sku_code") if action in {
        "KEEP", "KEEP_HUMAN", "UNCONFIRMED"} else None
    invoices = ev.get("invoices") or []
    proposal = {
        "action": action,
        "oldSku": state.get("sku_code"), "oldBasis": state.get("match_basis"),
        "proposedSku": new_sku, "proposedBasis": basis if new_sku else None,
        "changes": bool(row is not None and str(row["product_sku_id"]) != str(state.get("product_sku_id"))),
        "document": (reading or {}).get("documentType"), "documentId": (reading or {}).get("documentId"),
        "words": " / ".join(v for v in ((reading or {}).get("model"), (reading or {}).get("variant")) if v) or None,
        "masterExShowroom": str(master) if master is not None else None,
        "documentExShowroom": str(offered) if offered is not None else None,
        "priceCheck": check, "why": plan.get("why") or plan.get("keepReason"), "skipped": plan.get("skipped"),
        "tasks": tasks,
        # What each kind of document says on its own, so the product owner can
        # read the booking form against the price list and the invoice.
        "booking": _document_summary(ev.get("booking"), ev.get("bookingResult")),
        "invoice": _document_summary(*invoices[0]) if invoices else None,
        "invoiceCount": len(invoices),
        "selectionStatus": state.get("selection_status"),
    }
    proposal["group"] = proposal_group(proposal)
    return proposal


def _document_summary(reading: dict[str, Any] | None, result: Any) -> dict[str, Any] | None:
    """One document's words, ex-showroom and the SKU its words name (if one)."""
    if reading is None:
        return None
    row = getattr(result, "row", None) if result is not None and result.identified else None
    master = _to_decimal((row or {}).get("master_ex_showroom"))
    offered = reading.get("exShowroom")
    return {
        "documentType": reading.get("documentType"), "documentId": reading.get("documentId"),
        "words": " / ".join(v for v in (reading.get("model"), reading.get("variant")) if v) or None,
        "exShowroom": str(offered) if offered is not None else None,
        "sku": row["sku_code"] if row is not None else None,
        "masterExShowroom": str(master) if master is not None else None,
    }


def proposal_group(proposal: dict[str, Any]) -> str:
    """Where a Journey sits in the product owner's review (exclusive, in this order):

    LEFT_ALONE -- a SKU a PC or Team Lead set, a review that is complete, a
                  cancelled booking, or nothing to read yet and nothing to ask;
    CHANGE     -- the SKU will be set or replaced;
    CONFIRM    -- the SKU stays (or there is none) and a PC task asks for a decision;
    KEEP       -- the SKU stays and nothing is asked."""
    action = proposal.get("action")
    tasks = proposal.get("tasks") or []
    if action in {"LEFT_ALONE", "KEEP_HUMAN"} or (action == "SKIP" and not tasks):
        return "LEFT_ALONE"
    if proposal.get("changes"):
        return "CHANGE"
    return "CONFIRM" if tasks else "KEEP"


def _other_pc_question(ev: dict[str, Any], plan: dict[str, Any]) -> bool:
    """Another PC task already asks which vehicle it is (the documents
    disagree, or an invoice's price does not check): no second question."""
    inv = plan.get("invoice") or {}
    return bool(inv.get("disagree") or ev.get("bookingDisagree") or inv.get("priceTasks"))


# ── writing the decision ──────────────────────────────────────────────────────
def _document_tasks(
    connection: Connection, *, tenant_id: str, journey_id: UUID, ev: dict[str, Any], state: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Raise the PC / TL tasks the documents call for and close the ones
    they no longer call for."""
    from audit_core.uc03_sku_tasks import (
        raise_booking_form_differs_task,
        raise_booking_forms_disagree_task,
        raise_invoice_review_task,
        raise_invoices_disagree_task,
        raise_price_mismatch_task,
        resolve_booking_form_differs_task,
        resolve_booking_forms_disagree_task,
        resolve_invoice_review_task,
        resolve_invoices_disagree_task,
        resolve_price_mismatch_tasks,
    )

    inv = plan.get("invoice") or {}
    info: dict[str, Any] = {}
    # The SKU the Journey will carry after this run: a price task naming
    # another vehicle says so in its title (review 471 B3).
    resulting = (
        plan["target"]["result"].row["product_sku_id"] if plan["action"] == "INVOICE_SET"
        else ev["bookingResult"].row["product_sku_id"] if plan["action"] == "BOOKING_PIN"
        else state.get("product_sku_id")
    )
    if inv.get("disagree"):
        key = raise_invoices_disagree_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                                           invoices=inv["disagree"])
        resolve_invoices_disagree_task(connection, tenant_id=tenant_id, journey_id=journey_id, keep={key})
        info["invoicesDisagree"] = True
    else:
        resolve_invoices_disagree_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    keep: set[str] = set()
    for reading, res, check in inv.get("priceTasks") or []:
        keep.add(raise_price_mismatch_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, source="INVOICE", reading=reading, row=res.row,
            master=_to_decimal(res.row.get("master_ex_showroom")), offered=reading.get("exShowroom"),
            current_sku_id=resulting,
        ))
        info["invoicePriceDiffers"] = True
    if not ev.get("skipped"):
        resolve_price_mismatch_tasks(
            connection, tenant_id=tenant_id, journey_id=journey_id, source="INVOICE", keep=keep,
            reason=("A vehicle invoice's price agrees with the SKU" if inv.get("target")
                    else "No vehicle invoice names this SKU at this price any more"),
        )
    if inv.get("review"):
        review_keep = {
            raise_invoice_review_task(
                connection, tenant_id=tenant_id, journey_id=journey_id, current_sku_code=state["sku_code"],
                current_basis=state.get("match_basis"), invoice_row=res.row, invoice=reading,
                evidence=_evidence(reading, res, check="MATCH", plan=ev["plan"], on=ev["on"]),
            )
            for reading, res in inv["review"]
        }
        # A review of a SKU no invoice names any more closes (an invoice that
        # was corrected must not leave the old SKU for the Team Lead to accept).
        resolve_invoice_review_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, keep=review_keep,
            reason="No vehicle invoice names this SKU at its master price any more",
        )
        info["reviewRaised"] = True
    elif not ev.get("skipped"):
        resolve_invoice_review_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    if is_human_decision(state):
        # A PC / TL decision is never overwritten and never re-asked as a
        # disagreement between forms (review of #471): a booking form naming
        # another SKU is one PC task that says so and keeps the decision.
        resolve_booking_forms_disagree_task(connection, tenant_id=tenant_id, journey_id=journey_id)
        differs = _booking_forms_differing(ev, state)
        if differs:
            key = raise_booking_form_differs_task(
                connection, tenant_id=tenant_id, journey_id=journey_id, current_sku_code=state.get("sku_code"),
                current_basis=state.get("match_basis"), forms=[_document_item(b, r) for b, r in differs],
            )
            resolve_booking_form_differs_task(connection, tenant_id=tenant_id, journey_id=journey_id, keep={key})
            info["bookingFormDiffers"] = True
        elif not ev.get("skipped"):
            resolve_booking_form_differs_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    elif ev.get("bookingDisagree"):
        key = raise_booking_forms_disagree_task(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            forms=[_document_item(b, r) for b, r in ev["bookingDisagree"]],
        )
        resolve_booking_forms_disagree_task(connection, tenant_id=tenant_id, journey_id=journey_id, keep={key})
        info["bookingFormsDisagree"] = True
    elif not ev.get("skipped"):
        resolve_booking_forms_disagree_task(connection, tenant_id=tenant_id, journey_id=journey_id)
    return info


def _booking_forms_differing(ev: dict[str, Any], state: dict[str, Any]) -> list[tuple[dict[str, Any], WordResult]]:
    """Booking forms whose words name one SKU other than the PC / TL one."""
    return [(b, r) for b, r in ev.get("bookings") or []
            if r.identified and b.get("documentId") and str(r.row["product_sku_id"]) != str(state["product_sku_id"])]


_NOT_SET_WHY = {
    "no_vehicle_model_read": "no booking form or vehicle invoice on file names the vehicle",
    "no_pricing_date": "the Journey has no booking date to price it on -- enter the booking date",
}


# Pages still on their way into the Journey: a booking form or invoice among
# them may name the vehicle yet (unclassified pages count as possibly one).
_IN_FLIGHT_QUEUE = ("QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING",
                    "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT")


def _no_sku_gate(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str:
    """Whether a Journey with no SKU is due a "no SKU" task yet (review of
    #471: never the moment a PAN or a receipt is read, never while the
    booking form is still being read, never on a cancelled booking):

    CANCELLED -- the booking was cancelled, is a duplicate, or closed with no
      delivery: no SKU is due;
    PENDING -- a booking form / vehicle invoice page (or a page not yet
      classified) is still queued, being read or waiting to retry;
    DUE -- a booking form or vehicle invoice has been read and still names no
      vehicle, or the Journey is past the Booking stage without a SKU;
    NOT_YET -- neither (only other documents so far)."""
    row = connection.execute(
        text(
            """
            SELECT
              EXISTS (SELECT 1 FROM auditcore.journey_stage_states s
                      WHERE s.tenant_id = :t AND s.journey_id = :j AND s.stage_code = 'BOOKING'
                        AND (s.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
                             OR s.closure_disposition = 'NO_DELIVERY')) AS cancelled,
              EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                      WHERE q.tenant_id = :t AND q.journey_id = :j AND q.queue_status = ANY(:in_flight)
                        AND (q.classified_document_type IS NULL
                             OR q.classified_document_type = ANY(:types))) AS pending,
              EXISTS (SELECT 1 FROM auditcore.journey_document_extracted_fields f
                      LEFT JOIN auditcore.evidence e
                        ON e.tenant_id = f.tenant_id AND e.di_document_id = f.di_document_id
                      WHERE f.tenant_id = :t AND f.journey_id = :j AND f.source_document_type_key = ANY(:types)
                        AND (e.evidence_id IS NULL OR e.association_status = 'ACTIVE')) AS read,
              (EXISTS (SELECT 1 FROM auditcore.journey_stage_states s
                       WHERE s.tenant_id = :t AND s.journey_id = :j
                         AND ((s.stage_code = 'BOOKING' AND s.business_completed_at_utc IS NOT NULL)
                              OR (s.stage_code <> 'BOOKING' AND s.first_started_at_utc IS NOT NULL)))
               OR EXISTS (SELECT 1 FROM auditcore.journeys j
                          WHERE j.tenant_id = :t AND j.journey_id = :j AND j.delivery_id IS NOT NULL)) AS past_booking
            """
        ),
        {"t": tenant_id, "j": journey_id, "in_flight": list(_IN_FLIGHT_QUEUE),
         "types": [*_BOOKING_DOCUMENT_TYPES, *_VEHICLE_INVOICE_TYPES]},
    ).mappings().one()
    if row["cancelled"]:
        return "CANCELLED"
    if row["pending"]:
        return "PENDING"
    return "DUE" if row["read"] or row["past_booking"] else "NOT_YET"


def booking_cancelled(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """A cancelled or duplicate booking, or one closed with no delivery: the
    reresolve run leaves it alone (review of #471)."""
    return _no_sku_gate(connection, tenant_id=tenant_id, journey_id=journey_id) == "CANCELLED"


def _no_sku_task(connection: Connection, *, tenant_id: str, journey_id: UUID, ev: dict[str, Any]) -> str | None:
    """A Journey with no SKU that the ladder could not run on leaves a task
    once one is due (2026-10-21 product owner rule; ``_no_sku_gate``). No
    price list in force on the pricing date: the "no price master in force"
    task says so. No booking date: the booking form's "enter the booking
    date" task when there is a form, else SKU_NOT_SET. Nothing names the
    vehicle: SKU_NOT_SET. A SKU_NOT_SET that is no longer due is closed.
    Returns the task type left open, or None."""
    from audit_core.uc03_p2_task_producer import resolve_if_open
    from audit_core.uc03_sku_tasks import raise_not_set_task

    gate = _no_sku_gate(connection, tenant_id=tenant_id, journey_id=journey_id)
    reason = str(ev.get("reason") or "")
    if gate != "DUE":
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                        dedupe_key=f"sku-not-set:{journey_id}",
                        evidence={"reason": {"CANCELLED": "The booking is cancelled or a duplicate",
                                             "PENDING": "A booking form or invoice is still being read",
                                             "NOT_YET": "No booking form or vehicle invoice read yet"}[gate]})
        return None
    if reason == "no_effective_price_list":
        from audit_core.uc03_sku_candidates import sync_price_master_in_force_task

        sync_price_master_in_force_task(connection, tenant_id=tenant_id, journey_id=journey_id)
        return "PRICE_MASTER_NOT_IN_FORCE"
    if reason == "no_pricing_date":
        from audit_core.uc03_p2_task_producer import sync_booking_date_task

        sync_booking_date_task(connection, tenant_id=tenant_id, journey_id=journey_id)
        open_task = connection.execute(
            text("SELECT 1 FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j "
                 "AND task_type='PC_BOOKING_DATE_MISSING' AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED')"),
            {"t": tenant_id, "j": journey_id},
        ).first()
        if open_task is not None:
            return "PC_BOOKING_DATE_MISSING"
    raise_not_set_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                       why=_NOT_SET_WHY.get(reason, reason.replace("_", " ") or "it could not be identified"),
                       reason=reason or "unknown")
    return "SKU_NOT_SET"


def _note_change(
    connection: Connection, *, tenant_id: str, journey_id: UUID, state: dict[str, Any], new_sku: str | None,
    basis: str, why: str, correlation_id: str,
) -> None:
    """The Journey's activity log: the SKU changed (or lost its footing) and
    why -- what the Team Lead reads (review 471 S7)."""
    from audit_core.uc03_p2_runtime import record_activity

    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="SKU_RESOLUTION_CHANGED",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"from": state.get("sku_code"), "fromBasis": state.get("match_basis"), "to": new_sku,
                 "toBasis": basis, "why": why},
        correlation_id=correlation_id or None,
    )
    logger.info("model_resolution_automatic_pin_changed tenant=%s journey=%s from=%s to=%s basis=%s",
                tenant_id, journey_id, state.get("sku_code"), new_sku, basis)


def _invoice_colour_for_pin(connection: Connection, *, tenant_id: str, journey_id: UUID, ev: dict[str, Any]) -> None:
    """Rule 2026-10-20: the retail invoice decides the colour. The price
    list carries no colours, so the colour a DMS / Tally invoice prints for
    the very SKU on the Journey replaces a different booked one."""
    current = _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)["product_sku_id"]
    if current is None:
        return
    for inv, res in ev.get("invoices") or []:
        if inv.get("retail") and inv.get("colour") and res.identified \
                and str(res.row["product_sku_id"]) == str(current):
            _apply_invoice_colour(connection, tenant_id=tenant_id, journey_id=journey_id, colour=inv["colour"])
            return


def _keep_pin(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, raise_findings: bool,
    state: dict[str, Any], info: dict[str, Any] | None = None, checked: bool = False,
    ev: dict[str, Any] | None = None, named: bool = False, booking_not_naming: bool = False,
) -> dict[str, Any]:
    from audit_core.uc03_sku_tasks import (
        resolve_not_confirmed_task,
        resolve_price_mismatch_tasks,
    )

    if ev is not None and not ev.get("skipped"):
        _invoice_colour_for_pin(connection, tenant_id=tenant_id, journey_id=journey_id, ev=ev)
    if booking_not_naming:
        # KEEP: the booking form no longer names this SKU (it fits several, or
        # none), so its "master X vs booking Y" task has nothing left to ask.
        resolve_price_mismatch_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, source="BOOKING",
                                     keep=set(), reason="The booking form no longer names this SKU")
    if named:
        resolve_not_confirmed_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                                   reason="A document names the SKU again")
    resolved = _resolve_open_flag(connection, tenant_id=tenant_id, journey_id=journey_id,
                                  correlation_id=correlation_id)
    _run_deal_reconciliation(connection, tenant_id=tenant_id, journey_id=journey_id,
                             correlation_id=correlation_id, raise_findings=raise_findings)
    return {"resolved": True, "flagsResolved": resolved, "skuCode": state.get("sku_code"),
            "basis": state.get("match_basis"), "crossChecked": checked,
            "mismatchFlagged": bool((info or {}).get("invoicesDisagree") or (info or {}).get("invoicePriceDiffers")
                                    or (info or {}).get("reviewRaised") or (info or {}).get("bookingFormsDisagree"))}


def _apply_invoice(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, raise_findings: bool,
    ev: dict[str, Any], target: dict[str, Any],
) -> dict[str, Any]:
    """The invoice sets the SKU: it replaces an automatic pin (the Team Lead
    is told when the vehicle changed). A PC / TL SKU never gets here."""
    from audit_core.uc03_sku_tasks import (
        raise_model_changed_observation,
        resolve_not_confirmed_task,
        resolve_price_mismatch_tasks,
    )

    state = ev["state"]
    inv, res = target["reading"], target["result"]
    row = res.row
    evidence = _evidence(inv, res, check="MATCH", plan=ev["plan"], on=ev["on"])
    before = _vehicle_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
    previous = state["product_sku_id"]
    changed = previous is not None and str(previous) != str(row["product_sku_id"])
    if str(previous) != str(row["product_sku_id"]) or state.get("match_basis") != BASIS_AUTO_INVOICE:
        _pin_sku(connection, tenant_id=tenant_id, journey_id=journey_id, product_sku_id=row["product_sku_id"],
                 method=_INVOICE_SELECTION_METHOD, basis=BASIS_AUTO_INVOICE, evidence=evidence)
    else:
        _update_match(connection, tenant_id=tenant_id, journey_id=journey_id, basis=BASIS_AUTO_INVOICE,
                      evidence=evidence)
    if inv.get("retail"):
        _apply_invoice_colour(connection, tenant_id=tenant_id, journey_id=journey_id, colour=inv.get("colour"))
    resolve_price_mismatch_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, source="BOOKING",
                                 keep=set(), reason="The vehicle invoice set the SKU (its words and price agree)")
    resolve_not_confirmed_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                               reason="The vehicle invoice set the SKU (its words and price agree)")
    _close_legacy_mismatch(connection, tenant_id=tenant_id, journey_id=journey_id, sku_id=row["product_sku_id"],
                           correlation_id=correlation_id)
    _resolve_open_flag(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id)
    _run_deal_reconciliation(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                             raise_findings=raise_findings)
    if changed:
        after = _vehicle_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)
        raise_model_changed_observation(connection, tenant_id=tenant_id, journey_id=journey_id,
                                        before=before, after=after)
        _note_change(connection, tenant_id=tenant_id, journey_id=journey_id, state=state, new_sku=row["sku_code"],
                     basis=BASIS_AUTO_INVOICE, why="A vehicle invoice's words and price name it",
                     correlation_id=correlation_id)
    return {"resolved": True, "changed": changed, "source": "INVOICE", "skuCode": row["sku_code"],
            "matchStage": "INVOICE_WORDS_AND_PRICE", "basis": BASIS_AUTO_INVOICE, "crossChecked": True,
            "mismatchFlagged": False}


def _apply_booking(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, raise_findings: bool,
    ev: dict[str, Any], info: dict[str, Any], check: str, basis: str,
) -> dict[str, Any]:
    """The booking form's words name one SKU: pin it, and compare its master
    ex-showroom with the form's (rule R3). A different or missing booking
    price still pins it -- so the deal shows against the SKU the words name
    -- but as AUTO_PRICE_MISMATCH, with a PC task to set the SKU."""
    from audit_core.uc03_sku_tasks import (
        raise_price_mismatch_task,
        resolve_not_confirmed_task,
        resolve_price_mismatch_tasks,
    )

    state, booking, res = ev["state"], ev["booking"], ev["bookingResult"]
    row = res.row
    master = _to_decimal(row.get("master_ex_showroom"))
    evidence = _evidence(booking, res, check=check, plan=ev["plan"], on=ev["on"])
    previous = state["product_sku_id"]
    changed = previous is not None and str(previous) != str(row["product_sku_id"])
    if str(previous) != str(row["product_sku_id"]) or state.get("match_basis") != basis:
        _pin_sku(connection, tenant_id=tenant_id, journey_id=journey_id, product_sku_id=row["product_sku_id"],
                 method=_SELECTION_METHOD, basis=basis, evidence=evidence)
    else:
        _update_match(connection, tenant_id=tenant_id, journey_id=journey_id, basis=basis, evidence=evidence)
    if basis == BASIS_AUTO_PRICE_MISMATCH:
        key = raise_price_mismatch_task(connection, tenant_id=tenant_id, journey_id=journey_id, source="BOOKING",
                                        reading=booking, row=row, master=master, offered=booking.get("exShowroom"))
        resolve_price_mismatch_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, source="BOOKING",
                                     keep={key}, reason="Superseded")
    else:
        resolve_price_mismatch_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, source="BOOKING",
                                     keep=set(), reason="The booking price agrees with the SKU's master price")
    resolve_not_confirmed_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                               reason="The booking form's words name the SKU")
    if changed:
        _note_change(connection, tenant_id=tenant_id, journey_id=journey_id, state=state, new_sku=row["sku_code"],
                     basis=basis, why="The booking form's words name it", correlation_id=correlation_id)
    _invoice_colour_for_pin(connection, tenant_id=tenant_id, journey_id=journey_id, ev=ev)
    _resolve_open_flag(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id)
    _run_deal_reconciliation(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                             raise_findings=raise_findings)
    return {"resolved": True, "changed": changed, "source": "BOOKING", "skuCode": row["sku_code"],
            "matchStage": "WORDS", "basis": basis, "priceCheck": check, "crossChecked": bool(ev["invoices"]),
            "mismatchFlagged": bool(info.get("invoicesDisagree") or info.get("invoicePriceDiffers"))}


def _apply_invoice_words_only(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, raise_findings: bool,
    ev: dict[str, Any], fallback: dict[str, Any], info: dict[str, Any],
) -> dict[str, Any]:
    """No SKU on the Journey and the booking form names none, but a vehicle
    invoice's words name exactly one SKU whose price does not agree (or could
    not be read). Like the booking form's case, the SKU the words name is put
    on the Journey as AUTO_PRICE_MISMATCH -- so the deal shows against it --
    and the PC task to set the SKU (raised with the invoice's tasks) stays
    open. Price chose nothing."""
    inv, res, check = fallback["reading"], fallback["result"], fallback["check"]
    row = res.row
    evidence = _evidence(inv, res, check=check, plan=ev["plan"], on=ev["on"])
    _pin_sku(connection, tenant_id=tenant_id, journey_id=journey_id, product_sku_id=row["product_sku_id"],
             method=_INVOICE_SELECTION_METHOD, basis=BASIS_AUTO_PRICE_MISMATCH, evidence=evidence)
    _resolve_open_flag(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id)
    _run_deal_reconciliation(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                             raise_findings=raise_findings)
    return {"resolved": True, "changed": False, "source": "INVOICE", "skuCode": row["sku_code"],
            "matchStage": "INVOICE_WORDS", "basis": BASIS_AUTO_PRICE_MISMATCH, "priceCheck": check,
            "crossChecked": True, "mismatchFlagged": True}


def _apply_unconfirmed(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, raise_findings: bool,
    ev: dict[str, Any], state: dict[str, Any], plan: dict[str, Any], info: dict[str, Any],
) -> dict[str, Any]:
    """The automatic SKU stays on the Journey (the deal keeps its prices) but
    its documents do not name it: marked AUTO_UNCONFIRMED with why, and the
    PC asked to confirm or set it -- unless another PC task already asks
    which vehicle it is."""
    from audit_core.uc03_sku_tasks import (
        raise_not_confirmed_task,
        resolve_price_mismatch_tasks,
    )

    why = plan["why"]
    previous_evidence = state.get("match_evidence") if isinstance(state.get("match_evidence"), dict) else {}
    if state.get("match_basis") != BASIS_AUTO_UNCONFIRMED or previous_evidence.get("why") != why:
        _update_match(connection, tenant_id=tenant_id, journey_id=journey_id, basis=BASIS_AUTO_UNCONFIRMED,
                      evidence={
                          "source": "RECHECK", "why": why, "skuCode": state.get("sku_code"),
                          "previousBasis": (previous_evidence.get("previousBasis")
                                            if state.get("match_basis") == BASIS_AUTO_UNCONFIRMED
                                            else state.get("match_basis")),
                          "previousEvidence": (previous_evidence.get("previousEvidence")
                                               if state.get("match_basis") == BASIS_AUTO_UNCONFIRMED
                                               else previous_evidence),
                          "decidedAtUtc": datetime.now(UTC).isoformat(),
                      })
        if state.get("match_basis") != BASIS_AUTO_UNCONFIRMED:
            _note_change(connection, tenant_id=tenant_id, journey_id=journey_id, state=state,
                         new_sku=state.get("sku_code"), basis=BASIS_AUTO_UNCONFIRMED,
                         why=f"Kept, but not confirmed by the documents: {why}", correlation_id=correlation_id)
    if not ev.get("skipped"):
        resolve_price_mismatch_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, source="BOOKING",
                                     keep=set(), reason="The booking form no longer names this SKU")
    if not _other_pc_question(ev, plan):
        vehicle = " ".join(v for v in (state.get("model_name"), state.get("variant_name")) if v) or None
        raise_not_confirmed_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                                 sku_id=state["product_sku_id"], sku_code=state.get("sku_code"), vehicle=vehicle,
                                 why=why)
    _run_deal_reconciliation(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                             raise_findings=raise_findings)
    return {"resolved": True, "changed": False, "unconfirmed": True, "skuCode": state.get("sku_code"),
            "basis": BASIS_AUTO_UNCONFIRMED, "reason": plan.get("skipped"), "why": why,
            "mismatchFlagged": bool(info)}


_REASON_TEXT = {
    "AMBIGUOUS": "its words fit {n} price-master SKUs",
    "REGISTRATION_UNKNOWN": ("its words fit both the Private and the Commercial SKU and the registration "
                             "type is not known"),
    "CATEGORY_UNCERTAIN": ("it does not say whether the vehicle is electric or petrol / diesel / CNG, and the "
                           "price list has this model as both"),
    "CATEGORY_CONFLICT": "it says both electric and petrol / diesel / CNG",
    "UNRECOGNISED_WORD": "it prints {unrecognised}, which no SKU of {model} carries",
    "NO_MATCH": "no SKU of {model} has these words",
    "NO_MODEL": "did not match any price-master SKU",
    "NO_TEXT": "names no vehicle",
}


def _raise_not_identified(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, ev: dict[str, Any],
    info: dict[str, Any],
) -> dict[str, Any]:
    """No document's words name exactly one SKU: MODEL_NOT_IDENTIFIED for the
    PC with what each document read and the shortlist. Nothing is pinned."""
    from audit_core.uc03_p2_task_producer import resolve_if_open
    from audit_core.uc03_sku_tasks import (
        raise_closest_match_task,
        raise_other_month_task,
        resolve_price_mismatch_tasks,
    )

    picked = _shortlist(ev)
    if picked is None:
        return {"skipped": True, "reason": "no_vehicle_model_read"}
    resolve_price_mismatch_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, source="BOOKING",
                                 keep=set(), reason="No SKU is named by the booking form's words")
    # MODEL_NOT_IDENTIFIED (with its shortlist) is the PC's question now.
    resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=f"sku-not-set:{journey_id}",
                    evidence={"reason": "The documents name the vehicle; MODEL_NOT_IDENTIFIED asks which SKU"})
    reading, result, matched, stage = picked["reading"], picked["result"], picked["matched"], picked["stage"]
    source = picked["source"]
    use_invoice = source == "INVOICE"
    document = "Vehicle invoice" if use_invoice else "Booking"
    report = _report_inputs(reading, ev)
    multiple = result.status in {"AMBIGUOUS", "REGISTRATION_UNKNOWN"} and len(matched) > 1
    not_in_list = result.status == "NO_MODEL"
    other_lists = (
        _other_price_list_hits(connection, tenant_id=tenant_id, inputs=report,
                               current_version_id=ev["plan"]["price_list_version_id"])
        if not_in_list else []
    )
    title = (
        "Vehicle model matched multiple price-master SKUs" if multiple
        else "Vehicle category (electric or not) is not certain"
        if result.status in {"CATEGORY_UNCERTAIN", "CATEGORY_CONFLICT"}
        else "Vehicle model is in another month's price list, not this Journey's" if other_lists
        else "Vehicle model could not be matched to the price masters"
    )
    reason = _REASON_TEXT.get(result.status, "did not name one SKU").format(
        n=len(result.candidates), model=result.detail.get("model") or report["model_name"],
        unrecognised=", ".join(f"'{w}'" for w in result.detail.get("unrecognised") or []) or "a word",
    )
    booking = ev["booking"]
    price_list = _plan_label(ev["plan"])
    description = (
        f"{document} model '{report['model_name']}'"
        + (f" / '{report['variant_name']}'" if report["variant_name"] else "")
        + (f": {reason}" if result.status != "NO_MODEL" else f" {reason}")
        + f" in {price_list}. "
        + (
            f"Booking model '{booking['model']}' did not settle it either. " if use_invoice and booking is not None
            else "No model was read off the Booking Form. " if use_invoice
            else ""
        )
        + (
            "The model is in "
            + "; ".join(f"{h['priceList']} as '{h['modelName']}' ({h['skuCount']} SKUs)" for h in other_lists)
            + " -- check the Journey's pricing date or the model name in this month's price list. "
            if other_lists else ""
        )
        + (f"The {len(matched)} closest SKUs are listed on the task. " if stage == _CATALOGUE_STAGE else "")
        + (
            f"The ex-showroom read ({result.detail['priceOffered']}) is nearest "
            f"{result.detail['priceNearestModel']}"
            + (f" ({result.detail['priceNearestSku']})" if result.detail.get("priceNearestSku") else "")
            + ", so the task lists that model; confirm it so the deal can be checked against the price "
              "and discount masters."
            if result.detail.get("priceNearestModel")
            else "A price never picks the SKU: confirm the model so the deal can be checked against the "
                 "price and discount masters."
        )
    )
    basis = ev["registration"]["basis"]
    safe_payload = {
        "source": source,
        "documentId": reading.get("documentId"),
        "modelName": report["model_name"],
        "variantName": report["variant_name"],
        "colourName": report["colour_name"],
        "bookingModelName": booking["model"] if booking else None,
        "invoiceModelName": next((inv["model"] for inv, _ in ev["invoices"]), None),
        "invoiceSkuCode": next((inv["skuCode"] for inv, _ in ev["invoices"] if inv.get("skuCode")), None),
        "priceList": price_list,
        "otherPriceLists": other_lists,
        "offeredExShowroom": str(reading["exShowroom"]) if reading.get("exShowroom") is not None else None,
        "matchStage": stage,
        "wordStatus": result.status,
        "words": result.detail.get("words"),
        "candidateCount": len(matched),
        "candidates": [
            {
                "skuCode": r["sku_code"], "modelName": r["model_name"], "variantName": r["variant_name"],
                "colourName": r["colour_name"],
                "masterTotal": str(_master_total(r, basis)) if _master_total(r, basis) is not None else None,
                "exShowroom": (str(_to_decimal(r.get("master_ex_showroom")))
                               if _to_decimal(r.get("master_ex_showroom")) is not None else None),
            }
            for r in matched[:10]
        ],
    }
    task_payload_extra = _candidate_task_payload(
        report, matched, multiple=multiple, stage=stage, source=source, other_lists=other_lists,
    )
    finding_id = _machine_flag(
        connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=_STAGE, rule_key=_RULE_KEY,
        finding_type=_FINDING_TYPE, severity="MEDIUM", title=title, description=description,
        correlation_id=correlation_id, safe_payload=safe_payload, task_payload_extra=task_payload_extra,
    )
    _refresh_open_flag(
        connection, tenant_id=tenant_id, journey_id=journey_id, finding_id=finding_id, title=title,
        description=description, safe_payload=safe_payload, task_payload_extra=task_payload_extra,
        correlation_id=correlation_id,
    )
    # A closest-SKU suggestion that no longer stands (the words now fit several
    # SKUs, or the model is in another month's list) is closed: it must not
    # stay for the PC to accept and pin with no document behind it.
    if other_lists or not picked["guess"]:
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                        dedupe_key=f"sku-closest:{journey_id}",
                        evidence={"reason": "The documents no longer suggest a closest SKU"})
    # Model in another month's list: a Team Lead approves pricing on it first.
    # A closest guess goes to the PC to accept or reject -- never pinned.
    if not other_lists:
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                        dedupe_key=f"sku-other-month:{journey_id}",
                        evidence={"reason": "The Journey's own price list has the model"})
        if picked["guess"]:
            raise_closest_match_task(
                connection, tenant_id=tenant_id, journey_id=journey_id, source=source,
                read_model=report["model_name"], read_variant=report["variant_name"], suggestion=picked["suggestion"],
                price_list=price_list,
                reason=None if not_in_list else reason,
            )
    else:
        raise_other_month_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, source=source,
            read_model=report["model_name"], price_list=price_list, hits=other_lists,
        )
    return {
        "raised": True, "matchStage": stage, "wordStatus": result.status, "candidateCount": len(matched),
        "source": source, "otherPriceLists": len(other_lists), "crossChecked": bool(ev["invoices"]),
        "mismatchFlagged": bool(info.get("invoicesDisagree") or info.get("invoicePriceDiffers")),
    }


def _report_inputs(reading: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    """A document's reading in the shape the shortlist payload expects."""
    return {
        "model_name": reading.get("model"), "variant_name": reading.get("variant"),
        "colour_name": reading.get("colour"), "registration_basis": ev["registration"]["basis"],
        "offered_ex_showroom": reading.get("exShowroom"), "offered_total": None,
    }


def _close_legacy_mismatch(
    connection: Connection, *, tenant_id: str, journey_id: UUID, sku_id: Any, correlation_id: str,
) -> None:
    """An INVOICE_SKU_MISMATCH finding raised before 2026-10-20 closes once
    the invoice and the Journey's SKU agree."""
    check_invoice_sku_mismatch(
        connection, tenant_id=tenant_id, journey_id=journey_id, confirmed_sku_id=sku_id, invoice_sku_id=sku_id,
        invoice_sku_code="", correlation_id=correlation_id,
    )


def get_model_resolution_candidates(
    connection: Connection, *, tenant_id: str, journey_id: UUID
) -> dict[str, Any]:
    """Read-only: today's shortlist for an open MODEL_NOT_IDENTIFIED gap,
    computed fresh from the documents against the Journey's price list
    (never a stale snapshot from whenever the finding was raised)."""
    finding_id = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
              AND finding_type_code = :ft
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            ORDER BY created_at_utc DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "ft": _FINDING_TYPE},
    ).scalar_one_or_none()
    if finding_id is None:
        raise NotFoundError(
            error_code="VAC-NF-010",
            title="No open model-resolution gap",
            detail="There is no open vehicle-model gap to resolve for this Journey.",
        )
    ev = _evaluate(connection, tenant_id=tenant_id, journey_id=journey_id)
    picked = None if ev.get("skipped") else _shortlist(ev)
    if picked is None:
        raise NotFoundError(
            error_code="VAC-NF-010",
            title="No vehicle model read",
            detail="Neither the Booking Form nor a vehicle invoice names the model yet -- nothing to shortlist.",
        )
    reading, result = picked["reading"], picked["result"]
    matched = list(result.candidates) if result.identified else picked["matched"]
    basis = ev["registration"]["basis"]
    return {
        "journeyId": journey_id,
        "findingId": finding_id,
        "reviewedModelName": reading.get("model"),
        "reviewedVariantName": reading.get("variant"),
        "reviewedColourName": reading.get("colour"),
        "matchStage": "WORDS" if result.identified else picked["stage"],
        "candidates": [
            {
                "productSkuId": r["product_sku_id"],
                "skuCode": r["sku_code"],
                "modelName": r["model_name"],
                "variantName": r["variant_name"],
                "colourName": r["colour_name"],
                "exShowroomPrice": _to_decimal(r.get("master_ex_showroom")),
                "totalPrice": _master_total(r, basis),
            }
            for r in matched[:10]
        ],
    }


def get_model_catalog(
    connection: Connection, *, tenant_id: str, journey_id: UUID, effective_on: date | None = None,
) -> dict[str, Any]:
    """Every SKU in the price list in force on a date, for the PC's model
    picker: ``effective_on`` when given, else the Journey's pricing date
    (never today)."""
    effective_on = effective_on or journey_pricing_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if effective_on is None:
        # Never today (rule R5): the PC enters the booking date first.
        raise AuditCoreError(
            error_code="VAC-SKU-006",
            status_code=422,
            title="No pricing date",
            detail="The Journey has no booking date yet, so no price list applies. Enter the booking date first.",
        )
    try:
        plan = _price_plan_for_journey(
            connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=effective_on
        )
    except AuditCoreError as exc:
        raise AuditCoreError(
            error_code="VAC-SKU-003",
            status_code=422,
            title="No effective price list",
            detail="There is no effective price list for this Journey to browse.",
        ) from exc

    rows = _sku_rows_for_version(
        connection, tenant_id=tenant_id, price_list_version_id=plan["price_list_version_id"]
    )
    basis = _journey_registration(connection, tenant_id=tenant_id, journey_id=journey_id)["basis"]
    oem_code = _oem_code_for_tenant(connection, tenant_id=tenant_id)

    def _catalog_entry(r: dict[str, Any]) -> dict[str, Any]:
        # Real Mahindra price lists often leave the fuel / transmission /
        # drive / seater columns blank and carry them in the Variant text;
        # the picker's dropdowns read them from there (_master_attributes).
        # The trim stays the master's own Trim column, never the matching key.
        attrs = _master_attributes(r, oem_code=oem_code) if oem_code else None
        return {
            "productSkuId": r["product_sku_id"],
            "skuCode": r["sku_code"],
            "modelName": r["model_name"],
            "variantName": r["variant_name"],
            "colourName": r["colour_name"],
            "fuel": attrs.fuel if attrs else r.get("fuel_powertrain"),
            "transmission": attrs.transmission if attrs else r.get("transmission"),
            "drive": attrs.drive if attrs else r.get("drive"),
            "seater": attrs.seater if attrs else r.get("seater"),
            "trim": r.get("trim"),
            "exShowroomPrice": _to_decimal(r.get("master_ex_showroom")),
            "totalPrice": _master_total(r, basis),
        }

    state = _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    return {
        "journeyId": journey_id,
        # Set only when a PC or Team Lead confirmed the SKU: a change is then
        # a Team Lead reviewed proposal. An automatic match (or none) is
        # replaced by a direct pick (2026-10-20).
        "confirmedProductSkuId": state["product_sku_id"] if is_human_decision(state) else None,
        "skus": [_catalog_entry(r) for r in rows],
    }


def _published_sku_row(connection: Connection, *, tenant_id: str, product_sku_id: UUID) -> dict[str, Any] | None:
    """The SKU when it is active in any published price list of the tenant
    (a pick on a Journey that has no pricing date yet)."""
    row = connection.execute(
        text(
            """
            SELECT s.product_sku_id, s.sku_code, pm.model_name, pv.variant_name, c.colour_name
            FROM auditcore.product_skus s
            JOIN auditcore.product_models pm ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            LEFT JOIN auditcore.colours c ON c.colour_id = s.colour_id
            WHERE s.product_sku_id = :sku AND s.is_active
              AND EXISTS (SELECT 1 FROM auditcore.price_list_items pli
                          JOIN auditcore.price_list_versions plv
                            ON plv.tenant_id = pli.tenant_id AND plv.price_list_version_id = pli.price_list_version_id
                          WHERE pli.tenant_id = :t AND pli.product_sku_id = s.product_sku_id
                            AND plv.lifecycle_status = 'PUBLISHED')
            """
        ),
        {"t": tenant_id, "sku": product_sku_id},
    ).mappings().one_or_none()
    return dict(row) if row else None


def confirm_model_resolution_sku(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    product_sku_id: UUID,
    actor_id: str,
    correlation_id: str,
    task_id: Any = None,
) -> dict[str, Any]:
    """A PC's pick of the SKU: on a Journey with no SKU, or one the resolver
    matched automatically (2026-10-20: an automatic match is not a decision,
    the PC replaces it directly). A SKU a PC or Team Lead already confirmed
    changes only through a Team Lead reviewed proposal (409 here).

    The pick is validated against the price list in force on the Journey's
    pricing date (never a client-supplied id blindly), pinned as
    PC_CONFIRMED, closes the open finding and the SKU tasks, and re-prices
    the deal."""
    state = _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if is_human_decision(state) and str(state["product_sku_id"]) != str(product_sku_id):
        raise AuditCoreError(
            error_code="VAC-SKU-005",
            status_code=409,
            title="SKU already confirmed",
            detail="This Journey already has a confirmed SKU. Propose a correction for the Team Lead instead.",
        )
    on = journey_pricing_date(connection, tenant_id=tenant_id, journey_id=journey_id)
    if on is not None:
        current = _current_rows(connection, tenant_id=tenant_id, journey_id=journey_id, on=on)
        if current.get("skipped"):
            raise AuditCoreError(
                error_code="VAC-SKU-003",
                status_code=422,
                title="No effective price list",
                detail="There is no effective price list for this Journey to confirm a SKU against.",
            )
        row = next((r for r in current["rows"] if str(r["product_sku_id"]) == str(product_sku_id)), None)
    else:
        row = _published_sku_row(connection, tenant_id=tenant_id, product_sku_id=product_sku_id)
    if row is None:
        raise AuditCoreError(
            error_code="VAC-SKU-002",
            status_code=422,
            title="Unknown or inactive SKU",
            detail="The selected SKU is not in this Journey's current effective price list.",
        )

    _pin_human(
        connection, tenant_id=tenant_id, journey_id=journey_id, product_sku_id=product_sku_id,
        method=_PC_PICK_METHOD, basis=BASIS_PC,
        evidence={
            "source": "PC", "actorId": actor_id, "skuCode": row["sku_code"],
            "replaced": {"skuCode": state.get("sku_code"), "basis": state.get("match_basis")}
            if state.get("product_sku_id") else None,
            "pricingDate": on.isoformat() if on else None, "decidedAtUtc": datetime.now(UTC).isoformat(),
            "taskId": str(task_id) if task_id else None,
        },
        task_id=task_id, actor_id=actor_id,
    )
    resolved = _resolve_open_flag(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id, actor_id=actor_id,
    )
    _run_deal_reconciliation(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id
    )
    return {
        "resolved": True,
        "flagsResolved": resolved,
        "productSkuId": row["product_sku_id"],
        "skuCode": row["sku_code"],
        "modelName": row["model_name"],
        "variantName": row["variant_name"],
        "colourName": row["colour_name"],
    }


def reassign_confirmed_sku(
    connection: Connection, *, tenant_id: str, journey_id: UUID, product_sku_id: UUID,
    method: str = _CORRECTION_SELECTION_METHOD, basis: str = BASIS_TL, evidence: dict[str, Any] | None = None,
) -> None:
    """A Team Lead's decision replaces the Journey's SKU, whoever set it:
    the model-selection correction the TL approved
    (``uc03_model_selection_corrections``) or the invoice's SKU the TL
    applied (``uc03_sku_tasks``). Saved as TL_APPROVED."""
    _pin_human(connection, tenant_id=tenant_id, journey_id=journey_id, product_sku_id=product_sku_id,
               method=method, basis=basis, evidence=evidence or {"source": "TL",
                                                                 "decidedAtUtc": datetime.now(UTC).isoformat()})




router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/booking/model-resolution",
    tags=["uc03-model-resolution"],
)


class ModelResolutionCandidateOut(BaseModel):
    productSkuId: UUID
    skuCode: str
    modelName: str
    variantName: str | None = None
    colourName: str | None = None
    exShowroomPrice: Decimal | None = None
    totalPrice: Decimal | None = None


class ModelResolutionCandidatesResponse(BaseModel):
    journeyId: UUID
    findingId: UUID
    reviewedModelName: str | None = None
    reviewedVariantName: str | None = None
    reviewedColourName: str | None = None
    matchStage: str
    candidates: list[ModelResolutionCandidateOut]


class ModelCatalogSkuOut(BaseModel):
    productSkuId: UUID
    skuCode: str
    modelName: str
    variantName: str | None = None
    colourName: str | None = None
    fuel: str | None = None
    transmission: str | None = None
    drive: str | None = None
    seater: str | None = None
    trim: str | None = None
    exShowroomPrice: Decimal | None = None
    totalPrice: Decimal | None = None


class ModelCatalogResponse(BaseModel):
    journeyId: UUID
    confirmedProductSkuId: UUID | None = None
    skus: list[ModelCatalogSkuOut]


class ConfirmModelResolutionSkuRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productSkuId: UUID


class ConfirmModelResolutionSkuResponse(BaseModel):
    journeyId: UUID
    productSkuId: UUID
    skuCode: str
    modelName: str
    variantName: str | None = None
    colourName: str | None = None
    flagsResolved: int


@router.get("", response_model=ModelResolutionCandidatesResponse)
def read_model_resolution_candidates(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ModelResolutionCandidatesResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return ModelResolutionCandidatesResponse(
        **get_model_resolution_candidates(connection, tenant_id=tenant_id, journey_id=journey_id)
    )


@router.get("/catalog", response_model=ModelCatalogResponse)
def read_model_catalog(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ModelCatalogResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return ModelCatalogResponse(**get_model_catalog(connection, tenant_id=tenant_id, journey_id=journey_id))


@router.post("/confirm-sku", response_model=ConfirmModelResolutionSkuResponse)
def confirm_model_resolution_sku_endpoint(
    tenant_id: str,
    journey_id: UUID,
    payload: ConfirmModelResolutionSkuRequest,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ConfirmModelResolutionSkuResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    result = confirm_model_resolution_sku(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        product_sku_id=payload.productSkuId,
        actor_id=human_principal.subject,
        correlation_id=get_correlation_id(request),
    )
    return ConfirmModelResolutionSkuResponse(
        journeyId=journey_id,
        productSkuId=result["productSkuId"],
        skuCode=result["skuCode"],
        modelName=result["modelName"],
        variantName=result["variantName"],
        colourName=result["colourName"],
        flagsResolved=result["flagsResolved"],
    )



def sync_model_resolution(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    raise_findings: bool = True,
) -> dict[str, Any]:
    """Resolve the SKU or raise MODEL_NOT_IDENTIFIED, inside a savepoint.

    Found live 2026-10-19 (journey 8c632a40, admin SKU run): an error inside
    the resolution -- e.g. a statement timeout while another reconcile held
    the customer row -- left the caller's whole transaction aborted, so the
    escalation that followed failed with InFailedSqlTransaction and the
    attempt vanished without a trace. The savepoint undoes only this attempt;
    the caller's transaction stays usable, the failure is escalated, and one
    structured line says what happened."""
    return _guarded(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                    raise_findings=raise_findings, source="BOOKING")


def sync_model_resolution_from_invoice(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str
) -> dict[str, Any]:
    """The Delivery-side entry (a vehicle invoice was read or corrected): the
    same ladder over every document, inside a savepoint."""
    return _guarded(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                    raise_findings=True, source="INVOICE")


def recheck_model_resolution(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, commit: bool,
) -> dict[str, Any]:
    """The ``reresolve`` run for one Journey (review 471 B2). Nothing is
    cleared first: the ladder decides from the documents, and

    * a SKU a PC or Team Lead set is never touched;
    * an automatic SKU (the pre-rule AUTO_LEGACY ones included) is replaced
      only when the documents now name a different SKU; the same SKU gets
      its new basis and evidence;
    * when the documents do not name it (or cannot be read: no pricing date,
      no price list in force, no document), the SKU is KEPT, marked
      AUTO_UNCONFIRMED and the PC asked to confirm or set it.

    ``commit=False`` (the dry run) writes nothing: the result carries the
    ``proposal`` (old SKU -> proposed SKU, basis, words, prices, the tasks
    that would be raised). With ``commit`` the decision is written inside a
    savepoint around the whole Journey: an error leaves it untouched."""
    try:
        state = _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        ev = _evaluate(connection, tenant_id=tenant_id, journey_id=journey_id, state=state)
        proposal = _proposal(ev, state, _decide(ev, state, recheck_legacy=True))
    except Exception as exc:
        logger.warning("recheck_model_resolution failed", exc_info=True)
        return {"error": True, "errorType": type(exc).__name__, "errorMessage": str(exc)[:300]}
    if proposal["action"] == "SKIP":
        gate = _no_sku_gate(connection, tenant_id=tenant_id, journey_id=journey_id)
        proposal["noSkuGate"] = gate
        if gate != "DUE":
            proposal["tasks"] = []
        proposal["group"] = proposal_group(proposal)
    if not commit:
        return {"dryRun": True, "proposal": proposal}
    result = _guarded(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
                      raise_findings=True, source="RERESOLVE", recheck_legacy=True)
    return {**result, "proposal": proposal}


def _guarded(
    connection: Connection, *, tenant_id: str, journey_id: UUID, correlation_id: str, raise_findings: bool,
    source: str, recheck_legacy: bool = False,
) -> dict[str, Any]:
    savepoint = connection.begin_nested()
    result = _sync_model_resolution_unguarded(
        connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=correlation_id,
        raise_findings=raise_findings, recheck_legacy=recheck_legacy,
    )
    _close_savepoint(savepoint, result, tenant_id=tenant_id, journey_id=journey_id, source=source)
    return result


def _close_savepoint(savepoint: Any, result: dict[str, Any], *, tenant_id: str, journey_id: UUID, source: str) -> None:
    if result.get("error"):
        savepoint.rollback()
    else:
        savepoint.commit()
    _outcome_log.info(
        "sku_resolution_outcome",
        tenant_id=tenant_id,
        journey_id=str(journey_id),
        source=source,
        outcome=(
            "ERROR" if result.get("error")
            else "UNCONFIRMED" if result.get("unconfirmed")
            else "RESOLVED" if result.get("resolved") or result.get("pinned")
            else "NOT_MATCHED" if result.get("raised")
            else "SKIPPED" if result.get("skipped")
            else "OTHER"
        ),
        reason=result.get("reason"),
        match_stage=result.get("matchStage"),
        word_status=result.get("wordStatus"),
        basis=result.get("basis"),
        sku_code=result.get("skuCode"),
        price_check=result.get("priceCheck"),
        decided_by=result.get("source"),
        candidate_count=result.get("candidateCount"),
        error_type=result.get("errorType"),
        error=result.get("errorMessage"),
    )


def _sync_model_resolution_unguarded(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    correlation_id: str,
    raise_findings: bool = True,
    recheck_legacy: bool = False,
) -> dict[str, Any]:
    """The ladder (see the module docstring and ``_decide``). Idempotent;
    never raises.

    1. A SKU a PC or Team Lead set stays (an invoice naming another SKU at
       its master price is put to the Team Lead).
    2. A vehicle invoice whose words name one SKU at its master price sets
       it, replacing an automatic pin.
    3. The booking form's words name one SKU: pinned, AUTO_MATCHED or
       AUTO_PRICE_MISMATCH (+ PC task) by the price check.
    4. An automatic pin the documents do not name is kept, marked
       AUTO_UNCONFIRMED, with a PC task -- except a pre-rule AUTO_LEGACY pin,
       which only the reresolve run (``recheck_legacy``) re-checks.
    5. No SKU: an invoice's words naming one SKU at another price pin it as
       AUTO_PRICE_MISMATCH (PC task); otherwise MODEL_NOT_IDENTIFIED.

    ``raise_findings=False`` (a read-only self-heal) leaves a Journey that
    has a SKU as it is and skips the deal's evidence / variance checks."""
    try:
        state = _pin_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        has_sku = state["product_sku_id"] is not None
        if has_sku and not raise_findings:
            return {"resolved": True, "flagsResolved": 0, "skuCode": state["sku_code"],
                    "basis": state["match_basis"]}
        ev = _evaluate(connection, tenant_id=tenant_id, journey_id=journey_id, state=state)
        plan = _decide(ev, state, recheck_legacy=recheck_legacy)
        common = {"tenant_id": tenant_id, "journey_id": journey_id, "correlation_id": correlation_id,
                  "raise_findings": raise_findings}
        action = plan["action"]
        if action == "SKIP":
            if raise_findings:
                if ev.get("reason") == "no_vehicle_model_read":
                    # No document names a model any more: a closest-SKU
                    # suggestion made from the old text must not stay to be
                    # accepted and pinned with no document behind it.
                    from audit_core.uc03_p2_task_producer import resolve_if_open

                    resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                                    dedupe_key=f"sku-closest:{journey_id}",
                                    evidence={"reason": "The documents no longer suggest a closest SKU"})
                return {**ev, "taskOpen": _no_sku_task(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                       ev=ev)}
            return ev
        if action == "UNCONFIRMED" and ev.get("skipped"):
            return _apply_unconfirmed(connection, ev=ev, state=state, plan=plan, info={}, **common)
        if action == "KEEP" and ev.get("skipped"):
            return _keep_pin(connection, state=state, **common)
        info = _document_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, ev=ev, state=state,
                               plan=plan)
        if action == "INVOICE_SET":
            return _apply_invoice(connection, ev=ev, target=plan["target"], **common)
        if action == "KEEP_HUMAN":
            return _keep_pin(connection, state=state, info=info, checked=bool(ev["invoices"]), ev=ev, **common)
        if action == "BOOKING_PIN":
            return _apply_booking(connection, ev=ev, info=info, check=plan["check"], basis=plan["basis"], **common)
        if action == "KEEP":
            return _keep_pin(connection, state=state, info=info, checked=bool(ev["invoices"]), ev=ev,
                             named=bool(plan.get("named")), booking_not_naming=True, **common)
        if action == "UNCONFIRMED":
            return _apply_unconfirmed(connection, ev=ev, state=state, plan=plan, info=info, **common)
        if action == "INVOICE_WORDS_PIN":
            return _apply_invoice_words_only(connection, ev=ev, fallback=plan["fallback"], info=info, **common)
        return _raise_not_identified(connection, tenant_id=tenant_id, journey_id=journey_id,
                                     correlation_id=correlation_id, ev=ev, info=info)
    except Exception as exc:
        raise_if_worker_transient(exc)  # retried, not skipped (uc03_db_transient)
        logger.warning("sync_model_resolution failed", exc_info=True)
        return {"error": True, "errorType": type(exc).__name__, "errorMessage": str(exc)[:300]}


__all__ = [
    "AUTOMATIC_BASES",
    "BASIS_LABELS",
    "HUMAN_BASES",
    "booking_cancelled",
    "check_invoice_sku_mismatch",
    "invoice_ex_showroom",
    "is_human_decision",
    "journey_pricing_date",
    "proposal_group",
    "reassign_confirmed_sku",
    "recheck_model_resolution",
    "sync_model_resolution",
    "sync_model_resolution_from_invoice",
]
