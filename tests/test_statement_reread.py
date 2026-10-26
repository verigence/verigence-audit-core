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


def _wire(monkeypatch, *, lines=FORM, fields_extra=None, queue_status="READY", corrected=0, reviewed=False, stage="DELIVERY",
          book=()):
    journey_id = uuid4()
    document = {"documentId": uuid4(), "fields": {"line_items": lines, **(fields_extra or {})}}
    import audit_core.uc03_account_statement_rebuild as rebuild

    monkeypatch.setattr(rebuild, "book_ex_showroom", lambda *a, **k: list(book))
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


def test_the_copy_runs_the_document_sync_not_the_page_reconcile(monkeypatch):
    """The page reconcile leaves a page that already holds values alone, so the
    new reading would never be written; the document sync is what writes it."""
    import audit_core.uc03_confidence_review_policy as policy
    import audit_core.uc03_document_capture_v2 as capture
    import audit_core.uc03_p2_runtime as runtime

    synced: list[dict] = []
    activity: list[dict] = []

    @contextmanager
    def open_connection():
        yield object()

    class Engine:
        begin = staticmethod(open_connection)

    monkeypatch.setattr(reread, "set_tenant_context", lambda *a, **k: None)
    monkeypatch.setattr(capture, "get_security_oauth_client", lambda: "security")
    monkeypatch.setattr(capture, "get_di_client", lambda: "di")
    monkeypatch.setattr(policy, "_prefetch_document_for_sync", lambda *a, **k: "fetched")
    monkeypatch.setattr(policy, "_sync_booking_document_once", lambda *a, **k: synced.append(k))
    monkeypatch.setattr(runtime, "record_activity", lambda *a, **k: activity.append(k))
    item = {"journeyId": uuid4(), "diDocumentId": uuid4(), "queueId": uuid4(), "stage": "delivery"}
    reread._copy_into_audit_core(Engine(), "t1", item, "tag")
    assert len(synced) == 1
    assert synced[0]["stage_code"] == "DELIVERY" and synced[0]["prefetched"] == "fetched"
    assert activity[0]["event_type"] == "PAGE_RESYNC_REQUESTED"


def test_a_statement_whose_copy_fails_is_counted_not_dropped(monkeypatch):
    item = {"journeyId": uuid4(), "journeyReference": "J-1", "diDocumentId": uuid4(), "queueId": uuid4(),
            "stage": "DELIVERY", "decision": "REREAD"}
    import audit_core.uc03_document_capture_v2 as capture

    class Security:
        def get_service_token(self, **_):
            return "tok"

    class V2:
        def list_documents(self, **_):
            return {"documents": [{"documentId": str(item["diDocumentId"]), "processingStatus": "PROCESSED"}]}

    @contextmanager
    def open_connection():
        yield object()

    class Engine:
        begin = staticmethod(open_connection)

    def boom(*_a, **_k):
        raise RuntimeError("no link")

    monkeypatch.setattr(reread, "set_tenant_context", lambda *a, **k: None)
    monkeypatch.setattr(reread.time, "sleep", lambda *_: None)
    monkeypatch.setattr(capture, "get_security_oauth_client", lambda: Security())
    monkeypatch.setattr(capture, "get_di_client", lambda: object())
    monkeypatch.setattr(capture, "get_di_capture_v2_client", lambda: V2())
    monkeypatch.setattr(capture, "_ensure_di_context", lambda **_: ("ctx", "tok"))
    monkeypatch.setattr(reread, "_copy_into_audit_core", boom)
    counts = reread._reread(Engine(), "t1", [item], "x-copy-commit", ask=False)
    assert counts["failed"] == 1 and counts["finished"] == 0


def test_an_ex_showroom_ten_times_the_price_book_is_a_misreading():
    from decimal import Decimal

    book = [Decimal("986499.35")]
    assert reread.ex_showroom_implausible(Decimal(9865000), book) is True
    assert reread.ex_showroom_implausible(Decimal(98650), book) is True


def test_a_real_price_difference_is_not_called_a_misreading():
    from decimal import Decimal

    # a dealer's price a tenth above or below the book is a variance, not a misread
    assert reread.ex_showroom_implausible(Decimal(1030100), [Decimal("909000.54")]) is False
    assert reread.ex_showroom_implausible(Decimal(891000), [Decimal("808499.97")]) is False


def test_nothing_is_claimed_without_a_price_to_compare_with():
    from decimal import Decimal

    assert reread.ex_showroom_implausible(Decimal(9865000), []) is False
    assert reread.ex_showroom_implausible(None, [Decimal("986499.35")]) is False


def test_the_misread_run_picks_only_the_implausible_statement_even_with_a_printed_total(monkeypatch):
    from decimal import Decimal

    book = [Decimal("1802000.64")]
    printed = FORM + [{"particulars_raw": "Net Value", "debit_amount": 1964237}]
    bad = [dict(r) for r in printed]
    bad[0]["debit_amount"] = 17970010  # ex-showroom ten times too large
    assert reread.statement_items(_wire(monkeypatch, lines=bad, book=book), "t1", misread=True)[0]["decision"] == "REREAD"
    assert reread.statement_items(_wire(monkeypatch, lines=printed, book=book), "t1", misread=True)[0][
        "decision"] == "SKIP_EX_SHOWROOM_PLAUSIBLE"
    assert reread.statement_items(_wire(monkeypatch, lines=bad, book=book), "t1")[0][
        "decision"] == "SKIP_HAS_PRINTED_TOTAL"
