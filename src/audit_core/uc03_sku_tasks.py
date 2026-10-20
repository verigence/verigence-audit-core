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

Accept / approve / reject run here, called from uc03_p2_tasks.submit_action.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

CLOSEST_TASK = "SKU_CLOSEST_MATCH_CONFIRM"
OTHER_MONTH_TASK = "SKU_OTHER_MONTH_PRICE_LIST"
MODEL_CHANGED_TASK = "MODEL_CHANGED_BY_INVOICE"
SKU_TASK_TYPES = frozenset({CLOSEST_TASK, OTHER_MONTH_TASK, MODEL_CHANGED_TASK})
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
    read_variant: str | None, suggestion: dict[str, Any], price_list: str | None,
) -> None:
    """Suggest the closest price-master SKU for the PC to accept or reject."""
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
            f"The {document} reads '{read}', which matches no model name in "
            f"{price_list or 'the price list'}. The closest SKU is {suggested} ({suggestion['sku_code']}). "
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
                                            pricing_reason, pricing_set_by_actor_id, pricing_set_at_utc)
            VALUES (:t, :j, :on, :basis, :reason, :actor, now())
            ON CONFLICT (tenant_id, journey_id) DO UPDATE
               SET pricing_effective_on=EXCLUDED.pricing_effective_on,
                   pricing_basis=EXCLUDED.pricing_basis,
                   pricing_reason=EXCLUDED.pricing_reason,
                   pricing_set_by_actor_id=EXCLUDED.pricing_set_by_actor_id,
                   pricing_set_at_utc=now(),
                   price_list_id=CASE WHEN auditcore.bookings.price_list_id IS NULL THEN NULL
                                      ELSE :price_list END,
                   updated_at_utc=now()
            """
        ),
        {"t": tenant_id, "j": journey_id, "on": version["effective_from"], "basis": _PRICING_BASIS,
         "reason": reason[:1000], "actor": actor_id, "price_list": version["price_list_id"]},
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
