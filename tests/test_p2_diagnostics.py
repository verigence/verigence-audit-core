"""The read-only diagnostics reports run against the real schema."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_diagnostics import REPORTS, run_diagnostics


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="diag")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_every_report_runs_read_only(journey, name):
    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text(REPORTS[name]), {"t": journey.tenant_id, "c": "%"}).mappings().all()


def test_run_logs_without_failing(journey, monkeypatch):
    from audit_core import uc03_p2_diagnostics as diag

    monkeypatch.setattr(diag, "_projects", lambda engine: [
        {"tenant_id": journey.tenant_id, "project_code": "X", "project_name": "X", "project_status": "ACTIVE"}])
    run_diagnostics(journey.engine, ["all"])


def _price_book(journey, prices: dict[tuple[str, str], str], *, effective_from):
    """A published price book: {(model, variant): ex-showroom}."""
    from decimal import Decimal
    from uuid import uuid4

    from audit_core.price_lists import (
        add_price_list_item,
        create_price_list,
        create_price_list_version,
        publish_price_list_version,
    )

    suffix = uuid4().hex[:8]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        oem_id = connection.execute(
            text("SELECT oem_id FROM auditcore.projects WHERE tenant_id = :t"),
            {"t": journey.tenant_id},
        ).scalar_one()
        price_list = create_price_list(connection, tenant_id=journey.tenant_id,
                                       code="OEM_NATIVE_PRICE_LIST", name="OEM")
        version = create_price_list_version(connection, tenant_id=journey.tenant_id,
                                            price_list_id=price_list, version_no=1,
                                            effective_from=effective_from)
        models: dict[str, object] = {}
        for index, ((model, variant), amount) in enumerate(prices.items()):
            if model not in models:
                models[model] = connection.execute(
                    text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                         "VALUES (:o, :c, :n) RETURNING model_id"),
                    {"o": oem_id, "c": f"PK-M{index}-{suffix}", "n": model},
                ).scalar_one()
            variant_id = connection.execute(
                text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                     "VALUES (:m, :c, :n) RETURNING variant_id"),
                {"m": models[model], "c": f"PK-V{index}-{suffix}", "n": variant},
            ).scalar_one()
            sku_id = connection.execute(
                text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                     "VALUES (:o, :m, :v, :c) RETURNING product_sku_id"),
                {"o": oem_id, "m": models[model], "v": variant_id, "c": f"PK-S{index}-{suffix}"},
            ).scalar_one()
            add_price_list_item(connection, tenant_id=journey.tenant_id, price_list_version_id=version,
                                product_sku_id=sku_id, component_key="EX_SHOWROOM",
                                standard_amount=Decimal(amount))
        publish_price_list_version(connection, tenant_id=journey.tenant_id,
                                   price_list_version_id=version, actor_id=journey.actor_id)
    return version


def _booked_on(journey, on):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date, pricing_effective_on) "
                 "VALUES (:t, :j, :d, :d) "
                 "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET pricing_effective_on = :d"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": on},
        )


def _simulated(journey):
    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text(REPORTS["price_key_simulation"]), {"t": journey.tenant_id, "c": "%"},
        ).mappings().all()


def test_the_price_key_simulation_reads_a_formatted_figure_and_names_the_one_variant(journey):
    """The measurement behind "cant we even able to pick a price" (2026-10-24):
    an ex-showroom figure read off the form is matched against the price book
    in force. A figure printed with a currency symbol and Indian grouping is
    the same key as the book's bare number."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    _price_book(journey, {
        ("SCORPIO N", "Z8L D MT 2WD"): "2399000.00",
        ("SCORPIO N", "Z8L D AT 2WD"): "2549000.00",
        ("THAR ROXX", "MX1 D MT RWD"): "1299000.00",
    }, effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="Rs. 23,99,000.00", document_type="booking_form")

    [row] = _simulated(journey)
    assert row["read_amount"] == 2399000
    assert row["had_a_price_book"] is True
    assert row["exact_skus"] == 1
    assert row["exact_variants"] == 1
    assert row["exact_names"] == "SCORPIO N ~ Z8L D MT 2WD"
    assert row["resolved_today"] is False, "the words never resolved it; the figure alone did"


def test_the_simulation_says_when_a_figure_cannot_pin_one_variant(journey):
    """Honest in both directions: two variants priced the same means the figure
    narrows to the model and no further, and the report says so rather than
    claiming a pin."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    _price_book(journey, {
        ("BOLERO NEO", "N4"): "999000.00",
        ("BOLERO NEO", "N4 OPT"): "999000.00",
    }, effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="999000", document_type="booking_form")

    [row] = _simulated(journey)
    assert row["exact_skus"] == 2
    assert row["exact_models"] == 1
    assert row["exact_variants"] == 2


def test_the_simulation_reports_the_nearest_price_when_nothing_matches_exactly(journey):
    """A form that rounded to the rupee still points at one variant; the gap is
    reported so a tolerance can be chosen from the data instead of guessed."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    _price_book(journey, {("XUV 3XO", "AX5L"): "1234567.89"}, effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1234568", document_type="booking_form")

    [row] = _simulated(journey)
    assert row["exact_skus"] == 0
    assert row["near_skus"] == 1
    assert row["near_variants"] == 1
    assert row["near_names"] == "XUV 3XO ~ AX5L"
    assert row["near_gap"] < 1


def test_the_shape_report_shows_a_figure_the_simulation_could_not_use(journey):
    from uuid import uuid4

    from p2_support import add_extracted_field

    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="as per invoice", document_type="booking_form")
    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        [row] = connection.execute(
            text(REPORTS["price_reads_shape"]), {"t": journey.tenant_id, "c": "%"},
        ).mappings().all()
    assert row["reads"] == 1
    assert row["usable_as_a_figure"] == 0
    assert row["shapes_not_usable"] == "as per invoice"
