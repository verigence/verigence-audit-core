"""uc03_backfill_document_sync_producers.py — retroactively run the newer
durable-state-driven UC03 producers against every existing Journey.

Every producer wired into the per-document sync trigger only ever runs when
a *new* document sync happens (a fresh DI webhook callback, PC Review
Confirm, or a manual /resync call on one Journey). A Journey whose documents
were all confirmed *before* a given producer was added never gets it run at
all -- not because the producer is broken, but because nothing ever
retriggered the sync pipeline for it. Confirmed directly: no flags/updated
fields appearing on old, already-processed deliveries after shipping the
delivery-date, SKU-invoice-fallback, customer/dealer-identity, and
duplicate-receipt producers.

This runs the same four producers directly against durable storage --
none of them need a fresh DI call, all four are idempotent/self-healing by
design, so this is safe to run repeatedly and cheap (no DI traffic at all):

  1. materialize_delivery_documents_from_durable_store -- delivery date
     (and every other Delivery materializer: insurance, vehicle,
     registration, finance, invoices, receipts) re-derived from whatever is
     already durably stored.
  2. sync_model_resolution_from_invoice -- SKU fallback from a Delivery
     invoice, for a Booking that never resolved one.
  3. sync_customer_identity_consistency -- customer-name AND receipt
     dealer-name checks (WRONG_DOCUMENT).
  4. sync_duplicate_receipt_detection -- DUPLICATE_RECEIPT.

Each Journey runs in its own transaction so one Journey's failure can't
roll back progress on the rest; failures are collected, not raised.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.uc03_customer_identity_consistency import (
    sync_customer_identity_consistency,
)
from audit_core.uc03_delivery_post_extraction_materialization import (
    materialize_delivery_documents_from_durable_store,
)
from audit_core.uc03_duplicate_receipt_detection import sync_duplicate_receipt_detection
from audit_core.uc03_model_resolution import sync_model_resolution_from_invoice

logger = logging.getLogger(__name__)
_run_log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/admin/backfill",
    tags=["uc03-backfill"],
)

_CORRELATION_ID = "backfill:document-sync-producers"


def _all_journey_ids_with_extracted_fields(connection, *, tenant_id: str) -> list[UUID]:
    return list(
        connection.execute(
            text(
                """
                SELECT DISTINCT journey_id
                FROM auditcore.journey_document_extracted_fields
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id},
        ).scalars().all()
    )


def backfill_document_sync_producers_for_tenant(
    engine: Engine, *, tenant_id: str
) -> dict[str, Any]:
    """Run all four producers against every Journey with any confirmed
    document data. Never raises; a per-Journey failure is recorded and
    skipped, not fatal to the rest of the run."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journey_ids = _all_journey_ids_with_extracted_fields(connection, tenant_id=tenant_id)

    processed = 0
    failed: list[str] = []
    totals: dict[str, int] = {
        "deliveryMaterializations": 0,
        "skuResolutionsFromInvoice": 0,
        "identityFindingsRaised": 0,
        "identityFindingsResolved": 0,
        "duplicateReceiptFindingsRaised": 0,
        "duplicateReceiptFindingsResolved": 0,
    }

    for journey_id in journey_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)

                delivery_result = materialize_delivery_documents_from_durable_store(
                    connection, tenant_id=tenant_id, journey_id=journey_id
                )
                if delivery_result.get("deliveryDateSet"):
                    totals["deliveryMaterializations"] += 1

                sku_result = sync_model_resolution_from_invoice(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                    correlation_id=_CORRELATION_ID,
                )
                if sku_result.get("resolved"):
                    totals["skuResolutionsFromInvoice"] += 1

                identity_result = sync_customer_identity_consistency(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                    correlation_id=_CORRELATION_ID,
                )
                totals["identityFindingsRaised"] += identity_result.get("raised", 0)
                totals["identityFindingsResolved"] += identity_result.get("resolved", 0)

                duplicate_result = sync_duplicate_receipt_detection(
                    connection, tenant_id=tenant_id, journey_id=journey_id,
                    correlation_id=_CORRELATION_ID,
                )
                totals["duplicateReceiptFindingsRaised"] += duplicate_result.get("raised", 0)
                totals["duplicateReceiptFindingsResolved"] += duplicate_result.get("resolved", 0)

            processed += 1
        except Exception:
            logger.warning(
                "uc03_backfill_journey_failed",
                extra={"tenant_id": tenant_id, "journey_id": str(journey_id)},
                exc_info=True,
            )
            failed.append(str(journey_id))

    return {
        "tenantId": tenant_id,
        "journeysConsidered": len(journey_ids),
        "journeysProcessed": processed,
        "journeysFailed": failed,
        **totals,
    }


class BackfillResult(BaseModel):
    tenantId: str
    journeysConsidered: int
    journeysProcessed: int
    journeysFailed: list[str]
    deliveryMaterializations: int
    skuResolutionsFromInvoice: int
    identityFindingsRaised: int
    identityFindingsResolved: int
    duplicateReceiptFindingsRaised: int
    duplicateReceiptFindingsResolved: int


@router.post("/document-sync-producers", response_model=BackfillResult)
def backfill_document_sync_producers(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> BackfillResult:
    del admin_request
    result = backfill_document_sync_producers_for_tenant(engine, tenant_id=tenant_id)
    return BackfillResult.model_validate(result)


_SKU_CORRELATION_ID = "backfill:sku-resolution"


def _sku_counts(engine: Engine, *, tenant_id: str) -> tuple[int, int]:
    """(journeys with a pinned SKU, journeys without one) among Journeys that have a product row."""
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        row = connection.execute(
            text(
                """
                SELECT count(DISTINCT journey_id) FILTER (WHERE product_sku_id IS NOT NULL),
                       count(DISTINCT journey_id) FILTER (WHERE product_sku_id IS NULL)
                FROM auditcore.journey_products
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id},
        ).one()
    return int(row[0] or 0), int(row[1] or 0)


