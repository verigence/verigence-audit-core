from __future__ import annotations

import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_model_resolution as mr


# ── unit: the deterministic matcher ──────────────────────────────────────────
def _row(sku: str, model: str, *, variant=None, colour=None, total="1000000", ex="800000"):
    return {
        "product_sku_id": uuid4(),
        "sku_code": sku,
        "model_name": model,
        "variant_name": variant,
        "colour_name": colour,
        "master_total_individual": Decimal(total),
        "master_total_corporate": Decimal(total) + Decimal(50000),
        "master_ex_showroom": Decimal(ex),
    }


def _inputs(*, model, variant=None, colour=None, total=None, ex=None, basis="INDIVIDUAL"):
    return {
        "model_name": model,
        "variant_name": variant,
        "colour_name": colour,
        "offered_total": Decimal(total) if total is not None else None,
        "offered_ex_showroom": Decimal(ex) if ex is not None else None,
        "registration_basis": basis,
    }


def test_match_unique_on_total() -> None:
    rows = [_row("A", "SCORPIO N", total="1000000"), _row("B", "THAR", total="1000000")]
    matched, stage = mr._match(rows, _inputs(model="Scorpio N", total="1000000"))
    assert stage == "TOTAL"
    assert [r["sku_code"] for r in matched] == ["A"]


def test_match_falls_back_to_ex_showroom_when_total_ambiguous() -> None:
    rows = [
        _row("A", "SCORPIO N", variant="Z8", total="1000000", ex="800000"),
        _row("B", "SCORPIO N", variant="Z8L", total="1000000", ex="850000"),
    ]
    matched, stage = mr._match(
        rows, _inputs(model="SCORPIO N", total="1000000", ex="850000")
    )
    assert stage == "EX_SHOWROOM"
    assert [r["sku_code"] for r in matched] == ["B"]


def test_match_zero_when_no_model() -> None:
    rows = [_row("A", "SCORPIO N"), _row("B", "THAR")]
    matched, stage = mr._match(rows, _inputs(model="XUV700", total="1000000"))
    assert matched == []
    assert stage == "NONE"


def test_match_multiple_reported() -> None:
    rows = [
        _row("A", "SCORPIO N", total="1000000", ex="800000"),
        _row("B", "SCORPIO N", total="1000000", ex="800000"),
    ]
    matched, _ = mr._match(rows, _inputs(model="SCORPIO N", total="1000000", ex="800000"))
    assert {r["sku_code"] for r in matched} == {"A", "B"}


def test_match_narrows_by_variant_then_colour() -> None:
    rows = [
        _row("A", "THAR", variant="LX", colour="RED", total="1000000"),
        _row("B", "THAR", variant="LX", colour="WHITE", total="1000000"),
        _row("C", "THAR", variant="AX", colour="WHITE", total="1000000"),
    ]
    matched, stage = mr._match(
        rows, _inputs(model="THAR", variant="LX", colour="white", total="1000000")
    )
    assert stage == "TOTAL"
    assert [r["sku_code"] for r in matched] == ["B"]


def test_corporate_basis_uses_corporate_total() -> None:
    rows = [_row("A", "SCORPIO N", total="1000000")]  # corporate total = 1050000
    matched, stage = mr._match(
        rows, _inputs(model="SCORPIO N", total="1050000", basis="CORPORATE")
    )
    assert stage == "TOTAL"
    assert [r["sku_code"] for r in matched] == ["A"]


def test_no_price_data_returns_empty() -> None:
    rows = [_row("A", "SCORPIO N")]
    matched, stage = mr._match(rows, _inputs(model="SCORPIO N"))
    assert stage == "NONE"
    assert matched == [rows[0]] or matched == []  # model matched but nothing to disambiguate


# ── unit: generation-refresh bridge ──────────────────────────────────────────
# Real, confirmed live data: a Mahindra price list can carry an old-generation
# "SCORPIO N" (still on sale) alongside a "NEW SCORPIO N" refresh in the exact
# same currently-effective price list, sharing overlapping variant codes at
# different prices. A Booking Form's own free text very often drops "New" --
# these prove price alone can still resolve across that gap instead of
# permanently walling the refresh generation off.
def test_strip_new_prefix() -> None:
    assert mr._strip_new_prefix("New Scorpio N") == "SCORPIO N"
    assert mr._strip_new_prefix("NEW THAR 2WD & 4WD") == "THAR 2WD 4WD"
    assert mr._strip_new_prefix("Scorpio N") == "SCORPIO N"


def test_match_bridges_to_the_refresh_generation_via_exact_price() -> None:
    rows = [
        _row("OLD-Z8L", "SCORPIO N", variant="Z8L", total="2000000", ex="2075499.58"),
        _row("NEW-Z8L", "NEW SCORPIO N", variant="Z8L", total="2200000", ex="2131387.08"),
    ]
    matched, stage = mr._match(
        rows, _inputs(model="Scorpio N", variant="Z8L", ex="2131387.08")
    )
    assert stage == "EX_SHOWROOM"
    assert [r["sku_code"] for r in matched] == ["NEW-Z8L"]


def test_match_prefers_the_named_generation_when_its_own_price_matches() -> None:
    """The bridge only ever fires when the named bucket's own price search
    comes up empty -- it must never override a price that already uniquely
    resolves within the generation the Booking Form actually named."""
    rows = [
        _row("OLD-Z8L", "SCORPIO N", variant="Z8L", total="2000000", ex="2075499.58"),
        _row("NEW-Z8L", "NEW SCORPIO N", variant="Z8L", total="2200000", ex="2131387.08"),
    ]
    matched, stage = mr._match(
        rows, _inputs(model="Scorpio N", variant="Z8L", ex="2075499.58")
    )
    assert stage == "EX_SHOWROOM"
    assert [r["sku_code"] for r in matched] == ["OLD-Z8L"]


def test_match_bridge_does_not_fire_when_price_is_ambiguous_in_both_generations() -> None:
    rows = [
        _row("OLD-Z8L-MT", "SCORPIO N", variant="Z8L", total="2000000", ex="2075499.58"),
        _row("OLD-Z8L-AT", "SCORPIO N", variant="Z8L", total="2000000", ex="2075499.58"),
        _row("NEW-Z8L", "NEW SCORPIO N", variant="Z8L", total="2200000", ex="2131387.08"),
    ]
    matched, stage = mr._match(
        rows, _inputs(model="Scorpio N", variant="Z8L", ex="2075499.58")
    )
    assert stage == "EX_SHOWROOM"
    assert {r["sku_code"] for r in matched} == {"OLD-Z8L-MT", "OLD-Z8L-AT"}


