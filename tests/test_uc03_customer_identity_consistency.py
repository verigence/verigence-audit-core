from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_customer_identity_consistency as cic


# ── unit: name normalization / fuzzy match ──────────────────────────────────
def test_identical_names_match() -> None:
    assert cic._names_match("Sanjaya Kumar Mohanty", "Sanjaya Kumar Mohanty")


def test_case_and_whitespace_variants_match() -> None:
    assert cic._names_match("SANJAYA KUMAR MOHANTY", "  sanjaya   kumar mohanty ")


def test_title_prefix_is_ignored() -> None:
    assert cic._names_match("Mr. Sanjaya Kumar Mohanty", "Sanjaya Kumar Mohanty")


def test_minor_typo_still_matches() -> None:
    # A single missing letter across an independently-scanned document is a
    # formatting/OCR variant, not a different person.
    assert cic._names_match("Sanjaya Kumar Mohanty", "Sanjay Kumar Mohanty")


def test_clearly_different_names_do_not_match() -> None:
    assert not cic._names_match("Sanjaya Kumar Mohanty", "Priya Nair")


def test_blank_value_is_not_this_checks_job() -> None:
    # Nothing to compare -- a missing/unparseable name is a different finding
    # type's problem (MANUAL_VERIFICATION), not a mismatch.
    assert cic._names_match("Sanjaya Kumar Mohanty", "")
    assert cic._names_match("", "")


# ── DB integration ───────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for identity-consistency integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-cic-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"CIC-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"CIC-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'CIC', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"CIC-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"CIC-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"CIC-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"CIC-J-{suffix}"},
        ).scalar_one()
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _set_named_field(
    c, *, stage_code, document_type_key, field_key, value, confidence=0.95, document_id=None
):
    document_id = document_id or uuid4()
    c.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, effective_value, confidence_score, is_modified
            ) VALUES (
                :t, :j, NULL, :doc,
                NULL, 1, :stage,
                :dtk, NULL, :fk,
                CAST(:v AS jsonb), CAST(:v AS jsonb), :conf, false
            )
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id, "doc": document_id, "stage": stage_code,
         "dtk": document_type_key, "fk": field_key, "v": json.dumps(value), "conf": confidence},
    )
    return document_id


def _open_wrong_document_findings(c) -> list[dict]:
    return [
        dict(row)
        for row in c.execute(
            text("SELECT rule_key, severity, finding_status FROM auditcore.audit_findings "
                 "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='WRONG_DOCUMENT' "
                 "AND finding_status IN ('OPEN','ACKNOWLEDGED')"),
            {"t": c.tenant_id, "j": c.journey_id},
        ).mappings().all()
    ]


def test_customer_name_check_is_a_noop_without_kyc_but_dealer_check_still_runs(journey) -> None:
    # No KYC document exists yet -- nothing to check customer names
    # against -- but the dealer-name check is independent of that and
    # still runs (checked against the dealer master record, not a KYC doc).
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Sanjaya Kumar Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result == {"raised": 0, "resolved": 0, "examined": 0, "referenceName": None}
    assert _open_wrong_document_findings(c) == []


