"""Phase 2 tasks the SKU resolution raises (decision 2026-10-20).

* SKU_CLOSEST_MATCH_CONFIRM (PC) -- the model text matched no SKU name, so
  the closest price-master SKU is suggested, never pinned on its own. The PC
  accepts it (it is confirmed) or rejects it and picks the SKU by hand with
  the in-built model picker.
* SKU_OTHER_MONTH_PRICE_LIST (TL) -- the model is not in the Journey's own
  price list but is in another month's. No SKU is resolved until a Team
  Lead approves pricing the Journey on that list; once approved the
  Journey's pricing date moves to it and the SKU is resolved there.
* MODEL_CHANGED_BY_INVOICE (TL, information) -- the invoice named a
  different vehicle from the one on the Journey; the invoice is the source
  of truth, so the Journey now carries the invoice's SKU, and this says so.
* SKU_PRICE_MISMATCH (PC) -- 2026-10-20 product owner rule R3: a document's
  words name one SKU but its master ex-showroom differs from the document's
  (the booking form's quote, or a vehicle invoice's R1 ex-showroom). The SKU
  the booking named stays on the Journey as an automatic match so the deal
  still shows; the PC keeps it (Accept) or picks the right SKU.
* SKU_INVOICES_DISAGREE (PC) -- two vehicle invoices name different SKUs;
  neither sets the SKU. The PC picks one of the SKUs they name (Accept with
  the SKU) or marks one invoice cancelled (Reject with the invoice: it is
  voided). Keyed by the invoices and the SKUs they name, so it stays closed
  once decided for those documents and a new disagreement raises a new one.
* SKU_BOOKING_FORMS_DISAGREE (PC) -- two booking forms name different SKUs
  (review 471 S6: never "the newest wins"); the same two actions.
* SKU_NOT_SET (PC) -- 2026-10-21 product owner rule "a Journey left with
  no clearly defined SKU gets a PC task": the Journey has no SKU and the
  ladder could not even run (no document names the vehicle, no booking
  date). Closes itself once a SKU is on the Journey.
* SKU_BOOKING_FORM_DIFFERS (PC) -- a booking form names a different SKU
  from the one a PC / Team Lead set. The decision is never overwritten; the
  PC keeps it (Accept) or proposes a correction to the Team Lead.
* SKU_NOT_CONFIRMED (PC) -- an automatic SKU on the Journey that its
  documents no longer name (a re-check could not confirm it, or the booking
  form was corrected): the SKU is kept so the deal still shows, marked
  AUTO_UNCONFIRMED, and the PC keeps it (Accept) or sets another.
* SKU_INVOICE_REVIEW (TL) -- the vehicle invoice names a different SKU (at
  its master price) from the one a PC or Team Lead set: a human decision is
  never overwritten automatically; the Team Lead applies the invoice's SKU
  (Accept) or keeps the current one (Reject, with a reason).

Accept / approve / reject run here, called from uc03_p2_tasks.submit_action.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

CLOSEST_TASK = "SKU_CLOSEST_MATCH_CONFIRM"
OTHER_MONTH_TASK = "SKU_OTHER_MONTH_PRICE_LIST"
MODEL_CHANGED_TASK = "MODEL_CHANGED_BY_INVOICE"
PRICE_MISMATCH_TASK = "SKU_PRICE_MISMATCH"
INVOICES_DISAGREE_TASK = "SKU_INVOICES_DISAGREE"
INVOICE_REVIEW_TASK = "SKU_INVOICE_REVIEW"
BOOKING_FORMS_DISAGREE_TASK = "SKU_BOOKING_FORMS_DISAGREE"
NOT_CONFIRMED_TASK = "SKU_NOT_CONFIRMED"
NOT_SET_TASK = "SKU_NOT_SET"
BOOKING_FORM_DIFFERS_TASK = "SKU_BOOKING_FORM_DIFFERS"
SKU_TASK_TYPES = frozenset({
    CLOSEST_TASK, OTHER_MONTH_TASK, MODEL_CHANGED_TASK, PRICE_MISMATCH_TASK, INVOICES_DISAGREE_TASK,
    INVOICE_REVIEW_TASK, BOOKING_FORMS_DISAGREE_TASK, NOT_CONFIRMED_TASK, NOT_SET_TASK, BOOKING_FORM_DIFFERS_TASK,
})
# The tasks an automatic match leaves for the PC; a PC / TL decision settles
# all of them for good (review 471 B4).
_DECIDED_BY_HUMAN = (PRICE_MISMATCH_TASK, INVOICES_DISAGREE_TASK, BOOKING_FORMS_DISAGREE_TASK, NOT_CONFIRMED_TASK)
_NOT_CONFIRMED_TITLE = "SKU not clearly identified from the documents \u2014 confirm or set it"
_PRICING_BASIS = "TL_PRICE_LIST"


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _closest_key(journey_id: UUID) -> str:
    return f"sku-closest:{journey_id}"


def _other_month_key(journey_id: UUID) -> str:
    return f"sku-other-month:{journey_id}"


def _vehicle_label(model: Any, variant: Any, colour: Any) -> str:
    return " ".join(str(v) for v in (model, variant, colour) if v) or "-"


def raise_closest_match_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, source: str, read_model: str | None,
    read_variant: str | None, suggestion: dict[str, Any], price_list: str | None, reason: str | None = None,
) -> None:
    """Suggest the closest price-master SKU for the PC to accept or reject.
    ``reason`` says why the words named no SKU when the model itself was
    found ("no SKU of MAXX HD has these words")."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    document = "vehicle invoice" if source == "INVOICE" else "Booking Form"
    read = " / ".join(v for v in (read_model, read_variant) if v) or "-"
    suggested = _vehicle_label(suggestion["model_name"], suggestion["variant_name"], suggestion["colour_name"])
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=_closest_key(journey_id),
        task_type=CLOSEST_TASK,
        source_type="RESOLVER",
        source_code="SKU_CLOSEST_MATCH",
        title="Confirm the vehicle: closest price-master match",
        description=(
            (f"The {document} reads '{read}': {reason} in {price_list or 'the price list'}. "
             if reason else
             f"The {document} reads '{read}', which matches no model name in "
             f"{price_list or 'the price list'}. ")
            + f"The closest SKU is {suggested} ({suggestion['sku_code']}). "
            "Accept it if it is the vehicle on the document; reject it to pick the SKU yourself."
        ),
        reference={
            "sourceCode": "SKU_CLOSEST_MATCH",
            "leftValue": read,
            "rightValue": f"{suggested} ({suggestion['sku_code']})",
            "source": source,
            "suggestedSkuId": str(suggestion["product_sku_id"]),
            "suggestedSkuCode": str(suggestion["sku_code"]),
            "suggestedVehicle": suggested,
            "priceList": price_list,
        },
        severity="MEDIUM",
        registry=get_registry(),
    )


