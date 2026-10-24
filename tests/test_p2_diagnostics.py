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
    """Bound with exactly what run_diagnostics binds, so a report that needs a
    parameter the runner does not supply fails here rather than on the worker
    (caught 2026-10-24: pricing_date_verdict needs the programme's date
    floor)."""
    from audit_core.uc03_p2_diagnostics import _date_floor

    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text(REPORTS[name]),
            {"t": journey.tenant_id, "c": "%", "floor": _date_floor()},
        ).mappings().all()


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


def _verdict(journey):
    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        [row] = connection.execute(
            text(REPORTS["price_key_verdict"]), {"t": journey.tenant_id, "c": "%"},
        ).mappings().all()
    return row


def test_the_verdict_counts_a_rounded_rupee_as_a_pin_and_not_an_exact_match(journey):
    """Measured on the real data 2026-10-24: Mahindra's book carries paisa
    (2478999.58) while the form prints the rounded rupee (2479000), so matching
    to the paisa almost never fires and a percentage band is far too wide --
    0.5% of a 24-lakh car is 12,000 rupees. The verdict reports the tightest
    band at which a figure names one variant, so the tolerance is chosen from
    the data rather than guessed."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    _price_book(journey, {
        ("THAR ROXX", "MX1 D MT RWD"): "1423000.58",
        ("THAR ROXX", "MX3 D MT RWD"): "1623000.58",
    }, effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")

    row = _verdict(journey)
    assert row["journeys"] == 1
    assert row["not_resolved"] == 1
    assert row["open_pinned_to_the_paisa"] == 0, "the paisa never matches a rounded form"
    assert row["open_pinned_within_a_rupee"] == 1
    assert float(row["median_gap"]) == 0.42


def test_the_verdict_separates_a_journey_with_no_price_book_from_a_price_that_fails(journey):
    """The nine Mahindra Orissa bookings priced before the earliest published
    book are a masters gap, not a matcher failure, and must not be counted as
    the price key failing."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    _price_book(journey, {("THAR ROXX", "MX1 D MT RWD"): "1423000.58"},
                effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 8, 1))  # before the book exists
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")

    row = _verdict(journey)
    assert row["no_price_book_in_force"] == 1
    assert row["priced_before_the_first_book"] == 1, "that month's book has to be loaded"
    assert row["no_pricing_date"] == 0
    assert row["open_price_settles_nothing"] == 0, "no book is not the price failing"
    assert row["open_pinned_within_a_rupee"] == 0


def test_the_verdict_separates_a_booking_with_no_date_from_one_outside_the_books(journey):
    """The two need different work: a Journey with no date needs its date read
    off the documents, one priced before the earliest book needs that month's
    book loaded. 74 of Mahindra Orissa's 171 were in this bucket on the first
    run, so the split decides what to fix first."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    _price_book(journey, {("THAR ROXX", "MX1 D MT RWD"): "1423000.58"},
                effective_from=date(2026, 10, 1))
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")
    # no bookings row at all: nothing says when this deal was priced

    row = _verdict(journey)
    assert row["no_price_book_in_force"] == 1
    assert row["no_pricing_date"] == 1
    assert row["priced_before_the_first_book"] == 0


def test_the_verdict_names_a_journey_where_the_price_contradicts_the_words(journey):
    """The number that decides whether a price key may pin at all. A Journey
    whose words chose one variant while the price names another is not a win,
    it is a case to read -- and the verdict must count it apart from agreement
    rather than letting it hide inside "the price pins one too"."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    version = _price_book(journey, {
        ("THAR ROXX", "MX1 D MT RWD"): "1423000.58",
        ("THAR ROXX", "MX3 D MT RWD"): "1623000.58",
    }, effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    # the words landed on MX3; the form's figure is MX1's price
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        mx3 = connection.execute(
            text("SELECT pli.product_sku_id FROM auditcore.price_list_items pli "
                 "JOIN auditcore.product_skus s ON s.product_sku_id = pli.product_sku_id "
                 "JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id "
                 "WHERE pli.price_list_version_id = :v AND pv.variant_name = 'MX3 D MT RWD'"),
            {"v": version},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, product_sku_id, "
                 "model_name_snapshot, variant_name_snapshot, selection_source) "
                 "VALUES (:t, :j, :s, 'THAR ROXX', 'MX3 D MT RWD', 'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": mx3},
        )
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")

    row = _verdict(journey)
    assert row["resolved_by_words"] == 1
    assert row["resolved_price_pins_one_too"] == 1
    assert row["resolved_price_agrees"] == 0
    assert row["resolved_price_disagrees"] == 1, "a contradiction must never read as a win"


def test_the_verdict_counts_agreement_when_the_price_names_the_pinned_variant(journey):
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    version = _price_book(journey, {
        ("THAR ROXX", "MX1 D MT RWD"): "1423000.58",
        ("THAR ROXX", "MX3 D MT RWD"): "1623000.58",
    }, effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        mx1 = connection.execute(
            text("SELECT pli.product_sku_id FROM auditcore.price_list_items pli "
                 "JOIN auditcore.product_skus s ON s.product_sku_id = pli.product_sku_id "
                 "JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id "
                 "WHERE pli.price_list_version_id = :v AND pv.variant_name = 'MX1 D MT RWD'"),
            {"v": version},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, product_sku_id, "
                 "model_name_snapshot, variant_name_snapshot, selection_source) "
                 "VALUES (:t, :j, :s, 'THAR ROXX', 'MX1 D MT RWD', 'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": mx1},
        )
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")

    row = _verdict(journey)
    assert row["resolved_price_agrees"] == 1
    assert row["resolved_price_disagrees"] == 0