def _journey_sku_state(connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT jp.product_sku_id IS NOT NULL AS has_sku,
                   sku.sku_code, jp.match_basis, j.review_completed_at_utc IS NOT NULL AS review_completed,
                   NULLIF(concat_ws(' · ', NULLIF(jp.model_name_snapshot, ''),
                                    NULLIF(jp.variant_name_snapshot, ''),
                                    NULLIF(jp.colour_name_snapshot, '')), '') AS model_label,
                   COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS journey_label,
                   (SELECT f.title || COALESCE(': ' || f.description, '') FROM auditcore.audit_findings f
                     WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                       AND f.finding_type_code = 'MODEL_NOT_IDENTIFIED'
                       AND f.finding_status IN ('OPEN', 'ACKNOWLEDGED')
                     ORDER BY f.created_at_utc DESC LIMIT 1) AS open_finding
            FROM auditcore.journeys j
            LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            LEFT JOIN auditcore.product_skus sku ON sku.product_sku_id = jp.product_sku_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            ORDER BY jp.created_at_utc NULLS LAST
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    return dict(row) if row else {}


def _record_item(
    connection, *, tenant_id: str, run_id: UUID, journey_id: UUID, label: str | None,
    model_label: str | None, outcome: str, sku_code: str | None, detail: str | None,
    proposal: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.sku_resolution_run_items
              (tenant_id, run_id, journey_id, journey_label, model_label, outcome, sku_code, detail, proposal)
            VALUES (:t, :r, :j, :label, :model, :outcome, :sku, :detail, CAST(:proposal AS jsonb))
            ON CONFLICT (tenant_id, run_id, journey_id) DO UPDATE
              SET journey_label = EXCLUDED.journey_label, model_label = EXCLUDED.model_label,
                  outcome = EXCLUDED.outcome, sku_code = EXCLUDED.sku_code, detail = EXCLUDED.detail,
                  proposal = EXCLUDED.proposal, recorded_at_utc = now()
            """
        ),
        {"t": tenant_id, "r": run_id, "j": journey_id, "label": label, "model": model_label,
         "outcome": outcome, "sku": sku_code, "detail": (detail or None) and detail[:500],
         "proposal": json.dumps(proposal, default=str) if proposal is not None else None},
    )
    # One line per Journey, readable from the logs without database access.
    _run_log.info(
        "sku_run_item", tenant_id=tenant_id, run_id=str(run_id), journey_id=str(journey_id), journey=label,
        model=model_label, outcome=outcome, sku_code=sku_code, detail=(detail or None) and detail[:500],
    )


RECHECK_DRY_RUN = "RERESOLVE_DRY_RUN"
RECHECK_COMMIT = "RERESOLVE_COMMIT"


def _recheck_item(
    connection, *, tenant_id: str, run_id: UUID, journey_id: UUID, before: dict[str, Any], commit: bool,
) -> None:
    """One Journey of a reresolve run (review 471 B2): nothing is cleared;
    the dry run writes only the run item, the commit writes the decision
    (inside a savepoint around the whole Journey)."""
    from audit_core.uc03_model_resolution import recheck_model_resolution

    result = recheck_model_resolution(connection, tenant_id=tenant_id, journey_id=journey_id,
                                      correlation_id=_SKU_CORRELATION_ID, commit=commit)
    if result.get("error"):
        detail = f"An internal step reported an error (nothing changed): {result.get('errorType')}: " \
                 f"{result.get('errorMessage')}"
        _record_item(connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                     label=before.get("journey_label"), model_label=before.get("model_label"), outcome="FAILED",
                     sku_code=before.get("sku_code"), detail=detail)
        _run_log.info("sku_reresolve_item", tenant_id=tenant_id, run_id=str(run_id), journey_id=str(journey_id),
                      dry_run=not commit, old_sku=before.get("sku_code"), error=detail)
        return
    after = before if not commit else _journey_sku_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    proposal = dict(result.get("proposal") or {})
    old, new = proposal.get("oldSku"), proposal.get("proposedSku")
    if proposal.get("action") == "UNCONFIRMED":
        outcome = "STILL_UNMATCHED"
    elif new and new != old:
        outcome = "NEWLY_MATCHED"
    elif new:
        outcome = "ALREADY_HAD_SKU"
    else:
        outcome = "STILL_UNMATCHED"
    detail = (
        ("[dry run] " if not commit else "")
        + f"{old or 'no SKU'} ({proposal.get('oldBasis') or '-'}) -> "
        + f"{new or 'no SKU'} ({proposal.get('proposedBasis') or '-'}) [{proposal.get('action')}]"
        + (f"; words '{proposal['words']}'" if proposal.get("words") else "")
        + (f"; master {proposal.get('masterExShowroom')} vs document {proposal.get('documentExShowroom')}"
           f" ({proposal.get('priceCheck')})" if proposal.get("priceCheck") else "")
        + (f"; {proposal['why']}" if proposal.get("why") else "")
        + (f"; tasks: {', '.join(proposal['tasks'])}" if proposal.get("tasks") else "")
    )
    _record_item(connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                 label=after.get("journey_label"), model_label=after.get("model_label"), outcome=outcome,
                 sku_code=new, detail=detail, proposal={**proposal, "dryRun": not commit})
    _run_log.info(
        "sku_reresolve_item", tenant_id=tenant_id, run_id=str(run_id), journey_id=str(journey_id),
        dry_run=not commit, action=proposal.get("action"), old_sku=old, old_basis=proposal.get("oldBasis"),
        proposed_sku=new, proposed_basis=proposal.get("proposedBasis"), words=proposal.get("words"),
        master_ex_showroom=proposal.get("masterExShowroom"), document_ex_showroom=proposal.get("documentExShowroom"),
        price_check=proposal.get("priceCheck"), why=proposal.get("why"), tasks=proposal.get("tasks"),
    )


def backfill_sku_resolution_for_tenant(
    engine: Engine, *, tenant_id: str, offset: int = 0, limit: int | None = None,
    run_id: UUID | None = None, actor_id: str | None = None, only_without_sku: bool = False,
    recheck: str | None = None,
) -> dict[str, Any]:
    """Run SKU resolution (booking model/price match, then the Delivery-invoice fallback) for
    the Project's Journeys, in a stable order, ``limit`` at a time starting at ``offset`` (all of
    them when ``limit`` is None). These are the same idempotent producers the app runs when a
    document is confirmed or "Run All Rules" is pressed; each Journey is its own transaction and
    a failure is recorded, never fatal to the rest. Callers loop on ``nextOffset`` so no single
    request runs long enough for the edge proxy to time out. Every Journey's outcome is kept
    against a run (``runId``) so the summary can be read afterwards.

    ``recheck`` (the "reresolve" run, review 471 B2): RERESOLVE_DRY_RUN
    writes only the run items -- per Journey the old SKU, the proposed SKU,
    basis, words, prices and the tasks that would be raised; RERESOLVE_COMMIT
    writes the decision (``recheck_model_resolution``). Nothing is ever
    cleared first, and a SKU a PC or Team Lead set is left as it is."""
    from audit_core.uc03_async_sync_tasks import sync_model_resolution_with_escalation
    from audit_core.uc03_model_resolution import booking_cancelled

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        journeys_total = int(
            connection.execute(
                text("SELECT count(*) FROM auditcore.journeys WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            ).scalar_one()
        )
        journey_ids = list(
            connection.execute(
                text(
                    """
                    SELECT journey_id FROM auditcore.journeys WHERE tenant_id = :tenant_id
                    ORDER BY created_at_utc, journey_id
                    OFFSET :offset LIMIT :limit
                    """
                ),
                {"tenant_id": tenant_id, "offset": offset, "limit": limit},
            ).scalars().all()
        )
        if run_id is None:
            run_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.sku_resolution_runs
                      (tenant_id, started_by_actor_id, journeys_total, run_mode)
                    VALUES (:t, :actor, :total, :mode) RETURNING run_id
                    """
                ),
                {"t": tenant_id, "actor": actor_id, "total": journeys_total, "mode": recheck or "RESOLVE"},
            ).scalar_one()
            first_batch = True
        else:
            first_batch = False
    if first_batch:
        with_before, _ = _sku_counts(engine, tenant_id=tenant_id)
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            connection.execute(
                text("UPDATE auditcore.sku_resolution_runs SET with_sku_before=:n WHERE tenant_id=:t AND run_id=:r"),
                {"n": with_before, "t": tenant_id, "r": run_id},
            )
    next_offset = offset + len(journey_ids)

    failed: list[str] = []
    for journey_id in journey_ids:
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                before = _journey_sku_state(connection, tenant_id=tenant_id, journey_id=journey_id)
                if only_without_sku and before.get("has_sku"):
                    # A run over the unresolved only: a pinned SKU is left as it is.
                    _record_item(
                        connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                        label=before.get("journey_label"), model_label=before.get("model_label"),
                        outcome="ALREADY_HAD_SKU", sku_code=before.get("sku_code"), detail=None,
                    )
                    continue
                if recheck is not None and booking_cancelled(connection, tenant_id=tenant_id,
                                                             journey_id=journey_id):
                    _record_item(
                        connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                        label=before.get("journey_label"), model_label=before.get("model_label"),
                        outcome="ALREADY_HAD_SKU" if before.get("has_sku") else "STILL_UNMATCHED",
                        sku_code=before.get("sku_code"),
                        detail="Booking cancelled, duplicate or closed with no delivery: not re-checked",
                    )
                    continue
                if recheck is not None and before.get("review_completed"):
                    # A Journey whose review is complete is not re-checked:
                    # its SKU (and the standards priced on it) stay as reviewed.
                    _record_item(
                        connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                        label=before.get("journey_label"), model_label=before.get("model_label"),
                        outcome="ALREADY_HAD_SKU" if before.get("has_sku") else "STILL_UNMATCHED",
                        sku_code=before.get("sku_code"), detail="Review completed: not re-checked",
                    )
                    continue
                if recheck is not None:
                    _recheck_item(connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                                  before=before, commit=recheck == RECHECK_COMMIT)
                    continue
                step_error = None
                reasons: list[str] = []
                # One pass: the ladder reads the booking form and every vehicle
                # invoice itself (the Booking and Delivery entries are the same
                # resolution since 2026-10-20).
                result = sync_model_resolution_with_escalation(
                    connection, tenant_id=tenant_id, journey_id=journey_id, correlation_id=_SKU_CORRELATION_ID,
                )
                if result.get("error"):
                    step_error = (
                        f"An internal step reported an error: {result.get('errorType')}: "
                        f"{result.get('errorMessage')}"
                    )
                elif result.get("reason") and result.get("reason") != "already_resolved":
                    reasons.append(str(result["reason"]))
                after = _journey_sku_state(connection, tenant_id=tenant_id, journey_id=journey_id)
                had, has = bool(before.get("has_sku")), bool(after.get("has_sku"))
                same = had and has and before.get("sku_code") == after.get("sku_code")
                outcome = (
                    "ALREADY_HAD_SKU" if same or (had and has)
                    else "NEWLY_MATCHED" if has else "STILL_UNMATCHED"
                )
                detail = (
                    step_error
                    or (None if has else after.get("open_finding") or (
                        "Skipped: " + ", ".join(dict.fromkeys(reasons)) if reasons
                        else "No matching SKU was found in the price masters."
                    ))
                )
                _record_item(
                    connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                    label=after.get("journey_label"), model_label=after.get("model_label"),
                    outcome=outcome, sku_code=after.get("sku_code"), detail=detail,
                )
        except Exception as exc:
            logger.warning(
                "uc03_sku_backfill_journey_failed",
                extra={"tenant_id": tenant_id, "journey_id": str(journey_id)},
                exc_info=True,
            )
            failed.append(str(journey_id))
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                _record_item(
                    connection, tenant_id=tenant_id, run_id=run_id, journey_id=journey_id,
                    label=None, model_label=None, outcome="FAILED", sku_code=None,
                    detail=f"{type(exc).__name__}: {exc}",
                )

    finished = next_offset >= journeys_total or not journey_ids
    with_after, without_after = _sku_counts(engine, tenant_id=tenant_id)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text(
                """
                UPDATE auditcore.sku_resolution_runs r SET
                  journeys_processed = c.processed, newly_matched = c.newly, already_had_sku = c.already,
                  still_unmatched = c.unmatched, failed = c.failed, with_sku_after = :after,
                  status = CASE WHEN :finished THEN 'COMPLETE' ELSE r.status END,
                  finished_at_utc = CASE WHEN :finished THEN now() ELSE r.finished_at_utc END
                FROM (
                  SELECT count(*) FILTER (WHERE outcome <> 'FAILED') AS processed,
                         count(*) FILTER (WHERE outcome = 'NEWLY_MATCHED') AS newly,
                         count(*) FILTER (WHERE outcome = 'ALREADY_HAD_SKU') AS already,
                         count(*) FILTER (WHERE outcome = 'STILL_UNMATCHED') AS unmatched,
                         count(*) FILTER (WHERE outcome = 'FAILED') AS failed
                  FROM auditcore.sku_resolution_run_items WHERE tenant_id = :t AND run_id = :r
                ) c
                WHERE r.tenant_id = :t AND r.run_id = :r
                """
            ),
            {"t": tenant_id, "r": run_id, "after": with_after, "finished": finished},
        )
        run = connection.execute(
            text("SELECT * FROM auditcore.sku_resolution_runs WHERE tenant_id=:t AND run_id=:r"),
            {"t": tenant_id, "r": run_id},
        ).mappings().one()
    return {
        "tenantId": tenant_id,
        "runId": run_id,
        "journeysTotal": journeys_total,
        "journeysConsidered": len(journey_ids),
        "journeysProcessed": len(journey_ids) - len(failed),
        "journeysFailed": failed,
        "nextOffset": None if finished else next_offset,
        "withSkuBefore": int(run["with_sku_before"]),
        "withSkuAfter": with_after,
        "withoutSkuAfter": without_after,
    }


