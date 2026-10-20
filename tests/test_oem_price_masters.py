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


def _items(connection, tenant_id, version_id) -> dict[tuple[str, str], tuple[Decimal, date]]:
    rows = connection.execute(
        text("""SELECT s.sku_code, i.component_key, i.standard_amount, i.price_since
                FROM auditcore.price_list_items i JOIN auditcore.product_skus s ON s.product_sku_id = i.product_sku_id
                WHERE i.tenant_id = :t AND i.price_list_version_id = :v"""),
        {"t": tenant_id, "v": version_id},
    ).all()
    return {(r[0], r[1]): (Decimal(r[2]), r[3]) for r in rows}


def test_a_partial_upload_makes_a_complete_version_and_dates_each_price(connection) -> None:
    """Decision 2026-09-30: a mid-month file with only the changed models is
    merged over the standing version; the others are carried forward at
    their standing price, and every row keeps its own price-since date."""
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    v1, _ = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000), ("XUV 3XO", "MX1", 800_000))),
        actor_id="admin",
    )
    stats: dict = {}
    v2, _ = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 19),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_050_000))),
        actor_id="admin", stats=stats,
    )
    assert stats == {"uploaded": 1, "changed": 1, "unchanged": 0, "carriedForward": 1, "standingVersionId": str(v1),
                     "laterVersionsRederived": 0}
    first, second = _items(connection, tenant_id, v1), _items(connection, tenant_id, v2)
    assert len(second) == len(first) == 20  # both SKUs, all ten components, in both versions
    assert second[("THAR_ROXX::MX1 PMT 2WD", "EX_SHOWROOM")] == (Decimal(1_050_000), date(2026, 9, 19))
    assert second[("THAR_ROXX::MX1 PMT 2WD", "TCS")] == (Decimal(100), date(2026, 9, 1))  # same figure: kept its date
    assert second[("XUV_3XO::MX1", "EX_SHOWROOM")] == (Decimal(800_000), date(2026, 9, 1))  # carried forward
    assert find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=date(2026, 9, 20))["price_list_version_id"] == v2
    assert find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=date(2026, 9, 10))["price_list_version_id"] == v1

    # The same file again: nothing changes, every date stands.
    again: dict = {}
    v3, _ = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 10, 1),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_050_000))),
        actor_id="admin", stats=again,
    )
    assert again["changed"] == 0 and again["unchanged"] == 1 and again["carriedForward"] == 1
    assert _items(connection, tenant_id, v3)[("THAR_ROXX::MX1 PMT 2WD", "EX_SHOWROOM")] == (Decimal(1_050_000), date(2026, 9, 19))


def test_an_earlier_dated_upload_reaches_the_versions_already_published_after_it(connection) -> None:
    """The EV sheet dated the 1st arrives after the consolidated list dated
    the 3rd (2026-09-30, DEV): the 3rd's version was built without the EV
    SKUs, so from the 3rd on the EV was "not priced". The upload re-derives
    the later version on its own date with the missing SKUs carried
    forward; published versions stay immutable and are superseded."""
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    consolidated, _ = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 3),
        parsed=parse_price_list(_price_bytes(("XUV 3XO", "MX1", 800_000))), actor_id="admin",
    )
    stats: dict = {}
    ev, _ = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(("BE6", "PACK ONE", 1_900_000))), actor_id="admin", stats=stats,
    )
    assert stats["carriedForward"] == 0 and stats["laterVersionsRederived"] == 1
    on_the_2nd = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=date(2026, 9, 2))
    assert on_the_2nd["price_list_version_id"] == ev
    on_the_5th = find_effective_price_plan(connection, tenant_id=tenant_id, effective_on=date(2026, 9, 5))
    assert on_the_5th["price_list_version_id"] not in {ev, consolidated} and on_the_5th["effective_from"] == date(2026, 9, 3)
    items = _items(connection, tenant_id, on_the_5th["price_list_version_id"])
    assert items[("XUV_3XO::MX1", "EX_SHOWROOM")] == (Decimal(800_000), date(2026, 9, 3))
    assert items[("BE6::PACK ONE", "EX_SHOWROOM")] == (Decimal(1_900_000), date(2026, 9, 1))
    assert len(_items(connection, tenant_id, consolidated)) == 10  # the old version is untouched

    # A third date already published after both: it is re-derived too, once.
    later, _ = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 7),
        parsed=parse_price_list(_price_bytes(("XUV 3XO", "MX1", 810_000))), actor_id="admin",
    )
    assert len(_items(connection, tenant_id, later)) == 20  # carried BE6 from the re-derived 3rd
    again: dict = {}
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 8, 20),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000))), actor_id="admin", stats=again,
    )
    assert again["laterVersionsRederived"] == 3  # the 1st, the 3rd and the 7th each gain THAR ROXX
    assert len(_items(connection, tenant_id, find_effective_price_plan(
        connection, tenant_id=tenant_id, effective_on=date(2026, 9, 9))["price_list_version_id"])) == 30


