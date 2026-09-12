"""Phase 4 of the rule-engine platform: DOCUMENT_SYNCED producers start
writing PASS/FAIL/SKIPPED rows to auditcore.rule_executions, not just a
finding on FAIL. WRONG_DOCUMENT and DUPLICATE_RECEIPT are instrumented so
far (see _sync_booking_document's identity-consistency and duplicate-
receipt call sites) -- this is their end-to-end coverage, calling the real
per-document sync pipeline with a fake DI/Security client, matching
test_uc03_sku_resolution_ordering.py's established pattern for exercising
_sync_booking_document directly.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core.di_client import DiDocument, DiFact


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "fake-token"


class _FakeDiClient:
    def __init__(self) -> None:
        self._documents: dict[str, DiDocument] = {}
        self._facts: dict[str, list[DiFact]] = {}

    def add(self, document: DiDocument, facts: list[DiFact]) -> None:
        self._documents[document.document_id] = document
        self._facts[document.document_id] = facts

    def get_audit_document(self, *, document_id: str, **kwargs) -> DiDocument:
        return self._documents[document_id]

    def get_audit_document_facts(self, *, document_id: str, **kwargs) -> list[DiFact]:
        return self._facts[document_id]


def _fact(field_key: str, value: str, confidence: float = 92.0) -> DiFact:
    return DiFact(
        canonical_field_id=field_key, field_key=field_key, value=value,
        value_source="EXTRACTION", confidence_score=confidence, version_no=1,
    )


def _confirmed(document_id, document_type_key: str) -> DiDocument:
    return DiDocument(
        document_id=str(document_id), upload_status="COMPLETE",
        processing_status="COMPLETED", confirmation_status="CONFIRMED",
        document_type_key=document_type_key, verification_state="NOT_VERIFIED",
    )


@pytest.fixture
def synced_document_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-desl-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DESL-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DESL-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date)
                VALUES (:t, :pc, 'DESL', :o, :cat, CURRENT_DATE - 60)"""),
            {"t": tenant_id, "pc": f"DESL-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DESL-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DESL-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DESL-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    yield engine, tenant_id, journey_id
    engine.dispose()


def _sync(engine, tenant_id, journey_id, document_id, di_client) -> None:
    with engine.begin() as c:
        confidence_policy._sync_booking_document(
            c, tenant_id=tenant_id, journey_id=journey_id, document_id=document_id,
            service_id="di-service", security_client=_FakeSecurityClient(),
            di_client=di_client, bump_version=True,
        )


def _executions_for_rule(engine, tenant_id, rule_code):
    with engine.begin() as c:
        return c.execute(
            text(
                "SELECT outcome, reason, triggering_event FROM auditcore.rule_executions "
                "WHERE tenant_id=:t AND rule_code=:rc ORDER BY evaluated_at_utc"
            ),
            {"t": tenant_id, "rc": rule_code},
        ).mappings().all()


def _wrong_document_executions(engine, tenant_id):
    return _executions_for_rule(engine, tenant_id, "WRONG_DOCUMENT")


def _add_evidence(engine, tenant_id, journey_id, customer_id, document_id, document_type_key) -> None:
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO auditcore.evidence
                (tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                 document_type_key, evidence_purpose)
                VALUES (:t, :j, :cu, :s, :d, :dtk, 'BOOKING')"""),
            {"t": tenant_id, "j": journey_id, "cu": customer_id, "s": uuid4(),
             "d": document_id, "dtk": document_type_key},
        )


def _customer_id(engine, tenant_id, journey_id):
    with engine.begin() as c:
        return c.execute(
            text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()


def test_no_reference_document_yet_records_skipped(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)
    booking_form_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, booking_form_id, "booking_form")

    di_client = _FakeDiClient()
    di_client.add(
        _confirmed(booking_form_id, "booking_form"),
        [_fact("customer_name", "Sanjaya Kumar Mohanty")],
    )
    _sync(engine, tenant_id, journey_id, booking_form_id, di_client)

    rows = _wrong_document_executions(engine, tenant_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "SKIPPED"
    assert rows[0]["triggering_event"] == "DOCUMENT_SYNCED"
    assert rows[0]["reason"] is not None


def test_matching_names_record_pass(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    aadhaar_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, aadhaar_id, "aadhaar")
    di_client = _FakeDiClient()
    di_client.add(_confirmed(aadhaar_id, "aadhaar"), [_fact("aadhaar_name", "Sanjaya Kumar Mohanty")])
    _sync(engine, tenant_id, journey_id, aadhaar_id, di_client)

    booking_form_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, booking_form_id, "booking_form")
    di_client.add(_confirmed(booking_form_id, "booking_form"), [_fact("customer_name", "Sanjaya Kumar Mohanty")])
    _sync(engine, tenant_id, journey_id, booking_form_id, di_client)

    rows = _wrong_document_executions(engine, tenant_id)
    # aadhaar's own sync: it IS the reference, nothing else to compare it to
    # yet -- SKIPPED. booking_form's sync: compared against the aadhaar
    # reference, matches -- PASS.
    assert [r["outcome"] for r in rows] == ["SKIPPED", "PASS"]


def test_mismatched_name_records_fail(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    aadhaar_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, aadhaar_id, "aadhaar")
    di_client = _FakeDiClient()
    di_client.add(_confirmed(aadhaar_id, "aadhaar"), [_fact("aadhaar_name", "Sanjaya Kumar Mohanty")])
    _sync(engine, tenant_id, journey_id, aadhaar_id, di_client)

    invoice_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, invoice_id, "customer_invoice_dms")
    di_client.add(_confirmed(invoice_id, "customer_invoice_dms"), [_fact("buyer_name", "Priya Nair")])
    _sync(engine, tenant_id, journey_id, invoice_id, di_client)

    rows = _wrong_document_executions(engine, tenant_id)
    assert [r["outcome"] for r in rows] == ["SKIPPED", "FAIL"]


def test_single_receipt_records_pass_for_duplicate_receipt(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    receipt_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, receipt_id, "dealer_receipt")
    di_client = _FakeDiClient()
    di_client.add(
        _confirmed(receipt_id, "dealer_receipt"),
        [
            _fact("receipt_number", "RCPT-001"),
            _fact("amount_paid", "50000"),
            _fact("receipt_date", "2026-09-01"),
        ],
    )
    _sync(engine, tenant_id, journey_id, receipt_id, di_client)

    rows = _executions_for_rule(engine, tenant_id, "DUPLICATE_RECEIPT")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "PASS"


def test_two_matching_receipts_record_fail_for_duplicate_receipt(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)
    di_client = _FakeDiClient()

    first_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, first_id, "dealer_receipt")
    di_client.add(
        _confirmed(first_id, "dealer_receipt"),
        [_fact("receipt_number", "RCPT-002"), _fact("amount_paid", "75000"), _fact("receipt_date", "2026-09-01")],
    )
    _sync(engine, tenant_id, journey_id, first_id, di_client)

    second_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, second_id, "dealer_receipt")
    di_client.add(
        _confirmed(second_id, "dealer_receipt"),
        [_fact("receipt_number", "RCPT-002"), _fact("amount_paid", "75000"), _fact("receipt_date", "2026-09-01")],
    )
    _sync(engine, tenant_id, journey_id, second_id, di_client)

    rows = _executions_for_rule(engine, tenant_id, "DUPLICATE_RECEIPT")
    # first sync: only one receipt on file yet -- PASS. second sync: now a
    # matching pair exists -- FAIL.
    assert [r["outcome"] for r in rows] == ["PASS", "FAIL"]


def test_high_confidence_field_records_pass_for_manual_verification(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    booking_form_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, booking_form_id, "booking_form")
    di_client = _FakeDiClient()
    di_client.add(
        _confirmed(booking_form_id, "booking_form"),
        [_fact("customer_name", "Sanjaya Kumar Mohanty", confidence=95.0)],
    )
    _sync(engine, tenant_id, journey_id, booking_form_id, di_client)

    rows = _executions_for_rule(engine, tenant_id, "MANUAL_VERIFICATION")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "PASS"


def test_low_confidence_field_records_fail_for_manual_verification(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    booking_form_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, booking_form_id, "booking_form")
    di_client = _FakeDiClient()
    di_client.add(
        # A realistic DI confidence value (65%, PERCENT scale, the scale
        # _machine_upsert_fact actually stores) -- proves the fix: the
        # pre-fix _LOW_CONFIDENCE_SQL compared this raw against 0.90 and
        # never flagged it (65.0 is not < 0.90), even though 65% is well
        # below the intended 90% review threshold.
        _confirmed(booking_form_id, "booking_form"),
        [_fact("customer_name", "Sanjaya Kumar Mohanty", confidence=65.0)],
    )
    _sync(engine, tenant_id, journey_id, booking_form_id, di_client)

    rows = _executions_for_rule(engine, tenant_id, "MANUAL_VERIFICATION")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "FAIL"


def _add_payment(engine, tenant_id, journey_id, *, amount, ref, method="UPI") -> None:
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO auditcore.payments
                (tenant_id, journey_id, amount, payment_method_code, payment_reference,
                 receipt_number, receipt_date, payment_stage, status_source)
                VALUES (:t, :j, :a, :m, :r, 'RC-1', DATE '2026-09-01', 'BOOKING', 'EVIDENCE')"""),
            {"t": tenant_id, "j": journey_id, "a": amount, "m": method, "r": ref},
        )


