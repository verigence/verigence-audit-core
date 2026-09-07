"""Integration tests for OEM native master ingestion into the tenant-scoped
versioned masters. Requires DATABASE_URL (a migrated Audit Core database).
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, text

from audit_core.oem_master_parsers import (
    DiscountRow,
    ParseResult,
    parse_price_list,
)
from audit_core.oem_price_masters import (
    _project_oem,
    ingest_discount_document,
    ingest_price_list,
)
from audit_core.price_lists import find_effective_price_plan

_HEADER = [
    "Sl. No.", "Category", "Model", "Variant", "Trim", "Fuel", "Transmission",
    "Drive", "Seater", "Ex-Showroom Price", "TCS", "Insurance",
    "Ext. Warranty (4th Yr)", "Ext. Warranty (4th & 5th Yr)", "Accessories Kit",
    "RSA (1 Yr)", "FASTag", "Registration\n(Individual / w/o Hyp.)",
    "On-Road Price\n(Individual / w/o Hyp.)", "Registration\n(Corporate / with Hyp.)",
    "On-Road Price\n(Corporate / with Hyp.)", "Source Sheet",
]


def _price_bytes(*price_rows: tuple[str, str, int]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Price List"
    ws.append(_HEADER)
    for sl, (model, variant, ex) in enumerate(price_rows, start=1):
        tcs, ins, ew4, ew45, acc, rsa, fastag, reg = 100, 200, 300, 400, 500, 60, 40, 5000
        onroad = ex + tcs + ins + ew4 + ew45 + acc + rsa + fastag + reg
        ws.append([
            sl, "PV", model, variant, variant.split()[0], "PETROL", "MT", "2WD", "5",
            ex, tcs, ins, ew4, ew45, acc, rsa, fastag, reg, onroad, reg, onroad, "S",
        ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for OEM master integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-oemm-{suffix}"
    with engine.begin() as conn:
        oem_id = conn.execute(
            text("SELECT oem_id FROM auditcore.oems WHERE oem_code = 'MAHINDRA'")
        ).scalar_one_or_none()
        if oem_id is None:
            oem_id = conn.execute(
                text("INSERT INTO auditcore.oems (oem_code, oem_name) "
                     "VALUES ('MAHINDRA','Mahindra') RETURNING oem_id")
            ).scalar_one()
        category_id = conn.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'Vehicle') RETURNING product_category_id"),
            {"c": f"CAT-{suffix}"},
        ).scalar_one()
        conn.execute(
            text("""
                INSERT INTO auditcore.projects
                    (tenant_id, project_code, project_name, oem_id, product_category_id, effective_start_date)
                VALUES (:t, :pc, 'OEM Master Project', :o, :cat, CURRENT_DATE)
            """),
            {"t": tenant_id, "pc": f"P-{suffix}", "o": oem_id, "cat": category_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.platform_super_admin','true',true)"))
        conn.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": tenant_id})
        conn.tenant_id = tenant_id  # type: ignore[attr-defined]
        yield conn
    engine.dispose()


def test_price_list_ingest_creates_catalogue_and_resolves_plan(connection) -> None:
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    parsed = parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000)))
    version_id, skus = ingest_price_list(
        connection,
        tenant_id=tenant_id,
        oem_id=project["oem_id"],
        effective_from=date(2026, 9, 3),
        parsed=parsed,
        actor_id="admin",
    )
    assert len(skus) == 1
    items = connection.execute(
        text("SELECT component_key, standard_amount FROM auditcore.price_list_items "
             "WHERE tenant_id = :t AND price_list_version_id = :v"),
        {"t": tenant_id, "v": version_id},
    ).mappings().all()
    keyed = {r["component_key"]: r["standard_amount"] for r in items}
    assert keyed["EX_SHOWROOM"] == Decimal("1000000.00")
    assert "REGISTRATION_CORPORATE" in keyed

    plan = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=date(2026, 9, 20))
    assert plan is not None
    assert plan["price_list_code"] == "OEM_NATIVE_PRICE_LIST"


def test_reupload_supersedes_by_effective_date(connection) -> None:
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 9, 3),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000))),
        actor_id="admin",
    )
    v2, _ = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 10, 1),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_100_000))),
        actor_id="admin",
    )
    sept = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=date(2026, 9, 15))
    octo = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=date(2026, 10, 15))
    assert sept["version_no"] == 1
    assert octo["version_no"] == 2
    assert octo["price_list_version_id"] == v2


def test_discount_ingest_and_tombstone(connection) -> None:
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 9, 3),
        parsed=parse_price_list(
            _price_bytes(
                ("THAR ROXX", "MX1 PMT 2WD", 1_000_000),
                ("SCORPIO N", "Z8 L", 2_000_000),
            )
        ),
        actor_id="admin",
    )

    sept = ParseResult(kind="EXCHANGE_SCHEME")
    sept.discount_rows = [
        DiscountRow(1, "EXCHANGE", "Thar Roxx", [], [("EXCHANGE_BONUS", Decimal(25000))],
                    Decimal(25000), {"section": "EXCHANGE_PERSONAL"}),
        DiscountRow(2, "EXCHANGE", "Scorpio-N", [], [("EXCHANGE_BONUS", Decimal(25000))],
                    Decimal(25000), {"section": "EXCHANGE_PERSONAL"}),
    ]
    summary = ingest_discount_document(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        master_kind="EXCHANGE_SCHEME", effective_from=date(2026, 9, 3), parsed=sept,
        actor_id="admin",
    )
    assert summary["published"] == 2
    assert summary["tombstoned"] == 0

    # October drops Scorpio-N -> its scheme is tombstoned with an empty version
    octo = ParseResult(kind="EXCHANGE_SCHEME")
    octo.discount_rows = [
        DiscountRow(1, "EXCHANGE", "Thar Roxx", [], [("EXCHANGE_BONUS", Decimal(30000))],
                    Decimal(30000), {"section": "EXCHANGE_PERSONAL"}),
    ]
    summary2 = ingest_discount_document(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        master_kind="EXCHANGE_SCHEME", effective_from=date(2026, 10, 1), parsed=octo,
        actor_id="admin",
    )
    assert summary2["published"] == 1
    assert summary2["tombstoned"] == 1

    live_benefits = connection.execute(
        text("""
            SELECT s.scheme_code, count(b.benefit_id) AS n
            FROM auditcore.discount_schemes s
            JOIN auditcore.discount_scheme_versions v
              ON v.tenant_id = s.tenant_id AND v.discount_scheme_id = s.discount_scheme_id
             AND v.version_no = (SELECT max(v2.version_no) FROM auditcore.discount_scheme_versions v2
                                 WHERE v2.tenant_id = v.tenant_id
                                   AND v2.discount_scheme_id = v.discount_scheme_id)
            LEFT JOIN auditcore.discount_scheme_benefits b
              ON b.tenant_id = v.tenant_id AND b.discount_scheme_version_id = v.discount_scheme_version_id
            WHERE s.tenant_id = :t
            GROUP BY s.scheme_code
        """),
        {"t": tenant_id},
    ).mappings().all()
    by_code = {r["scheme_code"]: r["n"] for r in live_benefits}
    scorpio = next(c for c in by_code if "SCORPIO" in c)
    assert by_code[scorpio] == 0  # tombstoned: latest version carries no benefit