def test_discount_grid_ingests_per_model_and_reports_unknown_models(connection) -> None:
    from audit_core.oem_master_parsers import GridRow
    from audit_core.oem_price_masters import grid_row_for_model, ingest_discount_grid

    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    _, skus = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(("THAR ROXX", "MX1 PMT 2WD", 1_000_000), ("PICK UP", "S6", 800_000))),
        actor_id="admin",
    )
    parsed = ParseResult(kind="DISCOUNT_GRID")
    parsed.grid_rows = [
        GridRow(3, "Thar Roxx", ["Thar Roxx"], True, 60, Decimal(5000), Decimal(60), Decimal(3000)),
        GridRow(4, "PICKUP, MAXX", ["PICKUP", "MAXX"], True, 30, Decimal(7000), Decimal(50), Decimal(3000)),
        GridRow(5, "SCORPIO N Old", ["SCORPIO N Old"], False, None, None, None, None),
    ]
    parsed.grid_parameters = [{"parameter": "Penalty Amount", "note": "₹30,000 per case"}]
    summary = ingest_discount_grid(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        effective_from=date(2026, 9, 1), parsed=parsed, upload_id=None, actor_id="admin",
    )
    assert summary["published"] == 4 and summary["resolved"] == 2 and summary["parameters"] == 1
    assert [u.split("'")[1] for u in summary["unresolved"]] == ["MAXX", "SCORPIO N Old"]
    thar_model = connection.execute(
        text("SELECT model_id FROM auditcore.product_skus WHERE product_sku_id = :s"),
        {"s": skus["THAR_ROXX::MX1 PMT 2WD"]},
    ).scalar_one()
    row = grid_row_for_model(connection, tenant_id=tenant_id, model_id=thar_model, effective_on=date(2026, 9, 15))
    assert row["booking_protection_days"] == 60 and Decimal(row["insurance_od_percent"]) == 60
    assert row["parameters"][0]["parameter"] == "Penalty Amount"
    assert grid_row_for_model(connection, tenant_id=tenant_id, model_id=thar_model, effective_on=date(2026, 8, 15)) is None


def test_the_master_object_key_is_filed_by_effective_month() -> None:
    """Where a master document is kept.

    Filed under the month the document is EFFECTIVE for, not the day it
    was uploaded: the August-named grid whose sheet says "Effective 1st
    Sep" belongs to September. The upload id keeps a re-upload of the same
    month from overwriting the file it supersedes, because uploads are not
    idempotent and two files for one month is ordinary.
    """
    from datetime import date as _date
    from uuid import UUID as _UUID

    from audit_core.oem_price_masters import _master_content_type, _master_object_key

    key = _master_object_key(
        tenant_id="tenant-mahindraorissa", oem_code="mahindra", master_kind="PRICE_LIST",
        effective_from=_date(2026, 10, 3),
        upload_id=_UUID("11111111-2222-3333-4444-555555555555"),
        filename="Mahindra Consolidated Price List Oct'26 (1).xlsx",
    )
    assert key == (
        "tenant-mahindraorissa/oem-masters/MAHINDRA/2026-10/PRICE_LIST/"
        "11111111-2222-3333-4444-555555555555_Mahindra_Consolidated_Price_List_Oct_26_1_.xlsx"
    )

    # A grid named for August but effective in September files under September.
    september = _master_object_key(
        tenant_id="t", oem_code="MAHINDRA", master_kind="DISCOUNT_GRID",
        effective_from=_date(2026, 9, 1),
        upload_id=_UUID("66666666-7777-8888-9999-000000000000"),
        filename="Odisha PV & CV Discount Grid-1 Aug 2026.xlsx",
    )
    assert "/2026-09/DISCOUNT_GRID/" in september

    assert _master_content_type("book.xlsx").endswith("spreadsheetml.sheet")
    assert _master_content_type("scheme.PDF") == "application/pdf"
    assert _master_content_type(None) == "application/octet-stream"