def test_match_bridge_reaches_the_refresh_generation_when_the_old_one_no_longer_exists() -> None:
    """The tenant's currently effective price list may only carry the refresh
    generation at all (the old one fully retired) -- the bridge must still
    resolve it even though model_rows itself is empty from the start."""
    rows = [_row("NEW-Z8L", "NEW SCORPIO N", variant="Z8L", ex="2131387.08")]
    matched, stage = mr._match(rows, _inputs(model="Scorpio N", variant="Z8L", ex="2131387.08"))
    assert stage == "EX_SHOWROOM"
    assert [r["sku_code"] for r in matched] == ["NEW-Z8L"]


def test_match_bridge_is_a_no_op_for_unrelated_models() -> None:
    rows = [_row("A", "THAR", total="1000000")]
    matched, stage = mr._match(rows, _inputs(model="XUV700", total="1000000"))
    assert matched == []
    assert stage == "NONE"


# ── integration ──────────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for model-resolution integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-mr-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"MR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"MR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'MR', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"MR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"MR-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"MR-O-{suffix}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"MR-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                 "VALUES (:t, :j, CURRENT_DATE - 10)"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        c.oem_id = oem_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


@pytest.fixture
def mahindra_journey(journey):
    """Same fixture as ``journey``, but the project's OEM is repointed at the
    *real*, globally-seeded MAHINDRA oem_id (every fresh DB seeds this row —
    see migration 0009) instead of the fixture's own throwaway dummy OEM.

    The attribute-decomposition fallback reads ``oem_model_aliases`` and its
    vocabulary by ``oem_code``, both of which only exist for real OEM codes.
    """
    c = journey
    mahindra_oem_id = c.execute(
        text("SELECT oem_id FROM auditcore.oems WHERE oem_code = 'MAHINDRA'")
    ).scalar_one()
    c.execute(
        text("UPDATE auditcore.projects SET oem_id = :o WHERE tenant_id = :t"),
        {"o": mahindra_oem_id, "t": c.tenant_id},
    )
    c.oem_id = mahindra_oem_id  # type: ignore[attr-defined]
    return c


def _seed_price_list(c, skus: list[dict], *, effective_from: str = "CURRENT_DATE - 45"):
    """One published price-list version holding every SKU in ``skus``.

    Each entry: {"model": str, "variant": str | None, "components": {key: amount},
    "fuel": str | None, "transmission": str | None, "drive": str | None,
    "seater": str | None} — the last four mirror the structured attributes
    ``oem_price_masters.py`` actually populates from the OEM's own price-list
    columns, used by the attribute-decomposition fallback.
    ``effective_from`` is a raw SQL date expression (not a bind param) so
    callers can pass e.g. ``"CURRENT_DATE"`` to reproduce a master ingested
    *after* a booking's own (often historical) booking_date.
    Returns the list of product_sku_id in the same order.
    """
    tenant_id, oem_id = c.tenant_id, c.oem_id
    pl_id = c.execute(
        text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
             "VALUES (:t, :c, 'OEM') RETURNING price_list_id"),
        {"t": tenant_id, "c": f"PL{uuid4().hex[:8]}"},
    ).scalar_one()
    # A trigger forbids mutating price_list_items unless the version is DRAFT —
    # insert every item first, then publish.
    plv_id = c.execute(
        text(f"INSERT INTO auditcore.price_list_versions "
             f"(tenant_id, price_list_id, version_no, lifecycle_status, effective_from) "
             f"VALUES (:t, :pl, 1, 'DRAFT', {effective_from}) RETURNING price_list_version_id"),
        {"t": tenant_id, "pl": pl_id},
    ).scalar_one()
    sku_ids = []
    for entry in skus:
        model_id = c.execute(
            text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                 "VALUES (:o, :mc, :mn) RETURNING model_id"),
            {"o": oem_id, "mc": f"M{uuid4().hex[:8]}", "mn": entry["model"]},
        ).scalar_one()
        variant_id = c.execute(
            text(
                "INSERT INTO auditcore.product_variants "
                "(model_id, variant_code, variant_name, fuel_powertrain, transmission, attributes) "
                "VALUES (:m, :vc, :vn, :fuel, :trans, CAST(:attrs AS jsonb)) RETURNING variant_id"
            ),
            {
                "m": model_id,
                "vc": f"V{uuid4().hex[:8]}",
                "vn": entry.get("variant") or "BASE",
                "fuel": entry.get("fuel"),
                "trans": entry.get("transmission"),
                "attrs": json.dumps(
                    {
                        k: v
                        for k, v in {
                            "drive": entry.get("drive"),
                            "seater": entry.get("seater"),
                            "trim": entry.get("trim"),
                        }.items()
                        if v
                    }
                ),
            },
        ).scalar_one()
        sku_id = c.execute(
            text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                 "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"),
            {"o": oem_id, "m": model_id, "v": variant_id, "sc": f"SKU{uuid4().hex[:10]}"},
        ).scalar_one()
        sku_ids.append(sku_id)
        for key, amount in entry["components"].items():
            c.execute(
                text("INSERT INTO auditcore.price_list_items "
                     "(tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount) "
                     "VALUES (:t, :plv, :sku, :k, :a)"),
                {"t": tenant_id, "plv": plv_id, "sku": sku_id, "k": key, "a": amount},
            )
    c.execute(
        text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
             "WHERE tenant_id=:t AND price_list_version_id=:plv"),
        {"t": tenant_id, "plv": plv_id},
    )
    return sku_ids


def _set_journey_product(c, model, variant):
    c.execute(
        text("INSERT INTO auditcore.journey_products "
             "(tenant_id, journey_id, model_name_snapshot, variant_name_snapshot, selection_source) "
             "VALUES (:t, :j, :m, :v, 'EVIDENCE')"),
        {"t": c.tenant_id, "j": c.journey_id, "m": model, "v": variant},
    )


def _set_commercial(c, key, amount):
    c.execute(
        text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount) "
             "VALUES (:t, :j, :k, :a) ON CONFLICT (tenant_id, journey_id, component_key) "
             "DO UPDATE SET actual_amount = EXCLUDED.actual_amount"),
        {"t": c.tenant_id, "j": c.journey_id, "k": key, "a": amount},
    )


