"""Read-only diagnostics, run by the P2 worker and written to the logs.

Set AUDIT_CORE_DIAG_REPORTS on the worker to a comma-separated list of the
report names below (or "all"); on start the worker runs each, read-only,
for every active Project, and logs one ``diag_row`` line per row. Nobody
needs database access to see the answers (2026-10-20: the product owner
does not work in the database). Unset the variable when done.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context

logger = structlog.get_logger(__name__)
_MAX_ROWS = int(os.environ.get("AUDIT_CORE_DIAG_MAX_ROWS", "600"))

REPORTS: dict[str, str] = {
    # Every Journey's vehicle: what was read, the SKU pinned, how and when.
    "sku_pins": """
        SELECT j.journey_id, j.journey_reference, j.created_by_display_name AS pc_name,
               o.outlet_name,
               COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
               jp.model_name_snapshot AS model, jp.variant_name_snapshot AS variant,
               jp.colour_name_snapshot AS colour, s.sku_code, pm.model_name AS master_model,
               pv.variant_name AS master_variant, jp.selection_status, jp.selection_method,
               jp.selection_source, jp.selection_score, jp.vehicle_category,
               jp.created_at_utc, jp.updated_at_utc,
               (SELECT string_agg(e.event_type || ':' || COALESCE(e.actor_id, 'SYSTEM') || '@'
                                  || to_char(e.occurred_at_utc, 'YYYY-MM-DD HH24:MI'), ' | '
                                  ORDER BY e.occurred_at_utc)
                  FROM auditcore.audit_finding_events e
                  JOIN auditcore.audit_findings f
                    ON f.tenant_id = e.tenant_id AND f.audit_finding_id = e.audit_finding_id
                 WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                   AND f.finding_type_code = 'MODEL_NOT_IDENTIFIED') AS model_finding_events
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.dealer_outlets o
          ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
        LEFT JOIN auditcore.product_models pm ON pm.model_id = s.model_id
        LEFT JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
        WHERE j.tenant_id = :t
        ORDER BY j.created_at_utc
    """,
    # Every Journey with no SKU pinned: who captured it, and where the model
    # text actually came from (2026-10-23: journeys whose booked model reads
    # "Access", "Baleno", "Dost" or a Hyundai, while the variant is a genuine
    # Mahindra variant -- the question is whether the EXCHANGE vehicle's make
    # and model is landing in the booked vehicle's field).
    "unresolved_models": """
        SELECT j.journey_reference, j.created_by_display_name AS pc_name, o.outlet_name,
               COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
               jp.model_name_snapshot AS booked_model, jp.variant_name_snapshot AS booked_variant,
               jp.selection_source, jp.selection_method,
               ti.old_vehicle_make_model AS exchange_vehicle,
               ti.old_vehicle_registration AS exchange_registration,
               b.booking_date, b.pricing_effective_on,
               (SELECT string_agg(DISTINCT f2.effective_value::text, ' | ')
                  FROM auditcore.journey_document_extracted_fields f2
                 WHERE f2.tenant_id = j.tenant_id AND f2.journey_id = j.journey_id
                   AND f2.field_key = 'ex_showroom_price' AND f2.effective_value IS NOT NULL
                ) AS ex_showroom_read,
               (SELECT string_agg(DISTINCT dt.classified_document_type_key || '.' || f.field_key || '=' || f.effective_value, ' | ')
                  FROM auditcore.journey_document_extracted_fields f
                  LEFT JOIN auditcore.document_capture_v2_documents dt
                    ON dt.tenant_id = f.tenant_id AND dt.journey_id = f.journey_id
                   AND dt.di_document_id = f.di_document_id
                 WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                   AND f.field_key IN ('vehicle_model', 'vehicle_variant', 'model_name_raw', 'variant_raw')
                   AND f.effective_value IS NOT NULL) AS model_fields_read,
               j.created_at_utc
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.dealer_outlets o
          ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        LEFT JOIN auditcore.trade_in_cases ti
          ON ti.tenant_id = j.tenant_id AND ti.journey_id = j.journey_id
        LEFT JOIN auditcore.bookings b
          ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        WHERE j.tenant_id = :t AND jp.product_sku_id IS NULL
        ORDER BY j.created_at_utc
    """,
    # Is an ex-showroom figure a key at all? Per published price book: how many
    # of its prices name exactly one variant, one model, or span models. Asked
    # 2026-10-24 ("cant we even able to pick a price") before changing the
    # matcher, because a price that names two models cannot pin a SKU.
    "price_key_coverage": """
        WITH ex AS (
            SELECT plv.price_list_version_id, plv.version_no, plv.effective_from,
                   s.product_sku_id, pm.model_name, pv.variant_name,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key = 'EX_SHOWROOM') AS ex_showroom
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
            JOIN auditcore.price_list_items pli
              ON pli.tenant_id = plv.tenant_id AND pli.price_list_version_id = plv.price_list_version_id
            JOIN auditcore.product_skus s      ON s.product_sku_id = pli.product_sku_id
            JOIN auditcore.product_models pm   ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE plv.tenant_id = :t
              AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND s.is_active AND pm.is_active AND pv.is_active
            GROUP BY plv.price_list_version_id, plv.version_no, plv.effective_from,
                     s.product_sku_id, pm.model_name, pv.variant_name
        ), per_price AS (
            SELECT version_no, effective_from, ex_showroom,
                   COUNT(*) AS skus,
                   COUNT(DISTINCT model_name) AS models,
                   COUNT(DISTINCT model_name || ' ~ ' || variant_name) AS variants
            FROM ex WHERE ex_showroom IS NOT NULL
            GROUP BY 1, 2, 3
        )
        SELECT version_no, effective_from,
               COUNT(*) AS distinct_prices,
               SUM(skus) AS priced_skus,
               COUNT(*) FILTER (WHERE variants = 1) AS prices_naming_one_variant,
               COUNT(*) FILTER (WHERE models = 1 AND variants > 1) AS prices_naming_one_model,
               COUNT(*) FILTER (WHERE models > 1) AS prices_spanning_models,
               SUM(skus) FILTER (WHERE variants = 1) AS skus_a_price_pins_outright,
               SUM(skus) FILTER (WHERE models = 1 AND variants > 1) AS skus_a_price_narrows_to_model,
               SUM(skus) FILTER (WHERE models > 1) AS skus_a_price_leaves_open
        FROM per_price
        GROUP BY 1, 2
        ORDER BY effective_from DESC, version_no DESC
    """,
    # One row: does any document print an identifier we could match outright,
    # and does its shape match the codes our own price master holds? Asked
    # 2026-10-24 ("I want 100% identification").
    #
    # The resolver already prefers a printed sku_code over every word rule
    # (uc03_model_resolution._match_one), matching it exactly against
    # product_skus.sku_code. But that column is a code WE compose --
    # "MAXX_HD::MAXX HD 1.7L LX BS6.2", built as model_code + "::" + variant
    # name in oem_price_masters -- not the OEM's own material code. An invoice
    # prints the OEM's. An exact string compare between the two can never
    # match, so the one path that needs no tolerance is dead on real
    # documents. This report proves or disproves that from the data rather
    # than from reading the code.
    "printed_identifier_coverage": """
        SELECT
          (SELECT COUNT(*) FROM auditcore.journeys WHERE tenant_id = :t) AS journeys,
          (SELECT COUNT(DISTINCT f.journey_id)
             FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id = :t AND f.field_key = 'sku_code'
              AND f.effective_value IS NOT NULL
              AND f.effective_value #>> '{}' <> '') AS journeys_printing_a_sku_code,
          (SELECT left(string_agg(DISTINCT left(f.effective_value #>> '{}', 48), ' | '), 400)
             FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id = :t AND f.field_key = 'sku_code'
              AND f.effective_value IS NOT NULL
              AND f.effective_value #>> '{}' <> '') AS printed_codes,
          (SELECT left(string_agg(DISTINCT left(s.sku_code, 48), ' | '), 400)
             FROM auditcore.product_skus s
             JOIN auditcore.price_list_items pli ON pli.product_sku_id = s.product_sku_id
            WHERE pli.tenant_id = :t) AS our_own_codes,
          -- the number that matters: printed codes that actually hit a row
          (SELECT COUNT(DISTINCT f.journey_id)
             FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id = :t AND f.field_key = 'sku_code'
              AND f.effective_value IS NOT NULL
              AND EXISTS (SELECT 1 FROM auditcore.product_skus s
                           WHERE lower(btrim(s.sku_code)) = lower(btrim(f.effective_value #>> '{}')))
          ) AS printed_codes_that_match_a_sku,
          (SELECT COUNT(DISTINCT f.journey_id)
             FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id = :t AND f.field_key ~ '(chassis|vin|engine)'
              AND f.effective_value IS NOT NULL
              AND f.effective_value #>> '{}' <> '') AS journeys_with_a_chassis_or_engine_number,
          (SELECT left(string_agg(DISTINCT f.field_key, ', '), 300)
             FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id = :t AND f.field_key ~ '(chassis|vin|engine|material|model_code|variant_code)'
              AND f.effective_value IS NOT NULL) AS identifier_fields_present
    """,
    # Every Journey whose pricing date is one the programme calls a misread,
    # with where the date came from. Sizes the defect fixed on 2026-10-24:
    # journey_pricing_date applied no date floor, so a handwritten 26 read as
    # 24 priced the deal in 2024, found no book, and was recorded as a masters
    # gap. Run this after the fix to see the same Journeys skip as
    # "no_pricing_date" and reach the invoice-date fallback instead.
    "pricing_date_integrity": """
        SELECT j.journey_reference, j.created_by_display_name AS pc_name,
               COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
               j.created_at_utc::date AS journey_created,
               b.pricing_effective_on AS applied_date,
               b.booking_date,
               (SELECT f.effective_value #>> '{}'
                  FROM auditcore.journey_document_extracted_fields f
                 WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                   AND f.field_key = 'booking_date' AND f.effective_value IS NOT NULL
                 ORDER BY f.updated_at_utc DESC LIMIT 1) AS booking_date_as_read,
               CASE
                 WHEN b.pricing_effective_on IS NOT NULL THEN 'applied by a person'
                 WHEN COALESCE(b.booking_date, '9999-12-31'::date) < :floor
                   THEN 'read before the programme started'
                 WHEN b.booking_date > CURRENT_DATE THEN 'read as a day after today'
                 WHEN b.booking_date IS NULL THEN 'no booking date at all'
                 ELSE 'usable'
               END AS verdict,
               (jp.product_sku_id IS NOT NULL) AS has_sku
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        LEFT JOIN auditcore.journey_products jp
          ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        WHERE j.tenant_id = :t
          AND b.pricing_effective_on IS NULL
          AND (b.booking_date IS NULL
               OR b.booking_date < :floor
               OR b.booking_date > CURRENT_DATE)
        ORDER BY verdict, j.journey_reference
    """,
    # And the same thing counted, one row, so the size is unarguable.
    "pricing_date_verdict": """
        SELECT COUNT(*) AS journeys,
               COUNT(*) FILTER (WHERE b.pricing_effective_on IS NOT NULL) AS applied_by_a_person,
               COUNT(*) FILTER (WHERE b.pricing_effective_on IS NULL
                                  AND b.booking_date IS NOT NULL
                                  AND b.booking_date >= :floor
                                  AND b.booking_date <= CURRENT_DATE) AS read_and_usable,
               COUNT(*) FILTER (WHERE b.pricing_effective_on IS NULL
                                  AND b.booking_date < :floor) AS read_before_the_programme,
               COUNT(*) FILTER (WHERE b.pricing_effective_on IS NULL
                                  AND b.booking_date > CURRENT_DATE) AS read_after_today,
               COUNT(*) FILTER (WHERE b.pricing_effective_on IS NULL
                                  AND b.booking_date IS NULL) AS no_booking_date,
               COUNT(*) FILTER (WHERE b.journey_id IS NULL) AS no_booking_row_at_all
        FROM auditcore.journeys j
        LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        WHERE j.tenant_id = :t
    """,
    # Every field the documents of this Project actually carry, with how many
    # Journeys carry it and what it looks like. Asked 2026-10-24 ("think
    # differently... I want 100% identification"): before inferring the SKU
    # from words or from a price, find out whether the vehicle is named
    # outright somewhere -- an OEM material or model code, a chassis or engine
    # number, a SKU code printed on the invoice. A printed identifier needs no
    # matching, no tolerance and no tie-break, so if one is there it beats
    # everything else we have tried.
    "field_inventory": """
        SELECT f.field_key,
               COUNT(DISTINCT f.journey_id) AS journeys,
               COUNT(*) AS reads,
               COUNT(DISTINCT COALESCE(dt.classified_document_type_key,
                                       f.source_document_type_key)) AS document_types,
               string_agg(DISTINCT COALESCE(dt.classified_document_type_key,
                                            f.source_document_type_key), ', ') AS documents,
               ROUND(AVG(f.confidence_score), 1) AS mean_confidence,
               left(string_agg(DISTINCT left(f.effective_value #>> '{}', 40), ' | '), 400) AS examples
        FROM auditcore.journey_document_extracted_fields f
        LEFT JOIN auditcore.document_capture_v2_documents dt
          ON dt.tenant_id = f.tenant_id AND dt.journey_id = f.journey_id
         AND dt.di_document_id = f.di_document_id
        WHERE f.tenant_id = :t AND f.effective_value IS NOT NULL
        GROUP BY f.field_key
        ORDER BY COUNT(DISTINCT f.journey_id) DESC, f.field_key
    """,
    # Narrower and sharper: for the Journeys with no SKU, which identifying
    # field does each one carry? One row per Journey, so the answer to "what is
    # the surest thing" is a column rather than an argument.
    "identifiers_per_unresolved": """
        SELECT j.journey_reference, j.created_by_display_name AS pc_name,
               COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
               jp.model_name_snapshot AS booked_model, jp.variant_name_snapshot AS booked_variant,
               COALESCE(b.pricing_effective_on, b.booking_date) AS priced_on,
               (SELECT string_agg(DISTINCT f.field_key, ', ' ORDER BY f.field_key)
                  FROM auditcore.journey_document_extracted_fields f
                 WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                   AND f.effective_value IS NOT NULL
                   AND f.field_key ~ '(chassis|vin|engine|sku|material|model_code|variant_code)'
                ) AS identifier_fields,
               (SELECT string_agg(DISTINCT left(f.effective_value #>> '{}', 32), ' | ')
                  FROM auditcore.journey_document_extracted_fields f
                 WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                   AND f.effective_value IS NOT NULL
                   AND f.field_key ~ '(chassis|vin|engine|sku|material|model_code|variant_code)'
                ) AS identifier_values,
               (SELECT COUNT(DISTINCT f.di_document_id)
                  FROM auditcore.journey_document_extracted_fields f
                 WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id) AS documents_read
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.journey_products jp
          ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        WHERE j.tenant_id = :t AND jp.product_sku_id IS NULL
        ORDER BY j.journey_reference
    """,
    # The 13 Journeys behind resolved_price_disagrees: the SKU the words pinned
    # against the one an ex-showroom figure names within a rupee. Each is either
    # a SKU pinned wrongly today -- an audit error we are blind to -- or a figure
    # that is not the list price. No price logic may pin until these are read
    # (2026-10-24).
    "price_disagreements": """
        WITH j AS (
            SELECT j.journey_id, j.journey_reference, j.created_by_display_name AS pc_name,
                   o.outlet_name,
                   COALESCE(NULLIF(cu.display_name, ''), j.journey_reference) AS customer,
                   COALESCE(b.pricing_effective_on, b.booking_date) AS priced_on,
                   jp.product_sku_id, jp.selection_method, jp.match_basis,
                   pm.model_name || ' ~ ' || pv.variant_name AS pinned_name
            FROM auditcore.journeys j
            LEFT JOIN auditcore.customers cu
              ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
            LEFT JOIN auditcore.product_skus ps ON ps.product_sku_id = jp.product_sku_id
            LEFT JOIN auditcore.product_models pm ON pm.model_id = ps.model_id
            LEFT JOIN auditcore.product_variants pv ON pv.variant_id = ps.variant_id
            WHERE j.tenant_id = :t AND jp.product_sku_id IS NOT NULL
        ), planned AS (
            SELECT j.*, (
                SELECT plv.price_list_version_id
                FROM auditcore.price_list_versions plv
                JOIN auditcore.price_lists pl
                  ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
                WHERE plv.tenant_id = :t
                  AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
                  AND plv.effective_from <= j.priced_on
                  AND (plv.effective_to IS NULL OR plv.effective_to >= j.priced_on)
                  AND (pl.price_list_code = 'OEM_NATIVE_PRICE_LIST'
                       OR NOT EXISTS (SELECT 1 FROM auditcore.price_lists own
                                       WHERE own.tenant_id = plv.tenant_id
                                         AND own.price_list_code = 'OEM_NATIVE_PRICE_LIST'))
                ORDER BY plv.effective_from DESC, plv.version_no DESC, plv.price_list_version_id DESC
                LIMIT 1) AS plv
            FROM j
        ), reads AS (
            SELECT f.journey_id,
                   COALESCE(dt.classified_document_type_key, f.source_document_type_key) AS document,
                   ROUND(replace(COALESCE((regexp_match(f.effective_value #>> '{}',
                                 '[0-9][0-9,]*(?:\\.[0-9]+)?'))[1], '0'), ',', '')::numeric, 2) AS amount
            FROM auditcore.journey_document_extracted_fields f
            LEFT JOIN auditcore.document_capture_v2_documents dt
              ON dt.tenant_id = f.tenant_id AND dt.journey_id = f.journey_id
             AND dt.di_document_id = f.di_document_id
            WHERE f.tenant_id = :t AND f.field_key = 'ex_showroom_price'
              AND f.effective_value IS NOT NULL
              AND COALESCE(f.source_document_type_key, '') NOT IN ('wholesale_invoice')
            GROUP BY 1, 2, 3
        ), book AS (
            SELECT pli.price_list_version_id, s.product_sku_id,
                   pm.model_name || ' ~ ' || pv.variant_name AS name,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key = 'EX_SHOWROOM') AS ex_showroom
            FROM auditcore.price_list_items pli
            JOIN auditcore.product_skus s      ON s.product_sku_id = pli.product_sku_id
            JOIN auditcore.product_models pm   ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE pli.tenant_id = :t AND s.is_active AND pm.is_active AND pv.is_active
            GROUP BY 1, 2, 3
        )
        SELECT p.journey_reference, p.pc_name, p.outlet_name, p.customer, p.priced_on,
               p.pinned_name AS words_chose, p.selection_method, p.match_basis,
               (SELECT bk.ex_showroom FROM book bk
                 WHERE bk.price_list_version_id = p.plv AND bk.product_sku_id = p.product_sku_id)
                 AS book_price_of_that_sku,
               r.document, r.amount AS figure_read,
               m.one_name AS price_names, m.gap
        FROM planned p
        JOIN reads r ON r.journey_id = p.journey_id
        JOIN LATERAL (
            SELECT COUNT(DISTINCT bk.name) AS variants, MIN(bk.name) AS one_name,
                   MIN(ABS(bk.ex_showroom - r.amount)) AS gap
            FROM book bk
            WHERE bk.price_list_version_id = p.plv
              AND bk.ex_showroom IS NOT NULL
              AND ABS(bk.ex_showroom - r.amount) <= 1
        ) m ON m.variants = 1 AND m.one_name <> p.pinned_name
        ORDER BY p.journey_reference, r.document
    """,
    # The verdict, aggregated in SQL so the answer is a handful of rows rather
    # than one per figure. Asked 2026-10-24: would a price key resolve what the
    # words cannot, and does it agree where the words already succeed?
    "price_key_verdict": """
        WITH j AS (
            SELECT j.journey_id, COALESCE(b.pricing_effective_on, b.booking_date) AS priced_on,
                   jp.product_sku_id
            FROM auditcore.journeys j
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
            WHERE j.tenant_id = :t
        ), planned AS (
            SELECT j.*, (
                SELECT plv.price_list_version_id
                FROM auditcore.price_list_versions plv
                JOIN auditcore.price_lists pl
                  ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
                WHERE plv.tenant_id = :t
                  AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
                  AND plv.effective_from <= j.priced_on
                  AND (plv.effective_to IS NULL OR plv.effective_to >= j.priced_on)
                  AND (pl.price_list_code = 'OEM_NATIVE_PRICE_LIST'
                       OR NOT EXISTS (SELECT 1 FROM auditcore.price_lists own
                                       WHERE own.tenant_id = plv.tenant_id
                                         AND own.price_list_code = 'OEM_NATIVE_PRICE_LIST'))
                ORDER BY plv.effective_from DESC, plv.version_no DESC, plv.price_list_version_id DESC
                LIMIT 1) AS plv
            FROM j
        ), reads AS (
            SELECT f.journey_id,
                   ROUND(replace(COALESCE((regexp_match(f.effective_value #>> '{}',
                                 '[0-9][0-9,]*(?:\\.[0-9]+)?'))[1], '0'), ',', '')::numeric, 2) AS amount
            FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id = :t AND f.field_key = 'ex_showroom_price'
              AND f.effective_value IS NOT NULL
              AND COALESCE(f.source_document_type_key, '') NOT IN ('wholesale_invoice')
            GROUP BY 1, 2
        ), book AS (
            SELECT pli.price_list_version_id, s.product_sku_id,
                   pm.model_name || ' ~ ' || pv.variant_name AS name,
                   pm.model_name,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key = 'EX_SHOWROOM') AS ex_showroom
            FROM auditcore.price_list_items pli
            JOIN auditcore.product_skus s      ON s.product_sku_id = pli.product_sku_id
            JOIN auditcore.product_models pm   ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE pli.tenant_id = :t AND s.is_active AND pm.is_active AND pv.is_active
            GROUP BY 1, 2, 3, 4
        ), per_journey AS (
            SELECT p.journey_id, p.priced_on,
                   (p.product_sku_id IS NOT NULL) AS resolved,
                   (p.plv IS NOT NULL) AS had_book,
                   (SELECT COUNT(*) FROM reads r WHERE r.journey_id = p.journey_id) AS figures,
                   -- the tightest band at which some figure names exactly one variant
                   MIN(CASE WHEN m.variants = 1 THEN m.band END) AS pinning_band,
                   MIN(CASE WHEN m.models = 1 THEN m.band END) AS model_band,
                   MIN(m.gap) FILTER (WHERE m.gap IS NOT NULL) AS closest_gap,
                   -- The one the price names within a rupee, against the one the
                   -- Journey actually carries. The question that decides whether
                   -- a price key is safe to ship is not how many it would add
                   -- but whether it ever contradicts a SKU the words already
                   -- chose, and that cannot be assumed (2026-10-24).
                   MIN(CASE WHEN m.variants = 1 AND m.band <= 1 THEN m.one_name END) AS price_names,
                   MAX(pm.model_name || ' ~ ' || pv.variant_name) AS pinned_name
            FROM planned p
            LEFT JOIN auditcore.product_skus ps ON ps.product_sku_id = p.product_sku_id
            LEFT JOIN auditcore.product_models pm ON pm.model_id = ps.model_id
            LEFT JOIN auditcore.product_variants pv ON pv.variant_id = ps.variant_id
            LEFT JOIN reads r ON r.journey_id = p.journey_id
            LEFT JOIN LATERAL (
                SELECT band,
                       COUNT(DISTINCT bk.model_name) AS models,
                       COUNT(DISTINCT bk.name) AS variants,
                       MIN(bk.name) AS one_name,
                       MIN(ABS(bk.ex_showroom - r.amount)) AS gap
                FROM (VALUES (0.00), (1.00), (2.00), (10.00), (100.00)) AS b(band)
                JOIN book bk ON bk.price_list_version_id = p.plv
                            AND bk.ex_showroom IS NOT NULL
                            AND ABS(bk.ex_showroom - r.amount) <= b.band
                GROUP BY band
            ) m ON true
            GROUP BY p.journey_id, p.priced_on, p.product_sku_id, p.plv
        )
        SELECT COUNT(*) AS journeys,
               COUNT(*) FILTER (WHERE resolved) AS resolved_by_words,
               COUNT(*) FILTER (WHERE NOT resolved) AS not_resolved,
               COUNT(*) FILTER (WHERE figures = 0) AS no_ex_showroom_read,
               COUNT(*) FILTER (WHERE NOT had_book) AS no_price_book_in_force,
               -- Why there is no book, because the answers are different work:
               -- a Journey with no date needs its date read, one priced before
               -- the earliest book needs that month's book loaded. The first
               -- run put 74 of 171 in this bucket, so the split decides what to
               -- fix first (2026-10-24).
               COUNT(*) FILTER (WHERE NOT had_book AND priced_on IS NULL) AS no_pricing_date,
               COUNT(*) FILTER (WHERE NOT had_book AND priced_on IS NOT NULL
                                  AND priced_on < (SELECT MIN(plv.effective_from)
                                                     FROM auditcore.price_list_versions plv
                                                    WHERE plv.tenant_id = :t
                                                      AND plv.lifecycle_status IN ('PUBLISHED','RETIRED')))
                 AS priced_before_the_first_book,
               COUNT(*) FILTER (WHERE NOT had_book AND priced_on IS NOT NULL
                                  AND priced_on >= (SELECT MIN(plv.effective_from)
                                                      FROM auditcore.price_list_versions plv
                                                     WHERE plv.tenant_id = :t
                                                       AND plv.lifecycle_status IN ('PUBLISHED','RETIRED')))
                 AS priced_in_a_gap_between_books,
               -- what a price key would do with the ones the words left open
               COUNT(*) FILTER (WHERE NOT resolved AND pinning_band = 0) AS open_pinned_to_the_paisa,
               COUNT(*) FILTER (WHERE NOT resolved AND pinning_band = 1) AS open_pinned_within_a_rupee,
               COUNT(*) FILTER (WHERE NOT resolved AND pinning_band = 2) AS open_pinned_within_two,
               COUNT(*) FILTER (WHERE NOT resolved AND pinning_band > 2) AS open_pinned_only_loosely,
               COUNT(*) FILTER (WHERE NOT resolved AND pinning_band IS NULL
                                  AND model_band IS NOT NULL) AS open_narrowed_to_one_model,
               COUNT(*) FILTER (WHERE NOT resolved AND pinning_band IS NULL
                                  AND model_band IS NULL AND had_book) AS open_price_settles_nothing,
               -- and whether it agrees where the words already succeeded
               COUNT(*) FILTER (WHERE resolved AND pinning_band <= 1) AS resolved_price_pins_one_too,
               COUNT(*) FILTER (WHERE resolved AND pinning_band <= 1
                                  AND price_names = pinned_name) AS resolved_price_agrees,
               -- The number that decides whether a price key may pin: every one
               -- of these is a Journey where the price would have chosen a
               -- different variant from the one the words chose, and each has to
               -- be read before the matcher is allowed to act on a price.
               COUNT(*) FILTER (WHERE resolved AND pinning_band <= 1
                                  AND price_names <> pinned_name) AS resolved_price_disagrees,
               COUNT(*) FILTER (WHERE resolved AND pinning_band IS NULL AND had_book)
                 AS resolved_price_pins_nothing,
               ROUND(percentile_disc(0.5) WITHIN GROUP (ORDER BY closest_gap), 2) AS median_gap,
               ROUND(percentile_disc(0.9) WITHIN GROUP (ORDER BY closest_gap), 2) AS p90_gap
        FROM per_journey
    """,
    # How the ex-showroom figures are actually stored, per document type, so a
    # figure the simulation could not parse is visible rather than silently
    # making the price key look worse than it is.
    "price_reads_shape": """
        SELECT COALESCE(dt.classified_document_type_key, f.source_document_type_key) AS document,
               COUNT(*) AS reads,
               COUNT(DISTINCT f.journey_id) AS journeys,
               COUNT(*) FILTER (
                   WHERE (f.effective_value #>> '{}') ~ '[0-9][0-9,]*(\\.[0-9]+)?') AS usable_as_a_figure,
               ROUND(AVG(f.confidence_score), 3) AS mean_confidence,
               string_agg(DISTINCT left(f.effective_value #>> '{}', 24), ' | ') FILTER (
                   WHERE (f.effective_value #>> '{}') !~ '[0-9][0-9,]*(\\.[0-9]+)?') AS shapes_not_usable
        FROM auditcore.journey_document_extracted_fields f
        LEFT JOIN auditcore.document_capture_v2_documents dt
          ON dt.tenant_id = f.tenant_id AND dt.journey_id = f.journey_id
         AND dt.di_document_id = f.di_document_id
        WHERE f.tenant_id = :t AND f.field_key = 'ex_showroom_price' AND f.effective_value IS NOT NULL
        GROUP BY 1
        ORDER BY reads DESC
    """,
    # Retrospective: for every Journey, every ex-showroom figure its documents
    # carry, and what an exact match on that figure against the price book in
    # force would have found -- beside what the words actually resolved. One
    # row per figure read. This measures the change before it is made.
    "price_key_simulation": """
        WITH j AS (
            SELECT j.journey_id, j.journey_reference, j.created_by_display_name AS pc_name,
                   o.outlet_name,
                   COALESCE(NULLIF(cu.display_name, ''), j.journey_reference) AS customer,
                   COALESCE(b.pricing_effective_on, b.booking_date) AS priced_on,
                   jp.product_sku_id, jp.model_name_snapshot AS pinned_model,
                   jp.variant_name_snapshot AS pinned_variant, jp.selection_method
            FROM auditcore.journeys j
            LEFT JOIN auditcore.customers cu
              ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
            LEFT JOIN auditcore.dealer_outlets o
              ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
            WHERE j.tenant_id = :t
        ), planned AS (
            SELECT j.*, (
                SELECT plv.price_list_version_id
                FROM auditcore.price_list_versions plv
                JOIN auditcore.price_lists pl
                  ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
                WHERE plv.tenant_id = :t
                  AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
                  AND plv.effective_from <= j.priced_on
                  AND (plv.effective_to IS NULL OR plv.effective_to >= j.priced_on)
                  AND (pl.price_list_code = 'OEM_NATIVE_PRICE_LIST'
                       OR NOT EXISTS (SELECT 1 FROM auditcore.price_lists own
                                       WHERE own.tenant_id = plv.tenant_id
                                         AND own.price_list_code = 'OEM_NATIVE_PRICE_LIST'))
                ORDER BY plv.effective_from DESC, plv.version_no DESC, plv.price_list_version_id DESC
                LIMIT 1) AS plv
            FROM j
        ), raw AS (
            -- A figure a form prints as "Rs. 8,48,999.65" is the same key as
            -- the book's 848999.65. Taking the first number-like token, not
            -- stripping non-digits: stripping leaves the dot of "Rs." in front
            -- of the number and the figure is lost (caught in test, 2026-10-24).
            SELECT f.journey_id,
                   COALESCE(dt.classified_document_type_key, f.source_document_type_key) AS document,
                   replace(COALESCE((regexp_match(f.effective_value #>> '{}',
                                     '[0-9][0-9,]*(?:\\.[0-9]+)?'))[1], ''), ',', '') AS cleaned,
                   f.confidence_score
            FROM auditcore.journey_document_extracted_fields f
            LEFT JOIN auditcore.document_capture_v2_documents dt
              ON dt.tenant_id = f.tenant_id AND dt.journey_id = f.journey_id
             AND dt.di_document_id = f.di_document_id
            WHERE f.tenant_id = :t
              AND f.field_key = 'ex_showroom_price'
              AND f.effective_value IS NOT NULL
        ), reads AS (
            SELECT journey_id, document, ROUND(cleaned::numeric, 2) AS amount,
                   MAX(confidence_score) AS confidence
            FROM raw
            WHERE cleaned ~ '^[0-9]+(\\.[0-9]+)?$' AND cleaned::numeric > 10000
            GROUP BY 1, 2, 3
        ), book AS (
            SELECT pli.price_list_version_id, s.product_sku_id, pm.model_name, pv.variant_name,
                   SUM(pli.standard_amount) FILTER (WHERE pli.component_key = 'EX_SHOWROOM') AS ex_showroom
            FROM auditcore.price_list_items pli
            JOIN auditcore.product_skus s      ON s.product_sku_id = pli.product_sku_id
            JOIN auditcore.product_models pm   ON pm.model_id = s.model_id
            JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE pli.tenant_id = :t AND s.is_active AND pm.is_active AND pv.is_active
            GROUP BY 1, 2, 3, 4
        )
        SELECT p.journey_reference, p.pc_name, p.outlet_name, p.customer, p.priced_on,
               (p.product_sku_id IS NOT NULL) AS resolved_today,
               p.pinned_model, p.pinned_variant, p.selection_method,
               (SELECT bk.ex_showroom FROM book bk
                 WHERE bk.price_list_version_id = p.plv AND bk.product_sku_id = p.product_sku_id)
                 AS pinned_ex_showroom,
               r.document, r.amount AS read_amount, r.confidence,
               (p.plv IS NOT NULL) AS had_a_price_book,
               m.exact_skus, m.exact_models, m.exact_variants, m.exact_names,
               n.near_skus, n.near_models, n.near_variants, n.near_names, n.near_gap
        FROM planned p
        JOIN reads r ON r.journey_id = p.journey_id
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS exact_skus,
                   COUNT(DISTINCT bk.model_name) AS exact_models,
                   COUNT(DISTINCT bk.model_name || ' ~ ' || bk.variant_name) AS exact_variants,
                   string_agg(DISTINCT bk.model_name || ' ~ ' || bk.variant_name, ' | ') AS exact_names
            FROM book bk
            WHERE bk.price_list_version_id = p.plv AND bk.ex_showroom = r.amount
        ) m ON true
        LEFT JOIN LATERAL (
            -- Within one rupee. Measured 2026-10-24: the book carries paisa
            -- (2478999.58) and forms print the rounded rupee (2479000), so
            -- equality to the paisa almost never fires. A percentage band is
            -- useless in the other direction -- 0.5% of a 24-lakh car is
            -- 12,000 rupees and swept in three unrelated models.
            SELECT COUNT(*) AS near_skus,
                   COUNT(DISTINCT bk.model_name) AS near_models,
                   COUNT(DISTINCT bk.model_name || ' ~ ' || bk.variant_name) AS near_variants,
                   string_agg(DISTINCT bk.model_name || ' ~ ' || bk.variant_name, ' | ') AS near_names,
                   MIN(ABS(bk.ex_showroom - r.amount)) AS near_gap
            FROM book bk
            WHERE bk.price_list_version_id = p.plv
              AND bk.ex_showroom IS NOT NULL
              AND ABS(bk.ex_showroom - r.amount) <= 1
        ) n ON true
        ORDER BY (p.product_sku_id IS NOT NULL), p.journey_reference, r.document
    """,
    # What the documents say the vehicle is, per Journey and document.
    "vehicle_reads": """
        SELECT f.journey_id, f.stage_code, f.source_document_type_key AS document_type, f.field_key,
               f.effective_value #>> '{}' AS value, f.confidence_score
        FROM auditcore.journey_document_extracted_fields f
        WHERE f.tenant_id = :t
          AND f.field_key IN ('model_name_raw', 'vehicle_description_raw', 'variant_raw', 'sku_code',
                              'vehicle_color', 'model_name', 'variant_name', 'vehicle_model', 'model')
          AND f.effective_value IS NOT NULL
        ORDER BY f.journey_id, f.source_document_type_key, f.field_key
    """,
    # Registration type and customer type values actually extracted.
    "registration_types": """
        SELECT 'registration_type_code' AS field, rr.registration_type_code AS value, count(*) AS journeys
        FROM auditcore.registration_records rr WHERE rr.tenant_id = :t
        GROUP BY rr.registration_type_code
        UNION ALL
        SELECT 'customer_type_code', cu.customer_type_code, count(*)
        FROM auditcore.journeys j JOIN auditcore.customers cu
          ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
        WHERE j.tenant_id = :t GROUP BY cu.customer_type_code
        ORDER BY 1, 3 DESC
    """,
    # Every price list version of the Project with its size.
    "price_lists": """
        SELECT pl.price_list_id, pl.price_list_code, pl.price_list_name, plv.price_list_version_id,
               plv.version_no, plv.effective_from, plv.effective_to, plv.lifecycle_status,
               plv.created_at_utc,
               count(DISTINCT pli.product_sku_id) AS skus,
               count(DISTINCT s.model_id) AS models
        FROM auditcore.price_list_versions plv
        JOIN auditcore.price_lists pl ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
        LEFT JOIN auditcore.price_list_items pli
          ON pli.tenant_id = plv.tenant_id AND pli.price_list_version_id = plv.price_list_version_id
        LEFT JOIN auditcore.product_skus s ON s.product_sku_id = pli.product_sku_id
        WHERE plv.tenant_id = :t
        GROUP BY pl.price_list_id, pl.price_list_code, pl.price_list_name, plv.price_list_version_id,
                 plv.version_no, plv.effective_from, plv.effective_to, plv.lifecycle_status, plv.created_at_utc
        ORDER BY plv.effective_from, plv.version_no
    """,
    # Models per price list version (to compare versions and Projects).
    # Every OEM master upload in any state (2026-10-20: "we have price master
    # for Oct as well" but only September shows).
    "master_uploads": """
        SELECT upload_id, oem_code, master_kind, effective_from, source_filename, status,
               row_counts::text AS row_counts, left(error_detail::text, 900) AS error_detail,
               uploaded_by_actor_id, uploaded_at_utc, published_at_utc,
               (source_object_key IS NOT NULL) AS stored, price_list_version_id::text AS price_list_version_id
        FROM auditcore.oem_master_uploads
        WHERE tenant_id = :t
        ORDER BY uploaded_at_utc
    """,
    # Pages not read, last 3 days: which booking, which file and page, the
    # document service's id and the stored reason (2026-10-21: "files take
    # 12 hours -- document issue or queueing?").
    "failed_pages": """
        SELECT j.journey_reference, b.original_filename, q.page_number, q.queue_status,
               q.di_document_id::text AS di_document_id, q.classified_document_type,
               q.attempt_count, left(q.last_error, 300) AS last_error,
               to_char(q.created_at_utc AT TIME ZONE 'Asia/Kolkata', 'MM-DD HH24:MI') AS uploaded_ist,
               to_char(q.updated_at_utc AT TIME ZONE 'Asia/Kolkata', 'MM-DD HH24:MI') AS updated_ist
        FROM auditcore.p2_document_queue q
        JOIN auditcore.journeys j ON j.tenant_id = q.tenant_id AND j.journey_id = q.journey_id
        JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
        WHERE q.tenant_id = :t
          AND q.queue_status NOT IN ('READY', 'NEEDS_REVIEW', 'CANCELLED', 'MERGED', 'SUPPORTING')
          AND q.created_at_utc > now() - interval '3 days'
        ORDER BY q.created_at_utc
    """,
    # Live checks before the 2026-10-21 fix merges (price masters, invoices).
    "pre_merge_checks": """
        SELECT 'DMS_TALLY_PAIR' AS kind, j.journey_reference AS a, v.document_type_key AS b,
               coalesce(v.invoice_number, '') || ' | ' || coalesce(v.invoice_date::text, '') AS c,
               coalesce(v.vin_number, '') || ' | ' || coalesce(v.grand_total_amount::text, '') AS d
        FROM auditcore.invoice_review_values v
        JOIN auditcore.journeys j ON j.tenant_id = v.tenant_id AND j.journey_id = v.journey_id
        WHERE v.tenant_id = :t AND v.journey_id IN (
            SELECT journey_id FROM auditcore.invoice_review_values WHERE tenant_id = :t
            GROUP BY journey_id
            HAVING bool_or(document_type_key LIKE '%dms%') AND bool_or(document_type_key LIKE '%tally%'))
        UNION ALL
        SELECT 'CREDIT_NOTE_EQ_VEHICLE', cn.journey_id::text, cn.invoice_number,
               cn.grand_total_amount::text, v.invoice_number
        FROM auditcore.invoice_review_values cn
        JOIN auditcore.invoice_review_values v
          ON v.tenant_id = cn.tenant_id AND v.journey_id = cn.journey_id
         AND v.document_type_key IN ('customer_invoice_dms','tax_invoice_tally','tax_invoice_dms','tax_invoice',
                                     'customer_invoice_dms_v2')
         AND abs(v.grand_total_amount - cn.grand_total_amount) <= 1
        WHERE cn.tenant_id = :t AND cn.document_type_key = 'credit_note'
        UNION ALL
        SELECT 'GENERIC_NO_PURPOSE', journey_id::text, invoice_number, grand_total_amount::text, NULL
        FROM auditcore.invoice_review_values
        WHERE tenant_id = :t AND document_type_key = 'invoice_generic' AND invoice_purpose IS NULL
        UNION ALL
        SELECT 'SOURCE_TYPE_SPELLING', source_document_type, count(*)::text, NULL, NULL
        FROM auditcore.commercial_line_source_values WHERE tenant_id = :t
        GROUP BY source_document_type
        UNION ALL
        SELECT 'BOOKING_BEFORE_FIRST_BOOK', b.journey_id::text,
               coalesce(b.pricing_effective_on, b.booking_date)::text, NULL, NULL
        FROM auditcore.bookings b
        WHERE b.tenant_id = :t AND coalesce(b.pricing_effective_on, b.booking_date) < (
            SELECT min(v.effective_from) FROM auditcore.price_lists pl
            JOIN auditcore.price_list_versions v ON v.tenant_id = pl.tenant_id AND v.price_list_id = pl.price_list_id
            WHERE pl.tenant_id = :t AND pl.price_list_code = 'OEM_NATIVE_PRICE_LIST'
              AND v.lifecycle_status IN ('PUBLISHED', 'RETIRED'))
        UNION ALL
        SELECT 'PINNED_SKU', s.sku_code, jp.selection_status || ' ' || coalesce(jp.selection_method, ''),
               coalesce(b.pricing_effective_on, b.booking_date)::text, pm.model_name
        FROM auditcore.journey_products jp
        JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
        JOIN auditcore.product_models pm ON pm.model_id = s.model_id
        LEFT JOIN auditcore.bookings b ON b.tenant_id = jp.tenant_id AND b.journey_id = jp.journey_id
        WHERE jp.tenant_id = :t
        UNION ALL
        SELECT 'PRICE_LISTS', pl.price_list_code, count(v.*)::text, max(v.effective_from)::text, NULL
        FROM auditcore.price_lists pl
        LEFT JOIN auditcore.price_list_versions v ON v.tenant_id = pl.tenant_id AND v.price_list_id = pl.price_list_id
        WHERE pl.tenant_id = :t
        GROUP BY pl.price_list_code
        ORDER BY 1, 2
    """,
    # OEM catalogue identity collisions (not tenant data).
    "model_identity": """
        SELECT upper(regexp_replace(pm.model_name, '[^A-Za-z0-9]', '', 'g')) AS squashed,
               string_agg(pm.model_code || ' / ' || pm.model_name, ' ; ' ORDER BY pm.created_at_utc) AS models,
               string_agg((SELECT count(*) FROM auditcore.product_skus s WHERE s.model_id = pm.model_id)::text,
                          ',' ORDER BY pm.created_at_utc) AS skus
        FROM auditcore.product_models pm JOIN auditcore.oems o ON o.oem_id = pm.oem_id
        WHERE o.oem_code = 'MAHINDRA'
        GROUP BY 1
        ORDER BY 1
    """,
    # Every Journey: the date it should be priced on, the price-list version
    # it is actually priced from, and whether that version was in force on
    # that date (2026-10-20: "prices are not being picked as per dates").
    "pricing_dates": """
        SELECT j.journey_reference, j.journey_id, b.booking_date, b.pricing_effective_on, b.pricing_basis,
               s.sku_code, jp.selection_method, jp.selection_status, jp.vehicle_category,
               used.effective_from AS used_from, used.effective_to AS used_to, used.version_no AS used_version,
               pl.price_list_code AS used_list,
               (SELECT string_agg(pl2.price_list_code || ' v' || v2.version_no || ' ' || v2.effective_from::text, '; ')
                FROM auditcore.price_list_versions v2
                JOIN auditcore.price_lists pl2 ON pl2.tenant_id = v2.tenant_id AND pl2.price_list_id = v2.price_list_id
                WHERE v2.tenant_id = j.tenant_id AND v2.lifecycle_status = 'PUBLISHED'
                  AND v2.effective_from <= coalesce(b.pricing_effective_on, b.booking_date)
                  AND (v2.effective_to IS NULL OR v2.effective_to >= coalesce(b.pricing_effective_on, b.booking_date))
               ) AS in_force_on_date,
               (SELECT pli.standard_amount::text FROM auditcore.price_list_items pli
                WHERE pli.tenant_id = j.tenant_id AND pli.price_list_version_id = j.price_list_version_id
                  AND pli.product_sku_id = jp.product_sku_id AND pli.component_key = 'ex_showroom_price'
                LIMIT 1) AS used_ex_showroom
        FROM auditcore.journeys j
        LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
        LEFT JOIN auditcore.price_list_versions used
          ON used.tenant_id = j.tenant_id AND used.price_list_version_id = j.price_list_version_id
        LEFT JOIN auditcore.price_lists pl ON pl.tenant_id = used.tenant_id AND pl.price_list_id = used.price_list_id
        WHERE j.tenant_id = :t
        ORDER BY b.booking_date NULLS FIRST, j.journey_reference
    """,
    "price_list_models": """
        SELECT pl.price_list_code, plv.version_no, plv.effective_from, pm.model_name,
               count(DISTINCT pli.product_sku_id) AS skus,
               string_agg(DISTINCT pv.attributes ->> 'category', ',') AS category
        FROM auditcore.price_list_versions plv
        JOIN auditcore.price_lists pl ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
        JOIN auditcore.price_list_items pli
          ON pli.tenant_id = plv.tenant_id AND pli.price_list_version_id = plv.price_list_version_id
        JOIN auditcore.product_skus s ON s.product_sku_id = pli.product_sku_id
        JOIN auditcore.product_models pm ON pm.model_id = s.model_id
        LEFT JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
        WHERE plv.tenant_id = :t AND plv.lifecycle_status = 'PUBLISHED'
        GROUP BY pl.price_list_code, plv.version_no, plv.effective_from, pm.model_name
        ORDER BY plv.effective_from, plv.version_no, pm.model_name
    """,
    # Every date before the 2026-07-01 floor that a Journey carries, where it
    # came from, whether its document is still on file and what task (if any)
    # asked the PC to review it (2026-10-20: SU-261020-001 "Booked 2020-10-13").
    "old_dates": """
        SELECT j.journey_id, j.journey_reference, 'booking_confirm_date' AS what,
               ss.booking_confirm_date::text AS value, NULL AS document_type, NULL AS document_status,
               NULL::numeric AS confidence, NULL AS review_task
        FROM auditcore.journeys j
        JOIN auditcore.journey_stage_states ss
          ON ss.tenant_id = j.tenant_id AND ss.journey_id = j.journey_id AND ss.stage_code = 'BOOKING'
        WHERE j.tenant_id = :t AND ss.booking_confirm_date < DATE '2026-07-01'
        UNION ALL
        SELECT j.journey_id, j.journey_reference, 'bookings.booking_date', b.booking_date::text,
               NULL, NULL, NULL, NULL
        FROM auditcore.journeys j
        JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        WHERE j.tenant_id = :t AND b.booking_date < DATE '2026-07-01'
        UNION ALL
        SELECT p.journey_id, j.journey_reference, 'payments.receipt_date', p.receipt_date::text,
               e.document_type_key, e.association_status, NULL,
               (SELECT string_agg(t.task_status, ',') FROM auditcore.p2_tasks t
                 WHERE t.tenant_id = p.tenant_id AND t.journey_id = p.journey_id
                   AND t.dedupe_key LIKE 'field-review:%' || p.source_di_document_id::text || '%')
        FROM auditcore.payments p
        JOIN auditcore.journeys j ON j.tenant_id = p.tenant_id AND j.journey_id = p.journey_id
        LEFT JOIN auditcore.evidence e
          ON e.tenant_id = p.tenant_id AND e.journey_id = p.journey_id
         AND e.di_document_id = p.source_di_document_id
        WHERE p.tenant_id = :t AND p.receipt_date < DATE '2026-07-01'
        UNION ALL
        SELECT f.journey_id, j.journey_reference, 'field:' || f.field_key, f.effective_value #>> '{}',
               f.source_document_type_key, e.association_status, f.confidence_score,
               (SELECT string_agg(t.task_status, ',') FROM auditcore.p2_tasks t
                 WHERE t.tenant_id = f.tenant_id AND t.journey_id = f.journey_id
                   AND t.dedupe_key LIKE 'field-review:%' || f.di_document_id::text || '%')
        FROM auditcore.journey_document_extracted_fields f
        JOIN auditcore.journeys j ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
        LEFT JOIN auditcore.evidence e
          ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id AND e.di_document_id = f.di_document_id
        WHERE f.tenant_id = :t
          AND f.field_key IN ('booking_date', 'receipt_date', 'delivery_date', 'invoice_date', 'gate_pass_date')
          AND (f.effective_value #>> '{}') !~ '2026'
        UNION ALL
        SELECT j.journey_id, j.journey_reference, 'deliveries.actual_delivered_at',
               dl.actual_delivered_at::date::text, dl.status_source, e.association_status, NULL,
               (SELECT string_agg(t.dedupe_key || '=' || t.task_status, ',') FROM auditcore.p2_tasks t
                 WHERE t.tenant_id = dl.tenant_id AND t.journey_id = dl.journey_id
                   AND t.dedupe_key LIKE 'field-review:%')
        FROM auditcore.deliveries dl
        JOIN auditcore.journeys j ON j.tenant_id = dl.tenant_id AND j.journey_id = dl.journey_id
        LEFT JOIN auditcore.evidence e
          ON e.tenant_id = dl.tenant_id AND e.evidence_id = dl.source_evidence_id
        WHERE dl.tenant_id = :t AND dl.actual_delivered_at < DATE '2026-07-01'
    """,
    # Why a date before the floor on a document still on file has (or has
    # not) a PC review task: who reviewed / corrected it, the document's
    # stage and every task on that document (2026-10-20).
    "date_review_state": """
        SELECT f.journey_id, j.journey_reference, f.stage_code, f.source_document_type_key AS document_type,
               e.document_type_key AS evidence_type, f.field_key, f.effective_value #>> '{}' AS value,
               f.reviewed_by_actor_id, f.reviewed_at_utc, f.is_modified, f.modified_by_actor_id,
               (SELECT string_agg(t.task_type || ':' || t.task_status || ':' || left(t.title, 60), ' | ')
                  FROM auditcore.p2_tasks t
                 WHERE t.tenant_id = f.tenant_id AND t.journey_id = f.journey_id
                   AND (t.dedupe_key LIKE '%' || f.di_document_id::text || '%'
                        OR t.task_type ILIKE '%DATE%')) AS tasks
        FROM auditcore.journey_document_extracted_fields f
        JOIN auditcore.journeys j ON j.tenant_id = f.tenant_id AND j.journey_id = f.journey_id
        JOIN auditcore.evidence e
          ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id AND e.di_document_id = f.di_document_id
        WHERE f.tenant_id = :t AND e.association_status = 'ACTIVE'
          AND f.field_key IN ('booking_date', 'receipt_date', 'delivery_date', 'invoice_date')
          AND (f.effective_value #>> '{}') !~ '2026'
    """,
    # The booking and delivery dates each Journey shows now, and where from.
    "booking_state": """
        SELECT j.journey_id, j.journey_reference, COALESCE(c.legal_name, c.display_name) AS customer,
               ss.business_status, ss.booking_confirm_date::text AS booking_confirm_date,
               ss.booking_confirmed_at_utc::text AS booking_confirmed_at_utc,
               b.booking_date::text AS booking_form_date,
               dl.actual_delivered_at::date::text AS delivered_on, dl.status_source AS delivery_source
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.journey_stage_states ss
          ON ss.tenant_id = j.tenant_id AND ss.journey_id = j.journey_id AND ss.stage_code = 'BOOKING'
        LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        LEFT JOIN auditcore.deliveries dl ON dl.tenant_id = j.tenant_id AND dl.journey_id = j.journey_id
        WHERE j.tenant_id = :t
        ORDER BY j.journey_reference
    """,
    # Each open duplicate-booking finding with the identity values both
    # sides were compared on (2026-10-20: Keshab vs Sarbeswar Pradhan,
    # "customer name matches a relative"). PAN / Aadhaar only as present or not.
    "duplicate_pairs": """
        WITH f AS (
            SELECT fi.journey_id, fi.audit_finding_id, fi.finding_status, fi.created_at_utc,
                   (SELECT ev.safe_payload FROM auditcore.audit_finding_events ev
                     WHERE ev.tenant_id = fi.tenant_id AND ev.audit_finding_id = fi.audit_finding_id
                     ORDER BY ev.occurred_at_utc DESC LIMIT 1) AS payload
            FROM auditcore.audit_findings fi
            WHERE fi.tenant_id = :t AND fi.finding_type_code = 'DUPLICATE_BOOKING'
              AND fi.finding_status IN ('OPEN', 'ACKNOWLEDGED')
        ), ids AS (
            SELECT x.journey_id,
                   string_agg(DISTINCT x.field_key || '=' ||
                       CASE WHEN x.field_key IN ('pan_number', 'aadhaar_number')
                            THEN 'present' ELSE COALESCE(x.effective_value #>> '{}', '') END
                       || ' [' || x.source_document_type_key || ']', ' | ') AS identity
            FROM auditcore.journey_document_extracted_fields x
            JOIN auditcore.evidence e ON e.tenant_id = x.tenant_id AND e.journey_id = x.journey_id
             AND e.di_document_id = x.di_document_id AND e.association_status = 'ACTIVE'
            WHERE x.tenant_id = :t AND x.field_key IN (
                'pan_number', 'aadhaar_number', 'aadhaar_name', 'pan_name', 'customer_name',
                'aadhaar_relationship_name', 'pan_relationship_name', 'address_pincode')
              AND x.effective_value IS NOT NULL
            GROUP BY x.journey_id
        )
        SELECT f.journey_id AS flagged_journey, jf.journey_reference AS flagged_ref,
               f.payload ->> 'matchBasis' AS basis, f.payload ->> 'believedOriginalJourneyId' AS holder_journey,
               jh.journey_reference AS holder_ref, idf.identity AS flagged_identity, idh.identity AS holder_identity,
               f.payload::text AS payload
        FROM f
        JOIN auditcore.journeys jf ON jf.tenant_id = :t AND jf.journey_id = f.journey_id
        LEFT JOIN auditcore.journeys jh
          ON jh.tenant_id = :t AND jh.journey_id::text = f.payload ->> 'believedOriginalJourneyId'
        LEFT JOIN ids idf ON idf.journey_id = f.journey_id
        LEFT JOIN ids idh ON idh.journey_id = jh.journey_id
    """,
    # Where each Journey's customer name came from (2026-10-20: journeys
    # holding the same PAN showed different customer names).
    "customer_names": """
        SELECT j.journey_id, j.journey_reference, c.display_name, c.legal_name, c.legal_name_status,
               c.legal_name_verified_by_actor_id, c.legal_name_verified_at_utc::text AS verified_at,
               e.document_type_key AS source_type, e.association_status AS source_status,
               (SELECT string_agg(k.document_type_key || ':' || k.association_status || ':' ||
                                  COALESCE(f.effective_value #>> '{}', ''), ' | ' ORDER BY k.linked_at_utc)
                  FROM auditcore.evidence k
                  JOIN auditcore.journey_document_extracted_fields f
                    ON f.tenant_id = k.tenant_id AND f.journey_id = k.journey_id
                   AND f.di_document_id = k.di_document_id
                 WHERE k.tenant_id = j.tenant_id AND k.journey_id = j.journey_id
                   AND k.document_type_key IN ('pan_card', 'aadhaar', 'customer_kyc')
                   AND f.field_key IN ('pan_name', 'aadhaar_name', 'customer_name')) AS kyc_names
        FROM auditcore.journeys j
        JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.evidence e
          ON e.tenant_id = c.tenant_id AND e.evidence_id = c.legal_name_source_evidence_id
        WHERE j.tenant_id = :t
        ORDER BY j.journey_reference
    """,
    # Uploads per PC per day, last 5 days (2026-10-20: the daily report showed
    # one PC uploading and the rest with only tasks).
    "uploads_by_day": """
        SELECT (ub.created_at_utc AT TIME ZONE 'Asia/Kolkata')::date AS day, ub.uploaded_by_actor_id,
               count(DISTINCT ub.journey_id) AS journeys, count(*) AS files, sum(ub.page_count) AS pages,
               string_agg(DISTINCT ub.batch_status, ',') AS statuses
        FROM auditcore.p2_upload_batches ub
        WHERE ub.tenant_id = :t AND ub.created_at_utc > now() - interval '5 days'
        GROUP BY 1, 2 ORDER BY 1, 2
    """,
    # Tasks created / reopened today (IST) by type, source and hour.
    "tasks_today": """
        SELECT 'OPENED' AS kind, tk.task_type, tk.source_type, tk.assigned_role_code,
               to_char(tk.created_at_utc AT TIME ZONE 'Asia/Kolkata', 'HH24') AS hour_ist, count(*) AS n
        FROM auditcore.p2_tasks tk
        WHERE tk.tenant_id = :t
          AND tk.created_at_utc >= ((now() AT TIME ZONE 'Asia/Kolkata')::date)::timestamp AT TIME ZONE 'Asia/Kolkata'
        GROUP BY 1, 2, 3, 4, 5
        UNION ALL
        SELECT 'REOPENED', tk.task_type, tk.source_type, tk.assigned_role_code,
               to_char(te.created_at_utc AT TIME ZONE 'Asia/Kolkata', 'HH24'), count(*)
        FROM auditcore.p2_task_events te
        JOIN auditcore.p2_tasks tk ON tk.tenant_id = te.tenant_id AND tk.task_id = te.task_id
        WHERE te.tenant_id = :t AND te.event_type = 'REOPENED'
          AND te.created_at_utc >= ((now() AT TIME ZONE 'Asia/Kolkata')::date)::timestamp AT TIME ZONE 'Asia/Kolkata'
        GROUP BY 1, 2, 3, 4, 5
        ORDER BY 1, 5, 2
    """,
    # The candidates on each open MODEL_NOT_IDENTIFIED finding.
    "open_sku_candidates": """
        SELECT f.journey_id, f.title, e.safe_payload -> 'candidates' AS candidates,
               e.safe_payload ->> 'matchStage' AS match_stage
        FROM auditcore.audit_findings f
        JOIN LATERAL (
            SELECT ev.safe_payload FROM auditcore.audit_finding_events ev
            WHERE ev.tenant_id = f.tenant_id AND ev.audit_finding_id = f.audit_finding_id
            ORDER BY ev.occurred_at_utc DESC LIMIT 1
        ) e ON true
        WHERE f.tenant_id = :t AND f.finding_type_code = 'MODEL_NOT_IDENTIFIED'
          AND f.finding_status IN ('OPEN', 'ACKNOWLEDGED')
    """,
    # Registration / road tax: every value read, every source row and the
    # line the deal shows, per Journey (2026-10-20: road tax and
    # registration mapped wrongly and twice).
    "registration_sources": """
        SELECT 'READ' AS kind, f.journey_id, f.source_document_type_key AS document_type, f.field_key AS key,
               left(f.effective_value #>> '{}', 400) AS value, NULL AS standard
        FROM auditcore.journey_document_extracted_fields f
        WHERE f.tenant_id = :t AND f.effective_value IS NOT NULL
          AND f.field_key ~ '(registration|road_tax|rto|green_tax|hsrp|line_items)'
          AND f.field_key NOT IN ('vehicle_registration_number', 'registration_number', 'old_vehicle_registration')
          AND f.journey_id IN (SELECT journey_id FROM auditcore.journeys WHERE tenant_id = :t
                               AND updated_at_utc > now() - interval '4 days')
        UNION ALL
        SELECT 'SOURCE', sv.journey_id, sv.source_document_type, sv.component_key, sv.amount::text, NULL
        FROM auditcore.commercial_line_source_values sv
        WHERE sv.tenant_id = :t AND sv.component_key ~ '(registration|road_tax|rto|green_tax|hsrp)'
        UNION ALL
        SELECT 'LINE', cl.journey_id, cl.source_reference, cl.component_key, cl.actual_amount::text,
               cl.standard_amount::text
        FROM auditcore.commercial_lines cl
        WHERE cl.tenant_id = :t AND cl.component_key ~ '(registration|road_tax|rto|green_tax|hsrp)'
        ORDER BY 2, 1, 4, 3
    """,
    # One Journey's vehicle reads and SKU state, for every Journey with an
    # open model finding or no SKU (2026-10-20: "still SKU are not getting
    # resolved").
    "sku_unresolved": """
        SELECT j.journey_id, COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
               jp.selection_status, jp.selection_method, jp.model_name_snapshot AS model,
               jp.variant_name_snapshot AS variant, jp.updated_at_utc,
               (SELECT string_agg(f.source_document_type_key || '.' || f.field_key || '=' ||
                                  left(f.effective_value #>> '{}', 120), ' | ' ORDER BY f.field_key)
                  FROM auditcore.journey_document_extracted_fields f
                 WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id
                   AND f.effective_value IS NOT NULL
                   AND f.field_key IN ('model_name_raw', 'vehicle_description_raw', 'variant_raw', 'sku_code',
                                       'vehicle_color', 'model_name', 'variant_name', 'vehicle_model', 'model',
                                       'fuel_type', 'transmission')) AS reads
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        WHERE j.tenant_id = :t
          AND (jp.product_sku_id IS NULL OR EXISTS (
                SELECT 1 FROM auditcore.audit_findings af
                WHERE af.tenant_id = j.tenant_id AND af.journey_id = j.journey_id
                  AND af.finding_type_code = 'MODEL_NOT_IDENTIFIED'
                  AND af.finding_status IN ('OPEN', 'ACKNOWLEDGED')))
        ORDER BY j.updated_at_utc DESC
    """,
    # One Journey's discount entitlements and every discount-master row that
    # could apply to its vehicle (2026-10-20: a Rs 2,50,000 cash discount on
    # an XUV 3XO). AUDIT_CORE_DIAG_CUSTOMER matches the customer, the journey
    # id or the journey reference.
    "discount_journey": """
        WITH js AS (
            SELECT j.journey_id, j.journey_reference, jp.product_sku_id, s.model_id, s.variant_id,
                   pv.variant_name
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
            LEFT JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
            WHERE j.tenant_id = :t
              AND (c.display_name ILIKE :c OR j.journey_id::text ILIKE :c OR j.journey_reference ILIKE :c)
        )
        SELECT 'APPLIED' AS kind, js.journey_reference AS ref, da.discount_key AS a,
               da.standard_eligible_amount::text AS b, da.actual_discount_amount::text AS c,
               da.eligibility_result || ' ' || coalesce(da.details::text, '') AS d,
               js.variant_name AS e, NULL AS f
        FROM js JOIN auditcore.discount_applications da
          ON da.tenant_id = :t AND da.journey_id = js.journey_id
        UNION ALL
        SELECT 'MASTER', js.journey_reference, b.benefit_key, b.amount_value::text,
               ds.scheme_code || ' / ' || coalesce(ds.scheme_category, '') || ' / ' || ds.scheme_name,
               'v' || dsv.version_no || ' ' || dsv.lifecycle_status || ' ' || dsv.effective_from::text
                   || '..' || coalesce(dsv.effective_to::text, '') || ' cust=' || coalesce(e.customer_type_code, '*')
                   || ' criteria=' || e.criteria::text,
               'variant=' || coalesce(pv.variant_name, '*') || ' sku=' || coalesce(e.product_sku_id::text, '*')
                   || ' dealer=' || coalesce(e.dealer_id::text, '*'),
               b.benefit_config::text
        FROM js
        JOIN auditcore.discount_scheme_eligibility e
          ON e.tenant_id = :t AND e.model_id = js.model_id
        JOIN auditcore.discount_scheme_versions dsv
          ON dsv.tenant_id = e.tenant_id AND dsv.discount_scheme_version_id = e.discount_scheme_version_id
        JOIN auditcore.discount_schemes ds
          ON ds.tenant_id = dsv.tenant_id AND ds.discount_scheme_id = dsv.discount_scheme_id
        JOIN auditcore.discount_scheme_benefits b
          ON b.tenant_id = dsv.tenant_id AND b.discount_scheme_version_id = dsv.discount_scheme_version_id
        LEFT JOIN auditcore.product_variants pv ON pv.variant_id = e.variant_id
        WHERE e.variant_id IS NULL OR e.variant_id = js.variant_id
        ORDER BY 2, 1, 3
    """,
    # One customer's deal, every figure and where it came from
    # (AUDIT_CORE_DIAG_CUSTOMER, an ILIKE pattern; 2026-10-20: "how the SKU
    # cost is arrived at and how the billing data is arrived").
    "deal_journey": """
        WITH js AS (
            SELECT j.journey_id, j.price_list_version_id, jp.product_sku_id
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            WHERE j.tenant_id = :t AND (c.display_name ILIKE :c OR j.journey_id::text ILIKE :c)
        )
        SELECT 'SKU' AS kind, js.journey_id, s.sku_code AS a, jp.selection_method AS b,
               plv.version_no::text || ' from ' || plv.effective_from::text AS c, jp.vehicle_category AS d
        FROM js
        LEFT JOIN auditcore.product_skus s ON s.product_sku_id = js.product_sku_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = :t AND jp.journey_id = js.journey_id
        LEFT JOIN auditcore.price_list_versions plv
          ON plv.tenant_id = :t AND plv.price_list_version_id = js.price_list_version_id
        UNION ALL
        SELECT 'PRICE_LIST_ITEM', js.journey_id, pli.component_key, pli.standard_amount::text, NULL, NULL
        FROM js JOIN auditcore.price_list_items pli
          ON pli.tenant_id = :t AND pli.price_list_version_id = js.price_list_version_id
         AND pli.product_sku_id = js.product_sku_id
        UNION ALL
        SELECT 'LINE', cl.journey_id, cl.component_key, cl.standard_amount::text, cl.actual_amount::text,
               cl.source_reference
        FROM js JOIN auditcore.commercial_lines cl ON cl.tenant_id = :t AND cl.journey_id = js.journey_id
        UNION ALL
        SELECT 'SOURCE', sv.journey_id, sv.line_kind || ':' || sv.component_key, sv.amount::text,
               sv.source_document_type, sv.source_document_id::text
        FROM js JOIN auditcore.commercial_line_source_values sv ON sv.tenant_id = :t AND sv.journey_id = js.journey_id
        UNION ALL
        SELECT 'DISCOUNT', da.journey_id, da.discount_key, da.standard_eligible_amount::text,
               da.actual_discount_amount::text, da.eligibility_result
        FROM js JOIN auditcore.discount_applications da ON da.tenant_id = :t AND da.journey_id = js.journey_id
        UNION ALL
        SELECT 'z_READ', f.journey_id, f.source_document_type_key || '.' || f.field_key,
               left(f.effective_value #>> '{}', 900), f.stage_code, NULL
        FROM js JOIN auditcore.journey_document_extracted_fields f ON f.tenant_id = :t AND f.journey_id = js.journey_id
        WHERE f.effective_value IS NOT NULL
          AND f.source_document_type_key IN ('customer_invoice_dms', 'tax_invoice_tally', 'booking_form',
                                             'booking_docket', 'cost_sheet', 'credit_note')
          AND (f.field_key ~ '(amount|price|total|discount|tax|gst|cess|line_items|bonus|tcs|charges)'
               OR f.field_key IN ('model_name_raw', 'variant_raw', 'vehicle_description_raw', 'vehicle_model'))
        UNION ALL
        SELECT 'EVENT', e.journey_id, e.event_type, left(e.details::text, 600),
               to_char(e.created_at_utc, 'YYYY-MM-DD HH24:MI'), NULL
        FROM js JOIN auditcore.p2_activity_events e ON e.tenant_id = :t AND e.journey_id = js.journey_id
        WHERE e.event_type ~ '(SKU|MODEL|PRICE)'
        -- The document reads sort last ("z_READ"), so the row cap never cuts
        -- the SKU, price list and lines.
        ORDER BY 2, 1, 3
    """,
}


def _projects(engine: Engine) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        return [dict(r) for r in connection.execute(
            text("SELECT tenant_id, project_code, project_name, project_status FROM auditcore.projects")
        ).mappings()]


def _date_floor() -> date:
    """The programme's date floor, for the reports that ask which dates are
    misreads. Falls back to a date before any real booking rather than failing
    the whole run if the registry cannot be read."""
    try:
        from audit_core.uc03_p2_dates import programme_date_floor

        return programme_date_floor() or date(1900, 1, 1)
    except Exception:  # noqa: BLE001 - a diagnostic must never fail on config
        return date(1900, 1, 1)


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)[:1500]
    return str(value)


def run_diagnostics(engine: Engine, names: list[str]) -> None:
    wanted = list(REPORTS) if names == ["all"] else [n for n in names if n in REPORTS]
    projects = _projects(engine)
    for project in projects:
        logger.info("diag_project", **{k: _plain(v) for k, v in project.items()})
    for project in projects:
        tenant_id = str(project["tenant_id"])
        for name in wanted:
            try:
                with engine.begin() as connection:
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    set_tenant_context(connection, tenant_id)
                    rows = connection.execute(
                        text(REPORTS[name]),
                        {"t": tenant_id, "c": os.environ.get("AUDIT_CORE_DIAG_CUSTOMER", "") or "%",
                         "floor": _date_floor()},
                    ).mappings().all()
                for row in rows[:_MAX_ROWS]:
                    logger.info("diag_row", report=name, tenant_id=tenant_id,
                                project=project["project_name"], **{k: _plain(v) for k, v in row.items()})
                logger.info("diag_report_done", report=name, tenant_id=tenant_id, rows=len(rows))
            except Exception:
                logger.warning("diag_report_failed", report=name, tenant_id=tenant_id, exc_info=True)