def reresolve_all_skus(engine: Engine, tenant_id: str, *, actor_id: str | None = None,
                       batch: int = 10, commit: bool = False) -> dict[str, Any]:
    """Re-check every Journey's SKU under the 2026-10-20 product owner rules
    (review 471 B2), a DRY RUN unless ``commit``:

    * the dry run writes nothing but the run items: per Journey the old SKU
      and basis, the proposed SKU and basis, the document's words, the
      master and document prices and the tasks that would be raised -- for
      the product owner to review before anything changes;
    * the commit writes the same decisions, each Journey in its own
      transaction and savepoint (an error leaves it untouched, recorded as
      FAILED). An automatic SKU is replaced only when the documents now name
      a different one; one they do not name is kept, marked AUTO_UNCONFIRMED,
      with the PC task "SKU not clearly identified from the documents"; a SKU
      a PC or Team Lead set is never touched. Changes go to the Journey's
      activity log."""
    offset: int | None = 0
    run_id = None
    result: dict[str, Any] = {}
    while offset is not None:
        result = backfill_sku_resolution_for_tenant(
            engine, tenant_id=tenant_id, offset=offset, limit=batch, run_id=run_id, actor_id=actor_id,
            recheck=RECHECK_COMMIT if commit else RECHECK_DRY_RUN,
        )
        run_id = result["runId"]
        offset = result["nextOffset"]
    return result


