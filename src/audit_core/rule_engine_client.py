"""rule_engine_client.py — Audit Core adapter for the rule-engine phase-audit API.

The rule-engine (verigence/rule-engine, Railway service ``audit-api``) runs the
85 price/discount/document anomaly rules against a DI Subject and returns the
anomalies for a phase. Audit Core calls it after a Booking Review is confirmed
and after a Delivery is completed, then materialises the anomalies into
``auditcore.audit_findings`` (see ``uc03_rule_engine_findings``).

The integration is dormant until ``RULE_ENGINE_BASE_URL`` is configured — the
dependency yields ``None`` and callers skip the rule-engine step entirely.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Self

import httpx
import structlog

from audit_core.telemetry import trace_span

logger = structlog.get_logger(__name__)

# ServiceIntegration token audience the rule-engine verifies (aud=audit).
RULE_ENGINE_AUDIENCE = os.environ.get("RULE_ENGINE_AUDIENCE", "audit").strip() or "audit"

_VALID_PHASES = frozenset({"BOOKING", "DELIVERY", "FINANCE", "EXCHANGE", "CORPORATE"})


class RuleEngineClientError(RuntimeError):
    """The rule-engine phase-audit call did not return a usable result."""

    def __init__(self, *, status_code: int, code: str) -> None:
        super().__init__(f"rule-engine request failed: {code}")
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class RuleEngineAnomaly:
    rule_code: str
    severity: str
    category: str | None
    detail: str | None
    left_value: str | None
    right_value: str | None


@dataclass(frozen=True)
class RuleEnginePhaseResult:
    phase: str
    audit_run_id: str | None
    verdict: str | None
    anomalies: tuple[RuleEngineAnomaly, ...]


@dataclass(frozen=True)
class NotReadyRule:
    rule_code: str
    reason: str | None


@dataclass(frozen=True)
class RuleEngineReadiness:
    """``GET /v1/tenants/{t}/subjects/{s}/audit/readiness`` -- for every
    WITHIN_CASE rule (regardless of phase), whether the rule-engine would
    evaluate it right now (``ready``) or would SKIP it and why
    (``not_ready``). This is the rule-engine's own live "executed vs not
    applicable" answer -- audit-core's Execution Log relays it rather than
    re-deriving applicability itself."""

    ready: tuple[str, ...]
    not_ready: tuple[NotReadyRule, ...]


@dataclass(frozen=True)
class RuleEngineRunSummary:
    """One row of ``GET /v1/tenants/{t}/subjects/{s}/audit/runs`` -- an
    aggregate summary of a past phase-audit run for one subject. Field
    names mirror the rule-engine's raw ``audit.audit_runs`` columns
    (snake_case; that endpoint returns the DB row shape unchanged)."""

    audit_run_id: str
    audit_scope: str | None
    trigger_mode: str | None
    triggered_by: str | None
    total_rules: int
    pass_count: int
    fail_count: int
    skipped_count: int
    critical_fail: int
    warning_fail: int
    info_fail: int
    verdict: str | None
    started_at_utc: str | None
    completed_at_utc: str | None


@dataclass(frozen=True)
class RuleEngineRule:
    """One row of the rule-engine's own ``audit.audit_rules`` catalog, as
    returned by ``GET /v1/tenants/{t}/audit/rules`` -- the source-of-truth
    definition for a RULE_ENGINE-executor row in audit-core's own
    ``rule_definitions`` registry (see uc03_rule_registry.py)."""

    rule_code: str
    category: str | None
    audit_scope: str | None
    phases: tuple[str, ...]
    comparator: str | None
    threshold: float | None
    severity: str | None
    finding_message: str | None
    enabled: bool


class RuleEngineClient:
    """Thin adapter for ``POST /v1/tenants/{t}/subjects/{s}/audit/{phase}``."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("rule-engine base URL is required")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def evaluate_phase(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        phase: str,
    ) -> RuleEnginePhaseResult:
        if not token:
            raise ValueError("rule-engine bearer token is required")
        normalised_phase = phase.strip().upper()
        if normalised_phase not in _VALID_PHASES:
            raise ValueError(f"unsupported rule-engine phase: {phase}")

        path = (
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}"
            f"/audit/{normalised_phase.lower()}"
        )
        payload = self._request_data(
            "POST",
            path,
            operation=f"evaluate_{normalised_phase.lower()}",
            token=token,
            json={"includeSkipped": False, "failFast": False},
        )
        return _phase_result(normalised_phase, payload)

    def list_rules(self, *, token: str, tenant_id: str) -> tuple[RuleEngineRule, ...]:
        """``GET /v1/tenants/{t}/audit/rules`` -- the rule-engine's own full
        catalog. Used to keep audit-core's unified ``rule_definitions``
        registry in sync with the rule-engine's rows; never used to
        evaluate anything (evaluation stays ``evaluate_phase``'s job)."""
        if not token:
            raise ValueError("rule-engine bearer token is required")
        payload = self._request_data(
            "GET",
            f"/v1/tenants/{tenant_id}/audit/rules",
            operation="list_rules",
            token=token,
        )
        raw_rules = payload.get("rules")
        rules: list[RuleEngineRule] = []
        if isinstance(raw_rules, list):
            for raw in raw_rules:
                if not isinstance(raw, dict):
                    continue
                rule_code = str(raw.get("rule_code") or "").strip()
                if not rule_code:
                    continue
                raw_phases = raw.get("phases")
                threshold = raw.get("threshold")
                rules.append(
                    RuleEngineRule(
                        rule_code=rule_code,
                        category=_opt_str(raw.get("category")),
                        audit_scope=_opt_str(raw.get("audit_scope")),
                        phases=tuple(raw_phases) if isinstance(raw_phases, list) else (),
                        comparator=_opt_str(raw.get("comparator")),
                        threshold=float(threshold) if isinstance(threshold, (int, float)) else None,
                        severity=_opt_str(raw.get("severity")),
                        finding_message=_opt_str(raw.get("finding_message")),
                        enabled=bool(raw.get("enabled", True)),
                    )
                )
        return tuple(rules)

    def readiness(
        self, *, token: str, tenant_id: str, subject_id: str
    ) -> RuleEngineReadiness:
        """``GET /v1/tenants/{t}/subjects/{s}/audit/readiness`` -- used by the
        Execution Log to write a SKIPPED row (with the rule-engine's own
        reason) for every rule that wasn't applicable, alongside PASS/FAIL
        for the ones that were."""
        if not token:
            raise ValueError("rule-engine bearer token is required")
        payload = self._request_data(
            "GET",
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}/audit/readiness",
            operation="readiness",
            token=token,
        )
        ready = tuple(
            str(code) for code in (payload.get("ready") or []) if str(code).strip()
        )
        not_ready: list[NotReadyRule] = []
        raw_not_ready = payload.get("notReady")
        if isinstance(raw_not_ready, list):
            for raw in raw_not_ready:
                if not isinstance(raw, dict):
                    continue
                rule_code = str(raw.get("ruleCode") or "").strip()
                if not rule_code:
                    continue
                not_ready.append(
                    NotReadyRule(rule_code=rule_code, reason=_opt_str(raw.get("reason")))
                )
        return RuleEngineReadiness(ready=ready, not_ready=tuple(not_ready))

    def runs(
        self, *, token: str, tenant_id: str, subject_id: str
    ) -> tuple[RuleEngineRunSummary, ...]:
        """``GET /v1/tenants/{t}/subjects/{s}/audit/runs`` -- historical
        per-subject run summaries (aggregate counts only, not per-rule).
        Not on the Execution Log write path today; kept for the future
        run-history read surface Phase 5's UI will want."""
        if not token:
            raise ValueError("rule-engine bearer token is required")
        payload = self._request_data(
            "GET",
            f"/v1/tenants/{tenant_id}/subjects/{subject_id}/audit/runs",
            operation="runs",
            token=token,
        )
        raw_runs = payload.get("runs")
        runs: list[RuleEngineRunSummary] = []
        if isinstance(raw_runs, list):
            for raw in raw_runs:
                if not isinstance(raw, dict):
                    continue
                audit_run_id = str(raw.get("audit_run_id") or "").strip()
                if not audit_run_id:
                    continue
                runs.append(
                    RuleEngineRunSummary(
                        audit_run_id=audit_run_id,
                        audit_scope=_opt_str(raw.get("audit_scope")),
                        trigger_mode=_opt_str(raw.get("trigger_mode")),
                        triggered_by=_opt_str(raw.get("triggered_by")),
                        total_rules=int(raw.get("total_rules") or 0),
                        pass_count=int(raw.get("pass_count") or 0),
                        fail_count=int(raw.get("fail_count") or 0),
                        skipped_count=int(raw.get("skipped_count") or 0),
                        critical_fail=int(raw.get("critical_fail") or 0),
                        warning_fail=int(raw.get("warning_fail") or 0),
                        info_fail=int(raw.get("info_fail") or 0),
                        verdict=_opt_str(raw.get("verdict")),
                        started_at_utc=_opt_str(raw.get("started_at_utc")),
                        completed_at_utc=_opt_str(raw.get("completed_at_utc")),
                    )
                )
        return tuple(runs)

    # ── internals ────────────────────────────────────────────────────────────

    def _request_data(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        token: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        started = time.perf_counter()
        result = "SUCCESS"
        try:
            with trace_span(
                "audit_core.dependency.rule_engine",
                attributes={
                    "dependency": "rule_engine",
                    "operation": operation,
                    "method": method,
                },
            ):
                try:
                    response = self._client.request(method, path, headers=headers, **kwargs)
                except httpx.HTTPError as exc:
                    result = "UNAVAILABLE"
                    raise RuleEngineClientError(
                        status_code=503, code="RULE_ENGINE_UNAVAILABLE"
                    ) from exc
                if response.status_code < 200 or response.status_code >= 300:
                    result = "FAILURE"
                    raise RuleEngineClientError(
                        status_code=response.status_code, code="RULE_ENGINE_HTTP_ERROR"
                    )
                try:
                    envelope = response.json()
                except ValueError as exc:
                    result = "FAILURE"
                    raise RuleEngineClientError(
                        status_code=response.status_code, code="RULE_ENGINE_BAD_ENVELOPE"
                    ) from exc
        finally:
            logger.debug(
                "rule_engine_dependency_call",
                operation=operation,
                method=method,
                result=result,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        if not isinstance(envelope, dict) or envelope.get("errorCode") != "000":
            raise RuleEngineClientError(
                status_code=response.status_code, code="RULE_ENGINE_BUSINESS_ERROR"
            )
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise RuleEngineClientError(
                status_code=response.status_code, code="RULE_ENGINE_BAD_ENVELOPE"
            )
        return data


def _phase_result(phase: str, data: dict[str, Any]) -> RuleEnginePhaseResult:
    raw_anomalies = data.get("anomalies")
    anomalies: list[RuleEngineAnomaly] = []
    if isinstance(raw_anomalies, list):
        for raw in raw_anomalies:
            if not isinstance(raw, dict):
                continue
            rule_code = str(raw.get("ruleCode") or "").strip()
            if not rule_code:
                continue
            anomalies.append(
                RuleEngineAnomaly(
                    rule_code=rule_code,
                    severity=str(raw.get("severity") or "").strip().upper(),
                    category=_opt_str(raw.get("category")),
                    detail=_opt_str(raw.get("detail")),
                    left_value=_opt_str(raw.get("leftValue")),
                    right_value=_opt_str(raw.get("rightValue")),
                )
            )
    return RuleEnginePhaseResult(
        phase=phase,
        audit_run_id=_opt_str(data.get("auditRunId")),
        verdict=_opt_str(data.get("verdict")),
        anomalies=tuple(anomalies),
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def get_rule_engine_client() -> Iterator[RuleEngineClient | None]:
    """FastAPI dependency. Yields ``None`` when the integration is not configured."""
    base_url = os.environ.get("RULE_ENGINE_BASE_URL", "").strip()
    if not base_url:
        yield None
        return
    timeout = _positive_float(os.environ.get("RULE_ENGINE_TIMEOUT_SECONDS"), 10.0)
    with RuleEngineClient(base_url=base_url, timeout_seconds=timeout) as client:
        yield client


def build_rule_engine_client() -> RuleEngineClient | None:
    """Non-dependency factory for background tasks. Caller owns ``close()``."""
    base_url = os.environ.get("RULE_ENGINE_BASE_URL", "").strip()
    if not base_url:
        return None
    timeout = _positive_float(os.environ.get("RULE_ENGINE_TIMEOUT_SECONDS"), 10.0)
    return RuleEngineClient(base_url=base_url, timeout_seconds=timeout)


def _positive_float(raw: str | None, default: float) -> float:
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default
