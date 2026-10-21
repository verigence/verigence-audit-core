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



# 2026-10-20 product owner rule R3: the price-picking matcher (_match,
# _price_disambiguate, the "New X" generation price bridge, _price_pick) is
# gone; its unit tests encoded price choosing the variant and were removed.
# Word identification is tested in test_uc03_sku_words.py.


# ── unit: catalogue search once the model name itself never resolved ─────────
def _ev_rows():
    return [
        _row("AX5L-EV", "XUV 3XO EV", variant="AX5L", ex="1500000") | {"fuel_powertrain": "ELECTRIC", "trim": "AX5L"},
        _row("AX7L-EV", "XUV 3XO EV", variant="AX7L", ex="1700000") | {"fuel_powertrain": "ELECTRIC", "trim": "AX7L"},
        _row("AX7L-P", "XUV 3XO", variant="AX7L PET MT", ex="1200000") | {"fuel_powertrain": "PETROL", "trim": "AX7L"},
        _row("Z8L", "SCORPIO N", variant="Z8L D AT", ex="2000000") | {"fuel_powertrain": "DIESEL", "trim": "Z8L"},
    ]


def _reading(model, variant=None, ex=None):
    return {"model": model, "variant": variant, "exShowroom": Decimal(ex) if ex is not None else None}


def test_catalogue_search_shortlists_the_misread_nameplate() -> None:
    """The real DEV case: handwritten "XUV3XO AX7L EV" extracted as "XUV300" /
    "W7L EV". The stated EV excludes the petrol twin and the folded nameplate
    keeps both electric trims: the PC gets exactly those two to pick from."""
    matched = mr._catalogue_search(_ev_rows(), _reading("XUV300", "W7L EV"), oem_code="MAHINDRA")
    assert sorted(r["sku_code"] for r in matched) == ["AX5L-EV", "AX7L-EV"]


def test_catalogue_search_never_pins_on_an_exact_price() -> None:
    """2026-10-20 rule R3 (this used to pin AX7L-EV on its price): the
    price only puts the nearest row first."""
    matched = mr._catalogue_search(_ev_rows(), _reading("XUV300", "W7L EV", ex="1700000"), oem_code="MAHINDRA")
    assert [r["sku_code"] for r in matched] == ["AX7L-EV", "AX5L-EV"]


def test_catalogue_search_puts_the_stated_trim_first() -> None:
    matched = mr._catalogue_search(_ev_rows(), _reading("XUV300", "AX7L EV"), oem_code="MAHINDRA")
    assert matched[0]["sku_code"] == "AX7L-EV"


def test_catalogue_search_finds_nothing_for_an_unrelated_nameplate() -> None:
    rows = [_row("AX", "THAR ROXX", variant="AX", ex="1400000") | {"fuel_powertrain": "DIESEL", "trim": "AX"}]
    assert mr._catalogue_search(rows, _reading("Scorpio N", "Z8L"), oem_code="MAHINDRA") == []


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
    # 2026-10-20 product owner rule R3: an automatic match is not
    # "Confirmed" (only a PC / TL decision is).
    assert sku["selection_status"] == "TENTATIVE"
    assert _vehicle(c)["match_basis"] == "AUTO_MATCHED"
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


def test_integration_get_model_catalog_leaves_trim_blank_rather_than_showing_the_matching_key(
    mahindra_journey,
) -> None:
    """Regression: a first version of the blank-column fallback above also
    fell back to _master_trim_key for trim -- a glued, uppercased MATCHING
    key built from residue text, e.g. 'Classic S11 BS6.2 - E' residues to
    'CLASSICS11E' (it still includes the model-name fragment 'CLASSIC').
    Confirmed live: that string was shown as a pickable trim option in the
    Modify Model dropdown, actively misleading a PC rather than merely
    blank. Unlike fuel/transmission/drive/seater, there is no trustworthy
    fallback for trim -- when the master's own raw trim column is blank,
    the catalog must leave it blank too."""
    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "SCORPIO CLASSIC", "variant": "Classic S11 BS6.2 - E",
         "components": {"EX_SHOWROOM": "1400000"}},
    ])

    catalog = mr.get_model_catalog(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert len(catalog["skus"]) == 1
    assert catalog["skus"][0]["trim"] is None


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


def test_integration_a_model_only_booking_is_never_settled_by_price(journey) -> None:
    """2026-10-20 product owner rule R3 (this test used to expect the
    booking's ex-showroom to pick the variant): the words name the model
    only, so the PC picks; the price only orders the shortlist (the AX,
    whose master price equals the booking's, comes first)."""
    c = journey
    lx, ax = _seed_price_list(c, [
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
    assert result.get("raised") is True
    assert result.get("matchStage") == "WORDS_AMBIGUOUS"
    assert result.get("candidateCount") == 2
    assert _vehicle(c)["product_sku_id"] is None
    assert _open_model_flags(c) == 1
    shortlist = mr.get_model_resolution_candidates(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert [x["productSkuId"] for x in shortlist["candidates"]] == [ax, lx]


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
    ])
    _set_journey_product(c, "SCORPIO N Z8 (S)", "DAT 2WD 7STR")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("resolved") is True
    assert result.get("matchStage") == "WORDS"

    row = c.execute(
        text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["product_sku_id"] == z8s_diesel_at
    assert row["selection_status"] == "TENTATIVE"
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
    assert row["selection_status"] == "TENTATIVE"
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
    assert result.get("matchStage") == "WORDS"

    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == ax7l_2wd
    assert _open_model_flags(c) == 0


def test_integration_fuzzy_resolves_a_mid_string_ocr_misread_trim(mahindra_journey) -> None:
    """Exact attribute decomposition finds nothing at all: the Booking
    Form's trim was OCR'd as "Z9S" (an "8" misread as "9"), which is
    neither a prefix nor a suffix of either real master trim. The
    controlled fuzzy fallback (uc03_model_attribute_matching.
    fuzzy_match_by_attributes) still resolves it unambiguously -- Z9S is
    genuinely, measurably closer to Z8S than to Z8T, and every other
    stated fact (Diesel, AT, 2WD, 7-seater) corroborates it."""
    c = mahindra_journey
    z8s, *_ = _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8 S D AT 2WD 7 STR BS6.2 - N",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1750000"}},
        {"model": "SCORPIO N", "variant": "Z8T D AT 2WD 7 STR BS6.2 - N",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "1780000"}},
    ])
    _set_journey_product(c, "SCORPIO N Z9S", "DAT 2WD 7STR")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    # A fuzzy reading is the closest match, not the vehicle named: it is
    # suggested to the PC to accept or reject (decision 2026-10-20).
    assert result.get("raised") is True
    assert result.get("matchStage") == "WORDS_UNRECOGNISED"
    assert _vehicle(c)["product_sku_id"] is None
    assert _sku_task(c, "SKU_CLOSEST_MATCH_CONFIRM")["reference"]["suggestedSkuId"] == str(z8s)


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
    assert result.get("matchStage") == "WORDS_AMBIGUOUS"
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
    assert result.get("matchStage") == "WORDS_AMBIGUOUS"
    assert result.get("candidateCount") == 2


