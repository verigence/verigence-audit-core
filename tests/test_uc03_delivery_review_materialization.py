from __future__ import annotations

import inspect
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_delivery_review_materialization as materialization
from audit_core.uc03_document_review_v2 import ReviewV2Document, ReviewV2Field
from audit_core.uc03_review_effective_values import (
    confirm_delivery_review_v2_effective_values,
)


@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for finance materialization integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-fin-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"FIN-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"FIN-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'FIN', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"FIN-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"FIN-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"FIN-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"FIN-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'DELIVERY', 'DELIVERY_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        c.journey_id = journey_id  # type: ignore[attr-defined]
        yield c
    engine.dispose()


def _field(field_key: str, value, *, confidence: float = 99.0) -> ReviewV2Field:
    return ReviewV2Field(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=1,
        reviewState="READY",
    )


def _document(document_type: str, fields: list[ReviewV2Field]) -> ReviewV2Document:
    document_id = uuid4()
    return ReviewV2Document(
        documentId=document_id,
        label=document_type,
        documentTypeKey=document_type,
        originalFilename=f"{document_id}.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=fields,
    )


def test_delivery_order_contract_materializes_exact_chassis_field_only() -> None:
    document = _document(
        "delivery_order_cover",
        [
            _field("chassis", "SHOULD-NOT-MATCH", confidence=100.0),
            _field("chassis_no", "MA1ABC123", confidence=95.0),
        ],
    )

    selected = materialization._best_field(
        [document],
        materialization._CHASSIS_FIELD_KEYS,
        source_priority=materialization._VEHICLE_SOURCE_PRIORITY,
    )

    assert selected is not None
    assert selected[1].fieldKey == "chassis_no"
    assert selected[1].value == "MA1ABC123"


def test_invoice_source_precedes_delivery_order_for_vehicle_identifier() -> None:
    delivery_order = _document(
        "delivery_order_cover",
        [_field("chassis_no", "DO-CHASSIS", confidence=99.0)],
    )
    invoice = _document(
        "customer_invoice_dms",
        [_field("chassis_number", "INV-CHASSIS", confidence=93.0)],
    )

    selected = materialization._best_field(
        [delivery_order, invoice],
        materialization._CHASSIS_FIELD_KEYS,
        source_priority=materialization._VEHICLE_SOURCE_PRIORITY,
    )

    assert selected is not None
    assert selected[0].documentTypeKey == "customer_invoice_dms"
    assert selected[1].value == "INV-CHASSIS"


def test_rto_challan_registration_fields_mapping() -> None:
    # Confirmed live gap: DI extracts registration_state/territory/district/
    # type from an RTO Challan fine (rto_challan.py), but only
    # registration_number was ever wired into registration_records, leaving
    # a PC to type State/District in by hand.
    assert materialization._RTO_CHALLAN_DOCUMENT_TYPE == "rto_challan"
    assert materialization._RTO_CHALLAN_FIELDS == {
        "registration_state": "registration_state",
        "registration_territory": "registration_territory",
        "registration_district": "registration_district",
        "registration_type": "registration_type_code",
    }


def test_rto_challan_fields_only_selected_from_an_actual_rto_challan() -> None:
    challan = _document(
        "rto_challan",
        [
            _field("registration_state", "Odisha"),
            _field("registration_district", "Cuttack"),
        ],
    )
    other = _document(
        "customer_invoice_dms",
        [_field("registration_state", "SHOULD-NOT-MATCH")],
    )

    state = materialization._best_field(
        [other, challan], ("registration_state",), document_types={"rto_challan"},
    )
    assert state is not None
    assert state[0].documentTypeKey == "rto_challan"
    assert state[1].value == "Odisha"

    district = materialization._best_field(
        [challan], ("registration_district",), document_types={"rto_challan"},
    )
    assert district is not None
    assert district[1].value == "Cuttack"


def test_financed_by_is_picked_up_from_any_invoice_document() -> None:
    # financed_by is a common invoice-schema field, not scoped to one
    # document type -- unlike the RTO Challan fields above.
    invoice = _document(
        "customer_invoice_dms",
        [_field("financed_by", "HDFC Bank")],
    )
    selected = materialization._best_field([invoice], materialization._FINANCED_BY_FIELD_KEYS)
    assert selected is not None
    assert selected[1].value == "HDFC Bank"


def test_hp_charges_only_selected_from_an_actual_rto_challan() -> None:
    challan = _document("rto_challan", [_field("hp_charges_amount", "5000")])
    other = _document("customer_invoice_dms", [_field("hp_charges_amount", "SHOULD-NOT-MATCH")])

    selected = materialization._best_field(
        [other, challan], materialization._HP_CHARGES_FIELD_KEYS, document_types={"rto_challan"},
    )
    assert selected is not None
    assert selected[0].documentTypeKey == "rto_challan"
    assert selected[1].value == "5000"


