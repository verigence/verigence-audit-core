"""Read-only diagnostics, run by the P2 worker and written to the logs.

Set AUDIT_CORE_DIAG_REPORTS on the worker to a comma-separated list of the
report names below (or "all"); on start the worker runs each, read-only,
for every active Project, and logs one ``diag_row`` line per row. Nobody
needs database access to see the answers (2026-10-20: the product owner
does not work in the database). Unset the variable when done.
"""
from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context

logger = structlog.get_logger(__name__)
_MAX_ROWS = 600

REPORTS: dict[str, str] = {
    # Every Journey's vehicle: what was read, the SKU pinned, how and when.
    "sku_pins": """
        SELECT j.journey_id, COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
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
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
        LEFT JOIN auditcore.product_models pm ON pm.model_id = s.model_id
        LEFT JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
        WHERE j.tenant_id = :t
        ORDER BY j.created_at_utc
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
        WHERE f.tenant_id = :t AND f.field_key IN ('booking_date', 'receipt_date')
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
}


def _projects(engine: Engine) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        return [dict(r) for r in connection.execute(
            text("SELECT tenant_id, project_code, project_name, project_status FROM auditcore.projects")
        ).mappings()]


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
                    rows = connection.execute(text(REPORTS[name]), {"t": tenant_id}).mappings().all()
                for row in rows[:_MAX_ROWS]:
                    logger.info("diag_row", report=name, tenant_id=tenant_id,
                                project=project["project_name"], **{k: _plain(v) for k, v in row.items()})
                logger.info("diag_report_done", report=name, tenant_id=tenant_id, rows=len(rows))
            except Exception:
                logger.warning("diag_report_failed", report=name, tenant_id=tenant_id, exc_info=True)
