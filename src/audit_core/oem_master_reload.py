"""One-off re-load of the Mahindra price masters (2026-10-21).

The live September books were loaded with dates typed by hand that do not
match the sheets (the 03 Sep book from 02 Sep, the 07 Sep EV additions from
03 Sep, no book for 01 Sep, no October book). Published versions cannot be
changed or hidden, and a RETIRED one still answers for its dates, so the
fix is to publish the right file again on each wrong date: the later upload
on a date wins. The re-review of PR 468 validated five uploads that give the
right book on every day:

  01Sep consolidated @ 2026-09-01   the OEM book for 1-2 Sep
  01Sep consolidated @ 2026-09-02   supersedes the 03 Sep book typed 02 Sep
  03Sep consolidated @ 2026-09-03   supersedes the EV versions typed 03 Sep
  02 Nos Added EV    @ 2026-09-07   the two additions from 7 Sep
  October            @ 2026-10-03   October from 3 Oct

The live 08 Sep and 19 Sep versions (the 03 Sep book plus the additions,
then plus Veero LNT) are already right and are not touched.

Run by the worker when AUDIT_CORE_MASTER_RELOAD_TAG is set. A tag containing
"-schemes" re-loads the scheme masters instead (``SCHEME_RELOAD_PLAN``,
review of PR 472). Once per tag and
tenant (a durable claim in auditcore.oem_master_reload_runs). A DRY RUN
unless the tag ends with "-commit": every check is run and logged and the
transaction is rolled back. Per Mahindra tenant, ONE transaction: the five
ingests, an oem_master_uploads row for each (with the file stored where the
upload route stores it, when storage is configured), then the checks; any
failed check rolls everything back and is logged.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.oem_master_parsers import parse_price_list
from audit_core.oem_price_masters import (
    _master_content_type,
    _master_object_key,
    find_book_sku,
    ingest_price_list,
)
from audit_core.price_lists import OEM_NATIVE_PRICE_LIST_CODE, find_effective_price_plan

logger = structlog.get_logger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "oem_masters" / "mahindra"
RELOAD_PLAN: tuple[tuple[str, date], ...] = (
    ("2026-09/Mahindra_Consolidated_Price_List_01Sep2026.xlsx", date(2026, 9, 1)),
    ("2026-09/Mahindra_Consolidated_Price_List_01Sep2026.xlsx", date(2026, 9, 2)),
    ("2026-09/Mahindra_Consolidated_Price_List_03Sep2026.xlsx", date(2026, 9, 3)),
    ("2026-09/02 Nos Added EV PRICE -07th Sept'26.xlsx", date(2026, 9, 7)),
    ("2026-10/Mahindra_Consolidated_Price_List_Oct2026.xlsx", date(2026, 10, 3)),
)
# SKUs in the book in force on each date after the re-load (the live-history
# harness and the re-review: 323 / 323 / 317 / 319 / 319 / 333 / 327). The 08
# and 19 Sep books are the live versions the plan does not touch: on those
# dates the check is "the same version, unchanged, still in force", and its
# count is printed (19 Sep depends on the live Veero sheet: 333 in the
# harness with a 26-row stand-in).
EXPECTED_SKUS: dict[date, int | None] = {
    date(2026, 9, 1): 323,
    date(2026, 9, 2): 323,
    date(2026, 9, 3): 317,
    date(2026, 9, 7): 319,
    date(2026, 9, 8): 319,
    date(2026, 9, 19): None,
    date(2026, 10, 3): 327,
}
UNTOUCHED_DATES = (date(2026, 9, 8), date(2026, 9, 19))
SPOT_PRICES = (
    ("MAXX CITY", "MAXX CITY 1.5 LX BS6.2", date(2026, 9, 15), Decimal("839501.15")),
    ("MAXX CITY", "MAXX CITY 1.5 LX BS6.2", date(2026, 10, 15), Decimal("852000.62")),
)


def mahindra_tenants(engine: Engine) -> list[str]:
    with engine.begin() as connection:
        set_platform_super_admin_context(connection)
        return list(connection.execute(text(
            """
            SELECT p.tenant_id FROM auditcore.projects p JOIN auditcore.oems o ON o.oem_id = p.oem_id
            WHERE o.oem_code = 'MAHINDRA' AND p.project_status = 'ACTIVE' ORDER BY p.tenant_id
            """
        )).scalars().all())


def _book(connection: Connection, tenant_id: str, on: date) -> tuple[dict[str, Any] | None, set[UUID]]:
    plan, items = _book_items(connection, tenant_id, on)
    return plan, {sku for sku, _, _ in items}


def _book_items(connection: Connection, tenant_id: str, on: date) -> tuple[dict[str, Any] | None, set[tuple]]:
    """The version in force on the date and its (sku, component, amount) rows."""
    plan = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=on)
    if plan is None:
        return None, set()
    items = {tuple(r) for r in connection.execute(text(
        "SELECT product_sku_id, component_key, standard_amount FROM auditcore.price_list_items "
        "WHERE tenant_id = :t AND price_list_version_id = :v"
    ), {"t": tenant_id, "v": plan["price_list_version_id"]}).all()}
    return plan, items


def _priced_on_some_date(connection: Connection, tenant_id: str) -> set[UUID]:
    """Every SKU in a version that is in force on at least one date: the
    highest version of each effective date of the OEM's own book."""
    return set(connection.execute(text(
        """
        WITH top AS (
            SELECT DISTINCT ON (v.effective_from) v.price_list_version_id
            FROM auditcore.price_list_versions v
            JOIN auditcore.price_lists pl ON pl.tenant_id = v.tenant_id AND pl.price_list_id = v.price_list_id
            WHERE v.tenant_id = :t AND pl.price_list_code = :code AND v.lifecycle_status IN ('PUBLISHED', 'RETIRED')
            ORDER BY v.effective_from, v.version_no DESC
        )
        SELECT DISTINCT i.product_sku_id FROM auditcore.price_list_items i JOIN top USING (price_list_version_id)
        WHERE i.tenant_id = :t
        """
    ), {"t": tenant_id, "code": OEM_NATIVE_PRICE_LIST_CODE}).scalars())