def test_delivery_insurance_mapping_matches_di_insurance_cover_contract() -> None:
    assert materialization._INSURANCE_DOCUMENT_TYPE == "insurance_cover"
    assert materialization._INSURANCE_FIELDS == {
        "insurer_name": "insurer_name",
        "policy_number": "policy_reference",
        "premium_amount": "actual_premium_amount",
        "agent_intermediary_name": "agent_intermediary_name",
        "agent_intermediary_code": "agent_intermediary_code",
        "misp_code": "misp_code",
    }
    assert materialization._REGISTRATION_FIELD_KEYS == (
        "registration_number",
        "insured_vehicle_reg",
    )


def test_insurance_addons_are_selected_from_the_di_add_ons_array_field() -> None:
    # add_ons is a DI array field (zero dep, engine protect, etc.) kept out
    # of the per-column text merge and handled as its own JSON column.
    document = _document(
        "insurance_cover",
        [_field("add_ons", ["zero_depreciation", "engine_protection"])],
    )
    selected = materialization._best_field(
        [document], ("add_ons",), document_types={"insurance_cover"},
    )
    assert selected is not None
    assert selected[1].value == ["zero_depreciation", "engine_protection"]


def test_delivery_business_materializer_calls_all_canonical_projections(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        materialization,
        "materialize_delivery_vehicle",
        lambda *args, **kwargs: calls.append("vehicle") or 2,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_registration",
        lambda *args, **kwargs: calls.append("registration") or 1,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_finance",
        lambda *args, **kwargs: calls.append("finance") or 5,
    )
    monkeypatch.setattr(
        materialization,
        "sync_finance_hypothecation_findings",
        lambda *args, **kwargs: calls.append("finance_hypothecation") or {"raised": 1, "resolved": 0},
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_insurance",
        lambda *args, **kwargs: calls.append("insurance") or 3,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_date",
        lambda *args, **kwargs: calls.append("delivery_date") or 1,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_commercial_lines",
        lambda *args, **kwargs: calls.append("commercials") or 4,
    )
    monkeypatch.setattr(
        materialization,
        "materialize_delivery_receipts",
        lambda *args, **kwargs: calls.append("receipts")
        or {
            "reviewRowsWritten": 1,
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "skippedWithoutAmount": 0,
        },
    )

    result = materialization.materialize_reviewed_delivery_business_values(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        documents=[],
        actor_id="reviewer-a",
    )

    assert calls == [
        "vehicle", "registration", "finance", "finance_hypothecation",
        "insurance", "delivery_date", "commercials", "receipts",
    ]
    assert result["vehicleFields"] == 2
    assert result["registrationFields"] == 1
    assert result["financeFields"] == 5
    assert result["financeHypothecationRaised"] == 1
    assert result["financeHypothecationResolved"] == 0
    assert result["insuranceFields"] == 3
    assert result["deliveryDateSet"] == 1
    assert result["commercialLines"] == 4
    assert result["receiptPaymentsCreated"] == 1
    # Not monkeypatched -- runs for real against documents=[] and returns 0
    # without touching the (dummy) connection, same as invoices/bank lines.
    assert result["scrappageCertificatesMaterialized"] == 0


def test_delivery_review_materializes_before_marking_stage_verified() -> None:
    # confirm_delivery_review_v2_effective_values is the live handler for
    # POST /delivery/review/confirm (confirm_delivery_review_v2, previously
    # tested here, was shadowed dead code -- see
    # test_uc03_delivery_review_confirm.py). Canonical materialization also
    # runs async per document as DI confirms it (durable-state-driven, safe
    # to call redundantly); this synchronous call is confirm's own safety
    # net for a correction applied at confirm time, matching Booking's.
    source = inspect.getsource(confirm_delivery_review_v2_effective_values)
    materialize_at = source.index("materialize_reviewed_delivery_business_values(")
    verified_at = source.index("pc_verification_status='VERIFIED'")

    assert materialize_at < verified_at


