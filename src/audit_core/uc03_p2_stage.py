"""Phase 2 Journey stage engine, driven by the P2 templates.

Stage gates come from ``p2_templates/document_templates.yaml`` (see
uc03_p2_registry). The engine reads existing business state and writes only
p2_* runtime rows:

- DOCUMENT_READY   an ACTIVE document of the template's type has durable facts
- PAYMENT_MINIMUM  eligible (non-duplicate) receipts reach the tenant minimum.
                   Receipts are one series across both stages (blueprint
                   20.2): in date order, the prefix that first meets the
                   minimum is the Booking payment and every later receipt is
                   a Delivery receipt (receipt_split). Receipts without a date
                   still count as money received.
- NO_OPEN_TASKS    no open task of the listed types (legacy or P2)

Completion is rule driven, never a button: Booking completes when every
Booking gate passes; Delivery completes when its documents (mandatory plus
conditional ones made mandatory by evidence), vehicle proof and the PC's
document / verification tasks are all done, and is then marked for TL
review (requirements document, 28 Sep 2026).
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.uc03_booking_confirmation_rules import _minimum_booking_amount
from audit_core.uc03_duplicate_receipt_detection import (
    ReceiptRecord,
    compute_duplicate_groups,
    normalize_receipt_date,
    normalize_receipt_number,
)
from audit_core.uc03_p2_dates import date_floor_verdict
from audit_core.uc03_p2_registry import DocumentTemplate, Gate, Registry, get_registry
from audit_core.uc03_p2_runtime import record_activity

_OPEN_LEGACY_TASK_STATUSES = ("PENDING", "READY", "CLAIMED", "IN_PROGRESS", "RETRY_WAIT")
_OPEN_P2_TASK_STATUSES = (
    "READY", "IN_PROGRESS", "ACTION_COMPLETED", "VERIFYING",
    "AWAITING_REQUESTER_REVIEW", "RETURNED",
)
# Legacy evidence rows may carry the pre-canonical key for the same document.
_LEGACY_TYPE_ALIASES = {"booking_form": ("booking_docket",), "dealer_receipt": ("payment_receipt",)}


def _evidence_types(template: DocumentTemplate) -> list[str]:
    types: list[str] = [template.key]
    for di_type in template.di_types:
        types.append(di_type)
        types.extend(_LEGACY_TYPE_ALIASES.get(di_type, ()))
    return list(dict.fromkeys(types))


def ready_document_count(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    template: DocumentTemplate,
) -> int:
    if _RECEIPT_TYPE in template.di_types:
        # Receipts belong to a stage by the minimum booking amount, not by
        # when they were uploaded (see receipt_split).
        split = receipt_split(connection, tenant_id=tenant_id, journey_id=journey_id)
        return len(split["DELIVERY" if str(template.stage).upper() == "DELIVERY" else "BOOKING"])
    return int(
        connection.execute(
            text(
                """
                SELECT COUNT(DISTINCT e.di_document_id)
                FROM auditcore.evidence e
                WHERE e.tenant_id=:tenant_id
                  AND e.journey_id=:journey_id
                  AND e.association_status='ACTIVE'
                  AND e.document_type_key = ANY(:document_types)
                  AND EXISTS (
                    SELECT 1
                    FROM auditcore.journey_document_extracted_fields f
                    WHERE f.tenant_id=e.tenant_id
                      AND f.journey_id=e.journey_id
                      AND f.di_document_id=e.di_document_id
                      AND f.effective_value IS NOT NULL
                  )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "document_types": _evidence_types(template),
            },
        ).scalar_one()
        or 0
    )


_RECEIPT_TYPE = "dealer_receipt"