def test_no_payments_yet_records_skipped_for_payment_reconciliation(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    # A receipt-type document with zero payments in auditcore.payments yet
    # (reconcile_payments reads from that table, not from the document's own
    # extracted fields) -- confirming this document type is what actually
    # gates the call site, not the document's own content.
    receipt_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, receipt_id, "dealer_receipt")
    di_client = _FakeDiClient()
    di_client.add(_confirmed(receipt_id, "dealer_receipt"), [])
    _sync(engine, tenant_id, journey_id, receipt_id, di_client)

    payment_rows = _executions_for_rule(engine, tenant_id, "PAYMENT_BANK_UNMATCHED")
    assert len(payment_rows) == 1
    assert payment_rows[0]["outcome"] == "SKIPPED"

    sync_failure_rows = _executions_for_rule(engine, tenant_id, "AUTOMATED_SYNC_FAILURE")
    # Two rows: payment reconciliation's own half (this document type
    # triggers it), plus SKU resolution's own half (runs unconditionally
    # for stage=="BOOKING", regardless of document type).
    assert len(sync_failure_rows) == 2
    assert all(row["outcome"] == "PASS" for row in sync_failure_rows)


def test_unmatched_payment_records_fail_for_payment_reconciliation(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)
    _add_payment(engine, tenant_id, journey_id, amount=50000, ref="UTR-NO-MATCH")

    receipt_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, receipt_id, "dealer_receipt")
    di_client = _FakeDiClient()
    di_client.add(_confirmed(receipt_id, "dealer_receipt"), [])
    _sync(engine, tenant_id, journey_id, receipt_id, di_client)

    rows = _executions_for_rule(engine, tenant_id, "PAYMENT_BANK_UNMATCHED")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "FAIL"


