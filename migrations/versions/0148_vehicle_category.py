"""0148_vehicle_category — PV / CV / EV on every vehicle model and Journey.

Decision 2026-10-20 (product owner): the vehicle's category -- PV (passenger
vehicle), CV (commercial vehicle) or EV (electric vehicle) -- is stored in
Audit Core and shown on Journey 360.

The category already has a home on the model: product_models.segment_id ->
auditcore.segments (PASSENGER_VEHICLE / COMMERCIAL / BATTERY_ELECTRIC). The
segment-template upload fills it; the native OEM price-list upload did not --
it kept the row's "Category" column (PV / CV / BEV) only inside
product_variants.attributes. This migration:

  * backfills product_models.segment_id from that stored category where every
    variant of the model agrees (and an ELECTRIC fuel as EV); a model whose
    rows say only "ICE" (dealer per-model sheets do not say PV or CV) is left
    unset rather than guessed;
  * adds journey_products.vehicle_category ('PV' / 'CV' / 'EV', NULL when
    not known) and the function auditcore.vehicle_category_for_sku();
  * keeps the Journey's category in step with its SKU by trigger, whichever
    code path pins or changes the SKU;
  * backfills it for every Journey that already has a SKU.
"""
from __future__ import annotations

from alembic import op

revision = "0148_vehicle_category"
down_revision = "0147_variance_any_invoice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH per_variant AS (
            SELECT pv.model_id,
                   CASE
                     WHEN upper(COALESCE(pv.attributes ->> 'category', '')) IN ('BEV', 'EV')
                       OR upper(COALESCE(pv.fuel_powertrain, '')) IN ('ELECTRIC', 'EV', 'BEV')
                       THEN 'BATTERY_ELECTRIC'
                     WHEN upper(COALESCE(pv.attributes ->> 'category', '')) = 'PV' THEN 'PASSENGER_VEHICLE'
                     WHEN upper(COALESCE(pv.attributes ->> 'category', '')) = 'CV' THEN 'COMMERCIAL'
                   END AS segment_code
            FROM auditcore.product_variants pv
        ), per_model AS (
            SELECT model_id, min(segment_code) AS segment_code
            FROM per_variant
            WHERE segment_code IS NOT NULL
            GROUP BY model_id
            HAVING count(DISTINCT segment_code) = 1
        )
        UPDATE auditcore.product_models pm
        SET segment_id = s.segment_id, updated_at_utc = now()
        FROM per_model m
        JOIN auditcore.segments s ON s.segment_code = m.segment_code
        WHERE pm.model_id = m.model_id AND pm.segment_id IS NULL
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.vehicle_category_for_sku(p_sku uuid)
        RETURNS varchar
        LANGUAGE sql STABLE
        AS $$
            SELECT CASE
                     WHEN seg.segment_code = 'BATTERY_ELECTRIC'
                       OR upper(COALESCE(pv.attributes ->> 'category', '')) IN ('BEV', 'EV')
                       OR upper(COALESCE(pv.fuel_powertrain, '')) IN ('ELECTRIC', 'EV', 'BEV')
                       THEN 'EV'
                     WHEN seg.segment_code = 'COMMERCIAL'
                       OR upper(COALESCE(pv.attributes ->> 'category', '')) = 'CV' THEN 'CV'
                     WHEN seg.segment_code = 'PASSENGER_VEHICLE'
                       OR upper(COALESCE(pv.attributes ->> 'category', '')) = 'PV' THEN 'PV'
                   END
            FROM auditcore.product_skus s
            JOIN auditcore.product_models pm ON pm.model_id = s.model_id
            LEFT JOIN auditcore.segments seg ON seg.segment_id = pm.segment_id
            LEFT JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE s.product_sku_id = p_sku
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE auditcore.journey_products
          ADD COLUMN IF NOT EXISTS vehicle_category varchar(2),
          ADD CONSTRAINT ck_journey_products_vehicle_category
            CHECK (vehicle_category IS NULL OR vehicle_category IN ('PV', 'CV', 'EV'))
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.set_journey_vehicle_category()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.product_sku_id IS NULL THEN
                NEW.vehicle_category := NULL;
            ELSE
                NEW.vehicle_category := auditcore.vehicle_category_for_sku(NEW.product_sku_id);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journey_products_vehicle_category
        BEFORE INSERT OR UPDATE OF product_sku_id ON auditcore.journey_products
        FOR EACH ROW EXECUTE FUNCTION auditcore.set_journey_vehicle_category()
        """
    )
    op.execute(
        """
        UPDATE auditcore.journey_products
        SET vehicle_category = auditcore.vehicle_category_for_sku(product_sku_id)
        WHERE product_sku_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_journey_products_vehicle_category ON auditcore.journey_products")
    op.execute("DROP FUNCTION IF EXISTS auditcore.set_journey_vehicle_category()")
    op.execute(
        "ALTER TABLE auditcore.journey_products DROP CONSTRAINT IF EXISTS ck_journey_products_vehicle_category"
    )
    op.execute("ALTER TABLE auditcore.journey_products DROP COLUMN IF EXISTS vehicle_category")
    op.execute("DROP FUNCTION IF EXISTS auditcore.vehicle_category_for_sku(uuid)")