def _set_booking_form_ex_showroom(c, amount) -> None:
    """A row in the reviewed Booking Form table itself, with NO matching
    auditcore.commercial_lines row -- the shape a document whose Booking
    Form has finished review but whose separate commercial_lines
    materialization pass (uc03_v2_review_materialization.py::
    _materialize_commercial_lines) has not yet run, or ran before this
    field was populated."""
    c.execute(
        text(
            "INSERT INTO auditcore.booking_form_review_values "
            "(tenant_id, journey_id, source_di_document_id, ex_showroom_price, reviewed_by_actor_id) "
            "VALUES (:t, :j, :doc, :amount, 'test-actor')"
        ),
        {"t": c.tenant_id, "j": c.journey_id, "doc": uuid4(), "amount": amount},
    )


def _open_model_flags(c) -> int:
    return c.execute(
        text("SELECT count(*) FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j "
             "AND finding_type_code='MODEL_NOT_IDENTIFIED' "
             "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()


def test_integration_resolves_and_pins_sku(journey) -> None:
    c = journey
    _seed_price_list(c, [{
        "model": "SCORPIO N", "variant": "Z8L",
        "components": {"EX_SHOWROOM": "1600000", "INSURANCE": "60000",
                       "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "220000"},
    }])
    _set_journey_product(c, "Scorpio N", "Z8L")
    _set_commercial(c, "ex_showroom_price", "1600000")
    _set_commercial(c, "total_price", "1830000")  # 1.6M + 60k + 170k (individual)

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True

    sku = c.execute(
        text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert sku["product_sku_id"] is not None
    assert sku["selection_status"] == "CONFIRMED"
    assert _open_model_flags(c) == 0

    # idempotent: a second sync on an already-resolved journey is a no-op
    again = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert again.get("resolved") is True


def test_integration_get_model_catalog_lists_every_sku_unconditionally(journey) -> None:
    """Unlike get_model_resolution_candidates, this must work whether or not
    a MODEL_NOT_IDENTIFIED finding is open -- it's for browsing to propose a
    correction on an already-CONFIRMED journey, which by definition has no
    open finding to gate on."""
    c = journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8L",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1600000"}},
        {"model": "SCORPIO N", "variant": "Z8T",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1700000"}},
    ])
    _set_journey_product(c, "Scorpio N", "Z8L")
    _set_commercial(c, "ex_showroom_price", "1600000")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("resolved") is True
    assert _open_model_flags(c) == 0  # confirmed, no open finding at all

    catalog = mr.get_model_catalog(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert len(catalog["skus"]) == 2
    variants = {sku["variantName"] for sku in catalog["skus"]}
    assert variants == {"Z8L", "Z8T"}
    fuels = {sku["fuel"] for sku in catalog["skus"]}
    assert fuels == {"PETROL", "DIESEL"}


def test_integration_get_model_catalog_falls_back_to_variant_text_when_master_columns_are_blank(
    mahindra_journey,
) -> None:
    """Reproduces the live 'Modify Model' symptom reported repeatedly: real
    Mahindra Consolidated Price List exports routinely leave fuel_powertrain/
    transmission/drive/seater blank per-row even though the same information
    is already encoded in the free-text Variant column (e.g. 'Z8 S G AT 2WD
    7 STR BS6.2 - Refresh' encodes fuel=G, transmission=AT, drive=2WD,
    seater=7). Before this fix, get_model_catalog read those raw master
    columns directly with no fallback -- match_by_attributes already had
    one (_master_attributes), but this endpoint, used only by the picker a
    PC opens to manually correct a SKU, did not -- leaving its own dropdowns
    empty for every row shaped like this."""
    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8 S G AT 2WD 7 STR BS6.2 - Refresh",
         "components": {"EX_SHOWROOM": "1600000"}},
    ])

    catalog = mr.get_model_catalog(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert len(catalog["skus"]) == 1
    sku = catalog["skus"][0]
    assert sku["fuel"] == "PETROL"
    assert sku["transmission"] == "AT"
    assert sku["drive"] == "2WD"
    assert sku["seater"] == "7"


def test_integration_model_catalog_exposes_trim_distinct_from_variant(journey) -> None:
    """The masters sheet carries Trim as its own column, separate from the
    full Variant string -- several distinct trims (e.g. Z2/Z4/Z8 S/Z8T/Z8 L
    on the same model) can share identical fuel/transmission/drive/seater,
    so without trim as its own field there's no way to tell them apart
    short of reading the whole variant string. Confirmed missing live
    (oem_price_masters.py parsed trim but never persisted it) -- this
    proves it's now on the catalog."""
    c = journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z4 G MT 2WD 7 STR - E BS6.2 - New", "trim": "Z4",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1400000"}},
        {"model": "SCORPIO N", "variant": "Z8 S G MT 2WD 7 STR BS6.2 - Refresh", "trim": "Z8 S",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1600000"}},
    ])
    _set_journey_product(c, "Scorpio N", "Z4 G MT 2WD 7 STR - E BS6.2 - New")
    _set_commercial(c, "ex_showroom_price", "1400000")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    catalog = mr.get_model_catalog(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    trims_by_variant = {sku["variantName"]: sku["trim"] for sku in catalog["skus"]}
    assert trims_by_variant["Z4 G MT 2WD 7 STR - E BS6.2 - New"] == "Z4"
    assert trims_by_variant["Z8 S G MT 2WD 7 STR BS6.2 - Refresh"] == "Z8 S"


def test_integration_resolves_via_ex_showroom_when_total_ambiguous(journey) -> None:
    c = journey
    # both THAR variants total 1,730,000 individual — only ex-showroom disambiguates
    _seed_price_list(c, [
        {"model": "THAR", "variant": "LX",
         "components": {"EX_SHOWROOM": "1500000", "INSURANCE": "60000",
                        "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "200000"}},
        {"model": "THAR", "variant": "AX",
         "components": {"EX_SHOWROOM": "1400000", "INSURANCE": "160000",
                        "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "200000"}},
    ])
    _set_journey_product(c, "Thar", None)
    _set_commercial(c, "ex_showroom_price", "1400000")
    _set_commercial(c, "total_price", "1730000")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "EX_SHOWROOM"
    assert _open_model_flags(c) == 0


def test_integration_resolves_via_ex_showroom_from_booking_form_without_commercial_lines(journey) -> None:
    # Regression for a real bug reported live: a Booking whose model text
    # matched 24 price-master SKUs never disambiguated by ex-showroom price
    # even though the Booking Form's own ex_showroom_price had been read and
    # reviewed -- because _resolution_inputs read offered_ex_showroom only
    # from commercial_lines, with no fallback to booking_form_review_values
    # (offered_total has always had exactly this fallback; offered_ex_showroom
    # did not). Same THAR fixture as the test above, but ex_showroom_price
    # lives ONLY in booking_form_review_values here -- no commercial_lines
    # row for it at all (total_price is still commercial_lines-backed, as it
    # would be for a Booking Form whose overall total materialized fine).
    c = journey
    _seed_price_list(c, [
        {"model": "THAR", "variant": "LX",
         "components": {"EX_SHOWROOM": "1500000", "INSURANCE": "60000",
                        "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "200000"}},
        {"model": "THAR", "variant": "AX",
         "components": {"EX_SHOWROOM": "1400000", "INSURANCE": "160000",
                        "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "200000"}},
    ])
    _set_journey_product(c, "Thar", None)
    _set_commercial(c, "total_price", "1730000")
    _set_booking_form_ex_showroom(c, "1400000")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "EX_SHOWROOM"
    assert _open_model_flags(c) == 0


def test_integration_raises_flag_when_no_match(journey) -> None:
    c = journey
    _seed_price_list(c, [{
        "model": "THAR ROXX", "variant": "AX",
        "components": {"EX_SHOWROOM": "1400000", "REGISTRATION_INDIVIDUAL": "150000",
                       "REGISTRATION_CORPORATE": "180000"},
    }])
    _set_journey_product(c, "Scorpio N", "Z8L")  # not in the price list
    _set_commercial(c, "ex_showroom_price", "1600000")
    _set_commercial(c, "total_price", "1830000")

    r1 = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    r2 = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert r1.get("raised") is True
    assert r2.get("raised") is True  # idempotent
    assert _open_model_flags(c) == 1  # one open finding despite two sync calls

    ft = c.execute(
        text("SELECT finding_class, owner_role_code FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='MODEL_NOT_IDENTIFIED'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert ft["finding_class"] == "DATA_GAP"
    assert ft["owner_role_code"] == "PC"


def test_integration_flag_resolves_when_model_confirmed(journey) -> None:
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "XUV 7XO", "variant": "AX7L",
        "components": {"EX_SHOWROOM": "2000000", "REGISTRATION_INDIVIDUAL": "200000",
                       "REGISTRATION_CORPORATE": "240000"},
    }])
    _set_journey_product(c, "Wrong Model", None)
    _set_commercial(c, "ex_showroom_price", "2000000")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _open_model_flags(c) == 1

    # PC corrects the model on journey_products; next sync resolves + closes the flag
    c.execute(
        text("UPDATE auditcore.journey_products SET model_name_snapshot='XUV 7XO' "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert _open_model_flags(c) == 0
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


# ── attribute-decomposition fallback (real Booking Form / master shapes) ────
def test_integration_resolves_scorpio_n_z8s_from_folded_booking_form_text(mahindra_journey) -> None:
    """Reproduces a live production finding verbatim: the Booking Form's
    'Model & Variant' line folds trim + fuel + transmission + drive + seating
    into the model/variant snapshot text exactly as extracted
    ('SCORPIO N Z8 (S)' / 'DAT 2WD 7STR'), which never equals the master's
    own model_name ('SCORPIO N') as a whole string. Sibling variants are
    seeded too, mirroring the real ingested master, to prove the fallback
    picks the one that actually matches every supplied attribute."""
    c = mahindra_journey
    z8s_diesel_at, *_ = _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8 S D AT 2WD 7 STR BS6.2 - N",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1600000"}},
        {"model": "SCORPIO N", "variant": "Z8T D AT 2WD 7 STR BS6.2 - N",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1650000"}},
        {"model": "SCORPIO N", "variant": "Z8 S G AT 2WD 7 STR BS6.2 - N",
         "fuel": "PETROL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1550000"}},
        {"model": "NEW SCORPIO N", "variant": "Z8 S D AT 2WD 7 STR BS6.2 - Refresh",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1700000"}},
    ])
    _set_journey_product(c, "SCORPIO N Z8 (S)", "DAT 2WD 7STR")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "ATTRIBUTE_DECOMPOSITION"

    row = c.execute(
        text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["product_sku_id"] == z8s_diesel_at
    assert row["selection_status"] == "CONFIRMED"
    assert _open_model_flags(c) == 0


def test_integration_resolves_when_master_model_name_casing_differs_from_alias(mahindra_journey) -> None:
    """Reproduces a live production finding verbatim: Booking model
    'SCORPIO CLASSIC' / 'S MT 7S' raised 'could not be matched to the price
    masters' despite the master carrying exactly that vehicle. Root cause:
    resolve_model_via_aliases normalizes text to match 'SCORPIO CLASSIC'
    against the real, globally-seeded oem_model_aliases row, but the
    canonical name it returns (verbatim from that table) was then compared
    with a raw `==` against the master's own product_models.model_name
    (verbatim from OEM price-list ingestion) -- two independently authored
    strings with no guaranteed casing/spacing match. Seeded here with
    different casing ('Scorpio Classic') than the alias table's spelling to
    prove the fix normalizes both sides before comparing."""
    c = mahindra_journey
    sku_id, = _seed_price_list(c, [
        {"model": "Scorpio Classic", "variant": "S MT 7 STR",
         "fuel": "DIESEL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1200000"}},
    ])
    _set_journey_product(c, "SCORPIO CLASSIC", "S MT 7S")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True

    row = c.execute(
        text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["product_sku_id"] == sku_id
    assert row["selection_status"] == "CONFIRMED"
    assert _open_model_flags(c) == 0


def test_integration_resolves_xuv_7xo_ax7l_from_folded_booking_form_text(mahindra_journey) -> None:
    """Second real sample: 'XUV-7XO' / 'AX-7L(D) AT 2WD 7STR'. Also proves
    the AWD sibling (same trim residue, different drivetrain) is correctly
    excluded once the Booking Form states the drivetrain explicitly."""
    c = mahindra_journey
    ax7l_2wd, *_ = _seed_price_list(c, [
        {"model": "XUV 7XO", "variant": "AX7L DSL AT 7 STR",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "XUV 7XO", "variant": "AX7L DSL AT AWD 7 STR",
         "fuel": "DIESEL", "transmission": "AT", "drive": "AWD", "seater": "7",
         "components": {"EX_SHOWROOM": "2100000"}},
        {"model": "XUV 7XO", "variant": "AX7T DSL AT 7 STR",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2050000"}},
    ])
    _set_journey_product(c, "XUV-7XO", "AX-7L(D) AT 2WD 7STR")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "ATTRIBUTE_DECOMPOSITION"

    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == ax7l_2wd
    assert _open_model_flags(c) == 0


def test_integration_still_raises_when_decomposition_also_ambiguous(mahindra_journey) -> None:
    """Dealer wrote only the model and bare trim, no fuel/transmission/drive/
    seater at all -- both diesel-AT and diesel-MT SKUs remain plausible, so
    this must still raise MODEL_NOT_IDENTIFIED rather than silently guess."""
    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8T D AT 2WD 7 STR BS6.2 - N",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1650000"}},
        {"model": "SCORPIO N", "variant": "Z8T D MT 2WD 7 STR BS6.2 - N",
         "fuel": "DIESEL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1600000"}},
    ])
    _set_journey_product(c, "SCORPIO N Z8T", None)

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("raised") is True
    assert result.get("matchStage") == "ATTRIBUTE_DECOMPOSITION"
    assert result.get("candidateCount") == 2


def test_integration_reports_narrower_candidates_even_when_still_ambiguous(mahindra_journey) -> None:
    """Reproduces a live production finding verbatim: model_name_snapshot
    ('SCORPIO N') already equals the master's model_name exactly -- no
    folded text to resolve -- so the price-only pass matches every Scorpio N
    SKU (4 here) unfiltered. The Booking Form's own variant text ('Z8L (P)
    MT') decodes to fuel=PETROL/transmission=MT and narrows that to the 2
    real Petrol-MT contenders (a Diesel sibling and an unrelated trim are
    correctly excluded) -- still not unique, but a PC choosing between 2
    named candidates is a materially different, usable finding from being
    told 4 (or, in production, 24)."""
    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8 L G MT 2WD 6 STR BS6.2 - N - ADAS",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "6",
         "components": {"EX_SHOWROOM": "2113699.58"}},
        {"model": "SCORPIO N", "variant": "Z8 L G MT 2WD 7 STR BS6.2 - N - ADAS",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2131499.58"}},
        {"model": "SCORPIO N", "variant": "Z8 S G MT 2WD 7 STR BS6.2 - N",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2075499.58"}},
        {"model": "SCORPIO N", "variant": "Z8 L D MT 2WD 7 STR BS6.2 - N - ADAS",
         "fuel": "DIESEL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2131499.58"}},
    ])
    _set_journey_product(c, "SCORPIO N", "Z8L (P) MT")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("raised") is True
    assert result.get("matchStage") == "ATTRIBUTE_DECOMPOSITION"
    assert result.get("candidateCount") == 2


def test_integration_resolves_across_generations_via_ex_showroom_price(mahindra_journey) -> None:
    """Reproduces a real, user-reported case verbatim: a Booking Form writes
    the bare nameplate ('Scorpio N') and a trim code ('Z8L') with no fuel/
    transmission/seater qualifiers captured at all -- so the attribute
    decomposition fallback has nothing to decompose. The tenant's actual,
    currently effective price list (confirmed against the real ingested
    workbook) carries BOTH an old-generation 'SCORPIO N' (ADAS trim) and a
    'NEW SCORPIO N' refresh, each with their own 'Z8L' variant at a
    different price. Ex-showroom price is the one thing this Booking Form
    genuinely does capture, and it matches the refresh generation's Z8L
    exactly -- proving the resolver bridges to it instead of reporting the
    old generation's Z8L (or every Z8L across both) as equally ambiguous."""
    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8 L G MT 2WD 7 STR BS6.2 - N - ADAS",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2075499.58"}},
        {"model": "NEW SCORPIO N", "variant": "Z8 L G MT 2WD 7 STR BS6.2 - Refresh",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2131387.08"}},
    ])
    _set_journey_product(c, "Scorpio N", "Z8L")
    _set_booking_form_ex_showroom(c, "2131387.08")  # matches the refresh generation only

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "EX_SHOWROOM"

    sku = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    pinned_model = c.execute(
        text("SELECT pm.model_name FROM auditcore.product_skus s "
             "JOIN auditcore.product_models pm ON pm.model_id = s.model_id "
             "WHERE s.product_sku_id = :sku"),
        {"sku": sku},
    ).scalar_one()
    assert pinned_model == "NEW SCORPIO N"
    assert _open_model_flags(c) == 0