def raise_other_month_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, source: str, read_model: str | None,
    price_list: str | None, hits: list[dict[str, Any]],
) -> None:
    """Ask a Team Lead to approve pricing the Journey on the other month's
    list that holds the model (the most recent one)."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    hit = hits[0]
    document = "vehicle invoice" if source == "INVOICE" else "Booking Form"
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=_other_month_key(journey_id),
        task_type=OTHER_MONTH_TASK,
        source_type="RESOLVER",
        source_code="SKU_OTHER_MONTH_PRICE_LIST",
        title="Approve pricing on another month's price list",
        description=(
            f"The {document} model '{read_model or '-'}' is not in this Journey's price list "
            f"({price_list or '-'}) but is in {hit['priceList']} as '{hit['modelName']}' "
            f"({hit['skuCount']} SKUs). Approve to price this Journey on that list and resolve its SKU there; "
            "until then no SKU is resolved. Reject to leave the PC to pick the SKU."
        ),
        reference={
            "sourceCode": "SKU_OTHER_MONTH_PRICE_LIST",
            "leftValue": f"{read_model or '-'} (not in {price_list or '-'})",
            "rightValue": f"{hit['priceList']}: {hit['modelName']}",
            "source": source,
            "priceListVersionId": hit["priceListVersionId"],
            "priceList": hit["priceList"],
            "effectiveFrom": hit["effectiveFrom"],
            "modelName": hit["modelName"],
            "skuCodes": hit["skuCodes"],
            "otherPriceLists": hits,
            "currentPriceList": price_list,
        },
        severity="MEDIUM",
        registry=get_registry(),
    )


def raise_model_changed_observation(
    connection: Connection, *, tenant_id: str, journey_id: UUID, before: dict[str, Any], after: dict[str, Any],
) -> None:
    """Information for the Team Lead: the invoice named a different vehicle
    and the Journey now carries it. Also on the Journey's activity."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_runtime import record_activity
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    was = _vehicle_label(before.get("model"), before.get("variant"), before.get("colour"))
    now = _vehicle_label(after.get("model"), after.get("variant"), after.get("colour"))
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=f"sku-model-changed:{journey_id}:{after['skuId']}",
        task_type=MODEL_CHANGED_TASK,
        source_type="RESOLVER",
        source_code="MODEL_CHANGED_BY_INVOICE",
        title="Vehicle changed to the invoiced model",
        description=(
            f"The Journey had {was} ({before.get('skuCode') or '-'}); the invoice bills {now} "
            f"({after.get('skuCode') or '-'}). The invoice is the source of truth, so the Journey now carries "
            "the invoiced vehicle and is priced on it. For information."
        ),
        reference={
            "sourceCode": "MODEL_CHANGED_BY_INVOICE",
            "leftValue": f"{was} ({before.get('skuCode') or '-'})",
            "rightValue": f"{now} ({after.get('skuCode') or '-'})",
            "before": before,
            "after": after,
        },
        severity="INFO",
        registry=get_registry(),
    )
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="MODEL_CHANGED_BY_INVOICE",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"from": f"{was} ({before.get('skuCode') or '-'})", "to": f"{now} ({after.get('skuCode') or '-'})"},
        correlation_id=None,
    )


def _money_text(value: Any) -> str:
    """Rupees the way the dealers write them: Rs 8,52,000.00."""
    if value is None:
        return "not read"
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    whole, paise = f"{abs(amount):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"Rs {sign}{','.join([*groups, tail]) if groups else tail}.{paise}"