def test_materialize_delivery_finance_fills_provider_then_hp_charges(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    invoice = _document("customer_invoice_dms", [_field("financed_by", "HDFC Bank")])

    written = materialization.materialize_delivery_finance(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[invoice],
    )
    assert written == 2  # finance_type_code (derived) + provider_name

    row = journey.execute(
        text("SELECT finance_type_code, provider_name, financed_amount "
             "FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    assert row["finance_type_code"] == "LOAN"
    assert row["provider_name"] == "HDFC Bank"
    assert row["financed_amount"] is None

    # A later RTO Challan fills the HP charges without disturbing the
    # provider already on record, and without creating a second row
    # (finance_records has no (tenant_id, journey_id) uniqueness -- confirmed
    # live gap this function guards against by matching the latest-row
    # pattern payments_finance.put_finance already uses).
    challan = _document("rto_challan", [_field("hp_charges_amount", "7500")])
    materialization.materialize_delivery_finance(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[invoice, challan],
    )
    row2 = journey.execute(
        text("SELECT provider_name, financed_amount FROM auditcore.finance_records "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    assert row2["provider_name"] == "HDFC Bank"
    assert row2["financed_amount"] == 7500

    count = journey.execute(
        text("SELECT count(*) FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    assert count == 1


def test_materialize_delivery_finance_no_financed_by_creates_no_row(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    challan = _document("rto_challan", [_field("hp_charges_amount", "5000")])

    written = materialization.materialize_delivery_finance(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[challan],
    )
    assert written == 0
    count = journey.execute(
        text("SELECT count(*) FROM auditcore.finance_records WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    assert count == 0


def test_finance_hypothecation_finding_raises_then_resolves(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    invoice = _document("customer_invoice_dms", [_field("financed_by", "SBI")])
    materialization.materialize_delivery_finance(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[invoice],
    )

    raised = materialization.sync_finance_hypothecation_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id,
    )
    assert raised == {"raised": 1, "resolved": 0}

    row = journey.execute(
        text("SELECT finding_type_code, finding_class, owner_role_code, finding_status "
             "FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    assert row["finding_type_code"] == "FINANCE_HYPOTHECATION_MISSING"
    assert row["finding_class"] == "DATA_GAP"
    assert row["owner_role_code"] == "PC"
    assert row["finding_status"] == "OPEN"

    # idempotent -- no duplicate finding on a repeat call
    again = materialization.sync_finance_hypothecation_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id,
    )
    assert again == {"raised": 1, "resolved": 0}
    count = journey.execute(
        text("SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    assert count == 1

    # HP charges arrive -- the finding resolves.
    challan = _document("rto_challan", [_field("hp_charges_amount", "3200")])
    materialization.materialize_delivery_finance(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[invoice, challan],
    )
    resolved = materialization.sync_finance_hypothecation_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id,
    )
    assert resolved == {"raised": 0, "resolved": 1}
    status = journey.execute(
        text("SELECT finding_status FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    assert status == "RESOLVED"


def test_finance_hypothecation_no_finding_when_not_financed(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    result = materialization.sync_finance_hypothecation_findings(
        journey, tenant_id=tenant_id, journey_id=journey_id,
    )
    assert result == {"raised": 0, "resolved": 0}
    count = journey.execute(
        text("SELECT count(*) FROM auditcore.audit_findings WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    assert count == 0


def test_materialize_delivery_date_sets_from_gate_pass_when_no_row_exists(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    gate_pass = _document("gate_pass", [_field("delivery_date", "2026-09-05")])

    written = materialization.materialize_delivery_date(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[gate_pass],
    )
    assert written == 1

    row = journey.execute(
        text("SELECT actual_delivered_at, status_source FROM auditcore.deliveries "
             "WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    assert row["status_source"] == "EVIDENCE"
    assert row["actual_delivered_at"].date().isoformat() == "2026-09-05"


def test_materialize_delivery_date_overrides_operational_default(journey) -> None:
    # A PC clicking "Delivery Completed" stamps actual_delivered_at=now() with
    # status_source='OPERATIONAL_INPUT' (uc03_delivery_commands.py). Once the
    # Gate Pass is confirmed, its printed date is the ground truth and must
    # replace that click-time default, not defer to it.
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    journey.execute(
        text("""
            INSERT INTO auditcore.deliveries (
                tenant_id, journey_id, actual_delivery_status_code,
                actual_delivered_at, status_source
            ) VALUES (
                :t, :j, 'DELIVERY_COMPLETED', now(), 'OPERATIONAL_INPUT'
            )
        """),
        {"t": tenant_id, "j": journey_id},
    )

    gate_pass = _document("gate_pass", [_field("delivery_date", "2026-08-20")])
    written = materialization.materialize_delivery_date(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[gate_pass],
    )
    assert written == 1

    row = journey.execute(
        text("SELECT actual_delivery_status_code, actual_delivered_at, status_source "
             "FROM auditcore.deliveries WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one()
    # The operational status code is untouched -- only the date/source change.
    assert row["actual_delivery_status_code"] == "DELIVERY_COMPLETED"
    assert row["status_source"] == "EVIDENCE"
    assert row["actual_delivered_at"].date().isoformat() == "2026-08-20"


def test_materialize_delivery_date_is_idempotent_on_rerun(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    gate_pass = _document("gate_pass", [_field("delivery_date", "2026-09-05")])

    materialization.materialize_delivery_date(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[gate_pass],
    )
    version_after_first = journey.execute(
        text("SELECT version_no FROM auditcore.deliveries WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()

    written = materialization.materialize_delivery_date(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[gate_pass],
    )
    assert written == 0
    version_after_second = journey.execute(
        text("SELECT version_no FROM auditcore.deliveries WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    assert version_after_second == version_after_first


def test_materialize_delivery_date_ignores_unparseable_or_missing_value(journey) -> None:
    tenant_id, journey_id = journey.tenant_id, journey.journey_id
    gate_pass = _document("gate_pass", [_field("delivery_date", "not a date")])

    written = materialization.materialize_delivery_date(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[gate_pass],
    )
    assert written == 0
    count = journey.execute(
        text("SELECT count(*) FROM auditcore.deliveries WHERE tenant_id=:t AND journey_id=:j"),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one()
    assert count == 0

    # A different document type carrying the same field key must not match.
    other = _document("customer_invoice_dms", [_field("delivery_date", "2026-09-05")])
    written = materialization.materialize_delivery_date(
        journey, tenant_id=tenant_id, journey_id=journey_id, documents=[other],
    )
    assert written == 0
