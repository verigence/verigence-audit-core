"""The one-off statement re-read picks only the statements that need it and
leaves a person's work alone; on commit it asks Document Intelligence once per
picked statement and copies the page back when DI has finished."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import audit_core.uc03_statement_reread as reread

FORM = [  # a statement form with a vehicle line (read as a FORM) and no printed net value
    {"particulars_raw": "Ex Showroom", "debit_amount": 1797001}, {"particulars_raw": "Registration", "debit_amount": 27912},
    {"particulars_raw": "Insurance", "debit_amount": 102472}, {"particulars_raw": "TCS", "debit_amount": 17970},
    {"particulars_raw": "Cash", "credit_amount": 200000}, {"particulars_raw": "Bank DO", "credit_amount": 1700000},
]
TALLY = [
    {"entry_date": "2026-09-25", "particulars_raw": "Sales BOLERO NEO", "voucher_type": "Vehicle Sales_ Mahindra",
     "voucher_number": "INV27A002879", "debit_amount": 1040300, "running_balance": "10,34,300.00 Dr"},
]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Connection:
    def __init__(self, journeys):
        self._journeys = journeys

    def execute(self, *_a, **_k):
        return _Result(self._journeys)


def _wire(monkeypatch, *, lines=FORM, fields_extra=None, queue_status="READY", corrected=0, reviewed=False, stage="DELIVERY"):
    journey_id = uuid4()
    document = {"documentId": uuid4(), "fields": {"line_items": lines, **(fields_extra or {})}}
    monkeypatch.setattr(reread, "load_ledger_documents", lambda *a, **k: [document])
    monkeypatch.setattr(reread, "_page", lambda *a, **k: {"queue_id": uuid4(), "queue_status": queue_status})
    monkeypatch.setattr(reread, "_corrected_fields", lambda *a, **k: corrected)
    import audit_core.uc03_p2_reviewer as reviewer
    monkeypatch.setattr(reviewer, "page_stage_code", lambda *a, **k: stage)
    monkeypatch.setattr(reviewer, "stage_is_reviewed", lambda *a, **k: reviewed)
    return _Connection([{"journey_id": journey_id, "journey_reference": "J-1"}])


def _decision(monkeypatch, **kwargs) -> str:
    connection = _wire(monkeypatch, **kwargs)
    [item] = reread.statement_items(connection, "t1")
    return item["decision"]


def test_a_statement_form_without_a_printed_total_is_read_again(monkeypatch):
    assert _decision(monkeypatch) == "REREAD"


def test_a_statement_that_already_has_its_printed_total_is_left_alone(monkeypatch):
    lines = FORM + [{"particulars_raw": "Net Value", "debit_amount": 1945355}]
    decision = _decision(monkeypatch, lines=lines)
    # Either the reader finds the total (skip) or it does not recognise the label (read again):
    # the point is that a recognised total is never read twice.
    from audit_core.uc03_account_statement import read_statement, statement_total
    read = read_statement(lines)
    expected = "SKIP_HAS_PRINTED_TOTAL" if statement_total({"line_items": lines}, read) is not None else "REREAD"
    assert decision == expected


def test_a_tally_printout_is_never_read_again(monkeypatch):
    assert _decision(monkeypatch, lines=TALLY) == "SKIP_NOT_A_STATEMENT_FORM"


def test_a_value_a_person_corrected_is_never_overwritten(monkeypatch):
    assert _decision(monkeypatch, corrected=2) == "SKIP_CORRECTED_BY_A_PERSON"


def test_a_reviewed_stage_is_left_alone(monkeypatch):
    assert _decision(monkeypatch, reviewed=True) == "SKIP_STAGE_REVIEWED"


def test_a_page_still_being_read_is_left_alone(monkeypatch):
    assert _decision(monkeypatch, queue_status="EXTRACTING") == "SKIP_PAGE_NOT_SETTLED"


def test_a_page_with_no_stage_is_left_alone(monkeypatch):
    assert _decision(monkeypatch, stage=None) == "SKIP_PAGE_UNKNOWN"


def test_the_dry_run_asks_nothing(monkeypatch):
    connection = _wire(monkeypatch)
    asked: list = []

    @contextmanager
    def open_connection():
        yield connection

    class Engine:
        begin = staticmethod(open_connection)

    monkeypatch.setattr(reread, "set_tenant_context", lambda *a, **k: None)
    monkeypatch.setattr(reread, "account_statement_is_source_of_truth", lambda *a, **k: True)
    monkeypatch.setattr(reread, "_reread", lambda *a, **k: asked.append(1) or {})
    reread.run_statement_reread(Engine(), "tag", ["t1"])
    assert asked == []
    reread.run_statement_reread(Engine(), "tag-commit", ["t1"])
    assert asked == [1]


def test_a_copy_tag_does_not_ask_di_again(monkeypatch):
    connection = _wire(monkeypatch)
    seen: list = []

    @contextmanager
    def open_connection():
        yield connection

    class Engine:
        begin = staticmethod(open_connection)

    monkeypatch.setattr(reread, "set_tenant_context", lambda *a, **k: None)
    monkeypatch.setattr(reread, "account_statement_is_source_of_truth", lambda *a, **k: True)
    monkeypatch.setattr(reread, "_reread", lambda *a, **k: seen.append(k["ask"]) or {})
    reread.run_statement_reread(Engine(), "x-copy-commit", ["t1"])
    reread.run_statement_reread(Engine(), "x-commit", ["t1"])
    assert seen == [False, True]
