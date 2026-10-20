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
  neither sets the SKU, the PC picks it.
* SKU_INVOICE_REVIEW (TL) -- the vehicle invoice names a different SKU (at
  its master price) from the one a PC or Team Lead set: a human decision is
  never overwritten automatically; the Team Lead applies the invoice's SKU
  (Accept) or keeps the current one (Reject, with a reason).

Accept / approve / reject run here, called from uc03_p2_tasks.submit_action.
"""
from __future__ import annotations

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
SKU_TASK_TYPES = frozenset({
    CLOSEST_TASK, OTHER_MONTH_TASK, MODEL_CHANGED_TASK, PRICE_MISMATCH_TASK, INVOICES_DISAGREE_TASK,
    INVOICE_REVIEW_TASK,
})
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


def _price_key(journey_id: UUID, source: str, document_id: str | None) -> str:
    if source == "INVOICE":
        return f"sku-price:{journey_id}:INVOICE:{document_id}"
    return f"sku-price:{journey_id}:BOOKING"


def raise_price_mismatch_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, source: str, reading: dict[str, Any],
    row: dict[str, Any], master: Any, offered: Any,
) -> str:
    """Rule R3 (2026-10-20): the document's words name this SKU, but its
    master ex-showroom is not the document's. The PC sets the SKU. Returns
    the task's dedupe key."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    invoice = source == "INVOICE"
    document = (f"vehicle invoice {reading.get('invoiceNumber') or ''}".strip() if invoice else "booking form")
    vehicle = _vehicle_label(row.get("model_name"), row.get("variant_name"), None)
    read = " / ".join(str(v) for v in (reading.get("model"), reading.get("variant")) if v) or "-"
    if offered is None:
        title = "SKU price could not be compared with the booking: set the SKU"
        reason = (f"The {document}'s ex-showroom price was not read, so the master price of {vehicle} "
                  f"({_money_text(master)}) could not be checked against it.")
    else:
        label = "invoice" if invoice else "booking"
        title = (f"SKU price differs from {label}: master {_money_text(master)} vs {label} "
                 f"{_money_text(offered)} -- set the SKU")
        reason = (f"The {document} reads '{read}', which names {vehicle} ({row.get('sku_code')}); its master "
                  f"ex-showroom is {_money_text(master)} but the {document} says {_money_text(offered)}.")
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=_price_key(journey_id, source, reading.get("documentId")),
        task_type=PRICE_MISMATCH_TASK,
        source_type="RESOLVER",
        source_code="SKU_PRICE_MISMATCH",
        title=title,
        description=(
            reason + (" The vehicle stays as the booking named it until the PC decides. " if not invoice
                      else " The invoice does not set the SKU. ")
            + "A price never picks the variant: accept this SKU if it is the vehicle sold, or pick the right one "
            "in the model picker."
        ),
        reference={
            "sourceCode": "SKU_PRICE_MISMATCH",
            "source": source,
            "documentId": reading.get("documentId"),
            "documentType": reading.get("documentType"),
            "invoiceNumber": reading.get("invoiceNumber"),
            "leftValue": f"master {_money_text(master)}",
            "rightValue": f"{'invoice' if invoice else 'booking'} {_money_text(offered)}",
            "skuId": str(row.get("product_sku_id")),
            "skuCode": row.get("sku_code"),
            "vehicle": vehicle,
            "masterExShowroom": str(master) if master is not None else None,
            "documentExShowroom": str(offered) if offered is not None else None,
        },
        severity="MEDIUM",
        registry=get_registry(),
    )
    return _price_key(journey_id, source, reading.get("documentId"))


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


