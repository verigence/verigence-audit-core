from __future__ import annotations

import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text

from audit_core import uc03_duplicate_receipt_detection as drd


def _record(document_id, *, receipt_number=None, amount="21000.00", receipt_date="2026-08-12"):
    return drd.ReceiptRecord(
        document_id=document_id,
        stage_code="BOOKING",
        document_type_key="dealer_receipt",
        receipt_number=receipt_number,
        amount=Decimal(amount) if amount is not None else None,
        receipt_date=receipt_date,
    )


def test_compute_duplicate_groups_matches_same_receipt_number_and_amount_and_date():
    """The exact shape reported live: same receipt number, amount, and date
    on two documents. Must be flagged with dates_match=True."""
    a, b = uuid4(), uuid4()
    groups = drd.compute_duplicate_groups([
        _record(a, receipt_number="AMC-B/20186/26-27"),
        _record(b, receipt_number="AMC-B/20186/26-27"),
    ])
    assert len(groups) == 1
    assert groups[0].match_basis == "RECEIPT_NUMBER_AND_AMOUNT"
    assert groups[0].dates_match is True
    assert {d.document_id for d in groups[0].documents} == {a, b}


def test_compute_duplicate_groups_still_matches_when_only_date_differs():
    """Same receipt number and amount is still flagged even if one scan's
    date was misread -- dealers do not reuse receipt numbers, so this is
    still almost certainly the same physical receipt. dates_match reports
    the discrepancy rather than suppressing the finding."""
    a, b = uuid4(), uuid4()
    groups = drd.compute_duplicate_groups([
        _record(a, receipt_number="AMC-B/20186/26-27", receipt_date="2026-08-12"),
        _record(b, receipt_number="AMC-B/20186/26-27", receipt_date="2026-08-13"),
    ])
    assert len(groups) == 1
    assert groups[0].dates_match is False


def test_compute_duplicate_groups_no_match_for_distinct_receipt_numbers():
    a, b = uuid4(), uuid4()
    groups = drd.compute_duplicate_groups([
        _record(a, receipt_number="AMC-B/20186/26-27"),
        _record(b, receipt_number="AMC-B/20222/26-27"),
    ])
    assert groups == []


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for duplicate-receipt integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-drd-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DRD-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DRD-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DRD', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DRD-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DRD-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DRD-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DRD-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.tenant_id = tenant_id  # type: ignore[attr-defined]
            c.journey_id = journey_id  # type: ignore[attr-defined]
            c.customer_id = customer_id  # type: ignore[attr-defined]
            yield c
    finally:
        delete_tenant_data(engine, tenant_id)
        engine.dispose()


def _set_receipt_field(c, *, stage_code, document_type_key, field_key, value, document_id):
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
        {"t": c.tenant_id, "j": c.journey_id, "doc": document_id, "stage": stage_code,
         "dtk": document_type_key, "fk": field_key, "v": json.dumps(value)},
    )


def _seed_receipt(c, *, stage_code, document_type_key, receipt_number=None, amount, receipt_date, document_id=None,
                  status="ACTIVE"):
    """A receipt on the booking (ACTIVE evidence, the check reads only those
    since 2026-11-01) with its read fields; ``status`` for a copy that is not."""
    if document_id is None:
        document_id = uuid4()
        c.execute(
            text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
                 "document_type_key, evidence_purpose, process_area, association_status) "
                 "VALUES (:t, :j, :c, :s, :d, :k, 'JOURNEY_DOCUMENT', :p, :st)"),
            {"t": c.tenant_id, "j": c.journey_id, "c": c.customer_id, "s": uuid4(), "d": document_id,
             "k": document_type_key, "p": stage_code, "st": status},
        )
    if receipt_number is not None:
        _set_receipt_field(c, stage_code=stage_code, document_type_key=document_type_key,
                            field_key="receipt_number", value=receipt_number, document_id=document_id)
    _set_receipt_field(c, stage_code=stage_code, document_type_key=document_type_key,
                        field_key="amount_paid", value=amount, document_id=document_id)
    _set_receipt_field(c, stage_code=stage_code, document_type_key=document_type_key,
                        field_key="receipt_date", value=receipt_date, document_id=document_id)
    return document_id