def _amount_key(value: Any) -> str:
    if value is None:
        return "-"
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _price_key(journey_id: UUID, source: str, document_id: str | None, sku_id: Any, master: Any,
               offered: Any) -> str:
    """One task per (Journey, document, SKU, prices) (review 471 B4): the
    same mismatch stays decided once the PC decided it, a different SKU or
    price on the same document is a new question."""
    return (f"sku-price:{journey_id}:{source}:{document_id or '-'}:{sku_id}:"
            f"{_amount_key(master)}:{_amount_key(offered)}")


def raise_price_mismatch_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, source: str, reading: dict[str, Any],
    row: dict[str, Any], master: Any, offered: Any, current_sku_id: Any = None,
) -> str:
    """Rule R3 (2026-10-20): the document's words name this SKU, but its
    master ex-showroom is not the document's (or the document's could not be
    read). The PC decides: Accept confirms the SKU this task names
    (``reference.skuId``), never whatever is on the Journey (review 471 B3).
    Returns the task's dedupe key."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    invoice = source == "INVOICE"
    document = (f"vehicle invoice {reading.get('invoiceNumber') or ''}".strip() if invoice else "booking form")
    vehicle = _vehicle_label(row.get("model_name"), row.get("variant_name"), None)
    read = " / ".join(str(v) for v in (reading.get("model"), reading.get("variant")) if v) or "-"
    other_vehicle = (invoice and current_sku_id is not None
                     and str(current_sku_id) != str(row.get("product_sku_id")))
    label = "invoice" if invoice else "booking"
    if not invoice:
        # 2026-10-21 product owner rule: the booking form's price differing
        # from (or missing against) the word-named SKU asks the PC to
        # confirm the model and price.
        title = ("Confirm the model and price: the booking form's ex-showroom was not read" if offered is None
                 else f"Confirm the model and price: master {_money_text(master)} vs booking "
                      f"{_money_text(offered)}")
    if offered is None:
        if invoice:
            title = f"SKU price could not be checked: the {document}'s ex-showroom was not read"
        reason = (f"The {document} reads '{read}', which names {vehicle} ({row.get('sku_code')}), but its "
                  f"ex-showroom price was not read, so the master price ({_money_text(master)}) could not be "
                  "checked against it.")
    else:
        if invoice:
            title = (f"SKU price differs from {label}: master {_money_text(master)} vs {label} "
                     f"{_money_text(offered)} -- set the SKU")
        reason = (f"The {document} reads '{read}', which names {vehicle} ({row.get('sku_code')}); its master "
                  f"ex-showroom is {_money_text(master)} but the {document} says {_money_text(offered)}.")
    if other_vehicle:
        title = (f"Vehicle invoice names a different vehicle from the booking: {vehicle} "
                 f"({row.get('sku_code')})" + ("" if offered is None else
                                               f", invoice {_money_text(offered)} vs master {_money_text(master)}"))
    key = _price_key(journey_id, source, reading.get("documentId"), row.get("product_sku_id"), master, offered)
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=key,
        task_type=PRICE_MISMATCH_TASK,
        source_type="RESOLVER",
        source_code="SKU_PRICE_MISMATCH",
        title=title,
        description=(
            reason
            + (" The Journey's SKU is a different vehicle; the invoice does not change it on its own. "
               if other_vehicle
               else " The vehicle stays as the booking named it until the PC decides. " if not invoice
               else " The invoice does not set the SKU. ")
            + f"A price never picks the variant: accept to confirm {vehicle} ({row.get('sku_code')}) if it is "
            "the vehicle sold, or pick the right one in the model picker."
        ),
        reference={
            "sourceCode": "SKU_PRICE_MISMATCH",
            "source": source,
            "documentId": reading.get("documentId"),
            "documentType": reading.get("documentType"),
            "invoiceNumber": reading.get("invoiceNumber"),
            "leftValue": f"master {_money_text(master)} ({row.get('sku_code')})",
            "rightValue": f"{label} {_money_text(offered)} ({reading.get('documentId') or '-'})",
            "skuId": str(row.get("product_sku_id")),
            "skuCode": row.get("sku_code"),
            "vehicle": vehicle,
            "differentFromJourney": bool(other_vehicle),
            "masterExShowroom": str(master) if master is not None else None,
            "documentExShowroom": str(offered) if offered is not None else None,
        },
        # 2026-10-22 product owner: where the SKU the booking form's words name has a
        # master ex-showroom that differs from the booking form's own, the PC validating
        # the model is a High priority task. A price that was not read, and the
        # invoice's own price check, stay Medium: they are not part of that rule.
        severity="HIGH" if (not invoice and offered is not None) else "MEDIUM",
        registry=get_registry(),
    )
    return key


def _open_task_keys(connection: Connection, *, tenant_id: str, journey_id: UUID, task_type: str) -> list[str]:
    from audit_core.uc03_p2_task_producer import _OPEN

    return [str(k) for k in connection.execute(
        text(
            "SELECT dedupe_key FROM auditcore.p2_tasks WHERE tenant_id = :t AND journey_id = :j "
            "AND task_type = :type AND task_status = ANY(:open)"
        ),
        {"t": tenant_id, "j": journey_id, "type": task_type, "open": list(_OPEN)},
    ).scalars().all() if k]


def resolve_price_mismatch_tasks(
    connection: Connection, *, tenant_id: str, journey_id: UUID, source: str | None, keep: set[str], reason: str,
) -> None:
    """Close the open SKU price tasks of one source (BOOKING / INVOICE, or
    every one when None) except ``keep``."""
    from audit_core.uc03_p2_task_producer import resolve_if_open

    prefix = f"sku-price:{journey_id}:" + (source or "")
    for key in _open_task_keys(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=PRICE_MISMATCH_TASK):
        if key.startswith(prefix) and key not in keep:
            resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                            evidence={"reason": reason})


def _disagree_key(prefix: str, journey_id: UUID, items: list[dict[str, Any]]) -> tuple[str, str]:
    pairs = sorted(f"{i.get('documentId')}={i.get('skuCode')}" for i in items)
    material = "; ".join(pairs)
    return f"{prefix}:{journey_id}:{hashlib.sha256(material.encode()).hexdigest()[:16]}", material


def _sku_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options: dict[str, dict[str, Any]] = {}
    for i in items:
        option = options.setdefault(str(i.get("skuId")), {
            "skuId": str(i.get("skuId")), "skuCode": i.get("skuCode"), "vehicle": i.get("vehicle"),
            "documentIds": [],
        })
        option["documentIds"].append(i.get("documentId"))
    return list(options.values())


def _raise_disagree(
    connection: Connection, *, tenant_id: str, journey_id: UUID, task_type: str, prefix: str,
    items: list[dict[str, Any]], title: str, what: str,
) -> str:
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    key, material = _disagree_key(prefix, journey_id, items)
    lines = "; ".join(
        f"{i.get('invoiceNumber') or i.get('documentType')}: {i.get('vehicle')} ({i.get('skuCode')})" for i in items
    )
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=key,
        task_type=task_type,
        source_type="RESOLVER",
        source_code=task_type,
        title=title,
        description=(
            f"The {what} on this Journey name different vehicles -- {lines}. None of them sets the SKU. "
            f"Pick the vehicle sold (one of these SKUs), or mark the {what[:-1]} that was cancelled; "
            "any other SKU can be set in the model picker."
        ),
        reference={
            "sourceCode": task_type,
            "leftValue": material,
            "rightValue": None,
            "documents": items,
            "skuOptions": _sku_options(items),
            "documentIds": [i.get("documentId") for i in items],
        },
        severity="MEDIUM",
        registry=get_registry(),
    )
    return key


def raise_invoices_disagree_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, invoices: list[dict[str, Any]],
) -> str:
    """Vehicle invoices on one Journey name different vehicles (rule R3):
    none of them sets the SKU; the PC picks it or marks one cancelled."""
    return _raise_disagree(
        connection, tenant_id=tenant_id, journey_id=journey_id, task_type=INVOICES_DISAGREE_TASK,
        prefix="sku-invoices-disagree", items=invoices,
        title="Vehicle invoices name different vehicles: set the SKU", what="vehicle invoices",
    )


def raise_booking_forms_disagree_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, forms: list[dict[str, Any]],
) -> str:
    """Booking forms on one Journey name different vehicles (review 471 S6):
    none of them sets the SKU; the PC picks it or marks one cancelled."""
    return _raise_disagree(
        connection, tenant_id=tenant_id, journey_id=journey_id, task_type=BOOKING_FORMS_DISAGREE_TASK,
        prefix="sku-booking-forms-disagree", items=forms,
        title="Booking forms name different vehicles: set the SKU", what="booking forms",
    )


def _resolve_open(
    connection: Connection, *, tenant_id: str, journey_id: UUID, task_type: str, keep: set[str], reason: str,
) -> None:
    from audit_core.uc03_p2_task_producer import resolve_if_open

    for key in _open_task_keys(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=task_type):
        if key not in keep:
            resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                            evidence={"reason": reason})


def resolve_invoices_disagree_task(connection: Connection, *, tenant_id: str, journey_id: UUID,
                                   keep: set[str] | None = None) -> None:
    _resolve_open(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=INVOICES_DISAGREE_TASK,
                  keep=keep or set(), reason="The vehicle invoices no longer name different vehicles")


def resolve_booking_forms_disagree_task(connection: Connection, *, tenant_id: str, journey_id: UUID,
                                        keep: set[str] | None = None) -> None:
    _resolve_open(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=BOOKING_FORMS_DISAGREE_TASK,
                  keep=keep or set(), reason="The booking forms no longer name different vehicles")


def _not_confirmed_key(journey_id: UUID, sku_id: Any) -> str:
    return f"sku-not-confirmed:{journey_id}:{sku_id}"


def raise_not_confirmed_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, sku_id: Any, sku_code: str | None,
    vehicle: str | None, why: str,
) -> str:
    """The automatic SKU stays on the Journey (the deal still shows), but
    its documents do not name it: the PC confirms it or sets another."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    key = _not_confirmed_key(journey_id, sku_id)
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=key,
        task_type=NOT_CONFIRMED_TASK,
        source_type="RESOLVER",
        source_code="SKU_NOT_CONFIRMED",
        title=_NOT_CONFIRMED_TITLE,
        description=(
            f"The Journey carries {vehicle or '-'} ({sku_code or '-'}), matched automatically, but {why}. "
            "It stays on the Journey so the deal still shows. Accept to confirm it if it is the vehicle sold, "
            "or set the right SKU in the model picker."
        ),
        reference={
            "sourceCode": "SKU_NOT_CONFIRMED",
            "leftValue": f"{vehicle or '-'} ({sku_code or '-'})",
            "rightValue": why,
            "skuId": str(sku_id),
            "skuCode": sku_code,
            "vehicle": vehicle,
        },
        severity="MEDIUM",
        registry=get_registry(),
    )
    return key