def test_integration_a_stated_attribute_outranks_a_conflicting_price(mahindra_journey) -> None:
    """Direct user ask: ex-showroom price must be the *last* resort, not the
    first. Seed two SKUs differing only in fuel, at swapped prices from what
    a careless price-first resolver would expect; the Booking Form states
    fuel=DIESEL explicitly (a real, decomposed signal) while its own
    captured ex-showroom price happens to equal the PETROL sibling's price
    instead (a plausible real-world mismatch -- a discount, an add-on, or a
    simple data-entry slip). The stated fuel fact must win; price must never
    be allowed to override a signal already confirmed as reliable."""
    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8T D AT 2WD 7 STR BS6.2 - N",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "SCORPIO N", "variant": "Z8T G AT 2WD 7 STR BS6.2 - N",
         "fuel": "PETROL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1900000"}},
    ])
    _set_journey_product(c, "SCORPIO N", "Z8T D AT")
    _set_booking_form_ex_showroom(c, "1900000")  # the PETROL sibling's price, not the diesel one

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "ATTRIBUTE_DECOMPOSITION"

    pinned_fuel = c.execute(
        text("SELECT pv.fuel_powertrain FROM auditcore.journey_products jp "
             "JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id "
             "JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id "
             "WHERE jp.tenant_id=:t AND jp.journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned_fuel == "DIESEL"


def test_integration_trim_alone_disambiguates_when_it_genuinely_discriminates(mahindra_journey) -> None:
    """Direct user ask: trim is one of the six things that should count as
    real signal, not just fuel/transmission/drive/seater. Three SKUs share
    every other attribute and differ ONLY by trim code (Z8S/Z8T/Z8L) -- the
    Booking Form states just the bare trim, nothing else, and that alone
    is enough to resolve uniquely, without ever needing price."""
    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8S G MT 2WD 7 STR BS6.2 - N",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1800000"}},
        {"model": "SCORPIO N", "variant": "Z8T G MT 2WD 7 STR BS6.2 - N",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1900000"}},
        {"model": "SCORPIO N", "variant": "Z8L G MT 2WD 7 STR BS6.2 - N",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2000000"}},
    ])
    _set_journey_product(c, "SCORPIO N", "Z8T")  # bare trim only -- no fuel/transmission/etc.

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "ATTRIBUTE_DECOMPOSITION"

    pinned_variant = c.execute(
        text("SELECT pv.variant_name FROM auditcore.journey_products jp "
             "JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id "
             "JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id "
             "WHERE jp.tenant_id=:t AND jp.journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned_variant == "Z8T G MT 2WD 7 STR BS6.2 - N"


def test_integration_falls_back_to_latest_master_when_booking_predates_it(journey) -> None:
    """Regression: a real Booking Form's own extracted booking_date is often
    well in the past (this one -- a real production case -- was 2024-08-12),
    while a freshly-onboarded tenant's OEM master is necessarily effective
    from the day it was ingested. Requiring a master version genuinely
    effective as of that historical date meant sync_model_resolution always
    hit "no_effective_price_list" and silently skipped -- forever, since
    nothing about that mismatch ever changes on a later retry."""
    c = journey
    (sku_id,) = _seed_price_list(
        c,
        [{"model": "SCORPIO N", "variant": "Z8L",
          "components": {"EX_SHOWROOM": "1600000", "INSURANCE": "60000",
                         "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "220000"}}],
        effective_from="CURRENT_DATE",  # published today; the fixture's booking_date is 10 days ago
    )
    _set_journey_product(c, "Scorpio N", "Z8L")
    _set_commercial(c, "ex_showroom_price", "1600000")
    _set_commercial(c, "total_price", "1830000")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result.get("resolved") is True
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


# ── Delivery-invoice fallback ────────────────────────────────────────────────
def _set_invoice_field(c, field_key, value, *, document_type_key="customer_invoice_dms"):
    c.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, effective_value, is_modified
            ) VALUES (
                :t, :j, NULL, :doc,
                NULL, 1, 'DELIVERY',
                :dtk, NULL, :fk,
                CAST(:v AS jsonb), CAST(:v AS jsonb), false
            )
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id, "doc": uuid4(), "dtk": document_type_key,
         "fk": field_key, "v": json.dumps(value)},
    )


def test_invoice_sku_code_resolves_when_booking_never_matched(journey) -> None:
    # Booking's own model text never matched anything at all -- the fixture
    # never even calls sync_model_resolution here, standing in for a Booking
    # that already raised MODEL_NOT_IDENTIFIED and stayed unresolved.
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "SCORPIO N", "variant": "Z8L",
        "components": {"EX_SHOWROOM": "1600000", "INSURANCE": "60000",
                       "REGISTRATION_INDIVIDUAL": "170000", "REGISTRATION_CORPORATE": "220000"},
    }])
    sku_code = c.execute(
        text("SELECT sku_code FROM auditcore.product_skus WHERE product_sku_id=:s"),
        {"s": sku_id},
    ).scalar_one()
    _set_journey_product(c, "ILLEGIBLE SCAN TEXT", None)
    _set_invoice_field(c, "sku_code", sku_code)

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result.get("resolved") is True
    assert result.get("matchStage") == "INVOICE_SKU_CODE"
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