def _claim(engine: Engine, tag: str, tenant_id: str, mode: str) -> bool:
    with engine.begin() as connection:
        return connection.execute(text(
            "INSERT INTO auditcore.oem_master_reload_runs (reload_tag, tenant_id, mode) VALUES (:g, :t, :m) "
            "ON CONFLICT DO NOTHING RETURNING tenant_id"
        ), {"g": tag, "t": tenant_id, "m": mode}).first() is not None


def _finish(engine: Engine, tag: str, tenant_id: str, status: str, summary: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE auditcore.oem_master_reload_runs SET status = :s, summary = CAST(:j AS jsonb), finished_at_utc = now() "
            "WHERE reload_tag = :g AND tenant_id = :t"
        ), {"s": status, "j": json.dumps(summary, default=str), "g": tag, "t": tenant_id})


def reload_tenant(connection: Connection, *, tenant_id: str, tag: str, store: bool) -> dict[str, Any]:
    """The five ingests and every check, on one open transaction. Returns
    {"checks": [...], "passed": bool, "uploads": [...]}; the caller commits
    or rolls back. ``store`` puts each file in object storage (commit runs)."""
    set_platform_super_admin_context(connection)
    set_tenant_context(connection, tenant_id)
    project = connection.execute(text(
        "SELECT p.oem_id, o.oem_code FROM auditcore.projects p JOIN auditcore.oems o ON o.oem_id = p.oem_id "
        "WHERE p.tenant_id = :t"), {"t": tenant_id}).mappings().one()
    actor = f"worker:{tag}"[:160]
    before = {d: _book_items(connection, tenant_id, d) for d in EXPECTED_SKUS}
    priced_before = _priced_on_some_date(connection, tenant_id)

    uploads: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for relative, on in RELOAD_PLAN:
        path = DATA_DIR / relative
        content = path.read_bytes()
        parsed = parse_price_list(content, filename=path.name)
        stats: dict[str, Any] = {}
        upload_id = connection.execute(text(
            """
            INSERT INTO auditcore.oem_master_uploads (
                tenant_id, oem_code, master_kind, source_filename, source_sha256, effective_from, status,
                row_counts, preview, uploaded_by_actor_id
            ) VALUES (:t, :oem, 'PRICE_LIST', :name, :sha, :eff, 'STAGED', CAST(:rows AS jsonb),
                      CAST(:preview AS jsonb), :actor) RETURNING upload_id
            """
        ), {"t": tenant_id, "oem": project["oem_code"], "name": path.name, "sha": hashlib.sha256(content).hexdigest(),
            "eff": on, "rows": json.dumps({"priceRows": len(parsed.price_rows), **parsed.meta}, default=str),
            "preview": json.dumps({"reloadTag": tag, "warnings": parsed.warnings}), "actor": actor}).scalar_one()
        version_id, _ = ingest_price_list(connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=on,
                                          parsed=parsed, actor_id=actor, stats=stats)
        conflicts += stats.get("modelIdentityConflicts", [])
        stored = None
        if store:
            key = _master_object_key(tenant_id=tenant_id, oem_code=project["oem_code"], master_kind="PRICE_LIST",
                                     effective_from=on, upload_id=upload_id, filename=path.name)
            try:
                from audit_core.uc03_p2_storage import get_p2_document_storage

                get_p2_document_storage().put_object(key, content, content_type=_master_content_type(path.name))
                stored = key
            except Exception as exc:  # noqa: BLE001 - the figures are published; the file is noted as not stored
                logger.warning("oem_master_reload_file_not_stored", tenant_id=tenant_id, file=path.name,
                               error=exc.__class__.__name__)
        connection.execute(text(
            """
            UPDATE auditcore.oem_master_uploads
            SET status = 'PUBLISHED', published_at_utc = now(), price_list_version_id = :v, source_object_key = :key,
                row_counts = row_counts || CAST(:stats AS jsonb)
            WHERE tenant_id = :t AND upload_id = :u
            """
        ), {"v": version_id, "key": stored, "stats": json.dumps(stats, default=str), "t": tenant_id, "u": upload_id})
        uploads.append({"file": path.name, "effectiveFrom": on, "uploadId": upload_id, "stored": stored is not None,
                        "uploaded": stats.get("uploaded"), "carriedForward": stats.get("carriedForward"),
                        "withdrawn": stats.get("withdrawn")})

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, expected: Any, actual: Any) -> None:
        checks.append({"check": name, "ok": bool(ok), "expected": expected, "actual": actual})

    for on, expected in EXPECTED_SKUS.items():
        plan, items = _book_items(connection, tenant_id, on)
        skus = {sku for sku, _, _ in items}
        if on in UNTOUCHED_DATES and before[on][0] is not None and before[on][0]["effective_from"] > date(2026, 9, 7):
            # Every price row the same (a re-derived copy of the version, if
            # any, carries the same rows).
            was_plan, was_items = before[on]
            check(f"book on {on}: the live book, every price row unchanged", items == was_items,
                  f"{len({s_ for s_, _, _ in was_items})} SKUs from {was_plan['effective_from']}",
                  f"{len(skus)} SKUs from {plan['effective_from'] if plan else '-'}"
                  + ("" if items == was_items else f", {len(items ^ was_items)} rows differ"))
            if expected is not None:
                check(f"SKUs in force on {on}", len(skus) == expected, expected, len(skus))
            continue
        if expected is None:  # no live version from that date: the 7 Sep book carries on
            expected = EXPECTED_SKUS[date(2026, 9, 7)]
        check(f"SKUs in force on {on}", len(skus) == expected, expected, len(skus))
    for model, variant, on, price in SPOT_PRICES:
        found = find_book_sku(connection, tenant_id=tenant_id, model_text=model, variant_text=variant, effective_on=on)
        actual = Decimal(str(found["sku"]["ex_showroom"])) if found["sku"] else found["reason"]
        check(f"{variant} ex-showroom on {on}", actual == price, str(price), str(actual))
    priced_after = _priced_on_some_date(connection, tenant_id)
    lost = priced_before - priced_after
    check("every SKU priced on some date before is priced on some date after", not lost, 0, len(lost))
    check("model identity conflicts", not conflicts, 0, conflicts or 0)
    for row in checks:
        logger.info("oem_master_reload_check", tenant_id=tenant_id, tag=tag, **{k: str(v) for k, v in row.items()})
    return {"checks": checks, "passed": all(c["ok"] for c in checks), "uploads": uploads,
            "booksAfter": {str(d): len(_book(connection, tenant_id, d)[1]) for d in sorted(EXPECTED_SKUS)}}


