from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from audit_core.uc03_finding_routing import (
    class_profile,
    classify_finding,
    escalation_level,
    permitted_actions,
    resolve_sla_policy,
    sla_due_at,
    visible_to_role,
)

BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


# ── classification ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("rule_key", "finding_type", "expected"),
    [
        ("BK_PAN_PRESENT", "CUSTOMER_IDENTITY_CONCERN", "DOCUMENT_GAP"),
        ("DOC_REQUIRED_ANSWER_NO:pan_card", "REQUIRED_DOCUMENT_ANSWER_NO", "DOCUMENT_GAP"),
        ("DL_NOT_INTIMATED", "DELIVERY_NOT_INTIMATED", "DATA_GAP"),
        ("PAY_UNVERIFIED_RECEIPT", "PAYMENT_UNVERIFIED", "DATA_GAP"),
        ("DL_VIN_RECONCILIATION", "VIN_RECONCILIATION_MISMATCH", "VIOLATION"),
        ("RE_PRICE_BOOKING_VS_INVOICE", "PRICING_ANOMALY", "VIOLATION"),
        ("RE_INSURANCE_COVER_NOTE_MISSING", "INSURANCE_ANOMALY", "DOCUMENT_GAP"),
        ("RE_DUPLICATE_CHASSIS_ACROSS_INVOICES", "CROSS_CASE_DUPLICATE", "VIOLATION"),
        (None, "DOCUMENT_EXCEPTION", "DOCUMENT_GAP"),
        (None, "PAYMENT_EXCEPTION", "DATA_GAP"),
        (None, "COMMERCIAL_EXCEPTION", "VIOLATION"),
        (None, "SOMETHING_UNKNOWN", "VIOLATION"),
        (None, None, "VIOLATION"),
    ],
)
def test_classify_finding(rule_key, finding_type, expected) -> None:
    assert classify_finding(rule_key, finding_type) == expected


def test_class_profile_owners_and_modes() -> None:
    assert class_profile("DOCUMENT_GAP").owner_role == "PC"
    assert class_profile("DOCUMENT_GAP").resolution_mode == "SELF_SERVICE"
    assert class_profile("VIOLATION").owner_role == "TL"
    assert class_profile("VIOLATION").resolution_mode == "ADJUDICATED"


# ── SLA ─────────────────────────────────────────────────────────────────────────

def test_default_sla_hours() -> None:
    policy = resolve_sla_policy({})
    assert sla_due_at(BASE, finding_class="DOCUMENT_GAP", severity="CRITICAL", policy=policy) == BASE + timedelta(hours=4)
    assert sla_due_at(BASE, finding_class="VIOLATION", severity="HIGH", policy=policy) == BASE + timedelta(hours=24)


def test_project_policy_overrides_sla() -> None:
    policy = resolve_sla_policy(
        {"uc03FindingSla": {"escalationStepHours": 6, "hours": {"VIOLATION": {"CRITICAL": 2}}}}
    )
    assert policy.escalation_step_hours == 6
    assert sla_due_at(BASE, finding_class="VIOLATION", severity="CRITICAL", policy=policy) == BASE + timedelta(hours=2)
    # untouched entries keep the default
    assert sla_due_at(BASE, finding_class="VIOLATION", severity="HIGH", policy=policy) == BASE + timedelta(hours=24)


def test_escalation_level_rises_after_due() -> None:
    policy = resolve_sla_policy({})  # step 24h
    due = BASE
    assert escalation_level(due, BASE - timedelta(hours=1), policy) == 0
    assert escalation_level(due, BASE + timedelta(minutes=1), policy) == 1
    assert escalation_level(due, BASE + timedelta(hours=25), policy) == 2
    assert escalation_level(due, BASE + timedelta(hours=49), policy) == 3
    assert escalation_level(due, BASE + timedelta(days=30), policy) == 3  # capped
    assert escalation_level(None, BASE, policy) == 0


# ── visibility ──────────────────────────────────────────────────────────────────

def test_visibility_ladder() -> None:
    # a PC-owned gap, on time → only PC sees it
    assert visible_to_role("PC", 0, "PC") is True
    assert visible_to_role("PC", 0, "TL") is False
    # escalated once → PC + TL
    assert visible_to_role("PC", 1, "TL") is True
    assert visible_to_role("PC", 1, "PM") is False
    # escalated twice → PC + TL + PM
    assert visible_to_role("PC", 2, "PM") is True
    # a TL-owned violation, on time → TL only; escalated → PM
    assert visible_to_role("TL", 0, "PM") is False
    assert visible_to_role("TL", 1, "PM") is True


# ── permitted actions ───────────────────────────────────────────────────────────

def test_self_serve_actions() -> None:
    assert set(permitted_actions(finding_class="DOCUMENT_GAP", role="PC", finding_status="OPEN")) == {
        "REMARK",
        "RESOLVE",
    }
    assert "ACCEPT" not in permitted_actions(finding_class="DATA_GAP", role="TL", finding_status="OPEN")


def test_adjudicated_actions() -> None:
    pc = permitted_actions(finding_class="VIOLATION", role="PC", finding_status="OPEN")
    assert pc == ["REMARK"]  # PC can only comment on a violation
    tl = permitted_actions(finding_class="VIOLATION", role="TL", finding_status="ACKNOWLEDGED")
    assert "ACCEPT" in tl and "REJECT" in tl
    # RESOLVE stays available as a plain close for TL and above
    assert "RESOLVE" in tl


def test_resolved_and_void_actions() -> None:
    assert "REOPEN" in permitted_actions(finding_class="VIOLATION", role="TL", finding_status="RESOLVED")
    assert "VOID" in permitted_actions(finding_class="VIOLATION", role="EXECUTIVE", finding_status="OPEN")
    assert "VOID" not in permitted_actions(finding_class="VIOLATION", role="TL", finding_status="OPEN")