def ready_document_counts(
    connection: Connection, registry: Registry, *, tenant_id: str, journey_id: UUID,
) -> dict[str, int]:
    """``ready_document_count`` for every template in two statements instead
    of one per template: the documents screen asks for all of them at once,
    and a Railway database answers each round trip slowly."""
    rows = connection.execute(
        text(
            """
            SELECT e.document_type_key, COUNT(DISTINCT e.di_document_id) AS ready
            FROM auditcore.evidence e
            WHERE e.tenant_id=:tenant_id
              AND e.journey_id=:journey_id
              AND e.association_status='ACTIVE'
              AND EXISTS (
                SELECT 1
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id=e.tenant_id
                  AND f.journey_id=e.journey_id
                  AND f.di_document_id=e.di_document_id
                  AND f.effective_value IS NOT NULL
              )
            GROUP BY e.document_type_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    by_type = {str(row["document_type_key"]): int(row["ready"] or 0) for row in rows}
    split: dict[str, Any] | None = None
    counts: dict[str, int] = {}
    for template in registry.documents.values():
        if _RECEIPT_TYPE in template.di_types:
            if split is None:
                split = receipt_split(connection, tenant_id=tenant_id, journey_id=journey_id)
            counts[template.key] = len(split["DELIVERY" if str(template.stage).upper() == "DELIVERY" else "BOOKING"])
        else:
            counts[template.key] = sum(by_type.get(key, 0) for key in _evidence_types(template))
    return counts


def receipt_split(connection: Connection, *, tenant_id: str, journey_id: UUID,
                  minimum: Decimal | None = None) -> dict[str, Any]:
    """Every receipt-backed payment of the Journey, split by stage.

    Receipts are one series across Booking and Delivery: in date order, the
    receipts up to the one that reaches the minimum booking amount are the
    Booking payment and every later receipt is a Delivery receipt. A receipt
    with the same amount, receipt number and date as an earlier one (or the
    same amount and date when neither has a number) is a duplicate and never
    counts. Voided or superseded receipt documents do not count either.
    """
    minimum = _minimum_booking_amount(connection, tenant_id=tenant_id) if minimum is None else minimum
    payments = connection.execute(
        text(
            """
            SELECT p.payment_id, p.amount, p.receipt_date, p.receipt_number, p.source_di_document_id
            FROM auditcore.payments p
            WHERE p.tenant_id=:tenant_id
              AND p.journey_id=:journey_id
              AND p.status_source='EVIDENCE'
              AND p.amount IS NOT NULL
              AND p.amount > 0
              AND EXISTS (
                SELECT 1 FROM auditcore.evidence e
                WHERE e.tenant_id=p.tenant_id AND e.journey_id=p.journey_id
                  AND e.di_document_id=p.source_di_document_id
                  AND e.association_status='ACTIVE'
              )
            ORDER BY p.receipt_date ASC NULLS LAST, p.created_at_utc ASC, p.payment_id ASC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    records = [
        ReceiptRecord(
            document_id=payment["payment_id"],
            stage_code="BOOKING",
            document_type_key=_RECEIPT_TYPE,
            receipt_number=normalize_receipt_number(payment["receipt_number"]),
            amount=Decimal(str(payment["amount"])),
            receipt_date=normalize_receipt_date(payment["receipt_date"]),
        )
        for payment in payments
    ]
    duplicates: set[Any] = set()
    for group in compute_duplicate_groups(records):
        if group.dates_match:  # same amount, number and date: the same receipt again
            duplicates.update(d.document_id for d in group.documents[1:])
    booking: list[dict[str, Any]] = []
    delivery: list[dict[str, Any]] = []
    running = Decimal(0)
    for payment in payments:
        if payment["payment_id"] in duplicates:
            continue
        entry = {
            "paymentId": str(payment["payment_id"]),
            "documentId": str(payment["source_di_document_id"]),
            "amount": str(Decimal(str(payment["amount"]))),
            "receiptNumber": payment["receipt_number"],
            "receiptDate": str(payment["receipt_date"]) if payment["receipt_date"] else None,
        }
        if running < minimum or (minimum <= 0 and not booking):
            booking.append(entry)
            running += Decimal(str(payment["amount"]))
        else:
            delivery.append(entry)
    total = sum((Decimal(e["amount"]) for e in booking + delivery), Decimal(0))
    return {
        "BOOKING": booking,
        "DELIVERY": delivery,
        "total": total,
        "bookingTotal": running,
        "duplicates": len(duplicates),
    }


def open_task_count(connection: Connection, *, tenant_id: str, journey_id: UUID, task_types: tuple[str, ...]) -> int:
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*) FROM auditcore.workflow_tasks
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND task_type = ANY(:task_types) AND task_status = ANY(:legacy_open))
            + (SELECT COUNT(*) FROM auditcore.p2_tasks
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND task_type = ANY(:task_types) AND task_status = ANY(:p2_open))
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "task_types": list(task_types),
            "legacy_open": list(_OPEN_LEGACY_TASK_STATUSES),
            "p2_open": list(_OPEN_P2_TASK_STATUSES),
        },
    ).scalar_one()
    return int(row or 0)


REVIEW_REASON_LABELS = {
    "LOW_CONFIDENCE": "read with low confidence",
    "DATE_BEFORE_FLOOR": "a date before the programme started",
    "DATE_UNREADABLE": "not a readable date",
}
# Reasons that make the verification High severity: the value is wrong as
# read, not merely uncertain.
HIGH_SEVERITY_REASONS = frozenset({"DATE_BEFORE_FLOOR", "DATE_UNREADABLE"})


def field_review_reasons(
    registry: Registry, template: DocumentTemplate, *, di_type: str | None, field_key: str,
    value: Any, confidence: float | None,
) -> list[str]:
    """Why a PC has to look at this value: low confidence, a date before the
    programme's floor, or a date the extractor could not read. Empty means
    the value stands as extracted."""
    reasons: list[str] = []
    if template.needs_review(field_key, confidence):
        reasons.append("LOW_CONFIDENCE")
    if registry.date_check_applies(di_type, field_key):
        verdict = date_floor_verdict(value, registry.extraction_rules.date_floor)
        if verdict:
            reasons.append(verdict)
    return reasons