def test_matching_names_raise_nothing(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Sanjaya Kumar Mohanty",
    )
    _set_named_field(
        c, stage_code="DELIVERY", document_type_key="insurance_cover",
        field_key="insured_name", value="Sanjaya K Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 0
    assert result["examined"] == 2  # booking_form + insurance_cover vs the aadhaar reference
    assert _open_wrong_document_findings(c) == []


def test_mismatched_invoice_raises_high_severity_wrong_document(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    invoice_doc = _set_named_field(
        c, stage_code="DELIVERY", document_type_key="customer_invoice_dms",
        field_key="buyer_name", value="Priya Nair",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 1
    findings = _open_wrong_document_findings(c)
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["rule_key"] == f"WRONG_DOCUMENT:{invoice_doc}"

    # finding_types registry classification actually applied (migration 0081).
    classified = c.execute(
        text("SELECT finding_class, owner_role_code FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND finding_type_code='WRONG_DOCUMENT'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert classified["finding_class"] == "VIOLATION"
    assert classified["owner_role_code"] == "TL"


def test_kyc_uploaded_after_a_mismatching_document_still_catches_it(journey) -> None:
    # Order does not matter -- a non-KYC document confirmed first, then KYC
    # arrives later and re-checks everything already on file.
    c = journey
    _set_named_field(
        c, stage_code="DELIVERY", document_type_key="insurance_cover",
        field_key="insured_name", value="Priya Nair",
    )
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="pan_card",
        field_key="pan_name", value="Sanjaya Kumar Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 1
    assert result["referenceName"] == "Sanjaya Kumar Mohanty"


def test_correction_to_a_matching_name_self_heals(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    invoice_doc = _set_named_field(
        c, stage_code="DELIVERY", document_type_key="customer_invoice_dms",
        field_key="buyer_name", value="Priya Nair",
    )
    cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert len(_open_wrong_document_findings(c)) == 1

    # The PC re-uploads / DI re-extracts the same document with the correct name.
    c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields "
             "SET effective_value = CAST(:v AS jsonb) "
             "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc"),
        {"v": json.dumps("Sanjaya Kumar Mohanty"), "t": c.tenant_id, "j": c.journey_id, "doc": invoice_doc},
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["resolved"] == 1
    assert _open_wrong_document_findings(c) == []


def test_kyc_document_itself_is_not_checked_against_its_own_name(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 0
    assert result["resolved"] == 0


# ── Receipt dealer-name consistency ─────────────────────────────────────────
# The fixture's dealer is named 'D' (auditcore.dealers.dealer_name).

def test_receipt_dealer_name_check_runs_even_without_kyc(journey) -> None:
    # Independent of the customer-name check: no KYC document exists yet,
    # but a receipt from a different dealership must still be caught.
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="dealer_receipt",
        field_key="dealer_name", value="Some Other Motors Pvt Ltd",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 1
    findings = _open_wrong_document_findings(c)
    assert len(findings) == 1
    assert findings[0]["severity"] == "HIGH"
    assert findings[0]["rule_key"].startswith("WRONG_DOCUMENT:DEALER:")


def test_matching_dealer_name_raises_nothing(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="DELIVERY", document_type_key="payment_receipt",
        field_key="dealer_name", value="D",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 0
    assert _open_wrong_document_findings(c) == []


def test_dealer_check_and_customer_check_track_independently_on_one_document(journey) -> None:
    # A single receipt document can fail both checks (wrong customer AND
    # wrong dealer) -- each must raise and resolve on its own rule_key,
    # never clobbering the other.
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    receipt_doc = _set_named_field(
        c, stage_code="BOOKING", document_type_key="dealer_receipt",
        field_key="customer_name", value="Priya Nair",
    )
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="dealer_receipt",
        field_key="dealer_name", value="Some Other Motors Pvt Ltd",
        document_id=receipt_doc,
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 2
    findings = {f["rule_key"] for f in _open_wrong_document_findings(c)}
    assert findings == {
        f"WRONG_DOCUMENT:{receipt_doc}",
        f"WRONG_DOCUMENT:DEALER:{receipt_doc}",
    }

    # Correcting only the dealer name resolves that one finding and leaves
    # the customer-name mismatch open.
    c.execute(
        text("UPDATE auditcore.journey_document_extracted_fields "
             "SET effective_value = CAST(:v AS jsonb) "
             "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:doc AND field_key='dealer_name'"),
        {"v": json.dumps("D"), "t": c.tenant_id, "j": c.journey_id, "doc": receipt_doc},
    )
    result2 = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result2["resolved"] == 1
    remaining = {f["rule_key"] for f in _open_wrong_document_findings(c)}
    assert remaining == {f"WRONG_DOCUMENT:{receipt_doc}"}


def test_non_receipt_document_types_are_not_dealer_checked(journey) -> None:
    # A dealer_name-like field on some other document type must not be
    # picked up -- only dealer_receipt/payment_receipt are in scope.
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="dealer_name", value="Some Other Motors Pvt Ltd",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 0
    assert _open_wrong_document_findings(c) == []


# ── identity-check hold (evidence.identity_check_status) ────────────────────
def _link_evidence(c, *, di_document_id, document_type_key, requirement_id=None):
    customer_id = c.execute(
        text("SELECT customer_id FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    return c.execute(
        text(
            """
            INSERT INTO auditcore.evidence (
                tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                document_type_key, evidence_purpose, journey_document_requirement_id,
                association_status
            ) VALUES (
                :t, :j, :cu, :subj, :doc, :dtk, 'BOOKING_DOCUMENT', :req, 'ACTIVE'
            )
            RETURNING evidence_id
            """
        ),
        {
            "t": c.tenant_id, "j": c.journey_id, "cu": customer_id, "subj": uuid4(),
            "doc": di_document_id, "dtk": document_type_key, "req": requirement_id,
        },
    ).scalar_one()


def _set_named_field_with_evidence(
    c, *, stage_code, document_type_key, field_key, value, confidence=0.95,
    document_id=None, requirement_id=None,
):
    document_id = document_id or uuid4()
    evidence_id = _link_evidence(
        c, di_document_id=document_id, document_type_key=document_type_key,
        requirement_id=requirement_id,
    )
    c.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_extracted_fields (
                tenant_id, journey_id, evidence_id, di_document_id,
                source_fact_ref, source_fact_version, stage_code,
                source_document_type_key, source_canonical_field_id, field_key,
                extracted_value, effective_value, confidence_score, is_modified
            ) VALUES (
                :t, :j, :ev, :doc,
                NULL, 1, :stage,
                :dtk, NULL, :fk,
                CAST(:v AS jsonb), CAST(:v AS jsonb), :conf, false
            )
            """
        ),
        {"t": c.tenant_id, "j": c.journey_id, "ev": evidence_id, "doc": document_id, "stage": stage_code,
         "dtk": document_type_key, "fk": field_key, "v": json.dumps(value), "conf": confidence},
    )
    return document_id, evidence_id


def _evidence_row(c, evidence_id):
    return dict(c.execute(
        text(
            "SELECT association_status, identity_check_status, void_reason "
            "FROM auditcore.evidence WHERE tenant_id=:t AND evidence_id=:e"
        ),
        {"t": c.tenant_id, "e": evidence_id},
    ).mappings().one())


def test_mismatch_holds_the_document_out_of_materialization(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    _document_id, evidence_id = _set_named_field_with_evidence(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Priya Nair",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 1
    assert _evidence_row(c, evidence_id)["identity_check_status"] == "HELD"
    # Not a delete -- still ACTIVE, still visibly "Received" on the checklist.
    assert _evidence_row(c, evidence_id)["association_status"] == "ACTIVE"


def test_matching_name_keeps_evidence_passed(journey) -> None:
    c = journey
    _set_named_field(
        c, stage_code="BOOKING", document_type_key="aadhaar",
        field_key="aadhaar_name", value="Sanjaya Kumar Mohanty",
    )
    _document_id, evidence_id = _set_named_field_with_evidence(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Sanjaya Kumar Mohanty",
    )

    cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert _evidence_row(c, evidence_id)["identity_check_status"] == "PASSED"


def test_no_kyc_yet_holds_every_named_document(journey) -> None:
    # Real requirement (clarified after this rule shipped): a non-KYC named
    # document's data can't be trusted for materialization until there's a
    # KYC name to check it against at all -- held, not just skipped.
    c = journey
    _document_id, evidence_id = _set_named_field_with_evidence(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Sanjaya Kumar Mohanty",
    )

    result = cic.sync_customer_identity_consistency(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )

    assert result["raised"] == 0  # no finding -- BK_PAN_PRESENT-style rules cover "upload KYC"
    assert _evidence_row(c, evidence_id)["identity_check_status"] == "HELD"


def test_reject_wrong_document_voids_evidence_and_creates_pc_reupload_task(journey) -> None:
    c = journey
    requirement_id = uuid4()
    document_id, evidence_id = _set_named_field_with_evidence(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Priya Nair", requirement_id=requirement_id,
    )

    cic.reject_wrong_document(
        c,
        tenant_id=c.tenant_id,
        journey_id=c.journey_id,
        di_document_id=document_id,
        stage_code="BOOKING",
        actor_id="tl-test-actor",
        reason="Confirmed wrong customer's document.",
        correlation_id="",
    )

    row = _evidence_row(c, evidence_id)
    assert row["association_status"] == "VOIDED"
    assert row["identity_check_status"] == "REJECTED"
    assert row["void_reason"] == "Confirmed wrong customer's document."

    task = c.execute(
        text(
            "SELECT task_type, assigned_role_code, task_payload FROM auditcore.workflow_tasks "
            "WHERE tenant_id=:t AND journey_id=:j AND task_type='PC_DOCUMENT_REUPLOAD'"
        ),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert task["assigned_role_code"] == "PC"
    assert task["task_payload"]["documentId"] == str(document_id)
    assert task["task_payload"]["requirementRef"] == str(requirement_id)


def test_reject_wrong_document_is_idempotent(journey) -> None:
    c = journey
    document_id, evidence_id = _set_named_field_with_evidence(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Priya Nair",
    )
    cic.reject_wrong_document(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, di_document_id=document_id,
        stage_code="BOOKING", actor_id="tl-test-actor", reason=None, correlation_id="",
    )
    # A second call (e.g. a retried request) must not raise or double-void.
    cic.reject_wrong_document(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, di_document_id=document_id,
        stage_code="BOOKING", actor_id="tl-test-actor", reason=None, correlation_id="",
    )
    assert _evidence_row(c, evidence_id)["association_status"] == "VOIDED"


def test_release_wrong_document_hold_clears_held_but_never_rejected(journey) -> None:
    c = journey
    held_document_id, held_evidence_id = _set_named_field_with_evidence(
        c, stage_code="BOOKING", document_type_key="booking_form",
        field_key="customer_name", value="Priya Nair",
    )
    c.execute(
        text("UPDATE auditcore.evidence SET identity_check_status='HELD' "
             "WHERE tenant_id=:t AND evidence_id=:e"),
        {"t": c.tenant_id, "e": held_evidence_id},
    )
    rejected_document_id, rejected_evidence_id = _set_named_field_with_evidence(
        c, stage_code="BOOKING", document_type_key="insurance_cover",
        field_key="insured_name", value="Priya Nair",
    )
    c.execute(
        text("UPDATE auditcore.evidence SET identity_check_status='REJECTED' "
             "WHERE tenant_id=:t AND evidence_id=:e"),
        {"t": c.tenant_id, "e": rejected_evidence_id},
    )

    cic.release_wrong_document_hold(c, tenant_id=c.tenant_id, di_document_id=held_document_id)
    cic.release_wrong_document_hold(c, tenant_id=c.tenant_id, di_document_id=rejected_document_id)

    assert _evidence_row(c, held_evidence_id)["identity_check_status"] == "PASSED"
    assert _evidence_row(c, rejected_evidence_id)["identity_check_status"] == "REJECTED"