# ── the scheme masters (2026-10-21; the review of PR 472) ─────────────────────
# A tag containing "-schemes" runs this plan instead of the price books (which
# are already re-loaded): the bulletins, the corporate policy and the dealer
# grid, in order, with the dates the documents print. The live September
# schemes were loaded on 08 Sep and the August grid on 05 Sep by the old
# ingestion (rows stored model-wide for variants nothing resolved, the grid
# lines for 3XO / 7XO / MAXX / XUV400 with no model). A version answers from
# its date, so each file is published on its own date AND again on every
# date a live version of its kind was loaded inside its validity: the later
# upload on a date wins, and its tombstones withdraw the old schemes the
# re-upload no longer names.
SCHEME_RELOAD_PLAN: tuple[tuple[str, str, date, date | None], ...] = (
    ("DISCOUNT_GRID", "2026-09/Odisha PV & CV Discount Grid-1 Aug 2026.xlsx", date(2026, 9, 1), None),
    ("CONSUMER_SCHEME", "2026-09/Consumer Scheme Bhubaneswar RO_Sep'26.pdf", date(2026, 9, 3), date(2026, 9, 30)),
    ("EXCHANGE_SCHEME", "2026-09/Exchange scheme Sept'26 - Bhubaneswar.pdf", date(2026, 9, 3), date(2026, 9, 30)),
    ("CORPORATE_POLICY", "2026-09/260903 Sept'26 Corporate Privilege Policy Mahindra PV and CV.xlsx",
     date(2026, 9, 3), date(2026, 9, 30)),
    ("CONSUMER_SCHEME", "2026-10/Consumer Scheme Bhubaneswar RO_Oct'26.pdf", date(2026, 10, 3), date(2026, 10, 31)),
    ("EXCHANGE_SCHEME", "2026-10/Exchange scheme Oct'26 - B'newar_Raipur_Patna_Guwa.pdf",
     date(2026, 10, 3), date(2026, 10, 31)),
    ("CORPORATE_POLICY", "2026-10/261003 Oct'26 Corporate Privilege Policy Mahindra PV and CV.xlsx",
     date(2026, 10, 3), date(2026, 10, 31)),
    ("DISCOUNT_GRID", "2026-10/Odisha PV & CV Discount Grid-1 Oct 2026.xlsx", date(2026, 10, 1), None),
)
SCHEME_CHECK_DATES = (date(2026, 9, 15), date(2026, 10, 15))
# Models with at least one scheme row applied (consumer, exchange, scrappage,
# welcome, corporate) on each check date, from the dry run on the live-shaped
# copy; a re-load that applies to fewer models fails.
EXPECTED_MODELS_WITH_SCHEMES: dict[date, int] = {date(2026, 9, 15): 16, date(2026, 10, 15): 16}
SCHEME_SPOT_CHECKS = (
    # Utkal's deal: XUV 3XO AX5 PM MT, the September cash offer.
    ("XUV 3XO", "AX5 PM MT", date(2026, 9, 15), "CASH_DISCOUNT", Decimal(30000)),
)
_KIND_CATEGORIES = {
    "CONSUMER_SCHEME": ("CONSUMER",),
    "EXCHANGE_SCHEME": ("EXCHANGE", "SCRAPPAGE", "WELCOME"),
    "CORPORATE_POLICY": ("CORPORATE",),
}


