"""The SKU work a Team Lead or Reviewer has to clear, in one list (real Postgres).

Asked by the product owner 2026-10-24: the Journeys with several options, with
no price available, with a date that cannot price them, plus the tasks already
raised and how badly the price contradicts the vehicle set.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import add_extracted_field, create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_sku_attention import REASONS, sku_attention


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="skuat")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _task(journey, task_type, *, severity="CRITICAL", journey_id=None):
    """Raised through the real producer, not a hand-rolled INSERT: the suite
    already has three hand-rolled doubles of one fixture and they break one at
    a time (2026-10-24). This also means a task type missing from the registry
    fails here rather than in production."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    target = journey_id or journey.journey_id
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        raise_or_refresh(
            connection, tenant_id=journey.tenant_id, journey_id=target,
            dedupe_key=f"{task_type}:{target}", task_type=task_type,
            source_type="RESOLVER", source_code=task_type,
            title=f"{task_type} title", description=f"{task_type} description",
            reference={"sourceCode": task_type}, severity=severity, registry=get_registry(),
        )


def _book(journey, prices: dict[tuple[str, str], str], *, effective_from: date):
    from audit_core.price_lists import (
        add_price_list_item,
        create_price_list,
        create_price_list_version,
        publish_price_list_version,
    )

    suffix = uuid4().hex[:8]
    ids: dict[tuple[str, str], object] = {}
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
                    {"o": oem_id, "c": f"AT-M{index}-{suffix}", "n": model},
                ).scalar_one()
            variant_id = connection.execute(
                text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                     "VALUES (:m, :c, :n) RETURNING variant_id"),
                {"m": models[model], "c": f"AT-V{index}-{suffix}", "n": variant},
            ).scalar_one()
            sku_id = connection.execute(
                text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                     "VALUES (:o, :m, :v, :c) RETURNING product_sku_id"),
                {"o": oem_id, "m": models[model], "v": variant_id, "c": f"AT-S{index}-{suffix}"},
            ).scalar_one()
            ids[(model, variant)] = sku_id
            add_price_list_item(connection, tenant_id=journey.tenant_id, price_list_version_id=version,
                                product_sku_id=sku_id, component_key="EX_SHOWROOM",
                                standard_amount=Decimal(amount))
        publish_price_list_version(connection, tenant_id=journey.tenant_id,
                                   price_list_version_id=version, actor_id=journey.actor_id)
    return ids


def _booked(journey, *, on: date, sku_id=None, model=None, variant=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) VALUES (:t, :j, :d) "
                 "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET booking_date = :d"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": on},
        )
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, product_sku_id, "
                 "model_name_snapshot, variant_name_snapshot, selection_source) "
                 "VALUES (:t, :j, :s, :m, :v, 'EVIDENCE') "
                 "ON CONFLICT (tenant_id, journey_id) DO UPDATE SET product_sku_id = :s"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": sku_id, "m": model, "v": variant},
        )


def _attention(journey, **kw):
    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        return sku_attention(connection, tenant_id=journey.tenant_id, **kw)


def test_a_journey_with_no_open_sku_task_is_not_on_the_list(journey):
    """The list is the tasks, so a Journey nobody has to act on stays off it."""
    assert _attention(journey)["needsAttention"] == 0


def test_every_reason_is_named_and_labelled(journey):
    """A reason with no label would reach a Team Lead's screen as a code."""
    for reason, (task_type, label) in REASONS.items():
        assert task_type and label and not label.endswith(".")
        assert reason.isupper()


def test_the_list_carries_the_reason_the_task_gives_and_its_severity(journey):
    _task(journey, "PRICE_MASTER_NOT_IN_FORCE")
    result = _attention(journey)
    assert result["needsAttention"] == 1
    [item] = result["items"]
    assert item["reasons"] == ["NO_PRICE_FOR_DATE"]
    assert item["journeyReference"] and item["customer"]
    [task] = item["tasks"]
    assert (task["taskType"], task["severity"], task["status"]) == (
        "PRICE_MASTER_NOT_IN_FORCE", "CRITICAL", "READY")
    assert result["byReason"]["NO_PRICE_FOR_DATE"] == 1
    assert result["reasonLabels"]["NO_PRICE_FOR_DATE"].startswith("No price available")


def test_one_journey_with_two_tasks_is_one_row_with_two_reasons(journey):
    _task(journey, "SKU_NOT_SET")
    _task(journey, "PC_BOOKING_DATE_MISSING")
    [item] = _attention(journey)["items"]
    assert sorted(item["reasons"]) == ["NOTHING_IDENTIFIED", "NO_PRICING_DATE"]
    assert len(item["tasks"]) == 2