def resolve_not_confirmed_task(connection: Connection, *, tenant_id: str, journey_id: UUID, reason: str) -> None:
    _resolve_open(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=NOT_CONFIRMED_TASK,
                  keep=set(), reason=reason)


def raise_invoice_review_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, current_sku_code: str | None,
    current_basis: str | None, invoice_row: dict[str, Any], invoice: dict[str, Any], evidence: dict[str, Any],
) -> str:
    """The vehicle invoice names a different SKU, at its master price, from
    the one a PC or Team Lead set. A human decision is never overwritten
    automatically: the Team Lead applies the invoice's SKU or keeps it.
    Returns the task's dedupe key (the caller keeps it open and closes the
    others)."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    vehicle = _vehicle_label(invoice_row.get("model_name"), invoice_row.get("variant_name"), None)
    who = "the Team Lead" if current_basis == "TL_APPROVED" else "the PC"
    key = f"sku-invoice-review:{journey_id}:{invoice_row.get('product_sku_id')}"
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=key,
        task_type=INVOICE_REVIEW_TASK,
        source_type="RESOLVER",
        source_code="SKU_INVOICE_REVIEW",
        title="Vehicle invoice names a different SKU from the confirmed one",
        description=(
            f"The SKU on this Journey ({current_sku_code or '-'}) was set by {who}. Vehicle invoice "
            f"{invoice.get('invoiceNumber') or ''} names {vehicle} ({invoice_row.get('sku_code')}) and its "
            f"ex-showroom ({evidence.get('documentExShowroom')}) equals that SKU's master price. Accept to price "
            "the Journey on the invoice's SKU; reject (with the reason) to keep the confirmed one."
        ),
        reference={
            "sourceCode": "SKU_INVOICE_REVIEW",
            "leftValue": current_sku_code,
            "rightValue": f"{vehicle} ({invoice_row.get('sku_code')})",
            "invoiceSkuId": str(invoice_row.get("product_sku_id")),
            "invoiceSkuCode": invoice_row.get("sku_code"),
            "currentSkuCode": current_sku_code,
            "documentId": invoice.get("documentId"),
            "invoiceNumber": invoice.get("invoiceNumber"),
            "evidence": evidence,
        },
        severity="HIGH",
        registry=get_registry(),
    )
    return key


def resolve_invoice_review_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, keep: set[str] | None = None,
    reason: str = "The invoice and the Journey's SKU agree",
) -> None:
    """Close the open invoice-review tasks except ``keep`` (the ones the
    current invoices still call for): a review of a SKU no invoice names any
    more must not stay for the Team Lead to accept."""
    from audit_core.uc03_p2_task_producer import resolve_if_open

    for key in _open_task_keys(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=INVOICE_REVIEW_TASK):
        if key not in (keep or set()):
            resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                            evidence={"reason": reason})


def resolve_automatic_match_tasks(
    connection: Connection, *, tenant_id: str, journey_id: UUID, reason: str, skip_task_id: Any = None,
    actor_id: str | None = None,
) -> int:
    """A PC or Team Lead set the SKU: the price checks, the documents'
    disagreements and the "not confirmed" question have nothing left to
    decide. They are closed as decided -- with the issue they asked about
    (``acceptedIssueHash``) -- so the same question is never raised again
    for the same documents and prices (review 471 B4). ``skip_task_id`` is
    the task whose own action made the decision (it is closed by it)."""
    from audit_core.uc03_p2_task_producer import _OPEN
    from audit_core.uc03_p2_tasks import record_task_event

    rows = connection.execute(
        text(
            "SELECT task_id, reference FROM auditcore.p2_tasks WHERE tenant_id = :t AND journey_id = :j "
            "AND task_type = ANY(:types) AND task_status = ANY(:open) FOR UPDATE"
        ),
        {"t": tenant_id, "j": journey_id, "types": list(_DECIDED_BY_HUMAN), "open": list(_OPEN)},
    ).mappings().all()
    closed = 0
    for row in rows:
        if skip_task_id is not None and str(row["task_id"]) == str(skip_task_id):
            continue
        reference = dict(row["reference"] or {})
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(),
                    completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:t AND task_id=:id
                """
            ),
            {"t": tenant_id, "id": row["task_id"], "result": _json({
                "outcome": "DECIDED", "reason": reason, "by": actor_id, "at": datetime.now(UTC).isoformat(),
                "acceptedIssueHash": reference.get("issueHash"),
            })},
        )
        record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=UUID(str(row["task_id"])),
                          event_type="COMPLETED", actor_id=actor_id or "SYSTEM", actor_role_code="SYSTEM",
                          comment=None, details={"outcome": "DECIDED", "reason": reason})
        closed += 1
    return closed