def _shadow_dates(connection: Connection, tenant_id: str, kind: str, on: date, until: date | None) -> list[date]:
    """The dates after ``on`` (up to ``until``) a live version of this kind was
    loaded on, before this re-load: the file is published again on each."""
    if kind == "DISCOUNT_GRID":
        rows = connection.execute(text(
            "SELECT DISTINCT effective_from FROM auditcore.dealer_discount_grid_versions WHERE tenant_id = :t "
            "AND lifecycle_status IN ('PUBLISHED', 'RETIRED') AND effective_from > :on "
            "AND (CAST(:until AS date) IS NULL OR effective_from < :until) ORDER BY 1"
        ), {"t": tenant_id, "on": on, "until": until}).scalars().all()
    else:
        rows = connection.execute(text(
            """
            SELECT DISTINCT v.effective_from FROM auditcore.discount_scheme_versions v
            JOIN auditcore.discount_schemes s ON s.tenant_id = v.tenant_id AND s.discount_scheme_id = v.discount_scheme_id
            WHERE v.tenant_id = :t AND v.lifecycle_status = 'PUBLISHED' AND s.scheme_category = ANY(:cats)
              AND v.effective_from > :on AND (CAST(:until AS date) IS NULL OR v.effective_from <= :until)
            ORDER BY 1
            """
        ), {"t": tenant_id, "on": on, "until": until, "cats": list(_KIND_CATEGORIES[kind])}).scalars().all()
    return list(rows)


