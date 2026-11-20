from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_deal_source_history import (
    load_source_breakdown,
    record_source_value,
)


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for deal-source-history integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dsh-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DSH-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DSH-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DSH', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DSH-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DSH-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DSH-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DSH-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def test_two_sources_for_the_same_component_both_persist(journey) -> None:
    c = journey
    record_source_value(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        line_kind="COMMERCIAL", component_key="accessories_cost",
        source_document_type="booking_form", amount=Decimal(30000),
    )
    record_source_value(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        line_kind="COMMERCIAL", component_key="accessories_cost",
        source_document_type="tax_invoice_tally", amount=Decimal(32000),
    )
    rows = load_source_breakdown(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    by_source = {r["sourceDocumentType"]: r["amount"] for r in rows}
    assert by_source == {"booking_form": Decimal(30000), "tax_invoice_tally": Decimal(32000)}


def test_reporting_the_same_source_again_updates_in_place_not_a_new_row(journey) -> None:
    c = journey
    record_source_value(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        line_kind="COMMERCIAL", component_key="ex_showroom_price",
        source_document_type="booking_form", amount=Decimal(1500000),
    )
    record_source_value(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        line_kind="COMMERCIAL", component_key="ex_showroom_price",
        source_document_type="booking_form", amount=Decimal(1510000),
    )
    rows = load_source_breakdown(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert len(rows) == 1
    assert rows[0]["amount"] == Decimal(1510000)


def test_none_amount_is_a_no_op(journey) -> None:
    c = journey
    record_source_value(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        line_kind="DISCOUNT", component_key="ACCESSORIES_KIT",
        source_document_type="booking_form", amount=None,
    )
    assert load_source_breakdown(c, tenant_id=c.tenant_id, journey_id=c.journey_id) == []


def test_commercial_and_discount_lines_do_not_collide_on_the_same_component_key(journey) -> None:
    c = journey
    record_source_value(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        line_kind="COMMERCIAL", component_key="accessories_cost",
        source_document_type="booking_form", amount=Decimal(30000),
    )
    record_source_value(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id,
        line_kind="DISCOUNT", component_key="accessories_cost",
        source_document_type="booking_form", amount=Decimal(25000),
    )
    rows = load_source_breakdown(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert len(rows) == 2
    by_kind = {r["lineKind"]: r["amount"] for r in rows}
    assert by_kind == {"COMMERCIAL": Decimal(30000), "DISCOUNT": Decimal(25000)}


def test_two_documents_of_one_type_keep_a_row_each(journey) -> None:
    """2026-10-20 (R2): the table is keyed per source document; a second
    invoice of the same type no longer overwrites the first. Two spellings
    of one type are one type."""
    c = journey
    first, second = uuid4(), uuid4()
    for document_id, amount in ((first, Decimal("852000.00")), (second, Decimal("862600.44"))):
        record_source_value(c, tenant_id=c.tenant_id, journey_id=c.journey_id, line_kind="COMMERCIAL",
                            component_key="ex_showroom_price", source_document_type="Customer Invoice DMS",
                            amount=amount, document_id=document_id)
    rows = load_source_breakdown(c, tenant_id=c.tenant_id, journey_id=c.journey_id)
    assert {(r["sourceDocumentType"], r["sourceDocumentId"], r["amount"]) for r in rows} == {
        ("customer_invoice_dms", first, Decimal("852000.00")),
        ("customer_invoice_dms", second, Decimal("862600.44")),
    }


# ── a value already on file as it stands is not written again inside a worker job
#    (2026-11-20, hosting cost) ────────────────────────────────────────────────
def _source_writes(c):
    from sqlalchemy import event

    seen: list[str] = []
    event.listen(c.engine, "before_cursor_execute", lambda conn, cur, s, p, ctx, m: seen.append(s))
    return lambda: sum("INSERT INTO auditcore.commercial_line_source_values" in s for s in seen)


def _record(c, amount, **over) -> None:
    record_source_value(c, **{
        "tenant_id": c.tenant_id, "journey_id": c.journey_id, "line_kind": "COMMERCIAL",
        "component_key": " Ex_Showroom_Price ", "source_document_type": "Tax Invoice Tally",
        "amount": amount, **over})


def _stored(c):
    return [tuple(row) for row in c.execute(text(
        "SELECT component_key, source_document_type, amount, source_document_id, source_evidence_id, "
        "withdrawn_at_utc, updated_at_utc FROM auditcore.commercial_line_source_values "
        "WHERE tenant_id=:t AND journey_id=:j ORDER BY 1, 2, 4"), {"t": c.tenant_id, "j": c.journey_id})]


def test_outside_a_job_every_value_is_written_as_before(journey) -> None:
    c = journey
    writes = _source_writes(c)
    _record(c, Decimal(1500000))
    _record(c, Decimal(1500000))
    assert writes() == 2


def test_inside_a_job_an_unchanged_value_is_not_written_again(journey) -> None:
    from audit_core import job_reads

    c = journey
    job_reads.install(c.engine)
    writes = _source_writes(c)
    document = uuid4()
    with job_reads.job():
        _record(c, Decimal(1500000))
        _record(c, Decimal(42000), component_key="insurance", document_id=document)
        before = _stored(c)
        assert writes() == 2
        for _ in range(3):  # the recompute of an unchanged Journey
            _record(c, Decimal("1500000.00"))
            _record(c, Decimal(42000), component_key="insurance", document_id=document)
        assert writes() == 2
        assert _stored(c) == before
        # A figure that moved, another document, another type: each is written.
        _record(c, Decimal(1510000))
        _record(c, Decimal(42000), component_key="insurance", document_id=uuid4())
        _record(c, Decimal(1500000), source_document_type="booking_form")
        assert writes() == 5
        assert len(_stored(c)) == 4
        assert {row[2] for row in _stored(c) if row[0] == "ex_showroom_price"} == {Decimal(1510000), Decimal(1500000)}


def test_inside_a_job_a_withdrawn_value_is_put_back(journey) -> None:
    from audit_core import job_reads

    c = journey
    job_reads.install(c.engine)
    writes = _source_writes(c)
    with job_reads.job():
        _record(c, Decimal(900))
        c.execute(text("UPDATE auditcore.commercial_line_source_values SET withdrawn_at_utc=now() "
                       "WHERE tenant_id=:t AND journey_id=:j"), {"t": c.tenant_id, "j": c.journey_id})
        _record(c, Decimal(900))
        assert writes() == 2
        assert [row[5] for row in _stored(c)] == [None]