def test_integration_the_spelling_names_the_generation_never_the_price(mahindra_journey) -> None:
    """2026-10-20 product owner rule R3 (this test used to expect the
    booking's ex-showroom to bridge "Scorpio N" to the refresh generation):
    with the price masters' model identity, "Scorpio N" names the old
    SCORPIO N while it is on sale and "SCORPIO-N" / "New Scorpio N" the
    refresh. The booking's price (the refresh's) does not move it: the old
    Z8L is pinned as a price mismatch and the PC is asked."""
    c = mahindra_journey
    old, new = _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8 L G MT 2WD 7 STR BS6.2 - N - ADAS",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2075499.58"}},
        {"model": "NEW SCORPIO N", "variant": "Z8 L G MT 2WD 7 STR BS6.2 - Refresh",
         "fuel": "PETROL", "transmission": "MT", "drive": "2WD", "seater": "7",
         "components": {"EX_SHOWROOM": "2131387.08"}},
    ])
    _set_journey_product(c, "Scorpio N", "Z8L")
    _set_booking_form_ex_showroom(c, "2131387.08")  # the refresh generation's price

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("basis") == "AUTO_PRICE_MISMATCH"
    assert _vehicle(c)["product_sku_id"] == old and old != new


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
    # The words name the diesel; the price differs from its master price, so
    # it is pinned as a price mismatch and the PC is asked to set the SKU.
    assert result.get("resolved") is True
    assert result.get("matchStage") == "WORDS"
    assert result.get("basis") == "AUTO_PRICE_MISMATCH"
    assert _sku_task(c, "SKU_PRICE_MISMATCH")["task_status"] not in ("VERIFIED_COMPLETE", "CANCELLED")

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
    assert result.get("matchStage") == "WORDS"

    pinned_variant = c.execute(
        text("SELECT pv.variant_name FROM auditcore.journey_products jp "
             "JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id "
             "JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id "
             "WHERE jp.tenant_id=:t AND jp.journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned_variant == "Z8T G MT 2WD 7 STR BS6.2 - N"


def test_integration_catalogue_search_lists_the_closest_skus_when_the_model_was_misread(mahindra_journey) -> None:
    c = mahindra_journey
    ax5l, ax7l, *_ = _seed_price_list(c, [
        {"model": "XUV 3XO EV", "variant": "AX5L", "fuel": "ELECTRIC", "trim": "AX5L",
         "components": {"EX_SHOWROOM": "1500000"}},
        {"model": "XUV 3XO EV", "variant": "AX7L", "fuel": "ELECTRIC", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "1700000"}},
        {"model": "XUV 3XO", "variant": "AX7L PET MT", "fuel": "PETROL", "transmission": "MT", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "1200000"}},
    ])
    _set_journey_product(c, "XUV300", "W7L EV")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("raised") is True
    assert result.get("matchStage") == "CATALOGUE_SEARCH"
    assert _open_model_flags(c) == 1

    shortlist = mr.get_model_resolution_candidates(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert shortlist["matchStage"] == "CATALOGUE_SEARCH"
    assert sorted(str(item["productSkuId"]) for item in shortlist["candidates"]) == sorted([str(ax5l), str(ax7l)])


def _sku_task(c, task_type):
    return c.execute(
        text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_type=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": task_type},
    ).mappings().one_or_none()


def test_integration_catalogue_search_suggests_and_the_pc_accepts(mahindra_journey) -> None:
    """Decision 2026-10-20: the closest price-master SKU is never pinned on its
    own -- the PC gets a task to accept or reject it."""
    from audit_core.uc03_sku_tasks import handle_sku_task_action

    c = mahindra_journey
    _, ax7l, *_ = _seed_price_list(c, [
        {"model": "XUV 3XO EV", "variant": "AX5L", "fuel": "ELECTRIC", "trim": "AX5L",
         "components": {"EX_SHOWROOM": "1500000"}},
        {"model": "XUV 3XO EV", "variant": "AX7L", "fuel": "ELECTRIC", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "1700000"}},
    ])
    _set_journey_product(c, "XUV300", "W7L EV")
    _set_commercial(c, "ex_showroom_price", "1700000")

    result = mr.sync_model_resolution(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("raised") is True and result.get("matchStage") == "CATALOGUE_SEARCH"
    assert _vehicle(c)["product_sku_id"] is None
    task = _sku_task(c, "SKU_CLOSEST_MATCH_CONFIRM")
    assert task["assigned_role_code"] == "PC" and task["task_status"] == "READY"
    assert task["reference"]["suggestedSkuId"] == str(ax7l)

    outcome = handle_sku_task_action(
        c, tenant_id=c.tenant_id, task=dict(task), action="ACCEPT", actor_id="pc-1", actor_role_code="PC",
        comment=None,
    )
    assert outcome["outcome"] == "SKU_ACCEPTED"
    assert _vehicle(c)["product_sku_id"] == ax7l
    assert _open_model_flags(c) == 0
    assert _sku_task(c, "SKU_CLOSEST_MATCH_CONFIRM")["task_status"] == "VERIFIED_COMPLETE"


def test_rejecting_the_closest_match_leaves_the_pc_to_pick_and_never_resuggests_it(mahindra_journey) -> None:
    from audit_core.uc03_sku_tasks import handle_sku_task_action

    c = mahindra_journey
    _seed_price_list(c, [
        {"model": "XUV 3XO EV", "variant": "AX5L", "fuel": "ELECTRIC", "trim": "AX5L",
         "components": {"EX_SHOWROOM": "1500000"}},
        {"model": "XUV 3XO EV", "variant": "AX7L", "fuel": "ELECTRIC", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "1700000"}},
    ])
    _set_journey_product(c, "XUV300", "W7L EV")
    _set_commercial(c, "ex_showroom_price", "1700000")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    task = _sku_task(c, "SKU_CLOSEST_MATCH_CONFIRM")
    outcome = handle_sku_task_action(
        c, tenant_id=c.tenant_id, task=dict(task), action="REJECT", actor_id="pc-1", actor_role_code="PC",
        comment="Not this one",
    )
    assert outcome["outcome"] == "SUGGESTION_REJECTED"
    assert _vehicle(c)["product_sku_id"] is None
    assert _open_model_flags(c) == 1  # still to pick by hand

    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _sku_task(c, "SKU_CLOSEST_MATCH_CONFIRM")["task_status"] == "VERIFIED_COMPLETE"