def _standing_rows(connection: Connection, tenant_id: str, on: date) -> list[dict[str, Any]]:
    """Every eligibility row of the scheme versions standing on the date."""
    return [dict(r) for r in connection.execute(text(
        """
        WITH started AS (
            SELECT DISTINCT ON (v.discount_scheme_id) v.discount_scheme_version_id, v.effective_to
            FROM auditcore.discount_scheme_versions v
            WHERE v.tenant_id = :t AND v.lifecycle_status = 'PUBLISHED' AND v.effective_from <= :on
            ORDER BY v.discount_scheme_id, v.effective_from DESC, v.created_at_utc DESC
        )
        SELECT pm.model_name, e.variant_id, e.criteria
        FROM started s
        JOIN auditcore.discount_scheme_eligibility e
          ON e.tenant_id = :t AND e.discount_scheme_version_id = s.discount_scheme_version_id
        JOIN auditcore.product_models pm ON pm.model_id = e.model_id
        WHERE (s.effective_to IS NULL OR s.effective_to >= :on)
          AND EXISTS (SELECT 1 FROM auditcore.discount_scheme_benefits b
                      WHERE b.tenant_id = :t AND b.discount_scheme_version_id = s.discount_scheme_version_id)
        """
    ), {"t": tenant_id, "on": on}).mappings().all()]