class SkuResolutionBackfillResult(BaseModel):
    tenantId: str
    runId: UUID
    journeysTotal: int
    nextOffset: int | None
    journeysConsidered: int
    journeysProcessed: int
    journeysFailed: list[str]
    withSkuBefore: int
    withSkuAfter: int
    withoutSkuAfter: int


class SkuResolutionRunSummary(BaseModel):
    runId: UUID
    status: str
    startedAtUtc: str
    finishedAtUtc: str | None
    journeysTotal: int
    journeysProcessed: int
    newlyMatched: int
    alreadyHadSku: int
    stillUnmatched: int
    failed: int
    withSkuBefore: int
    withSkuAfter: int


class SkuResolutionRunItem(BaseModel):
    journeyId: UUID
    journeyLabel: str | None
    modelLabel: str | None
    outcome: str
    skuCode: str | None
    detail: str | None


class SkuResolutionRunDetail(SkuResolutionRunSummary):
    items: list[SkuResolutionRunItem]


def _run_summary(row) -> dict[str, Any]:
    return {
        "runId": row["run_id"], "status": row["status"],
        "startedAtUtc": row["started_at_utc"].isoformat(),
        "finishedAtUtc": row["finished_at_utc"].isoformat() if row["finished_at_utc"] else None,
        "journeysTotal": row["journeys_total"], "journeysProcessed": row["journeys_processed"],
        "newlyMatched": row["newly_matched"], "alreadyHadSku": row["already_had_sku"],
        "stillUnmatched": row["still_unmatched"], "failed": row["failed"],
        "withSkuBefore": row["with_sku_before"], "withSkuAfter": row["with_sku_after"],
    }