# ── scheme resolution (2026-10-20 product owner rules) ──────────────────────────
def _eligibility(connection, tenant_id, code_like: str) -> list[dict]:
    return [dict(r) for r in connection.execute(
        text("""
            SELECT s.scheme_code, v.version_no, e.variant_id, pv.variant_name, pm.model_name, e.criteria,
                   e.customer_type_code
            FROM auditcore.discount_schemes s
            JOIN auditcore.discount_scheme_versions v
              ON v.tenant_id = s.tenant_id AND v.discount_scheme_id = s.discount_scheme_id
            JOIN auditcore.discount_scheme_eligibility e
              ON e.tenant_id = v.tenant_id AND e.discount_scheme_version_id = v.discount_scheme_version_id
            JOIN auditcore.product_models pm ON pm.model_id = e.model_id
            LEFT JOIN auditcore.product_variants pv ON pv.variant_id = e.variant_id
            WHERE s.tenant_id = :t AND s.scheme_code LIKE :c
            ORDER BY s.scheme_code, pv.variant_name
        """),
        {"t": tenant_id, "c": code_like},
    ).mappings().all()]


def test_scheme_variants_that_do_not_resolve_are_stored_unresolved_never_model_wide(connection) -> None:
    from audit_core.oem_master_parsers import split_variant_cell

    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(
            ("XUV 3XO", "AX5 PM MT", 1_029_900), ("XUV 3XO", "MX2 PRO PM AT", 900_000),
            ("XUV 3XO", "MX2 PRO PM MT", 880_000),
            ("XUV400", "EL PRO FC - Battery 34.5 Kwh - Charger 7.2 Kw - E", 1_700_000),
        )),
        actor_id="admin",
    )
    sept = ParseResult(kind="CONSUMER_SCHEME")
    ice = {"vehicleCategory": "ICE"}
    sept.discount_rows = [
        DiscountRow(1, "CONSUMER", "XUV 3XO", ["AX5 PM MT"], [("CASH_DISCOUNT", Decimal(30000))], Decimal(30000), ice),
        DiscountRow(2, "CONSUMER", "XUV 3XO", split_variant_cell("MX2 PRO PM AT & MT"),
                    [("CASH_DISCOUNT", Decimal(60000))], Decimal(60000), ice),
        DiscountRow(3, "CONSUMER", "XUV 3XO", ["MX9 IMAGINARY"], [("CASH_DISCOUNT", Decimal(10000))],
                    Decimal(10000), ice),
        DiscountRow(4, "CONSUMER", "XUV400", split_variant_cell(
            "EC PRO - Battery 34.5 Kwh-E-NCH\nEL PRO FC - Battery 34.5 Kwh -\nCharger 7.2 Kw - E"),
            [("CASH_DISCOUNT", Decimal(250000))], Decimal(250000), {"vehicleCategory": "EV"}),
    ]
    summary = ingest_discount_document(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        master_kind="CONSUMER_SCHEME", effective_from=date(2026, 9, 3), parsed=sept, actor_id="admin",
    )
    rows = _eligibility(connection, tenant_id, "MAH_CONSUMER_%")
    assert not [r for r in rows if r["criteria"].get("scope") == "MODEL"]  # nothing named became model-wide
    by_variant = {(r["model_name"], r["variant_name"]): r for r in rows if r["variant_id"]}
    assert ("XUV 3XO", "MX2 PRO PM AT") in by_variant and ("XUV 3XO", "MX2 PRO PM MT") in by_variant
    # The catalogue is shared by every test project of the OEM, so another
    # model spelt "XUV 3XO" may exist without these variants: unresolved
    # rows are checked by what no target model carries (the summary).
    unresolved = [r for r in rows if r["criteria"].get("scope") == "UNRESOLVED"]
    assert unresolved and all(r["variant_id"] is None for r in unresolved)
    texts = {t for r in unresolved for t in r["criteria"]["unresolvedVariantTexts"]}
    assert {"EC PRO - Battery 34.5 Kwh-E-NCH", "MX9 IMAGINARY"} <= texts
    bev = [r for r in rows if "EC PRO" in str(r["criteria"]) or "EL PRO" in str(r["criteria"])]
    assert bev and {r["model_name"] for r in bev} == {"XUV400"}  # the EV offer never reaches XUV 3XO
    assert {u["variantTexts"][0] for u in summary["unresolvedVariants"]} == {
        "MX9 IMAGINARY", "EC PRO - Battery 34.5 Kwh-E-NCH"}

    # An EV offer named against an ICE model is refused, not dropped quietly.
    from audit_core.errors import ValidationError

    wrong = ParseResult(kind="CONSUMER_SCHEME")
    wrong.discount_rows = [DiscountRow(1, "CONSUMER", "XUV 3XO", ["EC PRO"], [("CASH_DISCOUNT", Decimal(250000))],
                                       Decimal(250000), {"vehicleCategory": "EV"})]
    with pytest.raises(ValidationError) as refused:
        ingest_discount_document(
            connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
            master_kind="CONSUMER_SCHEME", effective_from=date(2026, 9, 3), parsed=wrong, actor_id="admin",
        )
    assert "an EV offer named against 'XUV 3XO'" in refused.value.detail