def _not_set_key(journey_id: UUID) -> str:
    return f"sku-not-set:{journey_id}"


def raise_not_set_task(connection: Connection, *, tenant_id: str, journey_id: UUID, why: str, reason: str) -> None:
    """No SKU on the Journey and nothing to identify it from yet: the PC is
    asked to supply what is missing or set the SKU (2026-10-21)."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=_not_set_key(journey_id),
        task_type=NOT_SET_TASK,
        source_type="RESOLVER",
        source_code="SKU_NOT_SET",
        title="No SKU on this Journey \u2014 set the vehicle",
        description=(
            f"The Journey has no SKU: {why}. The deal cannot be checked against the price and discount masters "
            "until it has one. Upload or correct the booking form or vehicle invoice, or set the SKU in the "
            "model picker. This task closes itself once a SKU is on the Journey."
        ),
        reference={"sourceCode": "SKU_NOT_SET", "leftValue": reason, "rightValue": why},
        severity="MEDIUM",
        registry=get_registry(),
    )


def raise_booking_form_differs_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, current_sku_code: str | None,
    current_basis: str | None, forms: list[dict[str, Any]],
) -> str:
    """A booking form names a different SKU from the one a PC or Team Lead
    set: the decision stays (never overwritten); the PC is told."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    key, material = _disagree_key("sku-booking-form-differs", journey_id, forms)
    who = "the Team Lead" if current_basis == "TL_APPROVED" else "the PC"
    lines = "; ".join(f"{f.get('vehicle')} ({f.get('skuCode')})" for f in forms)
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=key,
        task_type=BOOKING_FORM_DIFFERS_TASK,
        source_type="RESOLVER",
        source_code="SKU_BOOKING_FORM_DIFFERS",
        title="A booking form names a different vehicle from the confirmed SKU",
        description=(
            f"The SKU on this Journey ({current_sku_code or '-'}) was set by {who} and stays as it is. A booking "
            f"form on file names {lines}. Keep the confirmed vehicle if it is the one sold, or propose a "
            "correction for the Team Lead from the Deal tab."
        ),
        reference={"sourceCode": "SKU_BOOKING_FORM_DIFFERS", "leftValue": current_sku_code, "rightValue": material,
                   "documents": forms, "documentIds": [f.get("documentId") for f in forms]},
        severity="MEDIUM",
        registry=get_registry(),
    )
    return key


