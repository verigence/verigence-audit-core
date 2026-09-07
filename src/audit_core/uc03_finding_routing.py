"""uc03_finding_routing.py — classify audit findings and route them by role + SLA.

Every audit finding (machine or human) belongs to one *finding class*:

  DATA_GAP      missing / unconfirmed data on the case      → owned by PC, self-serve
  DOCUMENT_GAP  a required document is missing or rejected  → owned by PC, self-serve
  VIOLATION     a rule breach that needs a human verdict    → owned by TL, adjudicated

The class decides the starting owner role, whether the resolution is *self-serve*
(the PC fixes it and marks it done) or *adjudicated* (TL/PM Accept or Reject), and
the SLA. When a finding passes its SLA it is not reassigned — it stays with its
owner but its *escalation level* rises, which makes it visible (with a badge) to
the next role up the ladder:

    PC  →  TL  →  PM  →  EXECUTIVE

All logic here is pure (no DB, no time source other than an injected ``now``) so it
is cheap to evaluate on every read and easy to test.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

FindingClass = Literal["DATA_GAP", "DOCUMENT_GAP", "VIOLATION"]
ResolutionMode = Literal["SELF_SERVICE", "ADJUDICATED"]
Disposition = Literal["FIXED", "CONFIRMED_BREACH", "NOT_A_BREACH"]

_ROLE_LADDER: tuple[str, ...] = ("PC", "TL", "PM", "EXECUTIVE")


def _role_rank(role: str) -> int:
    normalized = (role or "").strip().upper()
    if normalized in {"EXEC", "EXECUTIVE"}:
        normalized = "EXECUTIVE"
    try:
        return _ROLE_LADDER.index(normalized)
    except ValueError:
        return -1


# ── class → routing profile ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClassProfile:
    owner_role: str
    resolution_mode: ResolutionMode


_CLASS_PROFILE: dict[str, ClassProfile] = {
    "DATA_GAP": ClassProfile(owner_role="PC", resolution_mode="SELF_SERVICE"),
    "DOCUMENT_GAP": ClassProfile(owner_role="PC", resolution_mode="SELF_SERVICE"),
    "VIOLATION": ClassProfile(owner_role="TL", resolution_mode="ADJUDICATED"),
}

DEFAULT_CLASS: FindingClass = "VIOLATION"  # unknown → human eyes


def class_profile(finding_class: str | None) -> ClassProfile:
    return _CLASS_PROFILE.get((finding_class or "").upper(), _CLASS_PROFILE[DEFAULT_CLASS])


# ── classification ──────────────────────────────────────────────────────────────
#
# rule_key is matched on the token before any ":" suffix (rule keys such as
# "DOC_REQUIRED_ANSWER_NO:pan_card" carry a per-requirement discriminator).

_DOCUMENT_GAP_RULE_PREFIXES: frozenset[str] = frozenset({
    "BK_DOCKET_PRESENT",
    "BK_PAN_PRESENT",
    "BK_MIN_BOOKING_PROOF_PRESENT",
    "BK_CONDITIONAL_DOCS_ADDRESSED",
    "BK_REQUIRED_CAPTURE_COMPLETE",
    "DOC_REQUIRED_ANSWER_NO",
    "DL_V2_REQUIRED_DOCUMENT_MISSING",
    "DL_V2_DOCUMENT_PROCESSING_FAILED",
})
_DATA_GAP_RULE_PREFIXES: frozenset[str] = frozenset({
    "DL_NOT_INTIMATED",
    "PAY_UNVERIFIED_RECEIPT",
})
_VIOLATION_RULE_PREFIXES: frozenset[str] = frozenset({
    "DL_VIN_RECONCILIATION",
    "WF_BOOKING_INCOMPLETE_AT_DELIVERY_START",
    "WF_DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE",
})

_DOCUMENT_GAP_TYPES: frozenset[str] = frozenset({
    "DOCUMENT_EXCEPTION",
    "DELIVERY_DOCUMENT_MISSING",
    "REQUIRED_DOCUMENT_ANSWER_NO",
})
_DATA_GAP_TYPES: frozenset[str] = frozenset({
    "PAYMENT_EXCEPTION",
    "PAYMENT_UNVERIFIED",
    "DELIVERY_NOT_INTIMATED",
})
_VIOLATION_TYPES: frozenset[str] = frozenset({
    "VIN_RECONCILIATION_MISMATCH",
    "DELIVERY_COMPLETED_WITH_AUDIT_INCOMPLETE",
    "BOOKING_PREREQUISITES_INCOMPLETE_AT_DELIVERY",
    "COMMERCIAL_EXCEPTION",
    "PROCESS_NON_COMPLIANCE",
    "CUSTOMER_IDENTITY_CONCERN",
    "PHYSICAL_OBSERVATION",
    "DELIVERY_EXCEPTION",
    # rule-engine categories mapped by uc03_rule_engine_findings
    "PRICING_ANOMALY",
    "DISCOUNT_ANOMALY",
    "ACCESSORY_ANOMALY",
    "INSURANCE_ANOMALY",
    "RTO_ANOMALY",
    "VEHICLE_IDENTITY_ANOMALY",
    "CROSS_CASE_DUPLICATE",
    "RULE_ENGINE_ANOMALY",
})


def _rule_stem(rule_key: str | None) -> str:
    if not rule_key:
        return ""
    return rule_key.split(":", 1)[0].strip().upper()


def classify_finding(rule_key: str | None, finding_type_code: str | None) -> FindingClass:
    """Best-effort finding class. rule_key wins; finding_type_code is the fallback."""
    stem = _rule_stem(rule_key)
    if stem:
        if stem in _DOCUMENT_GAP_RULE_PREFIXES:
            return "DOCUMENT_GAP"
        if stem in _DATA_GAP_RULE_PREFIXES:
            return "DATA_GAP"
        if stem in _VIOLATION_RULE_PREFIXES:
            return "VIOLATION"
        # rule-engine findings: "RE_<CODE>", "RE_<...>_MISSING" is a document gap
        if stem.startswith("RE_"):
            return "DOCUMENT_GAP" if stem.endswith("_MISSING") else "VIOLATION"

    kind = (finding_type_code or "").strip().upper()
    if kind in _DOCUMENT_GAP_TYPES:
        return "DOCUMENT_GAP"
    if kind in _DATA_GAP_TYPES:
        return "DATA_GAP"
    if kind in _VIOLATION_TYPES:
        return "VIOLATION"
    return DEFAULT_CLASS


# ── SLA policy ──────────────────────────────────────────────────────────────────

_DEFAULT_SLA_HOURS: dict[str, dict[str, int]] = {
    "DATA_GAP": {"CRITICAL": 4, "HIGH": 8, "MEDIUM": 24, "LOW": 48, "INFO": 72},
    "DOCUMENT_GAP": {"CRITICAL": 4, "HIGH": 8, "MEDIUM": 24, "LOW": 48, "INFO": 72},
    "VIOLATION": {"CRITICAL": 8, "HIGH": 24, "MEDIUM": 48, "LOW": 96, "INFO": 120},
}
_DEFAULT_ESCALATION_STEP_HOURS = 24
_MAX_ESCALATION_LEVEL = 3  # PC(owner) → +TL → +PM → +EXECUTIVE


@dataclass(frozen=True)
class SlaPolicy:
    hours: dict[str, dict[str, int]]
    escalation_step_hours: int

    def resolve_hours(self, finding_class: str, severity: str) -> int:
        table = self.hours.get(finding_class.upper()) or self.hours[DEFAULT_CLASS]
        return table.get(severity.upper(), table.get("MEDIUM", 24))


def resolve_sla_policy(policy_settings: Any) -> SlaPolicy:
    """Merge ``policy_settings["uc03FindingSla"]`` over the built-in defaults."""
    hours = {cls: dict(table) for cls, table in _DEFAULT_SLA_HOURS.items()}
    step = _DEFAULT_ESCALATION_STEP_HOURS

    override = policy_settings.get("uc03FindingSla") if isinstance(policy_settings, dict) else None
    if isinstance(override, dict):
        raw_step = override.get("escalationStepHours")
        if isinstance(raw_step, (int, float)) and raw_step > 0:
            step = int(raw_step)
        raw_hours = override.get("hours")
        if isinstance(raw_hours, dict):
            for cls, table in raw_hours.items():
                key = str(cls).upper()
                if key in hours and isinstance(table, dict):
                    for severity, value in table.items():
                        if isinstance(value, (int, float)) and value > 0:
                            hours[key][str(severity).upper()] = int(value)
    return SlaPolicy(hours=hours, escalation_step_hours=step)


def sla_due_at(
    created_at: datetime,
    *,
    finding_class: str,
    severity: str,
    policy: SlaPolicy,
) -> datetime:
    return created_at + timedelta(hours=policy.resolve_hours(finding_class, severity))


def escalation_level(sla_due_at_utc: datetime | None, now: datetime, policy: SlaPolicy) -> int:
    """0 while on time; +1 per ``escalation_step_hours`` past due, capped."""
    if sla_due_at_utc is None or now <= sla_due_at_utc:
        return 0
    overdue_hours = (now - sla_due_at_utc).total_seconds() / 3600.0
    step = max(policy.escalation_step_hours, 1)
    return min(_MAX_ESCALATION_LEVEL, 1 + int(overdue_hours // step))


def visible_to_role(owner_role: str, level: int, role: str) -> bool:
    """A finding at escalation ``level`` is visible to its owner plus ``level`` roles up."""
    owner_rank = _role_rank(owner_role)
    role_rank = _role_rank(role)
    if owner_rank < 0 or role_rank < 0:
        return False
    if role_rank == owner_rank:
        return True
    return owner_rank < role_rank <= owner_rank + max(level, 0)


# ── permitted actions ───────────────────────────────────────────────────────────

_OPEN_STATUSES = {"OPEN", "ACKNOWLEDGED"}


def permitted_actions(
    *,
    finding_class: str,
    role: str,
    finding_status: str,
) -> list[str]:
    """Actions this role may take on a finding of this class in this state."""
    profile = class_profile(finding_class)
    role_norm = (role or "").strip().upper()
    if role_norm in {"EXEC", "EXECUTIVE"}:
        role_norm = "EXECUTIVE"
    rank = _role_rank(role_norm)
    status = (finding_status or "").upper()
    actions: list[str] = []

    if status in _OPEN_STATUSES:
        actions.append("REMARK")

    if profile.resolution_mode == "SELF_SERVICE":
        # the PC owner and anyone above can mark a self-serve gap fixed
        if status in _OPEN_STATUSES and rank >= 0:
            actions.append("RESOLVE")
    elif status in _OPEN_STATUSES and rank >= _role_rank("TL"):  # ADJUDICATED
        # Accept / Reject are the meaningful verdicts on a violation; RESOLVE
        # stays available as a plain close for TL and above.
        actions.append("ACKNOWLEDGE")
        actions.append("ACCEPT")
        actions.append("REJECT")
        actions.append("RESOLVE")

    if status == "RESOLVED" and rank >= _role_rank("TL"):
        actions.append("REOPEN")
    if rank >= _role_rank("EXECUTIVE") and status != "VOIDED":
        actions.append("VOID")

    # de-dupe, keep order
    seen: set[str] = set()
    ordered: list[str] = []
    for action in actions:
        if action not in seen:
            seen.add(action)
            ordered.append(action)
    return ordered