def test_a_booking_before_every_master_is_not_priced_on_the_newest_one(journey) -> None:
    """2026-10-20 product owner rules (R5) reverse the earlier fallback that
    took the newest published master "regardless of its effective window"
    when the booking predated every master: that priced an August booking on
    the October book without a word. Now nothing is resolved against a book
    that was not in force, and the reason is given."""
    c = journey
    _seed_price_list(
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

    assert result == {"skipped": True, "reason": "no_effective_price_list"}
    assert _vehicle(c)["product_sku_id"] is None


# ── Delivery-invoice fallback ────────────────────────────────────────────────
def _set_invoice_field(c, field_key, value, *, document_type_key="customer_invoice_dms", stage_code="DELIVERY",
                       doc=None):
    """One field of one vehicle invoice. Every field of a test's invoice
    lands on the same document (rule R2, 2026-10-20: an invoice is read on
    its own; this helper used to scatter one invoice over several
    documents, which the resolver then mixed)."""
    if doc is None:
        doc = getattr(c, "invoice_doc", None) or uuid4()
        c.invoice_doc = doc  # type: ignore[attr-defined]
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
                NULL, 1, :stage,
                :dtk, NULL, :fk,
                CAST(:v AS jsonb), CAST(:v AS jsonb), false
            )
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id, "doc": doc, "dtk": document_type_key,
         "fk": field_key, "v": json.dumps(value), "stage": stage_code},
    )


def _set_invoice_price(c, ex_showroom, *, rate=18, discount=None, **kwargs):
    """The invoice's printed amounts for an ex-showroom (rule R1): taxable
    + CGST + SGST at the printed rate; with a discount, the gross selling
    price before it is printed too."""
    ex = Decimal(str(ex_showroom))
    selling = (ex / (1 + Decimal(rate) / 100)).quantize(Decimal("0.01"))
    taxable = selling - Decimal(str(discount or 0))
    half = (taxable * Decimal(rate) / 200).quantize(Decimal("0.01"))
    if discount:
        _set_invoice_field(c, "gross_amount_before_discount", str(selling), **kwargs)
        _set_invoice_field(c, "invoice_discount_amount", str(discount), **kwargs)
    _set_invoice_field(c, "taxable_amount", str(taxable), **kwargs)
    _set_invoice_field(c, "cgst_rate", str(rate / 2), **kwargs)
    _set_invoice_field(c, "sgst_rate", str(rate / 2), **kwargs)
    _set_invoice_field(c, "cgst_amount", str(half), **kwargs)
    _set_invoice_field(c, "sgst_amount", str(half), **kwargs)


def _make_automatic(c, sku_id) -> None:
    c.execute(
        text("UPDATE auditcore.journey_products SET product_sku_id=:s, selection_status='TENTATIVE', "
             "match_basis='AUTO_MATCHED' WHERE tenant_id=:t AND journey_id=:j"),
        {"s": sku_id, "t": c.tenant_id, "j": c.journey_id},
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
    _set_invoice_price(c, "1600000")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    # The printed SKU code names it and the invoice's ex-showroom (R1)
    # equals its master price: the invoice sets the SKU.
    assert result.get("resolved") is True
    assert result.get("matchStage") == "INVOICE_WORDS_AND_PRICE"
    assert result.get("basis") == "AUTO_INVOICE_MATCHED"
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

    # The invoice prints no price: its words still name the SKU, which goes
    # on the Journey as a price mismatch for the PC to set (2026-10-20).
    assert result.get("resolved") is True
    assert result.get("basis") == "AUTO_PRICE_MISMATCH"
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id
    assert _sku_task(c, "SKU_PRICE_MISMATCH") is not None


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

    assert result.get("resolved") is True and result.get("mismatchFlagged") is False
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


def test_invoice_naming_a_different_vehicle_replaces_the_sku_and_tells_the_tl(journey) -> None:
    """Rule R3 (2026-10-20): an invoice whose words name one SKU and whose
    ex-showroom (R1) equals its master price sets the SKU. It replaces an
    automatic match, and a Team Lead is told the model changed."""
    c = journey
    automatic_sku_id, invoice_sku_id = _seed_price_list(c, [
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "SCORPIO N", "variant": "Z8L", "components": {"EX_SHOWROOM": "1600000"}},
    ])
    invoice_sku_code = c.execute(
        text("SELECT sku_code FROM auditcore.product_skus WHERE product_sku_id=:s"),
        {"s": invoice_sku_id},
    ).scalar_one()
    _set_journey_product(c, "XUV700", "AX7L")
    _make_automatic(c, automatic_sku_id)
    _set_invoice_field(c, "sku_code", invoice_sku_code)
    _set_invoice_price(c, "1600000")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("changed") is True and result.get("source") == "INVOICE"
    row = _vehicle(c)
    assert row["product_sku_id"] == invoice_sku_id
    assert (row["model_name_snapshot"], row["variant_name_snapshot"]) == ("SCORPIO N", "Z8L")
    assert row["selection_method"] == "MODEL_RESOLUTION_INVOICE_V2"
    assert row["match_basis"] == "AUTO_INVOICE_MATCHED" and row["selection_status"] == "TENTATIVE"
    assert row["match_evidence"]["documentExShowroom"] == "1600000.00"
    assert row["match_evidence"]["priceCheck"] == "MATCH"
    task = _sku_task(c, "MODEL_CHANGED_BY_INVOICE")
    assert task["assigned_role_code"] == "TL" and task["severity"] == "INFO"
    assert "XUV700 AX7L" in task["description"] and "SCORPIO N Z8L" in task["description"]

    # Run again on the same invoice: nothing changes twice.
    again = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert again.get("changed") is False and _vehicle(c)["product_sku_id"] == invoice_sku_id


def test_an_invoice_never_overwrites_a_sku_the_pc_confirmed_the_tl_decides(journey) -> None:
    """A SKU a PC confirmed is a human decision: the invoice naming another
    SKU at its master price raises a Team Lead task instead of overwriting
    it; the TL's Accept applies the invoice's SKU as TL_APPROVED."""
    from audit_core.uc03_sku_tasks import handle_sku_task_action

    c = journey
    confirmed_sku_id, invoice_sku_id = _seed_price_list(c, [
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "SCORPIO N", "variant": "Z8L", "components": {"EX_SHOWROOM": "1600000"}},
    ])
    _set_journey_product(c, "XUV700", "AX7L")
    _confirm_sku(c, confirmed_sku_id)
    _set_invoice_field(c, "model_name_raw", "SCORPIO N")
    _set_invoice_field(c, "variant_raw", "Z8L")
    _set_invoice_price(c, "1600000")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("mismatchFlagged") is True
    assert _vehicle(c)["product_sku_id"] == confirmed_sku_id
    task = _sku_task(c, "SKU_INVOICE_REVIEW")
    assert task["assigned_role_code"] == "TL"

    handle_sku_task_action(c, tenant_id=c.tenant_id, task=dict(task), action="ACCEPT", actor_id="tl-1",
                           actor_role_code="TL", comment=None)
    row = _vehicle(c)
    assert row["product_sku_id"] == invoice_sku_id
    assert (row["selection_status"], row["match_basis"]) == ("CONFIRMED", "TL_APPROVED")