def _open_duplicate_tasks(c) -> list[dict]:
    return [
        dict(row)
        for row in c.execute(
            text("SELECT task_type, process_area, assigned_role_code, task_payload, task_status "
                 "FROM auditcore.workflow_tasks "
                 "WHERE tenant_id=:t AND journey_id=:j AND task_type='DUPLICATE_RECEIPT_NOTICE' "
                 "AND task_status IN ('PENDING','READY','CLAIMED','IN_PROGRESS','RETRY_WAIT')"),
            {"t": c.tenant_id, "j": c.journey_id},
        ).mappings().all()
    ]


def test_same_receipt_uploaded_five_times_is_one_duplicate_task(journey) -> None:
    c = journey
    for _ in range(5):
        _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                      receipt_number="RC-1001", amount="200000", receipt_date="2026-08-01")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 1
    assert result["raised"] == 1
    tasks = _open_duplicate_tasks(c)
    assert len(tasks) == 1
    assert tasks[0]["process_area"] == "BOOKING"
    assert tasks[0]["assigned_role_code"] == "PC"
    assert tasks[0]["task_payload"]["matchBasis"] == "RECEIPT_NUMBER_AND_AMOUNT"

    # Not in Audit at all -- this is a Task Queue item, not a
    # rule-classified finding.
    assert c.execute(
        text("SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id=:t"),
        {"t": c.tenant_id},
    ).scalar_one() == 0

    # idempotent
    again = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert again["raised"] == 1
    assert len(_open_duplicate_tasks(c)) == 1


def test_different_receipt_numbers_same_amount_are_not_flagged(journey) -> None:
    # Two genuinely separate real payments of the same round amount, each
    # with its own distinct receipt number, must not be flagged.
    c = journey
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-2001", amount="100000", receipt_date="2026-08-01")
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-2002", amount="100000", receipt_date="2026-08-05")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 0
    assert _open_duplicate_tasks(c) == []


def test_dealer_receipt_and_payment_receipt_same_receipt_are_cross_flagged(journey) -> None:
    """Direct user correction (2026-09-23): duplication has no relationship
    with stage. dealer_receipt and payment_receipt are the same real-world
    document under two different document_type_keys (see
    _DOCUMENT_TYPE_ALIASES) -- the same receipt number and amount on one of
    each is the same physical receipt uploaded twice, regardless of which
    stage each copy landed in, and must be flagged exactly like two
    same-type copies would be."""
    c = journey
    doc_a = _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                          receipt_number="RC-3001", amount="100000", receipt_date="2026-08-01")
    doc_b = _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                          receipt_number="RC-3001", amount="100000", receipt_date="2026-08-01")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 1
    tasks = _open_duplicate_tasks(c)
    assert len(tasks) == 1
    assert set(tasks[0]["task_payload"]["diDocumentIds"]) == {str(doc_a), str(doc_b)}


def test_two_genuinely_separate_payments_at_different_stages_are_not_flagged(journey) -> None:
    # Two different real payments (different receipt numbers) at different
    # stages must not be flagged just because they're both receipts now --
    # cross-stage matching only fires on a real receipt-number+amount match,
    # same as it always has within one stage.
    c = journey
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-4001", amount="100000", receipt_date="2026-08-01")
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  receipt_number="RC-4002", amount="100000", receipt_date="2026-09-15")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 0
    assert _open_duplicate_tasks(c) == []


def test_no_receipt_number_falls_back_to_amount_and_date(journey) -> None:
    c = journey
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-10")
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-10")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 1
    tasks = _open_duplicate_tasks(c)
    assert len(tasks) == 1
    assert tasks[0]["task_payload"]["matchBasis"] == "AMOUNT_AND_DATE_NO_RECEIPT_NUMBER"


def test_no_receipt_number_different_dates_are_not_flagged(journey) -> None:
    c = journey
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-10")
    _seed_receipt(c, stage_code="DELIVERY", document_type_key="payment_receipt",
                  amount="150000", receipt_date="2026-08-15")

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["groupCount"] == 0
    assert _open_duplicate_tasks(c) == []


