"""0152_sku_match_basis — how a Journey's SKU was set, and on what evidence.

Product owner rules 2026-10-20 (R3): a SKU is identified by the words its
documents print; a price may only check it, never choose it. Automatic
matches are not "Confirmed" -- only a PC or Team Lead decision is -- and an
automatic match is replaced by later evidence or by the PC's direct pick.

journey_products.selection_status keeps its CHECK ('TENTATIVE' /
'CONFIRMED'): automatic pins are now TENTATIVE, PC / TL decisions CONFIRMED.
This migration adds:

  * match_basis -- AUTO_MATCHED (words name one SKU, booking price agrees),
    AUTO_PRICE_MISMATCH (words name one SKU, price differs or missing; a PC
    task is open), AUTO_INVOICE_MATCHED (a vehicle invoice's words and price
    agree), AUTO_LEGACY (pinned by the resolver before this rule; it may
    have been chosen by price and is re-checked by the "reresolve" SKU run),
    PC_CONFIRMED, TL_APPROVED;
  * match_evidence -- the document, its words and the prices compared;
  * sku_resolution_runs.run_mode and sku_resolution_run_items.proposal --
    the reresolve run's dry run (old SKU -> proposed SKU, basis, words,
    prices, tasks), reviewed before the "-commit" run writes anything.

Aliases: "SCORPIO-N" names the new Scorpio N (October's spelling of
September's "New SCORPIO N"), as the price masters' model identity has it
(oem_price_masters.price_list_model_key); 0061 pointed it at the old
SCORPIO N. The other renames (NEW THAR, SCORPIO, XUV 3XO, XEV 9S, XUV 3XO
EV, the three Bolero models) already agree with that identity.

Backfill: existing pins are classified from selection_method AND from the
Journey's decision history, because until #441 (2026-10-20 08:17 UTC) the
PC's own picks were saved with the resolver's method MODEL_RESOLUTION_SYNC_V1
(the confirm-sku picker, an accepted SKU_CLOSEST_MATCH_CONFIRM, the pick on
a MODEL_NOT_IDENTIFIED task). On the live Mahindra project every pin carries
an automatic method, so the history is the only signal:

  * PC_CONFIRMED, CONFIRMED -- selection_method MODEL_RESOLUTION_PC_PICK_V1;
    or an automatic method where a human resolved the Journey's
    MODEL_NOT_IDENTIFIED finding (audit_finding_events RESOLVED with
    actor_role_snapshot HUMAN -- only the PC's confirm writes that), or the
    PC accepted a SKU_CLOSEST_MATCH_CONFIRM task naming the pinned SKU --
    unless an invoice replaced the SKU afterwards (a later
    MODEL_CHANGED_BY_INVOICE task). The evidence names the trace.
  * TL_APPROVED, CONFIRMED -- MODEL_SELECTION_CORRECTION_V1.
  * AUTO_LEGACY, TENTATIVE -- the remaining resolver pins. They stay on the
    Journey (normal syncs leave them alone) until the "reresolve-...-commit"
    SKU run re-checks them by their words.

AUTO_UNCONFIRMED marks an automatic pin whose documents no longer name it
(a re-check could not confirm it); the SKU is kept and a PC task raised.

Chain: 0150 -> 0151 (price masters, #468) -> 0153 (source values per
document, #470, which now follows 0151) -> 0152; the coordinator may
re-chain it at merge.

Nothing is deleted; the downgrade restores CONFIRMED on the relabelled rows.
"""
from __future__ import annotations

from alembic import op

revision = "0152_sku_match_basis"
down_revision = "0153_source_values_per_document"
branch_labels = None
depends_on = None

_AUTOMATIC_METHODS = (
    "'MODEL_RESOLUTION_SYNC_V1', 'MODEL_RESOLUTION_INVOICE_FALLBACK_V1', 'MODEL_RESOLUTION_INVOICE_OVERRIDE_V1'"
)