def resolve_booking_form_differs_task(connection: Connection, *, tenant_id: str, journey_id: UUID,
                                      keep: set[str] | None = None) -> None:
    _resolve_open(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=BOOKING_FORM_DIFFERS_TASK,
                  keep=keep or set(), reason="The booking forms name the confirmed SKU")


def resolve_sku_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID, sku_code: str | None) -> None:
    """A SKU is on the Journey: the suggestion, the price-list approval and
    the "no SKU" question have nothing left to decide."""
    from audit_core.uc03_p2_task_producer import resolve_if_open

    for key in (_closest_key(journey_id), _other_month_key(journey_id), _not_set_key(journey_id)):
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                        evidence={"skuCode": sku_code, "reason": "SKU resolved"})


def _close(
    connection: Connection, *, tenant_id: str, task: dict[str, Any], outcome: str, actor_id: str, role: str,
    comment: str | None, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from audit_core.uc03_p2_tasks import record_task_event

    reference = dict(task["reference"] or {})
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(),
                completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:result AS jsonb),
                updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND task_id=:task_id
            """
        ),
        {"tenant_id": tenant_id, "task_id": task["task_id"], "result": _json({
            "outcome": outcome, "by": actor_id, "role": role, "at": datetime.now(UTC).isoformat(),
            "comment": comment,
            # The same suggestion or request is never raised again once decided.
            "acceptedIssueHash": reference.get("issueHash"),
            **(extra or {}),
        })},
    )
    record_task_event(
        connection, tenant_id=tenant_id, journey_id=UUID(str(task["journey_id"])), task_id=UUID(str(task["task_id"])),
        event_type="COMPLETED", actor_id=actor_id, actor_role_code=role, comment=comment,
        details={"outcome": outcome, **(extra or {})},
    )
    return {"taskId": str(task["task_id"]), "status": "VERIFIED_COMPLETE", "outcome": outcome}


def _confirm(connection: Connection, *, tenant_id: str, journey_id: UUID, sku_id: Any, actor_id: str,
             task: dict[str, Any]) -> None:
    """The PC's decision on a SKU task: the SKU the task names (validated
    against the Journey's price list by ``confirm_model_resolution_sku``)."""
    from audit_core.uc03_model_resolution import confirm_model_resolution_sku

    if not sku_id:
        raise ValueError("This task names no SKU to confirm; pick one in the model picker")
    confirm_model_resolution_sku(connection, tenant_id=tenant_id, journey_id=journey_id,
                                 product_sku_id=UUID(str(sku_id)), actor_id=actor_id, correlation_id="",
                                 task_id=task["task_id"])