def reload_schemes_tenant(connection: Connection, *, tenant_id: str, tag: str, store: bool) -> dict[str, Any]:
    """The scheme plan and every check, on one open transaction (the caller
    commits or rolls back), like ``reload_tenant``."""
    from audit_core.oem_master_parsers import parse_master
    from audit_core.oem_price_masters import (
        grid_lines_unbound_to_books,
        ingest_corporate_policy,
        ingest_discount_document,
        ingest_discount_grid,
    )
    from audit_core.uc03_deal_reconciliation import _applicable_benefits

    set_platform_super_admin_context(connection)
    set_tenant_context(connection, tenant_id)
    project = connection.execute(text(
        "SELECT p.oem_id, o.oem_code FROM auditcore.projects p JOIN auditcore.oems o ON o.oem_id = p.oem_id "
        "WHERE p.tenant_id = :t"), {"t": tenant_id}).mappings().one()
    actor = f"worker:{tag}"[:160]
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, expected: Any, actual: Any) -> None:
        checks.append({"check": name, "ok": bool(ok), "expected": expected, "actual": actual})

    grid_dates = [on for kind, _, on, _ in SCHEME_RELOAD_PLAN if kind == "DISCOUNT_GRID"]
    planned: list[tuple[str, Path, date, date | None]] = []
    for kind, relative, on, until in SCHEME_RELOAD_PLAN:
        shadow_until = until if kind != "DISCOUNT_GRID" else next((d for d in grid_dates if d > on), None)
        for when in [on, *_shadow_dates(connection, tenant_id, kind, on, shadow_until)]:
            planned.append((kind, DATA_DIR / relative, when, until))

    uploads: list[dict[str, Any]] = []
    grid_versions: list[tuple[date, Any]] = []
    for kind, path, on, until in planned:
        content = path.read_bytes()
        parsed = parse_master(kind, content, filename=path.name)
        plan_from = next(d for k, r, d, _ in SCHEME_RELOAD_PLAN if DATA_DIR / r == path)
        if on == plan_from:
            check(f"{path.name}: the dates the document prints", parsed.effective_from_hint == on
                  and (kind == "DISCOUNT_GRID" or parsed.effective_to_hint == until),
                  f"{on} to {until}", f"{parsed.effective_from_hint} to {parsed.effective_to_hint}")
        upload_id = connection.execute(text(
            """
            INSERT INTO auditcore.oem_master_uploads (
                tenant_id, oem_code, master_kind, source_filename, source_sha256, effective_from, status,
                row_counts, preview, uploaded_by_actor_id
            ) VALUES (:t, :oem, :kind, :name, :sha, :eff, 'STAGED', CAST(:rows AS jsonb), CAST(:preview AS jsonb), :actor)
            RETURNING upload_id
            """
        ), {"t": tenant_id, "oem": project["oem_code"], "kind": kind, "name": path.name,
            "sha": hashlib.sha256(content).hexdigest(), "eff": on,
            "rows": json.dumps({"discountRows": len(parsed.discount_rows), "corporateRows": len(parsed.corporate_benefits),
                                "gridRows": len(parsed.grid_rows)}),
            "preview": json.dumps({"reloadTag": tag, "warnings": parsed.warnings[:50]}), "actor": actor}).scalar_one()
        common = {"tenant_id": tenant_id, "oem_id": project["oem_id"], "oem_code": project["oem_code"],
                  "effective_from": on, "parsed": parsed, "actor_id": actor}
        if kind == "DISCOUNT_GRID":
            summary = ingest_discount_grid(connection, upload_id=upload_id, **common)
            grid_versions.append((on, summary["gridVersionId"]))
        elif kind == "CORPORATE_POLICY":
            summary = ingest_corporate_policy(connection, upload_id=upload_id, effective_to=until, **common)
        else:
            summary = ingest_discount_document(connection, master_kind=kind, effective_to=until, **common)
        stored = None
        if store:
            key = _master_object_key(tenant_id=tenant_id, oem_code=project["oem_code"], master_kind=kind,
                                     effective_from=on, upload_id=upload_id, filename=path.name)
            try:
                from audit_core.uc03_p2_storage import get_p2_document_storage

                get_p2_document_storage().put_object(key, content, content_type=_master_content_type(path.name))
                stored = key
            except Exception as exc:  # noqa: BLE001 - the figures are published; the file is noted as not stored
                logger.warning("oem_master_reload_file_not_stored", tenant_id=tenant_id, file=path.name,
                               error=exc.__class__.__name__)
        connection.execute(text(
            "UPDATE auditcore.oem_master_uploads SET status = 'PUBLISHED', published_at_utc = now(), "
            "source_object_key = :key, discount_scheme_summary = CAST(:summary AS jsonb) "
            "WHERE tenant_id = :t AND upload_id = :u"
        ), {"key": stored, "summary": json.dumps(summary, default=str), "t": tenant_id, "u": upload_id})
        uploads.append({"file": path.name, "kind": kind, "effectiveFrom": on, "effectiveTo": until,
                        "uploadId": upload_id, "stored": stored is not None,
                        "published": summary.get("published"), "tombstoned": summary.get("tombstoned"),
                        "unresolved": len(summary.get("unresolved") or []),
                        "unresolvedVariants": len(summary.get("unresolvedVariants") or []),
                        "gridResolved": summary.get("resolved")})

    # A grid line must be bound to a model of every book the grid is used with
    # (the October grid, dated 1 Oct, named the new Scorpio N, which only the
    # book of 3 Oct prices; review of PR 472).
    for on, grid_version_id in grid_versions:
        gaps = grid_lines_unbound_to_books(connection, tenant_id=tenant_id, oem_id=project["oem_id"],
                                           oem_code=project["oem_code"], grid_version_id=grid_version_id)
        check(f"discount grid of {on}: every line bound to a model of the books it is used with", not gaps, [], gaps)

    applied: dict[str, dict[str, int]] = {}
    for on in SCHEME_CHECK_DATES:
        rows = _standing_rows(connection, tenant_id, on)
        per_model: dict[str, int] = {}
        model_wide_unresolved = 0
        for row in rows:
            criteria = row["criteria"] or {}
            if row["variant_id"] is None and criteria.get("scope") != "UNRESOLVED" and criteria.get(
                    "unresolvedVariantTexts"):
                model_wide_unresolved += 1
            if criteria.get("scope") == "UNRESOLVED":
                continue
            per_model[row["model_name"]] = per_model.get(row["model_name"], 0) + 1
        applied[str(on)] = dict(sorted(per_model.items()))
        check(f"model-wide rows of unresolved variants applied on {on}", model_wide_unresolved == 0, 0,
              model_wide_unresolved)
        check(f"models with a scheme applied on {on}", len(per_model) >= EXPECTED_MODELS_WITH_SCHEMES[on],
              f">= {EXPECTED_MODELS_WITH_SCHEMES[on]}", len(per_model))
    for model, variant, on, key, amount in SCHEME_SPOT_CHECKS:
        found = find_book_sku(connection, tenant_id=tenant_id, model_text=model, variant_text=variant, effective_on=on)
        actual: Any = found["reason"]
        if found["sku"]:
            sku = connection.execute(text("SELECT model_id, variant_id FROM auditcore.product_skus "
                                          "WHERE product_sku_id = :s"), {"s": found["sku"]["product_sku_id"]}).one()
            benefits = _applicable_benefits(connection, tenant_id=tenant_id, model_id=sku[0], variant_id=sku[1],
                                            effective_on=on, basis="INDIVIDUAL", variant_name=variant)
            actual = next((Decimal(str(b["amount_value"])) for b in benefits if b["canonical_key"] == key), None)
        check(f"{model} {variant} {key} on {on}", actual == amount, str(amount), str(actual))
    for row in checks:
        logger.info("oem_master_reload_check", tenant_id=tenant_id, tag=tag, **{k: str(v) for k, v in row.items()})
    return {"checks": checks, "passed": all(c["ok"] for c in checks), "uploads": uploads, "appliedPerModel": applied}