def classify_existing_pins(execute) -> None:
    """Label the pins already on file (see the module docstring). Takes the
    statement executor so the test suite can run it on rows it sets up."""
    # Human decisions saved under an automatic method (before #441).
    execute(
        f"""
        WITH traces AS (
            SELECT e.tenant_id, e.journey_id, e.occurred_at_utc AS at, 'MODEL_NOT_IDENTIFIED_RESOLVED' AS kind,
                   e.actor_id AS actor_id, NULL::text AS task_id
            FROM auditcore.audit_finding_events e
            JOIN auditcore.audit_findings f
              ON f.tenant_id = e.tenant_id AND f.audit_finding_id = e.audit_finding_id
            WHERE f.finding_type_code = 'MODEL_NOT_IDENTIFIED'
              AND e.event_type = 'RESOLVED' AND e.actor_role_snapshot = 'HUMAN'
            UNION ALL
            SELECT t.tenant_id, t.journey_id, COALESCE(t.verified_at_utc, t.updated_at_utc),
                   'SKU_CLOSEST_MATCH_ACCEPTED', t.completion_result->>'by', t.task_id::text
            FROM auditcore.p2_tasks t
            JOIN auditcore.journey_products jp
              ON jp.tenant_id = t.tenant_id AND jp.journey_id = t.journey_id
            WHERE t.task_type = 'SKU_CLOSEST_MATCH_CONFIRM'
              AND t.completion_result->>'outcome' = 'SKU_ACCEPTED'
              AND t.reference->>'suggestedSkuId' = jp.product_sku_id::text
        ),
        latest AS (
            SELECT DISTINCT ON (tenant_id, journey_id) tenant_id, journey_id, at, kind, actor_id, task_id
            FROM traces ORDER BY tenant_id, journey_id, at DESC
        )
        UPDATE auditcore.journey_products jp
        SET match_basis = 'PC_CONFIRMED', selection_status = 'CONFIRMED',
            match_evidence = jsonb_build_object(
                'source', 'MIGRATION_0152', 'trace', l.kind, 'decidedAtUtc', l.at,
                'actorId', l.actor_id, 'taskId', l.task_id, 'selectionMethod', jp.selection_method)
        FROM latest l
        WHERE l.tenant_id = jp.tenant_id AND l.journey_id = jp.journey_id
          AND jp.product_sku_id IS NOT NULL AND jp.selection_method IN ({_AUTOMATIC_METHODS})
          AND NOT EXISTS (
              SELECT 1 FROM auditcore.p2_tasks c
              WHERE c.tenant_id = jp.tenant_id AND c.journey_id = jp.journey_id
                AND c.task_type = 'MODEL_CHANGED_BY_INVOICE' AND c.created_at_utc > l.at)
        """
    )
    execute(
        f"""
        UPDATE auditcore.journey_products
        SET match_basis = 'AUTO_LEGACY', selection_status = 'TENTATIVE',
            match_evidence = jsonb_build_object('source', 'MIGRATION_0152', 'selectionMethod', selection_method,
                                                'statusBefore', selection_status)
        WHERE product_sku_id IS NOT NULL AND selection_method IN ({_AUTOMATIC_METHODS})
          AND match_basis IS NULL
        """
    )
    execute(
        """
        UPDATE auditcore.journey_products SET match_basis = 'PC_CONFIRMED'
        WHERE product_sku_id IS NOT NULL AND selection_method = 'MODEL_RESOLUTION_PC_PICK_V1'
        """
    )
    execute(
        """
        UPDATE auditcore.journey_products SET match_basis = 'TL_APPROVED'
        WHERE product_sku_id IS NOT NULL AND selection_method = 'MODEL_SELECTION_CORRECTION_V1'
        """
    )


def upgrade() -> None:
    op.execute(
        """
        UPDATE auditcore.oem_model_aliases SET canonical_model_name = 'NEW SCORPIO N', updated_at_utc = now()
        WHERE oem_code = 'MAHINDRA' AND alias_text = 'SCORPIO-N'
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
          ADD COLUMN IF NOT EXISTS match_basis varchar(30),
          ADD COLUMN IF NOT EXISTS match_evidence jsonb,
          ADD CONSTRAINT ck_journey_products_match_basis
            CHECK (match_basis IS NULL OR match_basis IN (
              'AUTO_MATCHED', 'AUTO_PRICE_MISMATCH', 'AUTO_INVOICE_MATCHED', 'AUTO_LEGACY',
              'AUTO_UNCONFIRMED', 'PC_CONFIRMED', 'TL_APPROVED'))
        """
    )
    # The reresolve run's dry run (review 471 B2): what each Journey would
    # get, kept against the run for the product owner to review.
    op.execute(
        """
        ALTER TABLE auditcore.sku_resolution_runs ADD COLUMN IF NOT EXISTS run_mode varchar(30);
        ALTER TABLE auditcore.sku_resolution_run_items ADD COLUMN IF NOT EXISTS proposal jsonb;
        """
    )
    classify_existing_pins(op.execute)


def downgrade() -> None:
    op.execute(
        "UPDATE auditcore.oem_model_aliases SET canonical_model_name = 'SCORPIO N' "
        "WHERE oem_code = 'MAHINDRA' AND alias_text = 'SCORPIO-N'"
    )
    op.execute(
        """
        UPDATE auditcore.journey_products SET selection_status = 'CONFIRMED'
        WHERE product_sku_id IS NOT NULL AND selection_status = 'TENTATIVE'
          AND match_basis LIKE 'AUTO%'
          AND COALESCE(match_evidence->>'statusBefore', 'CONFIRMED') <> 'TENTATIVE'
        """
    )
    op.execute("ALTER TABLE auditcore.sku_resolution_run_items DROP COLUMN IF EXISTS proposal")
    op.execute("ALTER TABLE auditcore.sku_resolution_runs DROP COLUMN IF EXISTS run_mode")
    op.execute("ALTER TABLE auditcore.journey_products DROP CONSTRAINT IF EXISTS ck_journey_products_match_basis")
    op.execute(
        "ALTER TABLE auditcore.journey_products DROP COLUMN IF EXISTS match_evidence, "
        "DROP COLUMN IF EXISTS match_basis"
    )