def review_severity(reasons: list[str]) -> str:
    return "HIGH" if HIGH_SEVERITY_REASONS.intersection(reasons) else "MEDIUM"


def unreviewed_fields(
    connection: Connection,
    registry: Registry,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage: str | None = None,
    document_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Fields on ACTIVE documents that a PC has to look at and nobody has.

    A field needs review when its confidence is below its template threshold
    (missing confidence is never trusted) or its date lies before the
    programme's date floor, it has a value, and it has not been confirmed or
    corrected. Each item carries its ``reasons`` and the ``severity`` of the
    task they warrant."""
    rows = connection.execute(
        text(
            """
            SELECT f.di_document_id, f.field_key, f.source_canonical_field_id,
                   f.source_fact_version, f.confidence_score, f.effective_value,
                   e.document_type_key, e.process_area
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id
             AND e.di_document_id=f.di_document_id AND e.association_status='ACTIVE'
            WHERE f.tenant_id=:tenant_id AND f.journey_id=:journey_id
              AND f.reviewed_at_utc IS NULL
              AND f.is_modified = false
              AND f.effective_value IS NOT NULL
              AND f.effective_value <> 'null'::jsonb
              AND f.effective_value <> '""'::jsonb
              AND (CAST(:document_id AS uuid) IS NULL OR f.di_document_id=CAST(:document_id AS uuid))
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "document_id": document_id},
    ).mappings().all()
    pending: list[dict[str, Any]] = []
    for row in rows:
        area = str(row["process_area"] or "").upper() or None
        template = registry.template_for_di_type(row["document_type_key"], stage=area)
        if template.is_supporting:
            continue
        if stage is not None and template.stage not in (stage, "ANY"):
            continue
        confidence = float(row["confidence_score"]) if row["confidence_score"] is not None else None
        field_key = str(row["field_key"])
        reasons = field_review_reasons(
            registry, template, di_type=row["document_type_key"], field_key=field_key,
            value=row["effective_value"], confidence=confidence,
        )
        if not reasons:
            continue
        pending.append(
            {
                "documentId": str(row["di_document_id"]),
                "templateKey": template.key,
                "documentName": template.display_name,
                "fieldKey": field_key,
                "canonicalFieldId": row["source_canonical_field_id"],
                "sourceFactVersion": int(row["source_fact_version"] or 1),
                "confidence": confidence,
                "threshold": template.review_threshold_for(field_key),
                "value": row["effective_value"],
                "reasons": reasons,
                "severity": review_severity(reasons),
            }
        )
    return pending


# ── conditional -> mandatory, from document evidence ─────────────────────────
# Conditions named by the requirements document (section 3). Each becomes
# true from evidence on the Journey's own documents -- booking form,
# invoices, ledger, insurance, finance -- so a conditional document turns
# mandatory by itself; nobody declares anything.
CONDITION_LABELS = {
    "financeCase": "finance case",
    "corporateDiscount": "corporate discount",
    "corporateCustomer": "corporate customer",
    "exchangeBenefit": "exchange benefit",
    "insuranceByDealer": "insurance through the dealership",
    "registrationByDealer": "registration through the dealership",
    "rsaSold": "RSA sold",
    "ewSold": "extended warranty sold",
    "accessoriesSold": "accessories sold",
    "scrappageClaimed": "scrappage benefit",
}

_AMOUNT_EVIDENCE = {
    # evidence key -> field keys whose positive amount is the evidence
    "corporateDiscount": ("corporate_discount_amount", "corporate_offer_amount", "CORPORATE_PRIVILEGE", "CORPORATE"),
    "exchangeBenefit": ("exchange_discount_amount", "exchange_claim_amount", "exchange_value", "exchange_credit",
                        "EXCHANGE_BONUS", "EXCHANGE"),
    "financeCase": ("loan_credit", "financed_amount", "loan_amount", "loan_disbursement_amount"),
    "insuranceByDealer": ("inhouse_insurance_discount_amount",),
    "registrationByDealer": ("registration_charges", "road_tax_amount", "road_tax_registration", "rto_amount",
                             "registration_amount"),
    "rsaSold": ("rsa_amount",),
    "ewSold": ("additional_warranty_amount", "extended_warranty_amount", "ew_amount"),
    "accessoriesSold": ("accessories_cost", "essential_kit_amount", "genuine_accessories_amount",
                        "non_genuine_accessories_amount", "accessories_amount"),
    "scrappageClaimed": ("scrappage_discount_amount", "SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_BONUS_COD"),
}
_TEXT_EVIDENCE = {
    # evidence key -> field keys whose non-empty value is the evidence
    "financeCase": ("financed_by", "financier_name", "hypothecation", "hypothecated_to"),
    "corporateCustomer": ("buyer_gstin", "customer_gstin", "gstin_of_buyer"),
}
_DEALER_WORDS = ("DEALER", "SHOWROOM", "IN-HOUSE", "INHOUSE", "IN HOUSE", "COMPANY")
_SELF_WORDS = ("SELF", "CUSTOMER", "OWN")
_YES_WORDS = ("YES", "Y", "TRUE", "APPLICABLE")
# A trade-in row exists for many journeys with no exchange at all (a docket
# read as "Exchange: No" stores NO_EXCHANGE; an empty valuation field stores
# nulls). An exchange vehicle is on file only when the row says so.
EXCHANGE_ON_FILE = (
    "(actual_status_code = 'EXCHANGE_TAKEN'"
    " OR NULLIF(btrim(COALESCE(old_vehicle_registration, '')), '') IS NOT NULL"
    " OR COALESCE(actual_value, quoted_value, 0) > 0"
    " OR (details ->> 'exchangeTaken') = 'true')"
)