def test_the_disagreement_report_names_the_journey_and_both_skus(journey):
    """The gating list for the price key: the SKU the words pinned beside the
    one the figure names, so each can be read rather than counted."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    version = _price_book(journey, {
        ("THAR ROXX", "MX1 D MT RWD"): "1423000.58",
        ("THAR ROXX", "MX3 D MT RWD"): "1623000.58",
    }, effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        mx3 = connection.execute(
            text("SELECT pli.product_sku_id FROM auditcore.price_list_items pli "
                 "JOIN auditcore.product_skus s ON s.product_sku_id = pli.product_sku_id "
                 "JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id "
                 "WHERE pli.price_list_version_id = :v AND pv.variant_name = 'MX3 D MT RWD'"),
            {"v": version},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, product_sku_id, "
                 "model_name_snapshot, variant_name_snapshot, selection_source, selection_method) "
                 "VALUES (:t, :j, :s, 'THAR ROXX', 'MX3 D MT RWD', 'EVIDENCE', 'WORDS_V2')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": mx3},
        )
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")

    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        [row] = connection.execute(
            text(REPORTS["price_disagreements"]), {"t": journey.tenant_id, "c": "%"},
        ).mappings().all()
    assert row["words_chose"] == "THAR ROXX ~ MX3 D MT RWD"
    assert row["price_names"] == "THAR ROXX ~ MX1 D MT RWD"
    assert float(row["figure_read"]) == 1423001
    assert float(row["book_price_of_that_sku"]) == 1623000.58
    assert row["document"] == "booking_form"
    assert float(row["gap"]) == 0.42


def test_the_disagreement_report_stays_silent_when_the_price_agrees(journey):
    """It must list contradictions only: a report that also lists agreement is
    a report nobody reads."""
    from datetime import date
    from uuid import uuid4

    from p2_support import add_extracted_field

    version = _price_book(journey, {("THAR ROXX", "MX1 D MT RWD"): "1423000.58"},
                          effective_from=date(2026, 10, 1))
    _booked_on(journey, date(2026, 10, 15))
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        mx1 = connection.execute(
            text("SELECT product_sku_id FROM auditcore.price_list_items "
                 "WHERE price_list_version_id = :v"), {"v": version},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO auditcore.journey_products (tenant_id, journey_id, product_sku_id, "
                 "model_name_snapshot, variant_name_snapshot, selection_source) "
                 "VALUES (:t, :j, :s, 'THAR ROXX', 'MX1 D MT RWD', 'EVIDENCE')"),
            {"t": journey.tenant_id, "j": journey.journey_id, "s": mx1},
        )
    add_extracted_field(journey, di_document_id=uuid4(), field_key="ex_showroom_price",
                        value="1423001", document_type="booking_form")

    with journey.engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, journey.tenant_id)
        rows = connection.execute(
            text(REPORTS["price_disagreements"]), {"t": journey.tenant_id, "c": "%"},
        ).mappings().all()
    assert rows == []


def test_a_small_report_logs_its_whole_answer_on_one_line(journey, monkeypatch):
    """A report returning a handful of rows is the answer to a question, and
    reading those rows back out of a log API that pages and cannot filter on
    structured attributes wasted most of a morning on 2026-10-24. The whole
    answer now goes out on one line as well."""
    import json as _json

    from audit_core import uc03_p2_diagnostics as diag

    lines: list[tuple[str, dict]] = []

    class _Log:
        def info(self, event, **kw):
            lines.append((event, kw))

        def warning(self, event, **kw):
            lines.append((event, kw))

    monkeypatch.setattr(diag, "logger", _Log())
    monkeypatch.setattr(diag, "_projects", lambda engine: [
        {"tenant_id": journey.tenant_id, "project_code": "X", "project_name": "X", "project_status": "ACTIVE"}])
    monkeypatch.setitem(diag.REPORTS, "_one_row", "SELECT 7 AS answer, NULL AS nothing")
    diag.run_diagnostics(journey.engine, ["_one_row"])

    [(_, answer)] = [line for line in lines if line[0] == "diag_answer"]
    assert answer["report"] == "_one_row" and answer["rows"] == 1
    assert _json.loads(answer["answer"]) == [{"answer": 7, "nothing": None}]


def test_a_large_report_gets_no_one_line_answer(journey, monkeypatch):
    """Only a small report; a page of rows on one line would be unreadable and
    would be truncated anyway."""
    from audit_core import uc03_p2_diagnostics as diag

    lines: list[str] = []

    class _Log:
        def info(self, event, **kw):
            lines.append(event)

        def warning(self, event, **kw):
            lines.append(event)

    monkeypatch.setattr(diag, "logger", _Log())
    monkeypatch.setattr(diag, "_projects", lambda engine: [
        {"tenant_id": journey.tenant_id, "project_code": "X", "project_name": "X", "project_status": "ACTIVE"}])
    monkeypatch.setattr(diag, "_ANSWER_ROWS", 2)
    monkeypatch.setitem(diag.REPORTS, "_many", "SELECT g AS n FROM generate_series(1, 9) g")
    diag.run_diagnostics(journey.engine, ["_many"])

    assert "diag_answer" not in lines
    assert lines.count("diag_row") == 9
