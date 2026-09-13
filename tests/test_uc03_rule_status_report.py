"""test_uc03_rule_status_report.py — Compliance Report's Rule Status tab.

rule_definitions is global reference data (no tenant_id column, seeded once
by migration 0083) -- same convention test_uc03_rule_registry.py already
uses: no fixture rows needed for it, just real seeded rule_codes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.security import HumanPrincipal
from audit_core.security_authorization import SecurityAuthorizationDecision
from audit_core.uc03_rule_status_report import get_rule_status


@dataclass
class _AllowAuthorization:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str) -> SecurityAuthorizationDecision:
        self.calls.append((user_id, tenant_id, permission_key))
        return SecurityAuthorizationDecision(
            allowed=True, reason_code="AUTHORIZED", user_id=user_id,
            tenant_id=tenant_id, permission_key=permission_key, role_key="TL",
        )


@pytest.fixture
def rule_status_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-rs-{suffix}"
    actor_id = f"tl-{suffix}"

    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"RS-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"RS-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'RS', :o, :cat, CURRENT_DATE - 1, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"RS-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"RS-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets "
                 "(tenant_id, dealer_id, outlet_code, outlet_name, outlet_classification, status) "
                 "VALUES (:t, :d, :c, 'O', 'ONSITE', 'ACTIVE') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"RS-O-{suffix}"},
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
                 "VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id, journey_reference) "
                 "VALUES (:t, :d, :o, :cu, :ref) RETURNING journey_id"),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "ref": f"RS-J-{suffix}"},
        ).scalar_one()

        # WRONG_DOCUMENT (instrumented) -- a clean PASS.
        c.execute(
            text("INSERT INTO auditcore.rule_executions "
                 "(tenant_id, journey_id, rule_code, triggering_event, outcome) "
                 "VALUES (:t, :j, 'WRONG_DOCUMENT', 'DOCUMENT_SYNCED', 'PASS')"),
            {"t": tenant_id, "j": journey_id},
        )
        # DUPLICATE_RECEIPT (instrumented) -- SKIPPED with a reason.
        c.execute(
            text("INSERT INTO auditcore.rule_executions "
                 "(tenant_id, journey_id, rule_code, triggering_event, outcome, reason) "
                 "VALUES (:t, :j, 'DUPLICATE_RECEIPT', 'DOCUMENT_SYNCED', 'SKIPPED', "
                 "'no receipt documents on this Journey yet')"),
            {"t": tenant_id, "j": journey_id},
        )
        # MANUAL_VERIFICATION (instrumented) -- deliberately left with NO
        # execution row: a true PENDING (awaiting its trigger event).

        # BK_DOCKET_PRESENT (NOT instrumented) -- a real finding exists for
        # it, so it should be reported EXECUTED/FAIL (inferred), not PENDING.
        c.execute(
            text("INSERT INTO auditcore.audit_findings "
                 "(tenant_id, journey_id, finding_type_code, severity, finding_status, "
                 " finding_class, title, rule_key, created_at_utc) "
                 "VALUES (:t, :j, 'DOCUMENT_EXCEPTION', 'HIGH', 'OPEN', 'VIOLATION', "
                 "'Docket missing', 'BK_DOCKET_PRESENT', now())"),
            {"t": tenant_id, "j": journey_id},
        )
        # BK_PAN_PRESENT (NOT instrumented) -- deliberately left with no
        # execution row and no finding: a genuinely unknown PENDING, flagged
        # with the coverage-gap note.

        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        yield tenant_id, journey_id, actor_id, c
    engine.dispose()


def test_rule_status_classifies_all_three_buckets_plus_inferred_execution(
    rule_status_setup,
) -> None:
    tenant_id, journey_id, actor_id, connection = rule_status_setup

    response = get_rule_status(
        tenant_id, journey_id,
        human_principal=HumanPrincipal(subject=actor_id),
        authorization_client=_AllowAuthorization(),
        connection=connection,
    )

    by_code = {r.ruleCode: r for r in response.rules}

    wrong_document = by_code["WRONG_DOCUMENT"]
    assert wrong_document.status == "EXECUTED"
    assert wrong_document.outcome == "PASS"
    assert wrong_document.note is None

    duplicate_receipt = by_code["DUPLICATE_RECEIPT"]
    assert duplicate_receipt.status == "NOT_APPLICABLE"
    assert duplicate_receipt.reason == "no receipt documents on this Journey yet"

    manual_verification = by_code["MANUAL_VERIFICATION"]
    assert manual_verification.status == "PENDING"
    assert manual_verification.note is None  # instrumented -- a real pending, no caveat needed

    bk_docket = by_code["BK_DOCKET_PRESENT"]
    assert bk_docket.status == "EXECUTED"
    assert bk_docket.outcome == "FAIL"
    assert "Inferred from an existing Audit Flag" in bk_docket.note

    bk_pan = by_code["BK_PAN_PRESENT"]
    assert bk_pan.status == "PENDING"
    assert "Not yet wired to the Execution Log" in bk_pan.note

    # Every real seeded rule_definitions row appears exactly once.
    assert len(response.rules) == len(by_code)


def test_rule_status_summary_counts_match_the_rule_list(rule_status_setup) -> None:
    tenant_id, journey_id, actor_id, connection = rule_status_setup

    response = get_rule_status(
        tenant_id, journey_id,
        human_principal=HumanPrincipal(subject=actor_id),
        authorization_client=_AllowAuthorization(),
        connection=connection,
    )

    assert response.summary.executed == sum(1 for r in response.rules if r.status == "EXECUTED")
    assert response.summary.pending == sum(1 for r in response.rules if r.status == "PENDING")
    assert response.summary.notApplicable == sum(1 for r in response.rules if r.status == "NOT_APPLICABLE")
    assert response.summary.executed + response.summary.pending + response.summary.notApplicable == len(response.rules)
    # Sanity: this tenant's fresh journey has real rules in every bucket.
    assert response.summary.executed >= 2
    assert response.summary.pending >= 2
    assert response.summary.notApplicable >= 1