def _as_amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    cleaned = "".join(ch for ch in str(value) if ch.isdigit() or ch in ".-")
    try:
        return Decimal(cleaned) if cleaned not in ("", ".", "-") else None
    except Exception:  # noqa: BLE001
        return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text_value = value.strip().strip('"')
    else:
        text_value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    return "" if text_value.lower() in {"", "null", "none", "na", "n/a", "-", "nil"} else text_value


def condition_reasons(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, str]:
    """Evidence key -> the evidence that makes it true (a readable sentence)."""
    fields = connection.execute(
        text(
            """
            SELECT f.field_key, COALESCE(f.effective_value, f.extracted_value) AS value, e.document_type_key
            FROM auditcore.journey_document_extracted_fields f
            JOIN auditcore.evidence e
              ON e.tenant_id=f.tenant_id AND e.journey_id=f.journey_id AND e.di_document_id=f.di_document_id
             AND e.association_status='ACTIVE'
            WHERE f.tenant_id=:t AND f.journey_id=:j
            UNION ALL
            SELECT component_key, to_jsonb(amount), source_document_type
            FROM auditcore.commercial_line_source_values WHERE tenant_id=:t AND journey_id=:j
            UNION ALL
            SELECT discount_key, to_jsonb(actual_discount_amount), 'deal'
            FROM auditcore.discount_applications WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    facts = connection.execute(
        text(
            f"""
            SELECT
              (SELECT upper(c.customer_type_code) FROM auditcore.journeys j
                 JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                WHERE j.tenant_id=:t AND j.journey_id=:j) AS customer_type,
              EXISTS (SELECT 1 FROM auditcore.trade_in_cases WHERE tenant_id=:t AND journey_id=:j
                        AND {EXCHANGE_ON_FILE}) AS trade_in,
              (SELECT provider_name FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j
                 AND (provider_name IS NOT NULL OR financed_amount > 0) ORDER BY updated_at_utc DESC LIMIT 1)
                AS financier,
              (SELECT upper(COALESCE(insurance_by, '')) FROM auditcore.insurance_records
                WHERE tenant_id=:t AND journey_id=:j ORDER BY updated_at_utc DESC LIMIT 1) AS insurance_record_by,
              (SELECT upper(COALESCE(registration_by, '')) FROM auditcore.registration_records
                WHERE tenant_id=:t AND journey_id=:j ORDER BY updated_at_utc DESC LIMIT 1) AS registration_record_by
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()

    reasons: dict[str, str] = {}

    def found(key: str, sentence: str) -> None:
        reasons.setdefault(key, sentence)

    def where(document_type: str | None) -> str:
        return (document_type or "a document").replace("_", " ")

    for row in fields:
        key, value, document_type = str(row["field_key"]), row["value"], row["document_type_key"]
        for condition, keys in _AMOUNT_EVIDENCE.items():
            if key in keys:
                amount = _as_amount(value)
                if amount is not None and amount > 0:
                    found(condition, f"The {where(document_type)} shows {key.replace('_', ' ').lower()} of ₹{amount:,.0f}.")
        for condition, keys in _TEXT_EVIDENCE.items():
            if key in keys and _as_text(value):
                found(condition, f"The {where(document_type)} shows {key.replace('_', ' ')} {_as_text(value)}.")
        upper = _as_text(value).upper()
        if key == "insurance_by" and any(w in upper for w in _DEALER_WORDS) and not any(w in upper for w in _SELF_WORDS):
            found("insuranceByDealer", f"The {where(document_type)} shows insurance by {_as_text(value)}.")
        if key == "registration_by" and any(w in upper for w in _DEALER_WORDS) and not any(w in upper for w in _SELF_WORDS):
            found("registrationByDealer", f"The {where(document_type)} shows registration by {_as_text(value)}.")
        if key == "exchange_applicable" and upper in _YES_WORDS:
            found("exchangeBenefit", f"The {where(document_type)} marks exchange as applicable.")
    if facts["customer_type"] in ("CORPORATE", "COMPANY", "BUSINESS", "INSTITUTIONAL"):
        found("corporateCustomer", "The customer is a company.")
    if facts["trade_in"]:
        found("exchangeBenefit", "An exchange vehicle is on file.")
    if facts["financier"]:
        found("financeCase", f"The deal is financed by {facts['financier']}.")
    if any(w in (facts["insurance_record_by"] or "") for w in _DEALER_WORDS):
        found("insuranceByDealer", "The insurance was arranged by the dealership.")
    if any(w in (facts["registration_record_by"] or "") for w in _DEALER_WORDS):
        found("registrationByDealer", "The registration is done by the dealership.")
    return reasons


def active_conditions(connection: Connection, *, tenant_id: str, journey_id: UUID) -> set[str]:
    return set(condition_reasons(connection, tenant_id=tenant_id, journey_id=journey_id))


def requirement_items(
    connection: Connection, registry: Registry, *, tenant_id: str, journey_id: UUID, stage: str,
    reasons: dict[str, str], ready_counts: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """The stage's checklist: mandatory documents, conditional documents
    made mandatory by evidence, and optional ones. Templates sharing a
    group are one requirement met by any of them."""
    items: dict[str, dict[str, Any]] = {}
    for template in registry.documents.values():
        if template.stage != stage or template.is_supporting:
            continue
        triggered = [c for c in template.conditions if c in reasons]
        if template.requirement == "CONDITIONAL" and not triggered:
            continue
        required = template.requirement in {"REQUIRED", "CONDITIONAL"}
        ready = (
            ready_counts[template.key] if ready_counts is not None
            else ready_document_count(connection, tenant_id=tenant_id, journey_id=journey_id, template=template)
        )
        key = template.group or template.key
        item = items.setdefault(key, {
            "key": key, "templates": [], "labels": [], "required": required,
            "requirement": template.requirement, "received": False, "readyCount": 0,
            "conditions": [], "reason": None,
        })
        item["templates"].append(template.key)
        item["labels"].append(template.display_name)
        item["readyCount"] += ready
        item["received"] = item["received"] or ready > 0
        item["required"] = item["required"] or required
        for condition in triggered:
            if condition not in item["conditions"]:
                item["conditions"].append(condition)
            item["reason"] = item["reason"] or reasons[condition]
    for item in items.values():
        item["label"] = " or ".join(item["labels"])
    return list(items.values())


def vehicle_proof(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*) FROM auditcore.delivery_vehicle_photos
                WHERE tenant_id=:t AND journey_id=:j AND deleted_at_utc IS NULL) AS photos,
              (SELECT row_to_json(v) FROM (
                 SELECT vin, chassis_number, engine_number, entered_by_role, created_at_utc
                 FROM auditcore.p2_vehicle_identifications WHERE tenant_id=:t AND journey_id=:j
                 ORDER BY created_at_utc DESC LIMIT 1) v) AS identity
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    photos = int(row["photos"] or 0)
    identity = row["identity"]
    return {"passed": photos > 0 or bool(identity), "photos": photos, "manualIdentity": identity}


def _open_tasks_for(connection: Connection, *, tenant_id: str, journey_id: UUID, role: str | None,
                    tabs: tuple[str, ...]) -> list[dict[str, Any]]:
    from audit_core.uc03_p2_tasks import task_queue_tab

    rows = connection.execute(
        text(
            """
            SELECT task_id, task_type, category, title, assigned_role_code
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j
              AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
              AND (CAST(:role AS varchar) IS NULL OR assigned_role_code=CAST(:role AS varchar))
            """
        ),
        {"t": tenant_id, "j": journey_id, "role": role},
    ).mappings().all()
    return [
        {"taskId": str(r["task_id"]), "title": r["title"], "taskType": r["task_type"]}
        for r in rows
        if not tabs or task_queue_tab(str(r["task_type"]), str(r["category"] or "")) in tabs
    ]


def _evaluate_gate(
    connection: Connection,
    registry: Registry,
    gate: Gate,
    *,
    tenant_id: str,
    journey_id: UUID,
    minimum: Decimal,
    reasons: dict[str, str] | None = None,
    stage: str = "BOOKING",
) -> dict[str, Any]:
    if gate.kind == "REQUIRED_DOCUMENTS":
        items = requirement_items(connection, registry, tenant_id=tenant_id, journey_id=journey_id,
                                  stage=stage, reasons=reasons or {})
        required = [i for i in items if i["required"]]
        missing = [{"key": i["key"], "label": i["label"], "reason": i["reason"]} for i in required if not i["received"]]
        return {"passed": not missing, "requiredCount": len(required),
                "receivedCount": len(required) - len(missing), "missing": missing}
    if gate.kind == "DOCUMENT_READY":
        counts = {
            key: ready_document_count(
                connection, tenant_id=tenant_id, journey_id=journey_id, template=registry.document(key),
            )
            for key in gate.documents
        }
        received = [count > 0 for count in counts.values()]
        passed = any(received) if gate.match == "ANY" else all(received)
        return {"passed": passed, "documentCount": sum(counts.values()), "documents": counts}
    if gate.kind == "PAYMENT_MINIMUM":
        split = receipt_split(connection, tenant_id=tenant_id, journey_id=journey_id, minimum=minimum)
        total = split["total"]
        return {
            "passed": total >= minimum,
            "receiptTotal": str(total),
            "minimumAmount": str(minimum),
            "receiptCount": len(split["BOOKING"]) + len(split["DELIVERY"]),
            "duplicateReceiptsExcluded": split["duplicates"],
            "shortfall": str(max(minimum - total, Decimal(0))),
            "bookingReceiptTotal": str(split["bookingTotal"]),
            "bookingReceipts": split["BOOKING"],
            "deliveryReceipts": split["DELIVERY"],
        }
    if gate.kind == "FIELDS_REVIEWED":
        pending = unreviewed_fields(
            connection, registry, tenant_id=tenant_id, journey_id=journey_id, stage="BOOKING",
        )
        documents = sorted({item["documentName"] for item in pending})
        return {"passed": not pending, "pendingCount": len(pending), "documents": documents}
    if gate.kind == "NO_OPEN_TASKS":
        pending = open_task_count(
            connection, tenant_id=tenant_id, journey_id=journey_id, task_types=gate.task_types,
        )
        return {"passed": pending == 0, "pendingCount": pending}
    if gate.kind == "VEHICLE_PROOF":
        return vehicle_proof(connection, tenant_id=tenant_id, journey_id=journey_id)
    if gate.kind == "OPEN_TASKS":
        pending = _open_tasks_for(connection, tenant_id=tenant_id, journey_id=journey_id,
                                  role=gate.role, tabs=gate.tabs)
        return {"passed": not pending, "pendingCount": len(pending), "tasks": pending[:10]}
    raise ValueError(f"Unsupported gate kind {gate.kind}")


def _upsert_gate(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    gate_key: str,
    passed: bool,
    details: dict[str, Any],
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_stage_gate_state (
                tenant_id, journey_id, stage_code, gate_key,
                gate_status, details, evaluated_at_utc
            ) VALUES (
                :tenant_id, :journey_id, :stage_code, :gate_key,
                :gate_status, CAST(:details AS jsonb), now()
            )
            ON CONFLICT (tenant_id, journey_id, stage_code, gate_key)
            DO UPDATE SET gate_status=EXCLUDED.gate_status,
                          details=EXCLUDED.details,
                          evaluated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage_code": stage_code,
            "gate_key": gate_key,
            "gate_status": "PASS" if passed else "WAITING",
            "details": json.dumps(details, default=str),
        },
    )


_IN_FLIGHT = ("QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING",
              "CLASSIFYING", "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT")


def _previous_gates(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[tuple[str, str], bool]:
    rows = connection.execute(
        text(
            """
            SELECT stage_code, gate_key, gate_status FROM auditcore.p2_stage_gate_state
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).all()
    return {(str(stage), str(key)): str(status) == "PASS" for stage, key, status in rows}


def _stage_gates(connection: Connection, registry: Registry, *, stage: str, tenant_id: str, journey_id: UUID,
                 minimum: Decimal, reasons: dict[str, str]) -> dict[str, dict[str, Any]]:
    gates: dict[str, dict[str, Any]] = {}
    for gate in registry.stages[stage].gates:
        result = _evaluate_gate(
            connection, registry, gate, tenant_id=tenant_id, journey_id=journey_id, minimum=minimum,
            reasons=reasons, stage=stage,
        )
        result.update({"label": gate.label, "kind": gate.kind})
        if not result["passed"]:
            result["action"] = gate.missing
        gates[gate.key] = result
        _upsert_gate(
            connection, tenant_id=tenant_id, journey_id=journey_id, stage_code=stage,
            gate_key=gate.key, passed=result["passed"], details=result,
        )
    return gates


def recompute_journey_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    registry: Registry | None = None,
    complete_delivery: bool = False,
) -> dict[str, Any]:
    """Rule-driven stage from the documents; no button completes anything.

    ``complete_delivery`` is set only once the rule tasks that follow from
    the documents have been raised (uc03_p2_worker.settle_journey), so a
    Delivery never completes ahead of a task that should hold it.

    Booking completes when the Booking Docket, the customer KYC (PAN or
    Aadhaar) and receipts reaching the minimum booking amount are in.
    Delivery completes when every mandatory and evidence-triggered
    conditional Delivery document is in, the vehicle is proven (pictures,
    or the VIN / chassis / engine entered by the PC) and the PC's document,
    verification and duplicate tasks are closed; it is then marked for TL
    review. ``transitions`` tells the worker what just happened.
    """
    registry = registry or get_registry()
    minimum = _minimum_booking_amount(connection, tenant_id=tenant_id)
    reasons = condition_reasons(connection, tenant_id=tenant_id, journey_id=journey_id)
    before = _previous_gates(connection, tenant_id=tenant_id, journey_id=journey_id)
    previous = connection.execute(
        text(
            """
            SELECT current_stage, booking_completion_state, delivery_completion_state
            FROM auditcore.p2_journey_runtime
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    gates = _stage_gates(connection, registry, stage="BOOKING", tenant_id=tenant_id, journey_id=journey_id,
                         minimum=minimum, reasons=reasons)
    booking_complete = all(item["passed"] for item in gates.values())
    in_flight = int(connection.execute(
        text(
            """
            SELECT COUNT(*) FROM auditcore.p2_document_queue
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND queue_status = ANY(:states)
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "states": list(_IN_FLIGHT)},
    ).scalar_one())
    delivery_started = bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM auditcore.evidence e
                  WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
                    AND e.association_status='ACTIVE'
                    AND upper(COALESCE(e.process_area,''))='DELIVERY'
                ) OR EXISTS (
                  SELECT 1 FROM auditcore.deliveries d
                  WHERE d.tenant_id=:tenant_id AND d.journey_id=:journey_id
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
    )
    delivery_gates = _stage_gates(connection, registry, stage="DELIVERY", tenant_id=tenant_id,
                                  journey_id=journey_id, minimum=minimum, reasons=reasons)
    docs_gate = delivery_gates.get("REQUIRED_DOCUMENTS") or {"passed": False}
    already_delivered = bool(previous and previous["delivery_completion_state"] == "COMPLETE")
    delivery_ready = delivery_started and all(item["passed"] for item in delivery_gates.values())
    can_complete_delivery = delivery_ready and complete_delivery

    if not booking_complete:
        stage = "BOOKING_VERIFY_DOCUMENTS" if in_flight else "BOOKING_DOCUMENT_UPLOAD"
        booking_state = "IN_PROGRESS"
        delivery_state = "COMPLETE" if already_delivered else "IN_PROGRESS"
    else:
        booking_state = "COMPLETE"
        if already_delivered or can_complete_delivery:
            stage, delivery_state = "DELIVERY_COMPLETE", "COMPLETE"
        elif not delivery_started:
            stage, delivery_state = "BOOKING_COMPLETE", "IN_PROGRESS"
        elif not docs_gate["passed"]:
            stage, delivery_state = "DELIVERY_DOCUMENT_UPLOAD", "IN_PROGRESS"
        else:
            stage, delivery_state = "DELIVERY_VERIFY_DOCUMENTS", "IN_PROGRESS"

    manual_pending = int(connection.execute(
        text(
            """
            SELECT COUNT(*) FROM auditcore.p2_tasks
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND task_type='MANUAL_VERIFICATION_REVIEW'
              AND task_status NOT IN ('VERIFIED_COMPLETE','CANCELLED','FAILED','DEAD_LETTER')
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one())
    total_row = gates.get("MINIMUM_BOOKING_PAYMENT") or {}
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_runtime (
                tenant_id, journey_id, current_stage,
                booking_minimum_amount, booking_receipt_total,
                booking_completion_state, delivery_completion_state,
                manual_verification_pending_count
            ) VALUES (
                :tenant_id, :journey_id, :stage,
                :minimum, :receipt_total, :booking_state, :delivery_state, :manual_pending
            )
            ON CONFLICT (tenant_id, journey_id)
            DO UPDATE SET current_stage=EXCLUDED.current_stage,
                          booking_minimum_amount=EXCLUDED.booking_minimum_amount,
                          booking_receipt_total=EXCLUDED.booking_receipt_total,
                          booking_completion_state=EXCLUDED.booking_completion_state,
                          delivery_completion_state=EXCLUDED.delivery_completion_state,
                          manual_verification_pending_count=EXCLUDED.manual_verification_pending_count,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "stage": stage,
            "minimum": minimum,
            "receipt_total": Decimal(str(total_row.get("receiptTotal") or 0)),
            "booking_state": booking_state,
            "delivery_state": delivery_state,
            "manual_pending": manual_pending,
        },
    )

    transitions: list[str] = []
    from audit_core.uc03_p2_workflow import (
        mark_booking_completed,
        mark_delivery_completed,
    )

    if booking_state == "COMPLETE" and mark_booking_completed(connection, tenant_id=tenant_id, journey_id=journey_id):
        transitions.append("BOOKING_COMPLETED")
    if delivery_started and docs_gate["passed"] and not before.get(("DELIVERY", "REQUIRED_DOCUMENTS")):
        transitions.append("DELIVERY_DOCUMENTS_COMPLETE")
    if delivery_state == "COMPLETE" and not already_delivered:
        mark_delivery_completed(connection, tenant_id=tenant_id, journey_id=journey_id,
                                gates={k: v.get("label") for k, v in delivery_gates.items()})
        transitions.append("DELIVERY_COMPLETED")

    delivery = {
        "passed": delivery_ready or already_delivered,
        "started": delivery_started,
        "requiredCount": docs_gate.get("requiredCount", 0),
        "receivedCount": docs_gate.get("receivedCount", 0),
        "missing": list(docs_gate.get("missing") or [])
        + ([{"key": "vehicle_proof", "label": "Vehicle pictures (or VIN / chassis / engine number)"}]
           if not (delivery_gates.get("VEHICLE_PROOF") or {}).get("passed") else [])
        + ([{"key": "pc_tasks", "label": f"{delivery_gates['PC_TASKS_CLOSED']['pendingCount']} open PC task(s)"}]
           if delivery_gates.get("PC_TASKS_CLOSED") and not delivery_gates["PC_TASKS_CLOSED"]["passed"] else []),
        "gates": delivery_gates,
    }
    result = {
        "stage": stage,
        "bookingCompletionState": booking_state,
        "deliveryCompletionState": delivery_state,
        "minimumBookingAmount": str(minimum),
        "bookingReceiptTotal": str(total_row.get("receiptTotal") or "0"),
        "manualVerificationPending": manual_pending,
        "gates": gates,
        "delivery": delivery,
        "conditions": sorted(reasons),
        "conditionReasons": reasons,
        "transitions": transitions,
    }
    if previous is None or previous["current_stage"] != stage or previous["booking_completion_state"] != booking_state:
        record_activity(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="STAGE_CHANGED",
            subject_type="JOURNEY",
            subject_id=str(journey_id),
            details={
                "from": previous["current_stage"] if previous else None,
                "to": stage,
                "bookingCompletionState": booking_state,
                "deliveryCompletionState": delivery_state,
            },
        )
    return result


# Backwards-compatible name used by the worker and API.
recompute_booking_stage = recompute_journey_stage


def read_booking_stage(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    registry = get_registry()
    runtime = connection.execute(
        text(
            """
            SELECT current_stage, booking_completion_state, delivery_completion_state,
                   booking_minimum_amount, booking_receipt_total,
                   manual_verification_pending_count, fact_version
            FROM auditcore.p2_journey_runtime
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    gate_rows = connection.execute(
        text(
            """
            SELECT stage_code, gate_key, gate_status, details
            FROM auditcore.p2_stage_gate_state
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    stored = {
        (str(row["stage_code"]), str(row["gate_key"])): {
            **dict(row["details"] or {}),
            "passed": str(row["gate_status"]) == "PASS",
        }
        for row in gate_rows
    }
    gates: dict[str, dict[str, Any]] = {}
    for gate in registry.stages["BOOKING"].gates:
        gates[gate.key] = stored.get(
            ("BOOKING", gate.key),
            {"passed": False, "label": gate.label, "kind": gate.kind, "action": gate.missing},
        )
    delivery_gates = {
        gate.key: stored.get(("DELIVERY", gate.key), {"passed": False, "label": gate.label, "kind": gate.kind,
                                                       "action": gate.missing})
        for gate in registry.stages["DELIVERY"].gates
    }
    docs = delivery_gates.get("REQUIRED_DOCUMENTS") or {}
    delivered = runtime is not None and str(runtime["delivery_completion_state"]) == "COMPLETE"
    delivery = {
        "passed": delivered or all(g.get("passed") for g in delivery_gates.values()),
        "requiredCount": docs.get("requiredCount", 0),
        "receivedCount": docs.get("receivedCount", 0),
        "missing": list(docs.get("missing") or []),
        "gates": delivery_gates,
    }
    minimum = (
        runtime["booking_minimum_amount"]
        if runtime is not None and runtime["booking_minimum_amount"] is not None
        else _minimum_booking_amount(connection, tenant_id=tenant_id)
    )
    return {
        "stage": str(runtime["current_stage"]) if runtime else "BOOKING_DOCUMENT_UPLOAD",
        "bookingCompletionState": str(runtime["booking_completion_state"]) if runtime else "IN_PROGRESS",
        "deliveryCompletionState": str(runtime["delivery_completion_state"]) if runtime else "IN_PROGRESS",
        "minimumBookingAmount": str(minimum),
        "bookingReceiptTotal": str(runtime["booking_receipt_total"] or 0) if runtime else "0",
        "manualVerificationPending": int(runtime["manual_verification_pending_count"] or 0) if runtime else 0,
        "factVersion": int(runtime["fact_version"] or 0) if runtime else 0,
        "gates": gates,
        "delivery": delivery,
        "stages": [
            {"code": state, "stage": code}
            for code, template in sorted(registry.stages.items(), key=lambda item: item[1].order)
            for state in template.states
        ],
    }