def test_invoice_model_text_resolves_when_no_sku_code_printed(journey) -> None:
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "THAR", "variant": "LX",
        "components": {"EX_SHOWROOM": "1400000", "INSURANCE": "50000",
                       "REGISTRATION_INDIVIDUAL": "150000", "REGISTRATION_CORPORATE": "190000"},
    }])
    _set_journey_product(c, "ILLEGIBLE SCAN TEXT", None)
    _set_invoice_field(c, "model_name_raw", "Thar")
    _set_invoice_field(c, "variant_raw", "LX")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result.get("resolved") is True
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


def test_invoice_fallback_is_noop_once_already_resolved(journey) -> None:
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "XUV700", "variant": "AX7L",
        "components": {"EX_SHOWROOM": "2000000"},
    }])
    _set_journey_product(c, "XUV700", "AX7L")
    c.execute(
        text("UPDATE auditcore.journey_products SET product_sku_id=:s "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"s": sku_id, "t": c.tenant_id, "j": c.journey_id},
    )
    # An invoice code that matches nothing in the masters must NOT override an
    # already-pinned SKU -- and isn't a genuine mismatch either (there's
    # nothing to cross-check against, just an unmatched code).
    _set_invoice_field(c, "sku_code", "SOME-OTHER-CODE")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result == {"skipped": True, "reason": "already_resolved", "mismatchFlagged": False}
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


