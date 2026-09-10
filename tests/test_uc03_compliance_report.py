from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.security import Principal
from audit_core.uc03_compliance_report import get_compliance_report

_PERMISSION = "audit.finding.read"


@pytest.fixture
def compliance_report_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-cr-{suffix}"
    actor_id = f"tl-{suffix}"

    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"CR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"CR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'CR', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"CR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'Aditya Motors') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"CR-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets "
                 "(tenant_id, dealer_id, outlet_code, outlet_name, outlet_classification, status) "
                 "VALUES (:t, :d, :c, 'South West Outlet', 'ONSITE', 'ACTIVE') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"CR-O-{suffix}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.business_assignments "
                 "(tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id, "
                 " effective_from, assignment_status) "
                 "VALUES (:t, :a, 'TL', :d, :o, now() - interval '1 day', 'ACTIVE')"),
            {"t": tenant_id, "a": actor_id, "d": dealer_id, "o": outlet_id},
        )
        customer_id = c.execute(
            text("INSERT INTO auditcore.customers (tenant_id, dealer_id, outlet_id, customer_type_code, display_name) "
                 "VALUES (:t, :d, :o, 'INDIVIDUAL', 'Sanjaya Kumar Mohanty') RETURNING customer_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:t, :d, :o, :cu, :ref) RETURNING journey_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "ref": f"JR-{suffix}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_reference, booking_date, deal_type_code) "
                 "VALUES (:t, :j, :ref, CURRENT_DATE - 30, 'RETAIL')"),
            {"t": tenant_id, "j": journey_id, "ref": f"BK-{suffix}"},
        )
        c.execute(
            text("INSERT INTO auditcore.journey_products "
                 "(tenant_id, journey_id, model_name_snapshot, variant_name_snapshot, colour_name_snapshot) "
                 "VALUES (:t, :j, 'Scorpio N', 'Z8 (S) AT 2WD', 'Stealth Black')"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("INSERT INTO auditcore.vehicle_records (tenant_id, journey_id, vin) "
                 "VALUES (:t, :j, 'MA1TC2XXXJ123456')"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, actual_amount) "
                 "VALUES (:t, :j, 'EX_SHOWROOM_PRICE', 1600000, 1600000)"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("INSERT INTO auditcore.discount_applications "
                 "(tenant_id, journey_id, discount_key, standard_eligible_amount, actual_discount_amount, eligibility_result) "
                 "VALUES (:t, :j, 'LOYALTY_DISCOUNT', 50000, 75000, 'EXCEEDS_POLICY')"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("INSERT INTO auditcore.payments (tenant_id, journey_id, payment_at_utc, amount, payment_method_code, payment_reference) "
                 "VALUES (:t, :j, now(), 1642500, 'BANK_TRANSFER', :ref)"),
            {"t": tenant_id, "j": journey_id, "ref": f"PAY-{suffix}"},
        )
        # One open, high-severity, recently-raised finding (Discounts).
        c.execute(
            text("INSERT INTO auditcore.audit_findings "
                 "(tenant_id, journey_id, finding_type_code, severity, finding_status, finding_class, title, created_at_utc) "
                 "VALUES (:t, :j, 'DISCOUNT_ANOMALY', 'HIGH', 'OPEN', 'VIOLATION', 'Discount exceeds policy by ₹25,000', now())"),
            {"t": tenant_id, "j": journey_id},
        )
        # One resolved finding (Documents) with a resolution reason and dates apart.
        c.execute(
            text("INSERT INTO auditcore.audit_findings "
                 "(tenant_id, journey_id, finding_type_code, severity, finding_status, finding_class, title, "
                 " resolution_reason, created_at_utc, resolved_at_utc) "
                 "VALUES (:t, :j, 'DOCUMENT_MISSING', 'LOW', 'RESOLVED', 'DOCUMENT_GAP', 'Insurance document missing', "
                 " 'Uploaded', :created, :resolved)"),
            {
                "t": tenant_id, "j": journey_id,
                "created": datetime.now(UTC) - timedelta(days=10),
                "resolved": datetime.now(UTC) - timedelta(days=8),
            },
        )
    engine.dispose()
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield {"connection": c, "tenant_id": tenant_id, "journey_id": journey_id, "actor_id": actor_id}
    engine.dispose()


def _principal(actor_id: str, tenant_id: str) -> Principal:
    return Principal(subject=actor_id, tenant_id=tenant_id, permissions=(_PERMISSION,))


def test_compliance_report_header_and_summary(compliance_report_setup) -> None:
    setup = compliance_report_setup
    report = get_compliance_report(
        setup["tenant_id"], setup["journey_id"],
        principal=_principal(setup["actor_id"], setup["tenant_id"]),
        connection=setup["connection"],
    )

    assert report.header.customerDisplayName == "Sanjaya Kumar Mohanty"
    assert report.header.vin == "MA1TC2XXXJ123456"
    assert report.header.productLabel == "Scorpio N · Z8 (S) AT 2WD · Stealth Black"
    assert report.summary.totalFindings == 2
    assert report.summary.openFindings == 1
    assert report.summary.resolvedFindings == 1
    assert report.summary.highOrCriticalOpen == 1


def test_compliance_report_sections_carry_line_items_and_flags(compliance_report_setup) -> None:
    setup = compliance_report_setup
    report = get_compliance_report(
        setup["tenant_id"], setup["journey_id"],
        principal=_principal(setup["actor_id"], setup["tenant_id"]),
        connection=setup["connection"],
    )
    by_key = {section.key: section for section in report.sections}

    assert "PRICING" in by_key
    assert by_key["PRICING"].lineItems[0].actualAmount == 1_600_000.0

    discounts = by_key["DISCOUNTS"]
    assert discounts.lineItems[0].actualAmount == 75_000.0
    assert len(discounts.flags) == 1
    assert discounts.flags[0].severity == "HIGH"
    assert discounts.flags[0].isNew is True

    payments = by_key["PAYMENTS"]
    assert payments.lineItems[0].actualAmount == 1_642_500.0

    # Insurance/Finance/Registration have no rows in this fixture and no
    # findings either -- they must not appear as empty sections.
    assert "INSURANCE" not in by_key
    assert "FINANCE" not in by_key
    assert "REGISTRATION" not in by_key


def test_compliance_report_resolved_history(compliance_report_setup) -> None:
    setup = compliance_report_setup
    report = get_compliance_report(
        setup["tenant_id"], setup["journey_id"],
        principal=_principal(setup["actor_id"], setup["tenant_id"]),
        connection=setup["connection"],
    )

    assert len(report.resolvedHistory) == 1
    resolved = report.resolvedHistory[0]
    assert resolved.title == "Insurance document missing"
    assert resolved.resolutionReason == "Uploaded"
    assert resolved.resolvedAtUtc is not None
    assert resolved.resolvedAtUtc > resolved.createdAtUtc

    # And it should NOT also appear as an open flag anywhere.
    for section in report.sections:
        assert resolved.findingId not in {flag.findingId for flag in section.flags}