def test_booking_side_checks_also_apply_the_invoice_over_the_sku(journey) -> None:
    """The Booking checks (and the nightly retry) apply the same rule, not
    only Delivery-side events."""
    c = journey
    automatic_sku_id, invoice_sku_id = _seed_price_list(c, [
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "SCORPIO N", "variant": "Z8L", "components": {"EX_SHOWROOM": "1600000"}},
    ])
    _set_journey_product(c, "XUV700", "AX7L")
    _make_automatic(c, automatic_sku_id)
    _set_invoice_field(c, "model_name_raw", "SCORPIO N")
    _set_invoice_field(c, "variant_raw", "Z8L")
    _set_invoice_price(c, "1600000")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("changed") is True
    assert _vehicle(c)["product_sku_id"] == invoice_sku_id


def _confirm_sku(c, sku_id) -> None:
    c.execute(
        text("UPDATE auditcore.journey_products SET product_sku_id=:s, selection_status='CONFIRMED' "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"s": sku_id, "t": c.tenant_id, "j": c.journey_id},
    )


def _open_mismatch(c) -> int:
    return c.execute(
        text("SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j "
             "AND rule_key='INVOICE_SKU_MISMATCH' AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()


def test_invoice_cross_check_reads_a_differently_spelled_invoice_through_the_full_ladder(mahindra_journey) -> None:
    """Rule 2026-10-01: the invoice's vehicle is checked even when the Booking
    Form already pinned one (and since 2026-10-20 it wins). The invoice spells the vehicle the way real
    DMS invoices do, not the way the master does; the plain name match
    used to find nothing and the check was silently skipped."""
    c = mahindra_journey
    ax7l, ax7t = _seed_price_list(c, [
        {"model": "XUV 7XO", "variant": "AX7L DSL AT 7 STR",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "XUV 7XO", "variant": "AX7T DSL AT 7 STR",
         "fuel": "DIESEL", "transmission": "AT", "drive": "2WD", "seater": "7", "trim": "AX7T",
         "components": {"EX_SHOWROOM": "2050000"}},
    ])
    _set_journey_product(c, "XUV 7XO", "AX7T DSL AT 7 STR")
    _make_automatic(c, ax7t)
    _set_invoice_field(c, "model_name_raw", "XUV-7XO")
    _set_invoice_field(c, "variant_raw", "AX-7L(D) AT 2WD 7STR")
    _set_invoice_price(c, "2000000", discount="25000")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    # The invoice is the source of truth (decision 2026-10-20): the Journey
    # takes the invoiced AX7L and the TL is told.
    assert result.get("changed") is True
    assert _open_mismatch(c) == 0
    assert _vehicle(c)["product_sku_id"] == ax7l
    assert ax7l != ax7t


def test_invoice_cross_check_agrees_when_the_confirmed_sku_is_one_of_the_invoice_matches(journey) -> None:
    c = journey
    first, _second = _seed_price_list(c, [
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
    ])
    _set_journey_product(c, "XUV700", "AX7L")
    _confirm_sku(c, first)
    _set_invoice_field(c, "model_name_raw", "XUV700")
    _set_invoice_field(c, "variant_raw", "AX7L")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    # Two identical rows: the invoice's words name no single SKU, so it sets
    # nothing; the confirmed SKU stays and nothing is flagged.
    assert result.get("resolved") is True and result.get("crossChecked") is True
    assert result.get("mismatchFlagged") is False
    assert _vehicle(c)["product_sku_id"] == first
    assert _open_mismatch(c) == 0


def test_invoice_with_a_different_price_sets_nothing_and_asks_the_pc(journey) -> None:
    """Rule R3: the invoice's words name one SKU but its ex-showroom (R1)
    is not that SKU's master price: it does not set the SKU; the PC gets a
    price task. The automatic booking match stays."""
    c = journey
    z8l, ax7l = _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8L", "components": {"EX_SHOWROOM": "1600000"}},
        {"model": "XUV700", "variant": "AX7L", "components": {"EX_SHOWROOM": "2000000"}},
    ])
    _set_journey_product(c, "SCORPIO N", "Z8L")
    _make_automatic(c, z8l)
    _set_invoice_field(c, "model_name_raw", "XUV700")
    _set_invoice_field(c, "variant_raw", "AX7L")
    _set_invoice_price(c, "1990000")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("mismatchFlagged") is True
    assert _vehicle(c)["product_sku_id"] == z8l
    task = _sku_task(c, "SKU_PRICE_MISMATCH")
    assert task["assigned_role_code"] == "PC"
    assert task["reference"]["skuId"] == str(ax7l)
    assert task["reference"]["documentExShowroom"] == "1990000.00"