def test_correction_that_breaks_the_match_cancels_the_task(journey) -> None:
    c = journey
    doc_a = _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                          receipt_number="RC-4001", amount="200000", receipt_date="2026-08-01")
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="RC-4001", amount="200000", receipt_date="2026-08-01")

    drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert len(_open_duplicate_tasks(c)) == 1

    # A PC correction reveals these were actually two different receipt numbers.
    c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields "
             "SET effective_value = CAST(:v AS jsonb) "
             "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc AND field_key='receipt_number'"),
        {"v": json.dumps("RC-4001-A"), "t": c.tenant_id, "j": c.journey_id, "doc": doc_a},
    )

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["resolved"] == 1
    assert _open_duplicate_tasks(c) == []
    status = c.execute(
        text("SELECT task_status FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND journey_id=:j AND task_type='DUPLICATE_RECEIPT_NOTICE'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert status == "CANCELLED"


def test_legacy_finding_still_resolves_after_the_task_queue_move(journey) -> None:
    """A DUPLICATE_RECEIPT finding raised before this producer moved onto
    the Task Queue must still resolve once the correction breaks the
    match -- nothing else in the codebase ever closes it any more."""
    c = journey
    doc_a = _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                          receipt_number="RC-5001", amount="200000", receipt_date="2026-08-01")
    c.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, finding_type_code, severity, finding_status,
                title, stage_code, origin_kind, origin_role_snapshot, rule_key,
                finding_class, owner_role_code
            ) VALUES (
                :t, :j, 'DUPLICATE_RECEIPT', 'HIGH', 'OPEN', 'legacy test',
                'BOOKING', 'MACHINE', 'SYSTEM', :rule_key, 'VIOLATION', 'TL'
            )
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id,
         "rule_key": "DUPLICATE_RECEIPT:dealer_receipt:RC-5001:200000"},
    )

    # The correction that would have broken this match, had it still been
    # a group of two -- resolving purely from current_rule_keys no longer
    # containing this rule_key.
    c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields "
             "SET effective_value = CAST(:v AS jsonb) "
             "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc AND field_key='receipt_number'"),
        {"v": json.dumps("RC-5001-A"), "t": c.tenant_id, "j": c.journey_id, "doc": doc_a},
    )

    result = drd.sync_duplicate_receipt_detection(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result["resolved"] == 1
    status = c.execute(
        text("SELECT finding_status FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='DUPLICATE_RECEIPT'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert status == "RESOLVED"


def test_a_copy_no_longer_on_the_booking_is_not_a_duplicate(journey) -> None:
    """2026-11-01, UTK-SU-261020-002: the copy a re-upload replaced kept its
    read fields and was paired with its replacement as "the same receipt
    uploaded twice". Only receipts on the booking count."""
    c = journey
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="PLSM/2627/01014", amount="11000", receipt_date="2026-10-20", status="SUPERSEDED")
    _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                  receipt_number="PLSM/2627/01014", amount="11000", receipt_date="2026-10-20")
    result = drd.sync_duplicate_receipt_detection(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["raised"] == 0 and result["examined"] == 1 and result["groups"] == []


def test_the_check_says_which_receipt_is_on_the_booking_twice(journey) -> None:
    """2026-11-01: the check's task read "The audit check failed. Review the
    related documents." and named no receipt."""
    c = journey
    first = _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                          receipt_number="AMC-B/28968/26-27", amount="100000", receipt_date="2026-10-22")
    second = _seed_receipt(c, stage_code="BOOKING", document_type_key="dealer_receipt",
                           receipt_number="AMC-B/28968/26-27", amount="100000", receipt_date="2026-10-22")
    result = drd.sync_duplicate_receipt_detection(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    finding = drd.duplicate_receipt_finding(result["groups"])
    assert finding["findingTitle"] == "Receipt AMC-B/28968/26-27 uploaded 2 times"
    assert finding["findingDescription"].startswith(
        "Receipt AMC-B/28968/26-27 for ₹1,00,000, dated 22 Oct 2026, is on the booking 2 times. "
        "It is counted once toward what the customer paid.")
    assert set(finding["documentIds"]) == {str(first), str(second)}


def test_copies_read_with_different_dates_are_each_still_counted():
    a, b = uuid4(), uuid4()
    groups = [drd.describe_group(g) for g in drd.compute_duplicate_groups([
        _record(a, receipt_number="BRMM/2627/08564", receipt_date="2026-10-20"),
        _record(b, receipt_number="BRMM/2627/08564", receipt_date="2026-10-21"),
    ])]
    text_ = drd.duplicate_receipt_finding(groups)["findingDescription"]
    assert "read with different dates (20 Oct 2026, 21 Oct 2026)" in text_
    assert "Each copy is still counted" in text_
    assert drd.duplicate_receipt_finding([]) == {}


# ---------------------------------------------------------------- 2026-11-26: a copy read differently

def _copy(receipt_number=None, *, amount="199000.00", receipt_date="2026-10-26", reference=None):
    return drd.ReceiptRecord(
        document_id=uuid4(), stage_code="BOOKING", document_type_key="receipt", receipt_number=receipt_number,
        amount=Decimal(amount), receipt_date=receipt_date, reference=drd.normalize_bank_reference(reference))


def test_a_copy_with_its_number_read_differently_or_not_read_is_the_same_receipt():
    """Product owner 2026-11-26 (UTK-JP-261031-007: "1,99,000 is coming duplicate
    why"): the receipts uploaded on 31 Oct were uploaded again on 10 Nov. The
    cash receipt was read once with no number and once as CRBM/2627/00927; the
    RTGS receipt as SM/2627/04806 and as BRBM/2627/04806. Each was counted twice."""
    cash_unread, cash = _copy(None), _copy("CRBM/2627/00927")
    rtgs_first, rtgs_again = _copy("SM/2627/04806", amount="500000"), _copy("BRBM/2627/04806", amount="500000")
    other = _copy("RBM/2627/04802", amount="500000", reference="SBIN326299553270")
    found = drd.repeated_receipts_basis([cash_unread, rtgs_first, other, cash, rtgs_again])
    assert found == {
        # The numbered copy is the one counted, whichever was uploaded first.
        cash_unread.document_id: (cash.document_id, drd.BASIS_NOT_READ),
        rtgs_again.document_id: (rtgs_first.document_id, drd.BASIS_LETTERS),
    }
    assert drd.repeated_receipts([cash_unread, rtgs_first, other, cash, rtgs_again]) == {
        cash_unread.document_id: cash.document_id, rtgs_again.document_id: rtgs_first.document_id}


def test_the_running_digits_of_a_receipt_number():
    assert drd.receipt_digits("BRBM/2627/04806") == drd.receipt_digits("SM/2627/4806") == "2627/4806"
    assert drd.receipt_digits("CRBM/2627/00927") != drd.receipt_digits("CRBM/2627/00928")
    assert drd.receipt_digits("A-12") is None and drd.receipt_digits(None) is None  # too few digits to match on


def test_separate_payments_are_not_joined():
    # Two numbered receipts of the same amount and day: a copy with no number is left counted.
    a, b, unread = _copy("CRBM/2627/00927"), _copy("CRBM/2627/00931"), _copy(None)
    assert drd.repeated_receipts([a, b, unread]) == {}
    # Another day, or another amount: not the same receipt.
    assert drd.repeated_receipts([_copy("CRBM/2627/00927"), _copy(None, receipt_date="2026-10-27")]) == {}
    assert drd.repeated_receipts([_copy("SM/2627/04806"), _copy("BRBM/2627/04806", amount="5000")]) == {}
    assert drd.repeated_receipts([_copy("SM/2627/04806"), _copy("BRBM/2627/04806", receipt_date="2026-10-27")]) == {}
    # Bank references read on both and different: two payments, whatever the numbers.
    first = _copy("SM/2627/04806", reference="SBIN326299553270")
    assert drd.repeated_receipts([first, _copy("BRBM/2627/04806", reference="UTIB426299001122")]) == {}
    assert drd.repeated_receipts([first, _copy(None, reference="UTIB426299001122")]) == {}


def test_the_same_bank_reference_is_one_payment_here_too():
    """Decided in this module since 2026-11-26, so the booking / delivery split
    reads it as the money totals do (live 2026-11-04, UTK-SA-261103-008)."""
    a = _copy("CDAM/2627/02127", amount="75000", reference="CR/828411245393 TILOTTAM")
    b = _copy("CDAM/2026/0212", amount="75000", reference="CR/828411245393 TILOTTAM")
    assert drd.repeated_receipts_basis([a, b]) == {b.document_id: (a.document_id, drd.BASIS_REFERENCE)}
    # A reference too short to name one payment does not join anything.
    c, d = _copy("X/1", amount="75000", reference="12345"), _copy("X/2", amount="75000", reference="12345")
    assert drd.repeated_receipts([c, d]) == {}


def test_the_notice_says_how_a_copy_was_recognised():
    cash_unread, cash = _copy(None), _copy("CRBM/2627/00927")
    rtgs_first, rtgs_again = _copy("SM/2627/04806", amount="500000"), _copy("BRBM/2627/04806", amount="500000")
    texts = {g.match_basis: drd._notice_text(g)
             for g in drd.compute_duplicate_groups([cash_unread, cash, rtgs_first, rtgs_again])}
    assert "the number on the other was not read" in texts[drd.BASIS_NOT_READ]
    assert "SM/2627/04806, BRBM/2627/04806" in texts[drd.BASIS_LETTERS]
