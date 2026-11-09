"""One line of a table field corrected on its own (product owner 2026-11-09).

The pure row logic, then the endpoint against real Postgres, calling the
endpoint function directly the way the other Phase 2 API tests do.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    AllowAllAuthorization,
    add_extracted_field,
    add_page,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import line_rows
from audit_core.uc03_line_item_rows import (
    RowChangeError,
    RowConflictError,
    apply_row_change,
    clean_row,
    is_table_field,
    parse_rows,
)
from audit_core.uc03_p2_documents import (
    P2LineItemChangeCommand,
    change_p2_document_line_item,
)

# The Shiva Construction NDC's payments, as DI stored them (JSON text).
NDC_ROWS = [
    {"particulars_raw": "BOOKING AMOUNT", "voucher_type": "UPI", "credit_amount": 21000},
    {"particulars_raw": "FINANCER AMOUNT", "voucher_type": "RTGS", "credit_amount": 1920000},
    {"particulars_raw": "DEPOSITED INTO BANK", "voucher_type": "NEFT", "credit_amount": 225541},
    {"particulars_raw": "DEPOSITED INTO BANK", "voucher_type": "Card", "credit_amount": 14872},
]


# ── the row logic ────────────────────────────────────────────────────────────

def test_rows_are_read_from_every_shape_di_left():
    assert parse_rows(json.dumps(NDC_ROWS)) == NDC_ROWS
    assert parse_rows(NDC_ROWS) == NDC_ROWS
    # Python repr from the old adapter bug (None, single quotes).
    assert parse_rows("[{'particulars_raw': 'RSA', 'debit_amount': 2021, 'credit_amount': None}]") == [
        {"particulars_raw": "RSA", "debit_amount": 2021, "credit_amount": None}
    ]
    assert parse_rows("12345") is None
    assert parse_rows("[1, 2]") is None
    assert parse_rows(None) is None


def test_only_array_fields_holding_rows_are_tables():
    assert is_table_field("array", json.dumps(NDC_ROWS))
    assert is_table_field("array", None)
    assert not is_table_field("array", '["insurance", "receipt"]')
    assert not is_table_field("string", json.dumps(NDC_ROWS))


def test_one_line_is_corrected_and_the_others_are_untouched():
    rows, before, after = apply_row_change(
        NDC_ROWS, operation="UPDATE", row_index=3, expected_row=NDC_ROWS[3],
        row={**NDC_ROWS[3], "credit_amount": "14,782"},
    )
    assert before == NDC_ROWS[3]
    assert after["credit_amount"] == 14782
    assert rows[:3] == NDC_ROWS[:3]
    assert rows[3]["credit_amount"] == 14782


def test_a_line_is_added_and_removed():
    rows, _, added = apply_row_change(
        NDC_ROWS, operation="ADD", row_index=None, expected_row=None,
        row={"particulars_raw": "RSA", "debit_amount": "2021"},
    )
    assert rows[-1] == added == {"particulars_raw": "RSA", "debit_amount": 2021}
    rows, removed, _ = apply_row_change(
        rows, operation="REMOVE", row_index=4, expected_row={"particulars_raw": "RSA", "debit_amount": "2021"},
        row=None,
    )
    assert removed["particulars_raw"] == "RSA"
    assert rows == NDC_ROWS


def test_a_line_changed_by_someone_else_is_refused():
    with pytest.raises(RowConflictError):
        apply_row_change(NDC_ROWS, operation="UPDATE", row_index=3, expected_row=NDC_ROWS[2],
                         row={**NDC_ROWS[3], "credit_amount": 1})
    with pytest.raises(RowConflictError):
        apply_row_change(NDC_ROWS, operation="REMOVE", row_index=9, expected_row=NDC_ROWS[0], row=None)


def test_nothing_changed_or_an_empty_line_is_refused():
    with pytest.raises(RowChangeError):
        apply_row_change(NDC_ROWS, operation="UPDATE", row_index=0, expected_row=NDC_ROWS[0],
                         row={**NDC_ROWS[0], "credit_amount": "21,000"})
    with pytest.raises(RowChangeError):
        clean_row({"particulars_raw": "  ", "credit_amount": ""})
    with pytest.raises(RowChangeError):
        clean_row({"bad key!": "x"})


def test_figures_stay_numbers_and_text_stays_text():
    row = clean_row({"particulars_raw": "FASTAG", "debit_amount": "500/-", "voucher_type": "1234"})
    assert row == {"particulars_raw": "FASTAG", "debit_amount": 500, "voucher_type": "1234"}


def test_the_checks_read_a_corrected_table_like_a_read_one():
    rows, _, _ = apply_row_change(NDC_ROWS, operation="ADD", row_index=None, expected_row=None,
                                  row={"particulars_raw": "RSA", "debit_amount": "2021"})
    assert line_rows(rows) == rows
    assert line_rows(json.dumps(rows)) == rows


# ── the endpoint ─────────────────────────────────────────────────────────────

@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2li")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _ndc(journey):
    _, _, document = add_page(journey, page_number=1, status="READY")
    add_extracted_field(
        journey, di_document_id=document, field_key="line_items", value=json.dumps(NDC_ROWS),
        document_type="customer_ledger", confidence=92.0, canonical_field_id="cf-line_items",
    )
    return document


def _change(journey, document, **command):
    with journey.engine.begin() as connection:
        return change_p2_document_line_item(
            tenant_id=journey.tenant_id,
            journey_id=journey.journey_id,
            document_id=document,
            field_key=command.pop("field_key", "line_items"),
            command=P2LineItemChangeCommand(canonicalFieldId=command.pop("canonical", "cf-line_items"),
                                            sourceFactVersion=1, **command),
            human_principal=SimpleNamespace(subject=journey.actor_id),
            authorization_client=AllowAllAuthorization(role_key="PC"),
            connection=connection,
        )


def _stored(journey, document):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        field = connection.execute(
            text("SELECT extracted_value, effective_value, is_modified FROM auditcore.journey_document_extracted_fields "
                 "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND field_key='line_items'"),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": document},
        ).mappings().one()
        log = connection.execute(
            text("SELECT previous_value, new_value, remarks FROM auditcore.journey_field_change_log "
                 "WHERE tenant_id=:t AND journey_id=:j ORDER BY 1"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().all()
        events = connection.execute(
            text("SELECT details FROM auditcore.p2_activity_events "
                 "WHERE tenant_id=:t AND journey_id=:j AND event_type='LINE_ITEM_CHANGED'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalars().all()
    return field, log, events


def test_one_line_of_the_ndc_is_corrected_through_the_ordinary_correction(journey):
    document = _ndc(journey)
    result = _change(journey, document, operation="UPDATE", rowIndex=3, expectedRow=NDC_ROWS[3],
                     row={**NDC_ROWS[3], "credit_amount": "14782"}, remarks="Card slip says 14,782")

    assert result["applied"] is True and result["line"] == 4
    field, log, events = _stored(journey, document)
    assert field["is_modified"] is True
    assert line_rows(field["effective_value"])[3]["credit_amount"] == 14782
    assert line_rows(field["effective_value"])[:3] == NDC_ROWS[:3]
    # The machine reading is kept as it was.
    assert line_rows(field["extracted_value"]) == NDC_ROWS
    assert len(log) == 1 and log[0]["remarks"] == "Line 4 corrected: Card slip says 14,782"
    assert events[0]["before"]["credit_amount"] == 14872 and events[0]["after"]["credit_amount"] == 14782


def test_a_missing_charge_is_added_as_a_line(journey):
    document = _ndc(journey)
    _change(journey, document, operation="ADD", row={"particulars_raw": "RSA", "debit_amount": "2021"})
    field, _, _ = _stored(journey, document)
    assert line_rows(field["effective_value"])[-1] == {"particulars_raw": "RSA", "debit_amount": 2021}
    assert len(line_rows(field["effective_value"])) == 5


def test_two_changes_in_a_row_build_on_each_other(journey):
    document = _ndc(journey)
    _change(journey, document, operation="REMOVE", rowIndex=0, expectedRow=NDC_ROWS[0])
    _change(journey, document, operation="UPDATE", rowIndex=0, expectedRow=NDC_ROWS[1],
            row={**NDC_ROWS[1], "voucher_type": "NEFT"})
    field, log, _ = _stored(journey, document)
    rows = line_rows(field["effective_value"])
    assert len(rows) == 3 and rows[0]["voucher_type"] == "NEFT"
    assert len(log) == 2


def test_a_stale_line_is_refused_and_nothing_is_written(journey):
    document = _ndc(journey)
    with pytest.raises(HTTPException) as refused:
        _change(journey, document, operation="UPDATE", rowIndex=3, expectedRow=NDC_ROWS[0],
                row={**NDC_ROWS[3], "credit_amount": "1"})
    assert refused.value.status_code == 409
    field, log, _ = _stored(journey, document)
    assert field["is_modified"] is not True and log == []


def test_a_date_in_a_line_cannot_be_after_today(journey):
    document = _ndc(journey)
    with pytest.raises(HTTPException) as refused:
        _change(journey, document, operation="ADD",
                row={"particulars_raw": "RECEIPT", "entry_date": "2099-01-01", "credit_amount": "5"})
    assert refused.value.status_code == 422


def test_a_value_that_is_not_a_table_is_refused(journey):
    _, _, document = add_page(journey, page_number=1, status="READY")
    add_extracted_field(journey, di_document_id=document, field_key="customer_name", value="SHIVA CONSTRUCTION",
                        document_type="customer_ledger", canonical_field_id="cf-customer_name")
    with pytest.raises(HTTPException) as refused:
        _change(journey, document, field_key="customer_name", canonical="cf-customer_name",
                operation="ADD", row={"particulars_raw": "X"})
    assert refused.value.status_code == 422


def test_the_document_review_gives_a_table_field_its_lines(journey, monkeypatch):
    from p2_support import principal

    from audit_core import uc03_p2_documents as documents
    from audit_core.di_client import DiDocument, DiFact

    document = _ndc(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET classified_document_type='customer_ledger', "
                 "template_key='customer_ledger' WHERE tenant_id=:t AND di_document_id=:d"),
            {"t": journey.tenant_id, "d": document},
        )
    _change(journey, document, operation="ADD", row={"particulars_raw": "FASTAG", "debit_amount": "500"})
    monkeypatch.setattr(documents, "_ensure_di_context", lambda **_: ("ctx", "token"))

    class Di:
        def get_audit_document(self, **_):
            return DiDocument(document_id=str(document), upload_status="UPLOADED",
                              processing_status="PROCESSED", confirmation_status="UNCONFIRMED")

        def get_audit_document_facts(self, **_):
            return (
                DiFact(canonical_field_id="cf-line_items", field_key="line_items", value=json.dumps(NDC_ROWS),
                       value_source="EXTRACTED", confidence_score=92.0, version_no=1),
                DiFact(canonical_field_id="cf-customer_name", field_key="customer_name", value="SHIVA CONSTRUCTION",
                       value_source="EXTRACTED", confidence_score=92.0, version_no=1),
            )

    review = documents.get_p2_document_review(
        tenant_id=journey.tenant_id, journey_id=journey.journey_id, document_id=document,
        human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
        engine=journey.engine,
        security_client=SimpleNamespace(get_service_token=lambda **_: "token"),
        di_client=Di(),
    )
    lines = next(f for f in review["fields"] if f["fieldKey"] == "line_items")
    name = next(f for f in review["fields"] if f["fieldKey"] == "customer_name")
    assert lines["extractedRows"] == NDC_ROWS
    assert lines["rows"] == [*NDC_ROWS, {"particulars_raw": "FASTAG", "debit_amount": 500}]
    assert name["rows"] is None