@router.post("/sku-resolution", response_model=SkuResolutionBackfillResult)
def backfill_sku_resolution(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=25)] = 3,
    runId: UUID | None = None,
) -> SkuResolutionBackfillResult:
    """One batch of Journeys; repeat with ``nextOffset`` (and the returned ``runId``) until it is null."""
    return SkuResolutionBackfillResult.model_validate(
        backfill_sku_resolution_for_tenant(
            engine, tenant_id=tenant_id, offset=offset, limit=limit, run_id=runId,
            actor_id=admin_request.user_id,
        )
    )


@router.get("/sku-resolution/runs", response_model=list[SkuResolutionRunSummary])
def list_sku_resolution_runs(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> list[dict[str, Any]]:
    """The most recent runs for the Project, newest first."""
    del admin_request
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(
                "SELECT * FROM auditcore.sku_resolution_runs WHERE tenant_id=:t "
                "ORDER BY started_at_utc DESC LIMIT 20"
            ),
            {"t": tenant_id},
        ).mappings().all()
    return [_run_summary(r) for r in rows]


@router.get("/sku-resolution/runs/{run_id}", response_model=SkuResolutionRunDetail)
def get_sku_resolution_run(
    tenant_id: str,
    run_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    """One run with what happened to each Journey (problems first)."""
    del admin_request
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        run = connection.execute(
            text("SELECT * FROM auditcore.sku_resolution_runs WHERE tenant_id=:t AND run_id=:r"),
            {"t": tenant_id, "r": run_id},
        ).mappings().one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        items = connection.execute(
            text(
                """
                SELECT journey_id, journey_label, model_label, outcome, sku_code, detail
                FROM auditcore.sku_resolution_run_items WHERE tenant_id=:t AND run_id=:r
                ORDER BY CASE outcome WHEN 'FAILED' THEN 0 WHEN 'STILL_UNMATCHED' THEN 1
                                      WHEN 'NEWLY_MATCHED' THEN 2 ELSE 3 END, journey_label
                """
            ),
            {"t": tenant_id, "r": run_id},
        ).mappings().all()
    return {
        **_run_summary(run),
        "items": [
            {"journeyId": i["journey_id"], "journeyLabel": i["journey_label"], "modelLabel": i["model_label"],
             "outcome": i["outcome"], "skuCode": i["sku_code"], "detail": i["detail"]}
            for i in items
        ],
    }


__all__ = [
    "backfill_document_sync_producers_for_tenant",
    "backfill_sku_resolution_for_tenant",
    "reresolve_all_skus",
    "router",
]