def raise_invoices_disagree_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, invoices: list[dict[str, Any]],
) -> None:
    """Vehicle invoices on one Journey name different vehicles (rule R3):
    none of them sets the SKU; the PC picks it."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    lines = "; ".join(
        f"{i.get('invoiceNumber') or i.get('documentType')}: {i.get('vehicle')} ({i.get('skuCode')})" for i in invoices
    )
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=f"sku-invoices-disagree:{journey_id}",
        task_type=INVOICES_DISAGREE_TASK,
        source_type="RESOLVER",
        source_code="SKU_INVOICES_DISAGREE",
        title="Vehicle invoices name different vehicles: set the SKU",
        description=(
            f"The vehicle invoices on this Journey name different vehicles -- {lines}. Neither sets the SKU. "
            "Check which invoice is the sale (a cancelled one should be voided) and pick the SKU in the model picker."
        ),
        reference={
            "sourceCode": "SKU_INVOICES_DISAGREE",
            "leftValue": invoices[0].get("skuCode") if invoices else None,
            "rightValue": invoices[1].get("skuCode") if len(invoices) > 1 else None,
            "invoices": invoices,
        },
        severity="MEDIUM",
        registry=get_registry(),
    )


def resolve_invoices_disagree_task(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    from audit_core.uc03_p2_task_producer import resolve_if_open

    resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                    dedupe_key=f"sku-invoices-disagree:{journey_id}",
                    evidence={"reason": "The vehicle invoices no longer name different vehicles"})


def raise_invoice_review_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, current_sku_code: str | None,
    current_basis: str | None, invoice_row: dict[str, Any], invoice: dict[str, Any], evidence: dict[str, Any],
) -> None:
    """The vehicle invoice names a different SKU, at its master price, from
    the one a PC or Team Lead set. A human decision is never overwritten
    automatically: the Team Lead applies the invoice's SKU or keeps it."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    vehicle = _vehicle_label(invoice_row.get("model_name"), invoice_row.get("variant_name"), None)
    who = "the Team Lead" if current_basis == "TL_APPROVED" else "the PC"
    raise_or_refresh(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        dedupe_key=f"sku-invoice-review:{journey_id}:{invoice_row.get('product_sku_id')}",
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


def resolve_invoice_review_task(connection: Connection, *, tenant_id: str, journey_id: UUID) -> None:
    from audit_core.uc03_p2_task_producer import resolve_if_open

    for key in _open_task_keys(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=INVOICE_REVIEW_TASK):
        resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                        evidence={"reason": "The invoice and the Journey's SKU agree"})


def resolve_automatic_match_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID, reason: str) -> None:
    """A PC or Team Lead set the SKU: the price checks and the invoices'
    disagreement have nothing left to decide."""
    resolve_price_mismatch_tasks(connection, tenant_id=tenant_id, journey_id=journey_id, source=None, keep=set(),
                                 reason=reason)
    resolve_invoices_disagree_task(connection, tenant_id=tenant_id, journey_id=journey_id)


def resolve_sku_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID, sku_code: str | None) -> None:
    """A SKU is on the Journey: the suggestion and the price-list approval
    have nothing left to decide."""
    from audit_core.uc03_p2_task_producer import resolve_if_open

    for key in (_closest_key(journey_id), _other_month_key(journey_id)):
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


def handle_sku_task_action(
    connection: Connection, *, tenant_id: str, task: dict[str, Any], action: str, actor_id: str,
    actor_role_code: str, comment: str | None,
) -> dict[str, Any] | None:
    """Accept / reject on a SKU task; None when the action is not one of them."""
    task_type = str(task["task_type"])
    journey_id = UUID(str(task["journey_id"]))
    reference = dict(task["reference"] or {})
    role = str(actor_role_code or "").upper()
    comment = (comment or "").strip() or None

    if task_type == CLOSEST_TASK and action == "ACCEPT":
        from audit_core.uc03_model_resolution import confirm_model_resolution_sku

        confirm_model_resolution_sku(
            connection, tenant_id=tenant_id, journey_id=journey_id,
            product_sku_id=UUID(str(reference["suggestedSkuId"])), actor_id=actor_id, correlation_id="",
        )
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

    if task_type == PRICE_MISMATCH_TASK and action == "ACCEPT":
        # The PC says the SKU on the Journey is the vehicle sold: it becomes
        # the PC's decision (CONFIRMED), which also closes this task.
        from audit_core.uc03_model_resolution import confirm_model_resolution_sku

        sku_id = connection.execute(
            text("SELECT product_sku_id FROM auditcore.journey_products WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one_or_none()
        if sku_id is None:
            raise ValueError("There is no SKU on the Journey to accept; pick one in the model picker")
        confirm_model_resolution_sku(connection, tenant_id=tenant_id, journey_id=journey_id,
                                     product_sku_id=UUID(str(sku_id)), actor_id=actor_id, correlation_id="")
        return _close(connection, tenant_id=tenant_id, task=task, outcome="SKU_ACCEPTED", actor_id=actor_id,
                      role=role, comment=comment, extra={"productSkuId": str(sku_id)})

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