def test_invoice_fallback_skips_without_any_model_read(journey) -> None:
    c = journey
    _seed_price_list(c, [{"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}}])
    _set_journey_product(c, None, None)

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result == {"skipped": True, "reason": "no_vehicle_model_read"}


def _vehicle(c):
    return c.execute(
        text("SELECT product_sku_id, model_name_snapshot, variant_name_snapshot, selection_method, "
             "selection_status, match_basis, match_evidence "
             "FROM auditcore.journey_products WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one_or_none()


def test_booking_checks_take_the_model_from_the_invoice_when_the_booking_form_has_none(journey) -> None:
    """Rule 2026-10-20: no model read off the Booking Form (no journey_products
    row at all, "Vehicle not identified yet") -- the Booking-side resolution
    itself reads the vehicle invoice, and the vehicle snapshot is refreshed
    from the master."""
    c = journey
    z8, z8l = _seed_price_list(c, [
        {"model": "SCORPIO N", "variant": "Z8", "components": {"EX_SHOWROOM": "1500000"}},
        {"model": "SCORPIO N", "variant": "Z8L", "components": {"EX_SHOWROOM": "1600000"}},
    ])
    _set_invoice_field(c, "model_name_raw", "Scorpio N")
    _set_invoice_field(c, "variant_raw", "Z8L")
    _set_invoice_price(c, "1600000")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert result.get("resolved") is True and result.get("source") == "INVOICE"
    row = _vehicle(c)
    assert row["product_sku_id"] == z8l
    assert (row["model_name_snapshot"], row["variant_name_snapshot"]) == ("SCORPIO N", "Z8L")
    assert row["selection_method"] == "MODEL_RESOLUTION_INVOICE_V2"
    assert z8 != z8l


def test_invoice_vehicle_description_is_read_when_there_is_no_model_box(journey) -> None:
    c = journey
    (thar,) = _seed_price_list(c, [{"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}}])
    _set_invoice_field(c, "vehicle_description_raw", "THAR")
    _set_invoice_field(c, "variant_raw", "LX")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert result.get("resolved") is True and _vehicle(c)["product_sku_id"] == thar


def test_invoice_still_under_booking_stage_is_read(journey) -> None:
    """The unified upload copies facts before the page is moved to Delivery."""
    c = journey
    (thar,) = _seed_price_list(c, [{"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}}])
    _set_invoice_field(c, "model_name_raw", "THAR", stage_code="BOOKING")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert result.get("resolved") is True and _vehicle(c)["product_sku_id"] == thar


def test_an_accessory_invoice_never_names_the_vehicle(journey) -> None:
    c = journey
    _seed_price_list(c, [{"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}}])
    _set_invoice_field(c, "model_name_raw", "THAR", document_type_key="accessory_invoice_dms")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert result == {"skipped": True, "reason": "no_vehicle_model_read"}
    assert _vehicle(c) is None


def test_booking_model_unmatched_then_the_invoice_settles_it_and_closes_the_gap(journey) -> None:
    c = journey
    thar, _scorpio = _seed_price_list(c, [
        {"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}},
        {"model": "SCORPIO N", "variant": "Z8", "components": {"EX_SHOWROOM": "1500000"}},
    ])
    _set_journey_product(c, "ZZZZ NOT A MODEL", None)
    first = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert first.get("resolved") is not True

    _set_invoice_field(c, "model_name_raw", "THAR")
    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert result.get("resolved") is True and result.get("source") == "INVOICE"
    assert _vehicle(c)["product_sku_id"] == thar
    assert _open_model_flags(c) == 0


def test_model_only_in_another_months_price_list_is_reported(journey) -> None:
    """Model names change between months: the Journey's own list carries
    "XUV 3XO", an older one still "XUV300". Nothing is pinned from the other
    list; the finding says which list has it."""
    c = journey
    _seed_price_list(c, [{"model": "XUV300", "variant": "W8", "components": {"EX_SHOWROOM": "1200000"}}],
                     effective_from="CURRENT_DATE - 400")
    _seed_price_list(c, [
        {"model": "XUV 3XO", "variant": "AX5", "components": {"EX_SHOWROOM": "1100000"}},
        {"model": "XUV 3XO", "variant": "AX7", "components": {"EX_SHOWROOM": "1300000"}},
    ])
    _set_journey_product(c, "XUV300", "W8")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert result.get("raised") is True and result.get("otherPriceLists") == 1
    assert _vehicle(c)["product_sku_id"] is None
    finding = c.execute(
        text("SELECT f.title, f.description, e.safe_payload FROM auditcore.audit_findings f "
             "JOIN auditcore.audit_finding_events e ON e.tenant_id=f.tenant_id "
             " AND e.audit_finding_id=f.audit_finding_id AND e.event_type='RAISED' "
             "WHERE f.tenant_id=:t AND f.journey_id=:j AND f.finding_type_code='MODEL_NOT_IDENTIFIED'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert finding["title"] == "Vehicle model is in another month's price list, not this Journey's"
    assert "as 'XUV300' (1 SKUs)" in finding["description"]
    [hit] = finding["safe_payload"]["otherPriceLists"]
    assert hit["modelName"] == "XUV300" and hit["skuCount"] == 1


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


# ── decision 2026-10-01: the ladder's one way out ─────────────────────────────
# Two real DEV journeys could not be pinned although the price list held the
# vehicle: "XUV3XO" + "AX7L EV" (the attribute filter emptied the petrol/
# diesel XUV3XO rows and the ladder stopped there, never reaching the EV
# sibling), and "MAXX HD 1.7L LX" at 9,96,500 (the fuzzy stage returned all
# eleven MAXX HD rows and the price was never asked). Every non-single exit
# now settles the same way: the form's price narrows a shortlist, nothing at
# all goes to the catalogue search, and the open finding follows the latest
# run instead of keeping the first run's wording.

def _seed_xuv3xo_family(c):
    """The DEV master's shape: the petrol/diesel XUV3XO rows and the two
    electric rows under the sibling nameplate "XUV3XO EV"."""
    ids = _seed_price_list(c, [
        {"model": "XUV3XO", "variant": "AX7L TGDI AT DT", "fuel": "PETROL", "transmission": "AT", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "1503500.54", "REGISTRATION_INDIVIDUAL": "290829.95"}},
        {"model": "XUV3XO", "variant": "AX7L DS MT DT", "fuel": "DIESEL", "transmission": "MT", "trim": "AX7L",
         "components": {"EX_SHOWROOM": "1407500.54", "REGISTRATION_INDIVIDUAL": "277389.72"}},
        {"model": "XUV3XO", "variant": "AX5 PM MT", "fuel": "PETROL", "transmission": "MT", "trim": "AX5",
         "components": {"EX_SHOWROOM": "1029900.54", "REGISTRATION_INDIVIDUAL": "224524.79"}},
        {"model": "XUV3XO EV", "variant": "XUV3XO EV - AX5 FH", "fuel": "ELECTRIC",
         "components": {"EX_SHOWROOM": "1389000.00", "REGISTRATION_INDIVIDUAL": "107033.00"}},
        {"model": "XUV3XO EV", "variant": "XUV3XO EV - AX7 L FH", "fuel": "ELECTRIC",
         "components": {"EX_SHOWROOM": "1496000.00", "REGISTRATION_INDIVIDUAL": "111267.00"}},
    ])
    return {"ax7l_petrol": ids[0], "ax7l_diesel": ids[1], "ax5": ids[2], "ev_ax5": ids[3], "ev_ax7l": ids[4]}


def _pinned(c):
    return c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()


def test_integration_an_emptied_attribute_filter_still_reaches_the_catalogue_search(mahindra_journey) -> None:
    """Pritishree Nayak on DEV: "XUV3XO" / "AX7L EV", ex-showroom 14,96,000.
    The nameplate resolves to the petrol/diesel XUV3XO rows, the stated EV
    rightly removes every one of them, and the ladder used to stop there
    with nothing. The catalogue search names the vehicle the list holds
    under "XUV3XO EV" on the exact price."""
    c = mahindra_journey
    skus = _seed_xuv3xo_family(c)
    _set_journey_product(c, "XUV3XO", "AX7L EV")
    _set_commercial(c, "ex_showroom_price", "1496000.00")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    # 2026-10-20: the EV word reads the model as the list's own EV model
    # and "AX7L" names its AX7 L row by the words; the booking price agrees.
    assert result.get("resolved") is True, result
    assert result["matchStage"] == "WORDS"
    assert result["basis"] == "AUTO_MATCHED"
    assert _pinned(c) == skus["ev_ax7l"]


def test_integration_an_ev_reading_without_a_price_is_pinned_as_a_price_mismatch(mahindra_journey) -> None:
    """The same reading with no price on the form: the words name the EV
    AX7 L, so it is pinned (never a petrol or diesel row), but as
    AUTO_PRICE_MISMATCH with a PC task -- the booking price could not be
    compared (2026-10-20 rule R3; this test used to expect a shortlist)."""
    c = mahindra_journey
    skus = _seed_xuv3xo_family(c)
    _set_journey_product(c, "XUV3XO", "AX7L EV")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("resolved") is True, result
    assert result["basis"] == "AUTO_PRICE_MISMATCH" and result["priceCheck"] == "MISSING"
    assert _pinned(c) == skus["ev_ax7l"]
    task = _sku_task(c, "SKU_PRICE_MISMATCH")
    assert task["title"] == "Confirm the model and price: the booking form's ex-showroom was not read"


def _seed_maxx_hd(c):
    return _seed_price_list(c, [
        {"model": "MAXX HD", "variant": "MAXX HD 1.7L LX BS6.2", "fuel": "DIESEL",
         "components": {"EX_SHOWROOM": "996499.88", "REGISTRATION_INDIVIDUAL": "48191.90"}},
        {"model": "MAXX HD", "variant": "MAXX HD 1.7 LX BS6.2", "fuel": "DIESEL",
         "components": {"EX_SHOWROOM": "971499.84", "REGISTRATION_INDIVIDUAL": "47689.15"}},
        {"model": "MAXX HD", "variant": "MAXX HD 1.7 LX CBC BS6.2", "fuel": "DIESEL",
         "components": {"EX_SHOWROOM": "947499.97", "REGISTRATION_INDIVIDUAL": "48006.50"}},
        {"model": "MAXX HD", "variant": "MAXX HD 1.7L MXi BS6.2", "fuel": "DIESEL",
         "components": {"EX_SHOWROOM": "1027499.97", "REGISTRATION_INDIVIDUAL": "59090.32"}},
        {"model": "MAXX HD", "variant": "MAXX HD 1.3 LX BS6.2", "fuel": "DIESEL",
         "components": {"EX_SHOWROOM": "961499.48", "REGISTRATION_INDIVIDUAL": "102286.01"}},
    ])


def test_integration_a_price_only_orders_a_wide_shortlist(mahindra_journey) -> None:
    """Sonali Das on DEV: "MAXX HD" / "LX" at 9,96,500. Before 2026-10-20 the
    ex-showroom (12 paise off the 1.7L LX row) pinned it. Rule R3: the words
    name three LX rows (1.3, 1.7, 1.7L), so the PC picks; the row whose
    price is nearest is only listed first."""
    c = mahindra_journey
    lx_17l, *_ = _seed_maxx_hd(c)
    _set_journey_product(c, "MAXX HD", "LX")
    _set_commercial(c, "ex_showroom_price", "996500.00")
    _set_commercial(c, "total_price", "996500.00")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("raised") is True, result
    assert result["candidateCount"] == 3
    assert _pinned(c) is None
    shortlist = mr.get_model_resolution_candidates(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert shortlist["candidates"][0]["productSkuId"] == lx_17l


def test_integration_a_wide_shortlist_stays_a_task_when_no_price_singles_one_out(mahindra_journey) -> None:
    """The same reading with a price between two rows: the shortlist is
    reported as before, nothing is guessed."""
    c = mahindra_journey
    _seed_maxx_hd(c)
    _set_journey_product(c, "MAXX HD", "LX")
    _set_commercial(c, "ex_showroom_price", "985000.00")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("raised") is True, result
    assert result["candidateCount"] > 1
    assert _open_model_flags(c) == 1


def test_integration_the_invoice_wording_pins_the_variant_it_names(mahindra_journey) -> None:
    """2026-10-20: invoices printing "BOL MAXX PUP HD 1.7 LX" were left with
    every Maxx HD row. Only "MAXX HD 1.7 LX BS6.2" is named by every word
    ("1.7L" is a different vehicle), with no price to help."""
    c = mahindra_journey
    _lx_17l, lx_17, *_ = _seed_maxx_hd(c)
    _set_journey_product(c, "BOL MAXX PUP HD 1.7 LX", "HD 1.7 LX")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("resolved") is True, result
    assert result["matchStage"] == "WORDS"
    assert _pinned(c) == lx_17
    assert _open_model_flags(c) == 0


def test_integration_an_open_finding_follows_the_latest_run(mahindra_journey) -> None:
    """Sonali Das again: the finding was first raised from the invoice's
    text with no candidates and kept that wording after the Booking Form
    arrived. Now the title, description, event payload and the self-serve
    task's candidates follow the latest run, and the finding resolves
    itself once the price settles the shortlist."""
    c = mahindra_journey
    lx_17l, *_ = _seed_maxx_hd(c)
    # (0150 now names "BOL MAXX PUP HD" as the Maxx HD; an unreadable model
    # text keeps this test's first run without candidates.)
    _set_journey_product(c, "BLR MXX PUP 1.7L LX", None)
    first = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert first.get("raised") is True and first["candidateCount"] == 0
    finding_id = _open_model_finding_id(c)
    before = c.execute(
        text("SELECT description FROM auditcore.audit_findings WHERE tenant_id=:t AND audit_finding_id=:f"),
        {"t": c.tenant_id, "f": finding_id},
    ).scalar_one()
    assert "did not match any price-master SKU" in before

    # The Booking Form is read: the model resolves, the price is between rows.
    c.execute(
        text("UPDATE auditcore.journey_products SET model_name_snapshot='MAXX HD', variant_name_snapshot='LX' "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    _set_commercial(c, "ex_showroom_price", "985000.00")
    second = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert second.get("raised") is True and second["candidateCount"] > 1
    assert _open_model_finding_id(c) == finding_id  # the same finding, refreshed, not a second one
    after = c.execute(
        text("SELECT title, description FROM auditcore.audit_findings WHERE tenant_id=:t AND audit_finding_id=:f"),
        {"t": c.tenant_id, "f": finding_id},
    ).mappings().one()
    assert after["title"] == "Vehicle model matched multiple price-master SKUs"
    assert "MAXX HD" in after["description"] and f"fit {second['candidateCount']}" in after["description"]
    events = c.execute(
        text("SELECT event_type, safe_payload FROM auditcore.audit_finding_events "
             "WHERE tenant_id=:t AND audit_finding_id=:f ORDER BY occurred_at_utc"),
        {"t": c.tenant_id, "f": finding_id},
    ).mappings().all()
    # Both events carry the same transaction timestamp: compare as a set.
    assert sorted(e["event_type"] for e in events) == ["RAISED", "REFRESHED"]
    [refreshed] = [e for e in events if e["event_type"] == "REFRESHED"]
    assert refreshed["safe_payload"]["candidateCount"] == second["candidateCount"]
    payload = _related_task_payload(c, finding_id)
    assert len(payload["candidates"]) == second["candidateCount"]

    # A price never settles it (2026-10-20); the words do once the form is
    # read in full: the finding resolves and the task closes.
    c.execute(
        text("UPDATE auditcore.journey_products SET variant_name_snapshot='1.7L LX' "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    _set_commercial(c, "ex_showroom_price", "996500.00")
    third = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert third.get("resolved") is True and _pinned(c) == lx_17l
    assert _open_model_flags(c) == 0


def test_confirm_pins_a_sku_on_a_journey_whose_model_was_never_read(journey) -> None:
    """Found live 2026-10-20: a journey whose Booking Form model was never
    read (no journey_products row) could not be given a SKU at all -- the
    PC's pick answered 404 "No reviewed booking model". A pick from the
    catalogue now pins the SKU and writes the master's model/variant/colour."""
    c = journey
    _sku_a, sku_b = _seed_ambiguous_thar(c)
    assert c.execute(
        text("SELECT COUNT(*) FROM auditcore.journey_products WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == 0
    catalog = mr.get_model_catalog(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert catalog["confirmedProductSkuId"] is None

    result = mr.confirm_model_resolution_sku(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, product_sku_id=sku_b,
        actor_id="pc-test-actor", correlation_id="",
    )

    assert result["productSkuId"] == sku_b
    row = c.execute(
        text("SELECT product_sku_id, selection_status, model_name_snapshot FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert row["product_sku_id"] == sku_b
    assert row["selection_status"] == "CONFIRMED"
    assert row["model_name_snapshot"]
    catalog = mr.get_model_catalog(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert catalog["confirmedProductSkuId"] == sku_b


def test_confirm_refuses_to_replace_a_confirmed_sku(journey) -> None:
    """A confirmed SKU changes only through a Team Lead reviewed proposal."""
    from audit_core.errors import AuditCoreError

    c = journey
    sku_a, sku_b = _seed_ambiguous_thar(c)
    mr.confirm_model_resolution_sku(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, product_sku_id=sku_a,
        actor_id="pc-test-actor", correlation_id="",
    )
    with pytest.raises(AuditCoreError) as refused:
        mr.confirm_model_resolution_sku(
            c, tenant_id=c.tenant_id, journey_id=c.journey_id, product_sku_id=sku_b,
            actor_id="pc-test-actor", correlation_id="",
        )
    assert refused.value.error_code == "VAC-SKU-005"


def test_other_month_list_waits_for_tl_approval_then_resolves_there(journey) -> None:
    """Decision 2026-10-20: the model only in another month's list raises a
    Team Lead task; no SKU (and no closest-match suggestion) until approved;
    on approval the Journey is priced on that list and the SKU resolves."""
    from audit_core.uc03_p2_tasks import TaskPermissionError
    from audit_core.uc03_sku_tasks import handle_sku_task_action

    c = journey
    (old_w8,) = _seed_price_list(
        c, [{"model": "XUV300", "variant": "W8", "components": {"EX_SHOWROOM": "1200000"}}],
        effective_from="CURRENT_DATE - 400",
    )
    _seed_price_list(c, [
        {"model": "XUV 3XO", "variant": "AX5", "components": {"EX_SHOWROOM": "1100000"}},
        {"model": "XUV 3XO", "variant": "AX7", "components": {"EX_SHOWROOM": "1300000"}},
    ])
    _set_journey_product(c, "XUV300", "W8")

    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    task = _sku_task(c, "SKU_OTHER_MONTH_PRICE_LIST")
    assert task["assigned_role_code"] == "TL" and task["task_status"] == "READY"
    assert _sku_task(c, "SKU_CLOSEST_MATCH_CONFIRM") is None
    assert _vehicle(c)["product_sku_id"] is None

    with pytest.raises(TaskPermissionError):
        handle_sku_task_action(c, tenant_id=c.tenant_id, task=dict(task), action="ACCEPT", actor_id="pc-1",
                               actor_role_code="PC", comment=None)
    outcome = handle_sku_task_action(c, tenant_id=c.tenant_id, task=dict(task), action="ACCEPT", actor_id="tl-1",
                                     actor_role_code="TL", comment="Model renamed this month")
    assert outcome["outcome"] == "PRICE_LIST_APPROVED"
    assert _vehicle(c)["product_sku_id"] == old_w8
    basis = c.execute(
        text("SELECT pricing_basis FROM auditcore.bookings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert basis == "TL_PRICE_LIST"
    assert _sku_task(c, "SKU_OTHER_MONTH_PRICE_LIST")["task_status"] == "VERIFIED_COMPLETE"


def test_vehicle_category_follows_the_sku(journey) -> None:
    c = journey
    (ev,) = _seed_price_list(c, [{"model": "BE 6", "variant": "PACK ONE", "fuel": "ELECTRIC",
                                  "components": {"EX_SHOWROOM": "1900000"}}])
    _set_journey_product(c, "BE 6", "PACK ONE")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    category = c.execute(
        text("SELECT product_sku_id, vehicle_category FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert category["product_sku_id"] == ev and category["vehicle_category"] == "EV"


def test_an_approved_price_list_version_is_used_whatever_the_date(journey) -> None:
    """Found on the full SKU run (2026-10-20): two versions take effect the
    same day and the date always picks the higher one. A Team Lead's approval
    names the version itself, and the price plan uses it."""
    from audit_core.uc03_sku_candidates import _price_plan_for_journey
    from audit_core.uc03_sku_tasks import approve_other_month_price_list

    c = journey
    (xuv,) = _seed_price_list(c, [{"model": "XUV3XO", "variant": "AX5", "components": {"EX_SHOWROOM": "1200000"}}],
                              effective_from="CURRENT_DATE - 400")
    _seed_price_list(c, [
        {"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}},
        {"model": "THAR", "variant": "AX", "components": {"EX_SHOWROOM": "1300000"}},
    ])
    old_version = c.execute(
        text("SELECT price_list_version_id FROM auditcore.price_list_items WHERE tenant_id=:t AND product_sku_id=:s "
             "LIMIT 1"),
        {"t": c.tenant_id, "s": xuv},
    ).scalar_one()
    _set_journey_product(c, "XUV 3XO", "AX5")
    outcome = approve_other_month_price_list(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, price_list_version_id=old_version, actor_id="tl-1",
        reason="Model is in that version",
    )
    assert outcome["skuResolved"] is True and _vehicle(c)["product_sku_id"] == xuv
    from datetime import UTC, datetime
    plan = _price_plan_for_journey(c, tenant_id=c.tenant_id, journey_id=c.journey_id,
                                   effective_on=datetime.now(UTC).date())
    assert plan["price_list_version_id"] == old_version


def test_a_price_never_picks_a_row_the_document_contradicts(mahindra_journey) -> None:
    """Rasmi Ranjan Biswal on DEV (2026-10-20): "MAHINDRA/BOLERO/MAXX PUP HD
    2.0L VXI DIESEL" / "VXi" was pinned as "MAXX HD 1.7 LX CBC" because the
    form's ex-showroom was within 0.5% of that row. The document says 2.0L
    and VXi: the row is only suggested, never pinned."""
    c = mahindra_journey
    _seed_maxx_hd(c)
    _set_journey_product(c, "MAXX HD 2.0L VXI DIESEL", "VXi")
    _set_commercial(c, "ex_showroom_price", "947600.00")

    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("resolved") is not True, result
    assert _pinned(c) is None


def test_the_oem_name_in_front_of_the_model_is_ignored() -> None:
    assert mr.clean_model_text("MAHINDRA BOLERO NEO+") == "BOLERO NEO+"
    assert mr.clean_model_text("MAHINDRA & MAHINDRA MAXX HD BOL MAXX PUP HD 1.7 LX") == "MAXX HD BOL MAXX PUP HD 1.7 LX"
    assert mr.clean_model_text("MAHINDRA/BOLERO/MAXX PUP HD 2.0L VXI DIESEL/OPEN") == "BOLERO MAXX PUP HD 2.0L VXI DIESEL OPEN"
    assert mr.clean_model_text("XUV 7XO") == "XUV 7XO"


def test_registration_type_reads_private_or_commercial() -> None:
    assert mr.registration_category("Personal") == "PRIVATE"
    assert mr.registration_category("Private") == "PRIVATE"
    assert mr.registration_category("Motor Car") == "PRIVATE"
    assert mr.registration_category("Commercial") == "COMMERCIAL"
    assert mr.registration_category("Fresh Permit") == "COMMERCIAL"
    assert mr.registration_category("NEW") is None
    assert mr.registration_category("New Registration (RTO Side)") is None
    assert mr.registration_category(None) is None


def test_the_registration_type_picks_the_private_or_commercial_row(journey) -> None:
    """The price list holds Bolero Neo N11 twice, (Private) and (Commercial);
    the Journey's registration type picks one (2026-10-20)."""
    c = journey
    pri, com = _seed_price_list(c, [
        {"model": "BOLERO NEO", "variant": "N11 BS6.2 (Private)", "components": {"EX_SHOWROOM": "1030000"}},
        {"model": "BOLERO NEO", "variant": "N11 BS6.2 (Commercial)", "components": {"EX_SHOWROOM": "1030000"}},
    ])
    _set_journey_product(c, "BOLERO NEO", "N11 BS6.2")
    first = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert first.get("resolved") is not True  # nothing says which registration

    c.execute(
        text("INSERT INTO auditcore.registration_records (tenant_id, journey_id, registration_type_code) "
             "VALUES (:t, :j, 'Commercial')"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    second = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert second.get("resolved") is True, second
    assert _pinned(c) == com and pri != com


def test_a_model_split_across_the_two_boxes_is_read_together(journey) -> None:
    """Susama Naik on DEV: model box "Maxx", variant box "HD 1.7 LX"."""
    c = journey
    hd, _city = _seed_price_list(c, [
        {"model": "MAXX HD", "variant": "MAXX HD 1.7 LX BS6.2", "components": {"EX_SHOWROOM": "971500"}},
        {"model": "MAXX CITY", "variant": "MAXX CITY 1.3 LX BS6.2", "components": {"EX_SHOWROOM": "800000"}},
    ])
    _set_journey_product(c, "Maxx", "HD 1.7 LX")
    _set_commercial(c, "ex_showroom_price", "971500")
    result = mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("resolved") is True, result
    assert _pinned(c) == hd


def test_the_retail_invoice_colour_replaces_a_different_booked_colour(journey) -> None:
    """Rule 2026-10-20: the DMS / Tally invoice decides model, variant,
    transmission and colour. The price list has no colours, so the invoiced
    colour replaces the booked one on the Journey and the TL is told."""
    c = journey
    (thar,) = _seed_price_list(c, [{"model": "THAR", "variant": "LX", "components": {"EX_SHOWROOM": "1400000"}}])
    _set_journey_product(c, "THAR", "LX")
    c.execute(text("UPDATE auditcore.journey_products SET colour_name_snapshot='Red' WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _pinned(c) == thar
    _set_invoice_field(c, "model_name_raw", "THAR")
    _set_invoice_field(c, "vehicle_color", "Stealth Black")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _vehicle(c)["product_sku_id"] == thar
    colour = c.execute(
        text("SELECT colour_name_snapshot FROM auditcore.journey_products WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert colour == "Stealth Black"
    assert _sku_task(c, "MODEL_CHANGED_BY_INVOICE") is not None


def test_dealer_spellings_of_the_pickups_and_the_neo_name_the_list_model(mahindra_journey) -> None:
    """2026-10-20 DEV: "BOL MAXX PUP HD 1.7 LX" and "BOLERO MAXX PUP HD" are
    the Maxx HD (the longest alias used to be "BOLERO" -> the Bolero Neo);
    "Bol Neo N10 R" is the Bolero Neo."""
    from audit_core.uc03_model_attribute_matching import resolve_model_via_aliases

    c = mahindra_journey
    aliases = mr._oem_model_aliases(c, oem_code="MAHINDRA")
    assert resolve_model_via_aliases(model_name="BOL MAXX PUP HD 1.7 LX", oem_aliases=aliases)[0] == "MAXX HD"
    assert resolve_model_via_aliases(model_name="BOLERO MAXX PUP HD 2.0L VXI", oem_aliases=aliases)[0] == "MAXX HD"
    # 2026-10-20 product owner rules (migration 0151): the Bolero family is
    # split as October prints it; the Neo is its own model.
    assert resolve_model_via_aliases(model_name="Bol Neo N10 R", oem_aliases=aliases)[0] == "BOLERO NEO"