def test_aliases_never_merge_models_and_conditions_make_separate_schemes(connection) -> None:
    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(
            ("THE BOSS- BOLERO+NEO", "B4 BS6.2 - Refresh", 900_000),
            ("THE BOSS- BOLERO+NEO", "N8 BS6.2 - Refresh", 1_000_000),
            ("THE BOSS- BOLERO+NEO", "P10 BS6.2", 1_100_000),
            ("PICK UP", "BOL CAMPER 2WD BS6.2 - Refresh", 950_000),
            ("PICK UP", "BOL PIK UP FB 1.3T - BS6.2 - PS", 850_000),
            ("XUV 3XO", "AX5 PM MT", 1_029_900), ("XUV 3XO", "REVX M PM MT", 1_100_000),
            ("XUV 3XO", "REVX EDGE PM MT", 1_200_000),
        )),
        actor_id="admin",
    )
    section = {"section": "EXCHANGE_PERSONAL"}
    exchange = ParseResult(kind="EXCHANGE_SCHEME")
    exchange.discount_rows = [
        DiscountRow(1, "EXCHANGE", "Camper", [], [("EXCHANGE_BONUS", Decimal(20000))], Decimal(20000),
                    {"section": "EXCHANGE_COMMERCIAL"}),
        DiscountRow(2, "EXCHANGE", "Pickup", [], [("EXCHANGE_BONUS", Decimal(35000))], Decimal(35000),
                    {"section": "EXCHANGE_COMMERCIAL"}),
        DiscountRow(3, "EXCHANGE", "XUV 3XO (Including REVX)", [], [("EXCHANGE_BONUS", Decimal(50000))],
                    Decimal(50000), section),
        DiscountRow(4, "EXCHANGE", "XUV 3XO (Including REVX) against Old Maruti Vehicle**", [],
                    [("EXCHANGE_BONUS", Decimal(70000))], Decimal(70000), section),
        DiscountRow(5, "EXCHANGE", "XUV300", [], [("EXCHANGE_BONUS", Decimal(20000))], Decimal(20000), section),
    ]
    summary = ingest_discount_document(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        master_kind="EXCHANGE_SCHEME", effective_from=date(2026, 9, 3), parsed=exchange, actor_id="admin",
    )
    assert summary["published"] == 4  # XUV300 is not sold: reported, not applied
    assert any("'XUV300' is not in the loaded price list" in u for u in summary["unresolved"])
    rows = _eligibility(connection, tenant_id, "MAH_EXCHANGE_%")
    codes = {r["scheme_code"] for r in rows}
    camper = [r for r in rows if r["scheme_code"].endswith("PICK_UP_CAMPER")]
    pickup = [r for r in rows if r["scheme_code"].endswith("PICK_UP_PICKUP")]
    assert [r["variant_name"] for r in camper] == ["BOL CAMPER 2WD BS6.2 - Refresh"]
    assert "BOL PIK UP FB 1.3T - BS6.2 - PS" in [r["variant_name"] for r in pickup]
    assert not [r for r in pickup if "CAMPER" in r["variant_name"]]
    maruti = [r for r in rows if r["scheme_code"].endswith("OLD_MARUTI")]
    assert maruti and all(r["criteria"]["requiredOldVehicle"] == {"brand": "MARUTI"} for r in maruti)
    general = [r for r in rows if "XUV" in r["scheme_code"] and not r["scheme_code"].endswith("OLD_MARUTI")]
    assert general and all(r["version_no"] == 1 for r in general + maruti)  # two schemes, not two versions
    assert len(codes) == 4

    corporate = ParseResult(kind="CORPORATE_POLICY")
    corporate.corporate_benefits = [
        CorporateBenefitRow(1, "Z", "BOLERO NEO+", Decimal(24000), Decimal(16000), Decimal(40000)),
        CorporateBenefitRow(2, "Z", "BOLERO NEO", Decimal(18000), Decimal(12000), Decimal(30000)),
        CorporateBenefitRow(3, "Z", "BOLERO", Decimal(18000), Decimal(12000), Decimal(30000)),
        CorporateBenefitRow(4, "F", "XUV 3XO (Not on 3XO EV)", Decimal(2500), Decimal(2500), Decimal(5000)),
        CorporateBenefitRow(5, "F", "3XO REVX (Not on REVX Edge)", Decimal(5000), Decimal(5000), Decimal(10000)),
    ]
    ingest_corporate_policy(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
        effective_from=date(2026, 9, 3), parsed=corporate, upload_id=uuid4(), actor_id="admin",
    )
    rows = _eligibility(connection, tenant_id, "MAH_CORPORATE_%")
    by_code: dict[str, list] = {}
    for r in rows:
        if r["criteria"].get("scope") != "UNRESOLVED":  # other test models of the same name
            by_code.setdefault(r["scheme_code"], []).append(r["variant_name"])
    assert by_code["MAH_CORPORATE_Z_THE_BOSS_BOLERO_NEO_BOLERO_NEO_PLUS"] == ["P10 BS6.2"]
    assert by_code["MAH_CORPORATE_Z_THE_BOSS_BOLERO_NEO_BOLERO_NEO"] == ["N8 BS6.2 - Refresh"]
    assert by_code["MAH_CORPORATE_Z_THE_BOSS_BOLERO_NEO_BOLERO"] == ["B4 BS6.2 - Refresh"]
    assert by_code["MAH_CORPORATE_F_XUV_3XO_REVX"] == ["REVX M PM MT"]  # REVX only, Edge excluded
    assert set(by_code["MAH_CORPORATE_F_XUV_3XO"]) == {None}  # the model-wide XUV 3XO line
    assert all(r["customer_type_code"].startswith("CORPORATE:") for r in rows)