def test_cash_payment_records_pass_for_payment_reconciliation(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)
    # Cash is NOT_APPLICABLE, not unmatched -- reconcile_payments never
    # raises a flag for it, so the Execution Log should read PASS.
    _add_payment(engine, tenant_id, journey_id, amount=25000, ref="", method="Cash")

    receipt_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, receipt_id, "dealer_receipt")
    di_client = _FakeDiClient()
    di_client.add(_confirmed(receipt_id, "dealer_receipt"), [])
    _sync(engine, tenant_id, journey_id, receipt_id, di_client)

    rows = _executions_for_rule(engine, tenant_id, "PAYMENT_BANK_UNMATCHED")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "PASS"


def test_no_model_snapshot_yet_records_skipped_for_model_resolution(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    # No vehicle_model fact at all -- journey_products.model_name_snapshot
    # never gets written, sync_model_resolution's own "nothing to resolve
    # yet" case.
    booking_form_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, booking_form_id, "booking_form")
    di_client = _FakeDiClient()
    di_client.add(_confirmed(booking_form_id, "booking_form"), [_fact("customer_name", "Sanjaya Kumar Mohanty")])
    _sync(engine, tenant_id, journey_id, booking_form_id, di_client)

    model_rows = _executions_for_rule(engine, tenant_id, "MODEL_NOT_IDENTIFIED")
    assert len(model_rows) == 1
    assert model_rows[0]["outcome"] == "SKIPPED"

    sync_failure_rows = _executions_for_rule(engine, tenant_id, "AUTOMATED_SYNC_FAILURE")
    # booking_form isn't a reconciliation-trigger document type, so payment
    # reconciliation's own AUTOMATED_SYNC_FAILURE half never runs here --
    # only SKU resolution's does.
    assert len(sync_failure_rows) == 1
    assert sync_failure_rows[0]["outcome"] == "PASS"