def run_master_reload(engine: Engine, tag: str, *, tenant_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """The one-off, for every Mahindra tenant (or ``tenant_ids``). Never raises.
    A tag containing "-schemes" re-loads the scheme masters
    (``SCHEME_RELOAD_PLAN``), any other tag the price books."""
    commit = tag.endswith("-commit")
    job = reload_schemes_tenant if "-schemes" in tag else reload_tenant
    mode = "COMMIT" if commit else "DRY_RUN"
    results: list[dict[str, Any]] = []
    for tenant_id in tenant_ids if tenant_ids is not None else mahindra_tenants(engine):
        try:
            if not _claim(engine, tag, tenant_id, mode):
                logger.info("oem_master_reload_already_done", tenant_id=tenant_id, tag=tag)
                results.append({"tenantId": tenant_id, "status": "ALREADY_DONE"})
                continue
            connection = engine.connect()
            transaction = connection.begin()
            try:
                outcome = job(connection, tenant_id=tenant_id, tag=tag, store=commit)
                if commit and outcome["passed"]:
                    transaction.commit()
                    status = "COMMITTED"
                else:
                    transaction.rollback()
                    status = ("DRY_RUN_PASSED" if outcome["passed"] else "DRY_RUN_FAILED") if not commit else "FAILED"
            except Exception as exc:  # logged, rolled back and recorded
                transaction.rollback()
                outcome = {"error": f"{exc.__class__.__name__}: {exc}"[:2000]}
                status = "DRY_RUN_FAILED" if not commit else "FAILED"
                logger.warning("oem_master_reload_error", tenant_id=tenant_id, tag=tag, exc_info=True)
            finally:
                connection.close()
            _finish(engine, tag, tenant_id, status, outcome)
            logger.info("oem_master_reload_finished", tenant_id=tenant_id, tag=tag, status=status,
                        books_after=outcome.get("booksAfter"), failed_checks=[
                            c["check"] for c in outcome.get("checks", []) if not c["ok"]])
            results.append({"tenantId": tenant_id, "status": status, **outcome})
        except Exception:  # one tenant never stops the others
            logger.warning("oem_master_reload_failed", tenant_id=tenant_id, tag=tag, exc_info=True)
            results.append({"tenantId": tenant_id, "status": "ERROR"})
    return results


__all__ = ["RELOAD_PLAN", "SCHEME_RELOAD_PLAN", "reload_schemes_tenant", "reload_tenant", "run_master_reload"]