def test_a_scheme_row_naming_an_unreadable_model_blocks_the_upload(connection) -> None:
    from audit_core.errors import ValidationError

    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(("XUV 3XO", "AX5 PM MT", 1_029_900))), actor_id="admin",
    )
    parsed = ParseResult(kind="EXCHANGE_SCHEME")
    parsed.discount_rows = [
        DiscountRow(1, "EXCHANGE", "XUV 3XO EDGE & REVX Variants", [], [("EXCHANGE_BONUS", Decimal(10000))],
                    Decimal(10000), {"section": "EXCHANGE_PERSONAL"}),
    ]
    with pytest.raises(ValidationError) as refused:
        ingest_discount_document(
            connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
            master_kind="EXCHANGE_SCHEME", effective_from=date(2026, 9, 3), parsed=parsed, actor_id="admin",
        )
    assert "name a model that is not known" in refused.value.detail
    assert "XUV 3XO EDGE & REVX Variants" in refused.value.detail


def test_variant_match_ignores_the_registration_suffix() -> None:
    from audit_core.oem_price_masters import _match_variants

    private, commercial = uuid4(), uuid4()
    matches = _match_variants(["P10 BS6.2", "Z8 G MT 2WD 7STR"], [
        ("P10 BS6.2 (Private)", private), ("P10 BS6.2 (Commercial)", commercial), ("Z8 G MT 2WD 7 STR", uuid4()),
    ])
    assert matches[0] == ("P10 BS6.2", [private, commercial])
    assert len(matches[1][1]) == 1