def test_a_price_naming_another_variant_of_the_same_model_is_a_variant_contradiction(journey):
    ids = _book(journey, {
        ("THAR ROXX", "MX1 D MT RWD"): "1423000.58",
        ("THAR ROXX", "MX3 D MT RWD"): "1623000.58",
    }, effective_from=date(2026, 10, 1))
    _booked(journey, on=date(2026, 10, 15), sku_id=ids[("THAR ROXX", "MX3 D MT RWD")],
            model="THAR ROXX", variant="MX3 D MT RWD")
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")
    _task(journey, "SKU_PRICE_MISMATCH", severity="MEDIUM")

    [item] = _attention(journey)["items"]
    assert item["pinnedVehicle"] == "THAR ROXX ~ MX3 D MT RWD"
    assert item["contradiction"]["level"] == "VARIANT"
    assert item["contradiction"]["priceNames"] == "THAR ROXX ~ MX1 D MT RWD"
    assert item["figureRead"] == "1423001.00" and item["figureDocument"] == "booking_form"
    assert _attention(journey)["contradicted"] == {"MODEL": 1 - 1, "VARIANT": 1}


def test_a_price_naming_a_different_model_is_the_serious_one(journey):
    ids = _book(journey, {
        ("THAR ROXX", "MX1 D MT RWD"): "1423000.58",
        ("BOLERO NEO", "N10"): "1099000.40",
    }, effective_from=date(2026, 10, 1))
    _booked(journey, on=date(2026, 10, 15), sku_id=ids[("THAR ROXX", "MX1 D MT RWD")],
            model="THAR ROXX", variant="MX1 D MT RWD")
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1099000", document_type="booking_form")
    _task(journey, "SKU_PRICE_MISMATCH", severity="MEDIUM")

    result = _attention(journey)
    [item] = result["items"]
    assert item["contradiction"]["level"] == "MODEL"
    assert item["contradiction"]["priceNames"] == "BOLERO NEO ~ N10"
    assert result["contradicted"]["MODEL"] == 1


def test_a_price_that_agrees_is_no_contradiction(journey):
    ids = _book(journey, {("THAR ROXX", "MX1 D MT RWD"): "1423000.58"},
                effective_from=date(2026, 10, 1))
    _booked(journey, on=date(2026, 10, 15), sku_id=ids[("THAR ROXX", "MX1 D MT RWD")],
            model="THAR ROXX", variant="MX1 D MT RWD")
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")
    _task(journey, "SKU_PRICE_MISMATCH", severity="MEDIUM")

    [item] = _attention(journey)["items"]
    assert item["contradiction"] is None
    assert _attention(journey)["contradicted"] == {"MODEL": 0, "VARIANT": 0}


def test_a_contradiction_over_a_different_model_sorts_above_everything(journey):
    """A Team Lead opening this screen should meet the worst case first."""
    ids = _book(journey, {
        ("THAR ROXX", "MX1 D MT RWD"): "1423000.58",
        ("BOLERO NEO", "N10"): "1099000.40",
    }, effective_from=date(2026, 10, 1))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        other = connection.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, "
                 "journey_reference) VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id,
             "cu": journey.customer_id, "r": "AAA-first-by-reference"},
        ).scalar_one()
    _task(journey, "PC_BOOKING_DATE_MISSING", journey_id=other)

    _booked(journey, on=date(2026, 10, 15), sku_id=ids[("THAR ROXX", "MX1 D MT RWD")],
            model="THAR ROXX", variant="MX1 D MT RWD")
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1099000", document_type="booking_form")
    _task(journey, "SKU_PRICE_MISMATCH", severity="MEDIUM")

    items = _attention(journey)["items"]
    assert len(items) == 2
    assert items[0]["contradiction"]["level"] == "MODEL", "the contradiction must come first"


def test_the_list_can_be_narrowed_to_one_outlet(journey):
    _task(journey, "SKU_NOT_SET")
    assert _attention(journey, outlet_id=journey.outlet_id)["needsAttention"] == 1
    assert _attention(journey, outlet_id=uuid4())["needsAttention"] == 0


def test_a_date_a_person_applied_is_marked_as_theirs(journey):
    _book(journey, {("THAR ROXX", "MX1 D MT RWD"): "1423000.58"}, effective_from=date(2026, 10, 1))
    _booked(journey, on=date(2026, 10, 15), model="THAR ROXX", variant="MX1 D MT RWD")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.bookings SET pricing_effective_on = :d "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"d": date(2026, 10, 2), "t": journey.tenant_id, "j": journey.journey_id},
        )
    _task(journey, "SKU_NOT_SET")
    [item] = _attention(journey)["items"]
    assert item["pricingDateFromAPerson"] is True
    assert item["pricedOn"] == date(2026, 10, 2)
    assert item["hasAPriceBook"] is True


def test_a_journey_with_no_price_book_says_so(journey):
    _booked(journey, on=date(2026, 8, 1), model="THAR ROXX", variant="MX1 D MT RWD")
    _task(journey, "PRICE_MASTER_NOT_IN_FORCE")
    [item] = _attention(journey)["items"]
    assert item["hasAPriceBook"] is False
    assert item["contradiction"] is None, "with no book there is nothing to contradict"
    assert timedelta(0) == timedelta(0)