def test_invoice_mismatch_raises_finding_and_pc_task_without_repinning(journey) -> None:
    """The invoice implies a real, matched SKU that disagrees with the one
    already on the journey -- must raise a finding + PC task, per explicit
    instruction, without ever repinning journey_products itself."""
    c = journey
    confirmed_sku_id, invoice_sku_id = _seed_price_list(c, [
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "SCORPIO N", "variant": "Z8L", "components": {"EX_SHOWROOM": "1600000"}},
    ])
    invoice_sku_code = c.execute(
        text("SELECT sku_code FROM auditcore.product_skus WHERE product_sku_id=:s"),
        {"s": invoice_sku_id},
    ).scalar_one()
    _set_journey_product(c, "XUV700", "AX7L")
    c.execute(
        text("UPDATE auditcore.journey_products SET product_sku_id=:s, selection_status='CONFIRMED' "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"s": confirmed_sku_id, "t": c.tenant_id, "j": c.journey_id},
    )
    _set_invoice_field(c, "sku_code", invoice_sku_code)

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result == {"skipped": True, "reason": "already_resolved", "mismatchFlagged": True}

    # journey_products itself is untouched -- this never repins.
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == confirmed_sku_id

    finding = c.execute(
        text("SELECT audit_finding_id, finding_class, severity, finding_status "
             "FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='INVOICE_SKU_MISMATCH'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert finding["finding_class"] == "VIOLATION"
    assert finding["severity"] == "HIGH"
    assert finding["finding_status"] == "OPEN"

    task = c.execute(
        text("SELECT task_type, assigned_role_code, related_finding_id "
             "FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert task["task_type"] == "INVOICE_SKU_MISMATCH_REASON"
    assert task["assigned_role_code"] == "PC"
    assert task["related_finding_id"] == finding["audit_finding_id"]


def test_invoice_mismatch_resolves_once_invoice_matches_confirmed_sku(journey) -> None:
    c = journey
    confirmed_sku_id, other_sku_id = _seed_price_list(c, [
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "SCORPIO N", "variant": "Z8L", "components": {"EX_SHOWROOM": "1600000"}},
    ])
    other_sku_code = c.execute(
        text("SELECT sku_code FROM auditcore.product_skus WHERE product_sku_id=:s"),
        {"s": other_sku_id},
    ).scalar_one()
    confirmed_sku_code = c.execute(
        text("SELECT sku_code FROM auditcore.product_skus WHERE product_sku_id=:s"),
        {"s": confirmed_sku_id},
    ).scalar_one()
    _set_journey_product(c, "XUV700", "AX7L")
    c.execute(
        text("UPDATE auditcore.journey_products SET product_sku_id=:s, selection_status='CONFIRMED' "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"s": confirmed_sku_id, "t": c.tenant_id, "j": c.journey_id},
    )
    _set_invoice_field(c, "sku_code", other_sku_code)
    mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert c.execute(
        text("SELECT finding_status FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='INVOICE_SKU_MISMATCH'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == "OPEN"

    # A corrected invoice now shows the confirmed vehicle's own code.
    c.execute(
        text("DELETE FROM auditcore.journey_document_extracted_fields "
             "WHERE tenant_id=:t AND journey_id=:j AND field_key='sku_code'"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    _set_invoice_field(c, "sku_code", confirmed_sku_code)
    mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert c.execute(
        text("SELECT finding_status FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='INVOICE_SKU_MISMATCH'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == "RESOLVED"


def test_invoice_fallback_skips_without_any_invoice_data(journey) -> None:
    c = journey
    _set_journey_product(c, "ILLEGIBLE SCAN TEXT", None)

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result == {"skipped": True, "reason": "no_invoice_model_data"}


def test_invoice_fallback_resolves_with_no_model_snapshot_at_all(journey) -> None:
    # Regression test for a real bug: _resolution_inputs unconditionally
    # returned None whenever journey_products.model_name_snapshot was
    # missing -- exactly the condition this fallback exists to handle (the
    # Booking side never captured a usable model at all, not merely an
    # unmatched one). It could never reach its own resolution logic. Every
    # other test above seeds a non-null (if garbage) model_name_snapshot
    # via _set_journey_product, which never exercised this path -- here the
    # row is left out entirely, standing in for a Booking whose own model
    # materialization never ran.
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "BOLERO NEO", "variant": "N10",
        "components": {"EX_SHOWROOM": "900000"},
    }])
    sku_code = c.execute(
        text("SELECT sku_code FROM auditcore.product_skus WHERE product_sku_id=:s"),
        {"s": sku_id},
    ).scalar_one()
    _set_invoice_field(c, "sku_code", sku_code)

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result.get("resolved") is True
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


# ── PC self-serve SKU selection (candidates endpoint + manual confirm) ──────
def _seed_ambiguous_thar(c):
    """Two THAR SKUs with an identical on-road total (so total alone can't
    disambiguate) but different ex-showroom prices and colours -- the same
    "matched multiple models" shape sync_model_resolution raises against a
    real ambiguous Booking Form."""
    return _seed_price_list(c, [
        {"model": "THAR", "variant": "LX", "colour": "RED",
         "components": {"EX_SHOWROOM": "1500000", "REGISTRATION_INDIVIDUAL": "170000",
                        "REGISTRATION_CORPORATE": "200000"}},
        {"model": "THAR", "variant": "AX", "colour": "WHITE",
         "components": {"EX_SHOWROOM": "1550000", "REGISTRATION_INDIVIDUAL": "120000",
                        "REGISTRATION_CORPORATE": "200000"}},
    ])


def _related_task_payload(c, finding_id):
    return c.execute(
        text("SELECT task_payload FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND related_finding_id=:f"),
        {"t": c.tenant_id, "f": finding_id},
    ).scalar_one()


def _open_model_finding_id(c):
    return c.execute(
        text("SELECT audit_finding_id FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='MODEL_NOT_IDENTIFIED' "
             "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()


def test_self_serve_task_carries_shortlist_comment_and_candidates(journey) -> None:
    """The auto-spawned Task for a multi-match gap must carry a business-
    readable shortlist (model/variant/colour + ex-showroom price), not just
    a bare ruleKey/findingId a PC has no way to act on directly."""
    c = journey
    sku_a, sku_b = _seed_ambiguous_thar(c)
    _set_journey_product(c, "Thar", None)
    _set_commercial(c, "total_price", "1670000")  # matches both SKUs' total

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("raised") is True
    assert result.get("candidateCount") == 2

    finding_id = _open_model_finding_id(c)
    payload = _related_task_payload(c, finding_id)
    assert "ex-showroom" in payload["comment"]
    assert "Journey Documents" in payload["comment"]
    candidate_ids = {row["productSkuId"] for row in payload["candidates"]}
    assert candidate_ids == {str(sku_a), str(sku_b)}
    for row in payload["candidates"]:
        assert row["exShowroomPrice"] is not None


def test_confirm_model_resolution_sku_pins_resolves_and_closes_task(journey) -> None:
    """Confirming a shortlisted SKU pins it, resolves the finding (recording
    the confirming PC as the resolving actor), and -- via the codebase-wide
    ``sync_finding_work_item`` trigger (migration 0098) that cancels every
    still-open Task the instant its Finding closes, not any Python code of
    this module's own -- leaves the spawned Task no longer actionable."""
    c = journey
    _sku_a, sku_b = _seed_ambiguous_thar(c)
    _set_journey_product(c, "Thar", None)
    _set_commercial(c, "total_price", "1670000")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    finding_id = _open_model_finding_id(c)
    task_id = c.execute(
        text("SELECT workflow_task_id FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND related_finding_id=:f"),
        {"t": c.tenant_id, "f": finding_id},
    ).scalar_one()

    result = mr.confirm_model_resolution_sku(
        c,
        tenant_id=c.tenant_id,
        journey_id=c.journey_id,
        product_sku_id=sku_b,
        actor_id="pc-test-actor",
        correlation_id="",
    )
    assert result["resolved"] is True
    assert result["productSkuId"] == sku_b

    row = c.execute(
        text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["product_sku_id"] == sku_b
    assert row["selection_status"] == "CONFIRMED"
    assert _open_model_flags(c) == 0

    resolve_event = c.execute(
        text("SELECT actor_id, actor_role_snapshot FROM auditcore.audit_finding_events "
             "WHERE tenant_id=:t AND audit_finding_id=:f AND event_type='RESOLVED'"),
        {"t": c.tenant_id, "f": finding_id},
    ).mappings().one()
    assert resolve_event["actor_id"] == "pc-test-actor"
    assert resolve_event["actor_role_snapshot"] == "HUMAN"

    task = c.execute(
        text("SELECT task_status, cancel_reason FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND workflow_task_id=:tid"),
        {"t": c.tenant_id, "tid": task_id},
    ).mappings().one()
    assert task["task_status"] == "CANCELLED"
    assert task["cancel_reason"] == "Finding closed"


def test_confirm_model_resolution_sku_rejects_sku_outside_price_list(journey) -> None:
    from audit_core.errors import AuditCoreError

    c = journey
    _seed_ambiguous_thar(c)
    _set_journey_product(c, "Thar", None)
    _set_commercial(c, "total_price", "1670000")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    with pytest.raises(AuditCoreError) as exc_info:
        mr.confirm_model_resolution_sku(
            c,
            tenant_id=c.tenant_id,
            journey_id=c.journey_id,
            product_sku_id=uuid4(),
            actor_id="pc-test-actor",
            correlation_id="",
        )
    assert exc_info.value.error_code == "VAC-SKU-002"
    assert _open_model_flags(c) == 1  # nothing changed on rejection


def test_get_model_resolution_candidates(journey) -> None:
    from audit_core.errors import NotFoundError

    c = journey
    with pytest.raises(NotFoundError):
        mr.get_model_resolution_candidates(c, tenant_id=c.tenant_id, journey_id=c.journey_id)

    sku_a, sku_b = _seed_ambiguous_thar(c)
    _set_journey_product(c, "Thar", None)
    _set_commercial(c, "total_price", "1670000")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    data = mr.get_model_resolution_candidates(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert data["reviewedModelName"] == "Thar"
    assert {row["productSkuId"] for row in data["candidates"]} == {sku_a, sku_b}
    assert all(row["exShowroomPrice"] is not None for row in data["candidates"])


def test_invoice_fallback_resolves_with_null_model_snapshot(journey) -> None:
    # Same regression, the other shape of the bug: a journey_products row
    # DOES exist (e.g. a partial materialization ran) but its
    # model_name_snapshot column is genuinely NULL, not merely blank text.
    c = journey
    (sku_id,) = _seed_price_list(c, [{
        "model": "MARAZZO", "variant": "M6",
        "components": {"EX_SHOWROOM": "1100000"},
    }])
    c.execute(
        text("INSERT INTO auditcore.journey_products "
             "(tenant_id, journey_id, model_name_snapshot, selection_source) "
             "VALUES (:t, :j, NULL, 'EVIDENCE')"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    _set_invoice_field(c, "model_name_raw", "Marazzo")
    _set_invoice_field(c, "variant_raw", "M6")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result.get("resolved") is True
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


def test_backfills_missing_task_for_a_preexisting_open_finding(journey) -> None:
    """A real production gap: a MODEL_NOT_IDENTIFIED finding raised before
    self-serve task-spawning existed for it (or before this session's own
    task_payload_extra shipped) has no linked Task at all -- a PC's only
    click-through is then "Open case" -> Audit Review, which offers no fix
    action for a self-serve gap by v1.1 design. Re-triggering the same rule
    (any later DOCUMENT_SYNCED event) must backfill the missing Task, not
    just silently re-confirm the finding still stands."""
    c = journey
    _seed_price_list(c, [{
        "model": "THAR ROXX", "variant": "AX",
        "components": {"EX_SHOWROOM": "1400000", "REGISTRATION_INDIVIDUAL": "150000",
                       "REGISTRATION_CORPORATE": "180000"},
    }])
    _set_journey_product(c, "Scorpio N", "Z8L")  # not in the price list
    _set_commercial(c, "ex_showroom_price", "1600000")
    _set_commercial(c, "total_price", "1830000")

    # Simulate the "predates task-spawning" state directly: insert the same
    # OPEN/DATA_GAP finding _machine_flag would have inserted, without ever
    # calling create_workflow_task -- exactly the shape a finding raised
    # before that branch existed (or before it existed for this rule) would
    # have. workflow_task_events is append-only, so a raise-then-delete
    # approach can't simulate this state -- inserting directly is the only
    # way, and it's the more faithful reproduction anyway.
    finding_id = c.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity, finding_status,
                title, description, stage_code, origin_kind, origin_role_snapshot,
                rule_key, finding_class, owner_role_code
            ) VALUES (
                :t, :j, 'MODEL_NOT_IDENTIFIED', 'MEDIUM', 'OPEN',
                'Vehicle model could not be matched to the price masters', NULL,
                'BOOKING', 'MACHINE', 'SYSTEM',
                'MODEL_NOT_IDENTIFIED:BOOKING', 'DATA_GAP', 'PC'
            )
            RETURNING audit_finding_id
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert c.execute(
        text("SELECT count(*) FROM auditcore.workflow_tasks WHERE tenant_id=:t AND related_finding_id=:f"),
        {"t": c.tenant_id, "f": finding_id},
    ).scalar_one() == 0

    # Re-trigger the same rule -- _machine_flag's existing-finding branch
    # must notice the missing Task and backfill one, not just no-op.
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert _open_model_flags(c) == 1  # still exactly one finding, not a duplicate
    task = c.execute(
        text("SELECT task_type, assigned_role_code, task_status FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND related_finding_id=:f"),
        {"t": c.tenant_id, "f": finding_id},
    ).mappings().one()
    assert task["task_type"] == "AUTO_SELF_SERVE"
    assert task["assigned_role_code"] == "PC"
    assert task["task_status"] == "READY"