def test_the_october_grid_keeps_tiers_and_variant_groups_and_never_falls_back(connection) -> None:
    from audit_core.oem_master_parsers import GridRow
    from audit_core.oem_price_masters import grid_row_for_model, ingest_discount_grid

    tenant_id = connection.tenant_id
    project = _project_oem(connection, tenant_id)
    _, skus = ingest_price_list(
        connection, tenant_id=tenant_id, oem_id=project["oem_id"], effective_from=date(2026, 9, 1),
        parsed=parse_price_list(_price_bytes(
            ("PICK UP", "BOL CAMPER 2WD BS6.2 - Refresh", 950_000), ("PICK UP", "BOL PIK UP FB 1.3T - BS6.2 - PS", 850_000),
            ("THAR ROXX", "MX1 PMT 2WD", 1_000_000),
        )),
        actor_id="admin",
    )
    camper_sku = next(sku for code, sku in skus.items() if "CAMPER" in code.upper())
    pick_up = connection.execute(text("SELECT model_id FROM auditcore.product_skus WHERE product_sku_id = :s"),
                                 {"s": camper_sku}).scalar_one()
    thar = connection.execute(text("SELECT model_id FROM auditcore.product_skus WHERE product_sku_id = :s"),
                              {"s": skus["THAR_ROXX::MX1 PMT 2WD"]}).scalar_one()
    try:
        august = ParseResult(kind="DISCOUNT_GRID")
        august.grid_rows = [GridRow(3, "Thar Roxx", ["Thar Roxx"], True, 60, Decimal(5000), Decimal(60), Decimal(3000))]
        ingest_discount_grid(connection, tenant_id=tenant_id, oem_id=project["oem_id"], oem_code="MAHINDRA",
                             effective_from=date(2026, 9, 1), parsed=august, upload_id=None, actor_id="admin")
        october = ParseResult(kind="DISCOUNT_GRID")
        tiers = {"BASE": Decimal(5000), "WITH_INSURANCE": Decimal(7000), "WITH_EW_INSURANCE": Decimal(7000)}
        october.grid_rows = [
            GridRow(14, "PICKUP, MAXX City", ["PICKUP", "MAXX City"], True, 30, Decimal(5000), Decimal(65),
                    Decimal(5000), tiers=tiers),
            GridRow(17, "Camper", ["Camper"], True, 60, Decimal(0), Decimal(65), Decimal(5000),
                    tiers={k: Decimal(0) for k in tiers}),
        ]
        summary = ingest_discount_grid(connection, tenant_id=tenant_id, oem_id=project["oem_id"],
                                       oem_code="MAHINDRA", effective_from=date(2026, 10, 1), parsed=october,
                                       upload_id=None, actor_id="admin")
        assert summary["resolved"] == 2  # MAXX City is not in this catalogue
        camper = grid_row_for_model(connection, tenant_id=tenant_id, model_id=pick_up, effective_on=date(2026, 10, 9),
                                    variant_name="BOL CAMPER 2WD BS6.2 - Refresh")
        pickup = grid_row_for_model(connection, tenant_id=tenant_id, model_id=pick_up, effective_on=date(2026, 10, 9),
                                    variant_name="BOL PIK UP FB 1.3T - BS6.2 - PS")
        assert camper["model_alias"] == "Camper" and Decimal(camper["agreed_buffer_amount"]) == 0
        assert pickup["model_alias"] == "PICKUP" and pickup["raw"]["tiers"]["WITH_INSURANCE"] == "7000"
        # Thar Roxx is in the August grid only: October has no line for it.
        assert grid_row_for_model(connection, tenant_id=tenant_id, model_id=thar, effective_on=date(2026, 9, 9))
        assert grid_row_for_model(connection, tenant_id=tenant_id, model_id=thar, effective_on=date(2026, 10, 9)) is None
    finally:
        connection.execute(text("DELETE FROM auditcore.dealer_discount_grid_rows WHERE tenant_id=:t"), {"t": tenant_id})
        connection.execute(text("DELETE FROM auditcore.dealer_discount_grid_versions WHERE tenant_id=:t"),
                           {"t": tenant_id})