def handle_sku_task_action(
    connection: Connection, *, tenant_id: str, task: dict[str, Any], action: str, actor_id: str,
    actor_role_code: str, comment: str | None, details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Accept / reject on a SKU task; None when the action is not one of them."""
    task_type = str(task["task_type"])
    journey_id = UUID(str(task["journey_id"]))
    reference = dict(task["reference"] or {})
    role = str(actor_role_code or "").upper()
    comment = (comment or "").strip() or None
    details = dict(details or {})

    if task_type == CLOSEST_TASK and action == "ACCEPT":
        _confirm(connection, tenant_id=tenant_id, journey_id=journey_id, sku_id=reference.get("suggestedSkuId"),
                 actor_id=actor_id, task=task)
        return _close(connection, tenant_id=tenant_id, task=task, outcome="SKU_ACCEPTED", actor_id=actor_id,
                      role=role, comment=comment, extra={"skuCode": reference.get("suggestedSkuCode")})
    if task_type == CLOSEST_TASK and action == "REJECT":
        # The PC picks the SKU by hand (the in-built model picker); the
        # MODEL_NOT_IDENTIFIED gap stays open until one is on the Journey.
        return _close(connection, tenant_id=tenant_id, task=task, outcome="SUGGESTION_REJECTED", actor_id=actor_id,
                      role=role, comment=comment, extra={"next": "PICK_SKU"})

    if task_type == OTHER_MONTH_TASK and action in {"ACCEPT", "REJECT"}:
        if role not in {"TL", "PM"}:
            from audit_core.uc03_p2_tasks import TaskPermissionError

            raise TaskPermissionError("Only a Team Lead can approve pricing on another month's price list")
        if action == "REJECT":
            if not comment:
                raise ValueError("Give a reason for rejecting")
            return _close(connection, tenant_id=tenant_id, task=task, outcome="PRICE_LIST_REJECTED",
                          actor_id=actor_id, role=role, comment=comment)
        outcome = approve_other_month_price_list(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            price_list_version_id=UUID(str(reference["priceListVersionId"])), actor_id=actor_id,
            reason=comment or f"Team Lead approved {reference.get('priceList')}",
        )
        return _close(connection, tenant_id=tenant_id, task=task, outcome="PRICE_LIST_APPROVED", actor_id=actor_id,
                      role=role, comment=comment, extra=outcome)

    if task_type in {PRICE_MISMATCH_TASK, NOT_CONFIRMED_TASK} and action == "ACCEPT":
        # "Keep this vehicle": the SKU this task names becomes the PC's
        # decision -- never whatever is on the Journey now (review 471 B3).
        _confirm(connection, tenant_id=tenant_id, journey_id=journey_id, sku_id=reference.get("skuId"),
                 actor_id=actor_id, task=task)
        return _close(connection, tenant_id=tenant_id, task=task, outcome="SKU_ACCEPTED", actor_id=actor_id,
                      role=role, comment=comment,
                      extra={"productSkuId": reference.get("skuId"), "skuCode": reference.get("skuCode")})

    if task_type in {INVOICES_DISAGREE_TASK, BOOKING_FORMS_DISAGREE_TASK} and action == "ACCEPT":
        # The PC picks the vehicle sold among the SKUs the documents name.
        options = {str(o.get("skuId")): o for o in reference.get("skuOptions") or []}
        chosen = str(details.get("skuId") or details.get("productSkuId") or "")
        if chosen not in options:
            raise ValueError("Pick one of the SKUs the documents name (or set another in the model picker)")
        _confirm(connection, tenant_id=tenant_id, journey_id=journey_id, sku_id=chosen, actor_id=actor_id, task=task)
        return _close(connection, tenant_id=tenant_id, task=task, outcome="SKU_PICKED", actor_id=actor_id,
                      role=role, comment=comment,
                      extra={"productSkuId": chosen, "skuCode": options[chosen].get("skuCode")})
    if task_type in {INVOICES_DISAGREE_TASK, BOOKING_FORMS_DISAGREE_TASK} and action == "REJECT":
        # "This one was cancelled": the document is voided (as from the
        # Documents tab) and the SKU is resolved again from the rest.
        from audit_core.uc03_model_resolution import sync_model_resolution
        from audit_core.uc03_p2_documents import void_p2_document

        document_id = str(details.get("documentId") or "")
        if document_id not in {str(d) for d in reference.get("documentIds") or []}:
            raise ValueError("Say which of the task's documents was cancelled")
        kind = "INVOICE" if task_type == INVOICES_DISAGREE_TASK else "BOOKING_FORM"
        void_p2_document(connection, tenant_id=tenant_id, journey_id=journey_id, document_id=UUID(document_id),
                         actor_id=actor_id, activity_details={"reason": f"{kind}_CANCELLED_PER_SKU_TASK",
                                                              "taskId": str(task["task_id"])})
        closed = _close(connection, tenant_id=tenant_id, task=task, outcome="DOCUMENT_CANCELLED", actor_id=actor_id,
                        role=role, comment=comment, extra={"documentId": document_id})
        sync_model_resolution(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id="")
        return closed

    if task_type == BOOKING_FORM_DIFFERS_TASK and action == "ACCEPT":
        # The PC keeps the confirmed vehicle: the same forms never ask again.
        return _close(connection, tenant_id=tenant_id, task=task, outcome="CONFIRMED_SKU_KEPT", actor_id=actor_id,
                      role=role, comment=comment)

    if task_type == INVOICE_REVIEW_TASK and action in {"ACCEPT", "REJECT"}:
        if role not in {"TL", "PM"}:
            from audit_core.uc03_p2_tasks import TaskPermissionError

            raise TaskPermissionError("Only a Team Lead can change a confirmed SKU")
        if action == "REJECT":
            if not comment:
                raise ValueError("Give a reason for keeping the confirmed SKU")
            return _close(connection, tenant_id=tenant_id, task=task, outcome="CONFIRMED_SKU_KEPT",
                          actor_id=actor_id, role=role, comment=comment)
        from audit_core.uc03_model_resolution import (
            _INVOICE_TL_METHOD,
            BASIS_TL,
            _run_deal_reconciliation,
            reassign_confirmed_sku,
        )

        reassign_confirmed_sku(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            product_sku_id=UUID(str(reference["invoiceSkuId"])), method=_INVOICE_TL_METHOD, basis=BASIS_TL,
            evidence={**(reference.get("evidence") or {}), "source": "TL", "actorId": actor_id,
                      "decidedAtUtc": datetime.now(UTC).isoformat()},
        )
        _run_deal_reconciliation(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id="")
        return _close(connection, tenant_id=tenant_id, task=task, outcome="INVOICE_SKU_APPLIED", actor_id=actor_id,
                      role=role, comment=comment, extra={"skuCode": reference.get("invoiceSkuCode")})

    if task_type == MODEL_CHANGED_TASK and action == "COMPLETE_ACTION":
        return _close(connection, tenant_id=tenant_id, task=task, outcome="NOTED", actor_id=actor_id, role=role,
                      comment=comment)
    return None


def approve_other_month_price_list(
    connection: Connection, *, tenant_id: str, journey_id: UUID, price_list_version_id: UUID, actor_id: str,
    reason: str,
) -> dict[str, Any]:
    """Price the Journey on the approved list: its pricing date moves to the
    day that list version took effect (and to that list, when the Booking
    names one), then the SKU is resolved against it."""
    from audit_core.errors import AuditCoreError
    from audit_core.uc03_model_resolution import sync_model_resolution
    from audit_core.uc03_p2_runtime import note_facts_changed, record_activity
    from audit_core.uc03_sku_candidates import _price_plan_for_journey

    version = connection.execute(
        text(
            """
            SELECT plv.price_list_id, plv.effective_from, plv.version_no, pl.price_list_name
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl ON pl.tenant_id=plv.tenant_id AND pl.price_list_id=plv.price_list_id
            WHERE plv.tenant_id=:t AND plv.price_list_version_id=:v AND plv.lifecycle_status='PUBLISHED'
            """
        ),
        {"t": tenant_id, "v": price_list_version_id},
    ).mappings().one_or_none()
    if version is None or version["effective_from"] is None:
        raise ValueError("That price list is no longer published")
    previous = connection.execute(
        text("SELECT pricing_effective_on, booking_date FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (tenant_id, journey_id, pricing_effective_on, pricing_basis,
                                            pricing_reason, pricing_set_by_actor_id, pricing_set_at_utc,
                                            pricing_price_list_version_id)
            VALUES (:t, :j, :on, :basis, :reason, :actor, now(), :version)
            ON CONFLICT (tenant_id, journey_id) DO UPDATE
               SET pricing_effective_on=EXCLUDED.pricing_effective_on,
                   pricing_basis=EXCLUDED.pricing_basis,
                   pricing_reason=EXCLUDED.pricing_reason,
                   pricing_set_by_actor_id=EXCLUDED.pricing_set_by_actor_id,
                   pricing_set_at_utc=now(),
                   price_list_id=CASE WHEN auditcore.bookings.price_list_id IS NULL THEN NULL
                                      ELSE :price_list END,
                   pricing_price_list_version_id=EXCLUDED.pricing_price_list_version_id,
                   updated_at_utc=now()
            """
        ),
        {"t": tenant_id, "j": journey_id, "on": version["effective_from"], "basis": _PRICING_BASIS,
         "reason": reason[:1000], "actor": actor_id, "price_list": version["price_list_id"],
         "version": price_list_version_id},
    )
    try:
        plan = _price_plan_for_journey(
            connection, tenant_id=tenant_id, journey_id=journey_id, effective_on=version["effective_from"],
        )
    except AuditCoreError as exc:
        raise ValueError("No price list is effective on that date") from exc
    if str(plan["price_list_version_id"]) != str(price_list_version_id):
        raise ValueError(
            f"Another price list takes precedence on {version['effective_from']}; this Journey cannot be priced on "
            f"{version['price_list_name']} v{version['version_no']} by its date."
        )
    result = sync_model_resolution(connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id="")
    record_activity(
        connection, tenant_id=tenant_id, journey_id=journey_id, event_type="PRICING_DATE_CHANGED",
        subject_type="JOURNEY", subject_id=str(journey_id),
        details={"from": str((previous or {}).get("pricing_effective_on") or (previous or {}).get("booking_date") or ""),
                 "to": str(version["effective_from"]), "basis": _PRICING_BASIS, "reason": reason},
        correlation_id=None,
    )
    note_facts_changed(connection, tenant_id=tenant_id, journey_id=journey_id,
                       reason="PRICING_DATE_CHANGED", correlation_id=None)
    return {"pricingEffectiveOn": str(version["effective_from"]), "skuResolved": bool(result.get("resolved")),
            "skuCode": result.get("skuCode")}
