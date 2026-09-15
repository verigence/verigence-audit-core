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
                    {k: v for k, v in {"drive": entry.get("drive"), "seater": entry.get("seater")}.items() if v}
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
    # A different SKU code on the invoice must NOT override an already-pinned SKU.
    _set_invoice_field(c, "sku_code", "SOME-OTHER-CODE")

    result = mr.sync_model_resolution_from_invoice(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result == {"skipped": True, "reason": "already_resolved"}
    pinned = c.execute(
        text("SELECT product_sku_id FROM auditcore.journey_products "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert pinned == sku_id


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


def test_confirm_model_resolution_sku_pins_resolves_and_completes_task(journey) -> None:
    c = journey
    _sku_a, sku_b = _seed_ambiguous_thar(c)
    _set_journey_product(c, "Thar", None)
    _set_commercial(c, "total_price", "1670000")
    mr.sync_model_resolution(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    finding_id = _open_model_finding_id(c)
    task_id, status_right_after_raise = c.execute(
        text("SELECT workflow_task_id, task_status FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND related_finding_id=:f"),
        {"t": c.tenant_id, "f": finding_id},
    ).one()
    assert status_right_after_raise == "READY", (
        f"task was {status_right_after_raise!r} immediately after sync_model_resolution "
        "raised it -- before confirm_model_resolution_sku ran at all"
    )

    def _status() -> str:
        return c.execute(
            text("SELECT task_status FROM auditcore.workflow_tasks WHERE tenant_id=:t AND workflow_task_id=:tid"),
            {"t": c.tenant_id, "tid": task_id},
        ).scalar_one()

    # Bisecting confirm_model_resolution_sku's own steps (pin -> resolve ->
    # reconcile) to find exactly which one flips the task to CANCELLED with
    # no event logged for it -- nothing in any of these three helpers'
    # source touches workflow_tasks except _resolve_open_flag's own new
    # _complete_self_serve_tasks call, yet the task ends up CANCELLED.
    mr._pin_sku(c, tenant_id=c.tenant_id, journey_id=c.journey_id, product_sku_id=sku_b)
    assert _status() == "READY", f"status after _pin_sku alone: {_status()!r}"

    # Isolate the exact ordering _resolve_open_flag uses: the Finding's own
    # UPDATE/INSERT FIRST (mirroring its SQL verbatim), THEN a status check
    # BEFORE _complete_self_serve_tasks runs at all -- to see whether the
    # Finding mutation alone (with zero task involvement) already corrupts
    # the task row, or whether it's specifically _complete_self_serve_tasks
    # running AFTER that mutation (in the same transaction) that misbehaves
    # (the earlier bisection calling _complete_self_serve_tasks BEFORE the
    # Finding mutation passed, which doesn't rule out that ordering).
    c.execute(
        text(
            """
            UPDATE auditcore.audit_findings
            SET finding_status = 'RESOLVED', disposition = 'FIXED',
                resolved_at_utc = now(), updated_at_utc = now()
            WHERE tenant_id = :t AND audit_finding_id = :fid
              AND finding_status IN ('OPEN', 'ACKNOWLEDGED')
            """
        ),
        {"t": c.tenant_id, "fid": finding_id},
    )
    c.execute(
        text(
            """
            INSERT INTO auditcore.audit_finding_events (
                tenant_id, audit_finding_id, journey_id, stage_code,
                event_type, actor_id, actor_role_snapshot, safe_payload, correlation_id
            ) VALUES (
                :t, :fid, :j, 'BOOKING', 'RESOLVED', NULL, 'SYSTEM', '{}'::jsonb, ''
            )
            """
        ),
        {"t": c.tenant_id, "fid": finding_id, "j": c.journey_id},
    )
    status_after_finding_mutation_alone = _status()
    assert status_after_finding_mutation_alone == "READY", (
        f"status after the Finding's own UPDATE/INSERT, before "
        f"_complete_self_serve_tasks ran at all: {status_after_finding_mutation_alone!r}"
    )

    mr._complete_self_serve_tasks(
        c, tenant_id=c.tenant_id, related_finding_id=finding_id, actor_id="pc-test-actor",
    )
    status_after_resolve = _status()
    if status_after_resolve != "COMPLETED":
        events = c.execute(
            text(
                "SELECT event_type, from_status, to_status, actor_id, actor_type, reason, "
                "occurred_at_utc FROM auditcore.workflow_task_events "
                "WHERE tenant_id=:t AND workflow_task_id=:tid ORDER BY occurred_at_utc"
            ),
            {"t": c.tenant_id, "tid": task_id},
        ).mappings().all()
        raise AssertionError(
            f"status after _resolve_open_flag alone: {status_after_resolve!r}; "
            f"event history: {[dict(e) for e in events]}"
        )

    mr._run_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    result = {"resolved": True, "productSkuId": sku_b}
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

    task_status = c.execute(
        text("SELECT task_status FROM auditcore.workflow_tasks WHERE tenant_id=:t AND workflow_task_id=:tid"),
        {"t": c.tenant_id, "tid": task_id},
    ).scalar_one()
    if task_status != "COMPLETED":
        events = c.execute(
            text(
                "SELECT event_type, from_status, to_status, actor_id, actor_type, reason, "
                "occurred_at_utc FROM auditcore.workflow_task_events "
                "WHERE tenant_id=:t AND workflow_task_id=:tid ORDER BY occurred_at_utc"
            ),
            {"t": c.tenant_id, "tid": task_id},
        ).mappings().all()
        raise AssertionError(f"expected COMPLETED, got {task_status!r}; event history: {[dict(e) for e in events]}")


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