def test_resolvable_model_records_pass_for_model_resolution(synced_document_setup) -> None:
    engine, tenant_id, journey_id = synced_document_setup
    customer_id = _customer_id(engine, tenant_id, journey_id)

    with engine.begin() as c:
        oem_id = c.execute(
            text("SELECT oem_id FROM auditcore.projects WHERE tenant_id=:t LIMIT 1"),
            {"t": tenant_id},
        ).scalar_one()
        model_id = c.execute(
            text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                 "VALUES (:o, :mc, 'SCORPIO N') RETURNING model_id"),
            {"o": oem_id, "mc": f"MR-M-{uuid4().hex[:8]}"},
        ).scalar_one()
        variant_id = c.execute(
            text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                 "VALUES (:m, :vc, 'Z8L') RETURNING variant_id"),
            {"m": model_id, "vc": f"MR-V-{uuid4().hex[:8]}"},
        ).scalar_one()
        sku_id = c.execute(
            text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                 "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"),
            {"o": oem_id, "m": model_id, "v": variant_id, "sc": f"MR-SKU-{uuid4().hex[:10]}"},
        ).scalar_one()
        price_list_id = c.execute(
            text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
                 "VALUES (:t, :c, 'OEM') RETURNING price_list_id"),
            {"t": tenant_id, "c": f"MR-PL-{uuid4().hex[:8]}"},
        ).scalar_one()
        price_list_version_id = c.execute(
            text("INSERT INTO auditcore.price_list_versions "
                 "(tenant_id, price_list_id, version_no, lifecycle_status, effective_from) "
                 "VALUES (:t, :pl, 1, 'DRAFT', CURRENT_DATE - 45) RETURNING price_list_version_id"),
            {"t": tenant_id, "pl": price_list_id},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.price_list_items "
                 "(tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount) "
                 "VALUES (:t, :plv, :sku, 'EX_SHOWROOM', 1988996)"),
            {"t": tenant_id, "plv": price_list_version_id, "sku": sku_id},
        )
        c.execute(
            text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
                 "WHERE price_list_version_id=:plv"),
            {"plv": price_list_version_id},
        )

    booking_form_id = uuid4()
    _add_evidence(engine, tenant_id, journey_id, customer_id, booking_form_id, "booking_form")
    di_client = _FakeDiClient()
    di_client.add(
        _confirmed(booking_form_id, "booking_form"),
        [
            _fact("vehicle_model", "SCORPIO N"),
            _fact("vehicle_variant", "Z8L"),
            _fact("ex_showroom_price", "1988996"),
        ],
    )
    _sync(engine, tenant_id, journey_id, booking_form_id, di_client)

    model_rows = _executions_for_rule(engine, tenant_id, "MODEL_NOT_IDENTIFIED")
    assert len(model_rows) == 1
    assert model_rows[0]["outcome"] == "PASS"
