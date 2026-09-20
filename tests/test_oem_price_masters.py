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
    CorporateBenefitRow,
    CorporateCompany,
    DiscountRow,
    ParseResult,
    parse_price_list,
)
from audit_core.oem_price_masters import (
    _project_oem,
    ingest_corporate_policy,
    ingest_discount_document,
    ingest_price_list,
)
from audit_core.price_lists import find_effective_price_plan
from audit_core.uc03_model_resolution import _sku_rows_for_version

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


def _price_bytes_full(*rows: dict) -> bytes:
    """Same header/layout as _price_bytes, but every structured attribute is
    explicit per row instead of derived from the variant string -- needed to
    reproduce a real master sheet where several variants of the same model
    share some attributes (fuel/transmission/drive, even the same trim) but
    differ in others (seater), which _price_bytes' one-row-per-call helper
    can never exercise."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Price List"
    ws.append(_HEADER)
    for sl, row in enumerate(rows, start=1):
        tcs, ins, ew4, ew45, acc, rsa, fastag, reg = 100, 200, 300, 400, 500, 60, 40, 5000
        ex = row["ex"]
        onroad = ex + tcs + ins + ew4 + ew45 + acc + rsa + fastag + reg
        ws.append([
            sl, "PV", row["model"], row["variant"], row["trim"], row["fuel"],
            row["transmission"], row["drive"], row["seater"],
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


def test_price_list_ingest_persists_trim_on_the_variant(connection) -> None:
    """oem_master_parsers.py has parsed Trim (a distinct masters column, not
    part of the Variant string) from day one, but oem_price_masters.py never
    included it in the attrs written to product_variants.attributes --
    confirmed live: several distinct trims sharing identical fuel/
    transmission/drive/seater made "Variant" the only disambiguator in the
    Modify Model picker, with no way to narrow by trim first."""
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 9, 3),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000))),
        actor_id="admin",
    )
    trim = connection.execute(
        text("""
            SELECT pv.attributes ->> 'trim' FROM auditcore.product_variants pv
            JOIN auditcore.product_models pm ON pm.model_id = pv.model_id
            WHERE pm.oem_id = :o AND pv.variant_name = 'MX1 PMT 2WD'
        """),
        {"o": project["oem_id"]},
    ).scalar_one()
    assert trim == "MX1"


def test_reupload_backfills_trim_onto_an_already_ingested_variant(connection) -> None:
    """A re-upload of the same masters file is a routine admin action, not a
    one-off -- when the parser starts extracting a field it didn't capture
    before, an already-ingested variant must pick it up too, not keep
    serving stale attributes forever just because its variant_code already
    existed."""
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 9, 3),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000))),
        actor_id="admin",
    )
    # Simulate the pre-fix state: strip trim back out, as if this variant had
    # been ingested before oem_price_masters.py carried it through.
    connection.execute(
        text("""
            UPDATE auditcore.product_variants SET attributes = attributes - 'trim'
            WHERE variant_id IN (
                SELECT pv.variant_id FROM auditcore.product_variants pv
                JOIN auditcore.product_models pm ON pm.model_id = pv.model_id
                WHERE pm.oem_id = :o AND pv.variant_name = 'MX1 PMT 2WD'
            )
        """),
        {"o": project["oem_id"]},
    )
    assert connection.execute(
        text("""
            SELECT pv.attributes ->> 'trim' FROM auditcore.product_variants pv
            JOIN auditcore.product_models pm ON pm.model_id = pv.model_id
            WHERE pm.oem_id = :o AND pv.variant_name = 'MX1 PMT 2WD'
        """),
        {"o": project["oem_id"]},
    ).scalar_one_or_none() is None

    # Re-upload the exact same file -- same variant_code, so the old
    # insert-only path would have left it untouched forever.
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 10, 1),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_100_000))),
        actor_id="admin",
    )
    trim = connection.execute(
        text("""
            SELECT pv.attributes ->> 'trim' FROM auditcore.product_variants pv
            JOIN auditcore.product_models pm ON pm.model_id = pv.model_id
            WHERE pm.oem_id = :o AND pv.variant_name = 'MX1 PMT 2WD'
        """),
        {"o": project["oem_id"]},
    ).scalar_one()
    assert trim == "MX1"


def test_price_list_ingest_persists_trim_across_sibling_variants_of_one_model(connection) -> None:
    """Reproduces the live master sheet verbatim: SCORPIO CLASSIC carries
    three variants that share fuel/transmission/drive (DIESEL/MT/2WD) and,
    for two of them, the identical trim "S" -- differing only in seater (7
    vs 9) for those two, and in trim ("S11") for the third. The single-row
    tests above (test_price_list_ingest_persists_trim_on_the_variant,
    test_reupload_backfills_trim_onto_an_already_ingested_variant) only
    ever ingest one variant per model per call, so they cannot catch a bug
    that only shows up when several sibling variants of the same model are
    ingested together in one upload -- which is the real-world shape being
    reported live as "trim is missing" for exactly these three rows."""
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    parsed = parse_price_list(_price_bytes_full(
        {"model": "SCORPIO CLASSIC", "variant": "Classic S BS6.2 - E", "trim": "S",
         "fuel": "DIESEL", "transmission": "MT", "drive": "2WD", "seater": "7", "ex": 1_336_701},
        {"model": "SCORPIO CLASSIC", "variant": "Classic S - 9 STR BS6.2 - E", "trim": "S",
         "fuel": "DIESEL", "transmission": "MT", "drive": "2WD", "seater": "9", "ex": 1_383_901},
        {"model": "SCORPIO CLASSIC", "variant": "Classic S11 BS6.2 - E", "trim": "S11",
         "fuel": "DIESEL", "transmission": "MT", "drive": "2WD", "seater": "7", "ex": 1_739_901},
    ))
    assert not parsed.errors, parsed.errors
    version_id, sku_id_by_code = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 9, 3), parsed=parsed, actor_id="admin",
    )
    assert len(sku_id_by_code) == 3

    rows = _sku_rows_for_version(connection, tenant_id=tenant_id, price_list_version_id=version_id)
    by_variant = {r["variant_name"]: r for r in rows}
    assert by_variant["Classic S BS6.2 - E"]["trim"] == "S"
    assert by_variant["Classic S - 9 STR BS6.2 - E"]["trim"] == "S"
    assert by_variant["Classic S11 BS6.2 - E"]["trim"] == "S11"
    for variant_name in by_variant:
        assert by_variant[variant_name]["fuel_powertrain"] == "DIESEL"
        assert by_variant[variant_name]["transmission"] == "MT"
        assert by_variant[variant_name]["drive"] == "2WD"


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


def test_corporate_policy_batches_large_company_registry(connection) -> None:
    # A company list can run to hundreds/thousands of rows — this exercises the
    # batched upsert (chunk_size=500 default) across a chunk boundary, plus a
    # duplicate corporate_code within one file (last one must win, matching the
    # old sequential upsert's semantics) and full-replace on re-upload.
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"],
        effective_from=date(2026, 9, 3),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000))),
        actor_id="admin",
    )

    parsed = ParseResult(kind="CORPORATE_POLICY")
    parsed.corporate_benefits = [
        CorporateBenefitRow(1, "Z", "Thar Roxx", Decimal(10000), Decimal(5000), Decimal(15000)),
    ]
    parsed.corporate_companies = [
        CorporateCompany(f"CORP-{i:04d}", f"Company {i}", "PSU", "Z") for i in range(600)
    ]
    parsed.corporate_companies.append(CorporateCompany("CORP-0000", "Company 0 Renamed", "PSU", "Z"))

    summary = ingest_corporate_policy(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        effective_from=date(2026, 9, 3), parsed=parsed, upload_id=uuid4(), actor_id="admin",
    )
    assert summary["published"] == 1
    assert summary["companies"] == 601

    rows = connection.execute(
        text("SELECT corporate_code, corporate_name FROM auditcore.corporate_privilege_registry "
             "WHERE oem_code = 'MAHINDRA' ORDER BY corporate_code"),
    ).mappings().all()
    assert len(rows) == 600  # the duplicate code collapsed to one row
    by_corp_code = {r["corporate_code"]: r["corporate_name"] for r in rows}
    assert by_corp_code["CORP-0000"] == "Company 0 Renamed"  # last one wins
    assert by_corp_code["CORP-0599"] == "Company 599"

    # re-upload fully replaces the registry
    parsed2 = ParseResult(kind="CORPORATE_POLICY")
    parsed2.corporate_benefits = parsed.corporate_benefits
    parsed2.corporate_companies = [CorporateCompany("CORP-0000", "Only Company Left", "PSU", "Z")]
    ingest_corporate_policy(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        effective_from=date(2026, 10, 1), parsed=parsed2, upload_id=uuid4(), actor_id="admin",
    )
    remaining = connection.execute(
        text("SELECT count(*) FROM auditcore.corporate_privilege_registry WHERE oem_code='MAHINDRA'")
    ).scalar_one()
    assert remaining == 1
