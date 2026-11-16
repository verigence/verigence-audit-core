"""AI cost on the SuperAdmin System Health page (product owner 2026-11-16).

"If we can capture the cost it will help ... I would like to know how much cost
we are entailing."

GET /v1/admin/system-health/ai-cost?days=30[&tenantId=] answers what the AI
provider calls of Document Intelligence cost: per IST day, per Project and per
document type, split into reading (extraction) and sorting (classification).

Where the figures come from:

- Document Intelligence prices every answered provider call when it is made,
  at the provider's list price for the model and service tier, and keeps the
  tokens and the cost with the call (DI GET /v1/platform/ai-usage). Failed
  answers are billed too and are counted.
- A read made before 16 Nov 2026 kept its tokens but no cost: it is priced
  from those tokens at the same list price (``costFromTokensUsd`` says how
  much of a figure was made this way).
- A classification made before usage began to be kept has neither tokens nor
  cost. It is counted (``callsWithoutCost``) and never estimated.

These are list prices in US dollars: what the calls cost before taxes, not
the provider's invoice. The rupee figure is the dollar figure at
``AUDIT_CORE_USD_INR`` (rupees per dollar, set on the service), shown as an
approximation.

Counts and amounts only; no Journey, document or customer data leaves this route.
"""
from __future__ import annotations

import calendar
import os
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.system_health_api import project_names
from audit_core.system_health_di import DiHealthUnavailable, fetch_di_ai_usage
from audit_core.uc03_p2_object_keys import IST

router = APIRouter(tags=["system-health"])
logger = structlog.get_logger(__name__)

MAX_DAYS = 92
IST_NAME = "Asia/Kolkata"
# Rupees per US dollar for the approximate rupee figure, when the service sets none.
DEFAULT_USD_INR = 94.0
# A month's projection from fewer complete days than this says more about the
# first days than about the month.
MIN_DAYS_TO_PROJECT = 3


def usd_inr() -> float:
    raw = os.environ.get("AUDIT_CORE_USD_INR", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_USD_INR
    except ValueError:
        return DEFAULT_USD_INR
    return value if value > 0 else DEFAULT_USD_INR


def _money(value: float) -> float:
    return round(value, 4)


def _empty_money() -> dict[str, Any]:
    return {"costUsd": 0.0, "extractionUsd": 0.0, "classificationUsd": 0.0, "costFromTokensUsd": 0.0,
            "reads": 0, "classifications": 0, "failedCalls": 0, "callsWithoutCost": 0,
            "promptTokens": 0, "outputTokens": 0}


def _add(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    """Add one DI usage row to a bucket. A row with no cost is counted, never costed."""
    calls = int(row.get("calls") or 0)
    reading = row.get("capability") == "EXTRACTION"
    bucket["reads" if reading else "classifications"] += calls
    bucket["failedCalls"] += int(row.get("failedCalls") or 0)
    bucket["promptTokens"] += int(row.get("promptTokens") or 0)
    bucket["outputTokens"] += int(row.get("outputTokens") or 0)
    cost = row.get("costUsd")
    if cost is None:
        # Nothing of this row can be costed: every call of it is without a cost.
        bucket["callsWithoutCost"] += calls
        return
    bucket["callsWithoutCost"] += int(row.get("callsWithoutUsage") or 0)
    bucket["extractionUsd" if reading else "classificationUsd"] += float(cost)
    bucket["costUsd"] += float(cost)
    bucket["costFromTokensUsd"] += float(row.get("costFromTokensUsd") or 0)


def _rounded(bucket: dict[str, Any]) -> dict[str, Any]:
    return {k: (_money(v) if k.endswith("Usd") else v) for k, v in bucket.items()}


def month_figures(daily: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """This month so far, and the month at the pace of its complete days. The
    projection needs the window to reach the 1st and a few complete days; it is a
    straight-line figure, not a forecast."""
    first = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    in_month = [d for d in daily if d["date"] >= first.isoformat()]
    covered = bool(daily) and daily[0]["date"] <= first.isoformat()
    so_far = sum(d["costUsd"] for d in in_month)
    complete = [d for d in in_month if d["date"] < today.isoformat()]
    projected = None
    if covered and len(complete) >= MIN_DAYS_TO_PROJECT:
        projected = _money(sum(d["costUsd"] for d in complete) / len(complete) * days_in_month)
    return {
        "month": first.strftime("%Y-%m"), "coveredFromFirst": covered, "costUsd": _money(so_far),
        "completeDays": len(complete), "daysInMonth": days_in_month, "projectedUsd": projected,
    }


def build_ai_cost(
    usage: dict[str, Any], *, names: dict[str, dict[str, Any]], journeys: dict[str, int],
    tenant_id: str | None, days: int, today: date, rate: float,
) -> dict[str, Any]:
    """DI's usage rows as the page shows them: totals, one row per IST day (every
    day of the window, zero-filled), per Project and per document type."""
    rows = [r for r in usage.get("rows") or [] if isinstance(r, dict)
            and (tenant_id is None or str(r.get("tenantId")) == tenant_id)]
    first_day = today - timedelta(days=days - 1)
    by_day: dict[str, dict[str, Any]] = {
        (first_day + timedelta(days=n)).isoformat(): _empty_money() for n in range(days)}
    by_project: dict[str, dict[str, Any]] = {}
    by_type: dict[str, dict[str, Any]] = {}
    totals = _empty_money()
    for row in rows:
        day = by_day.get(str(row.get("date")))
        if day is None:  # outside the window (a clock or zone edge): not this page's
            continue
        _add(day, row)
        _add(totals, row)
        _add(by_project.setdefault(str(row.get("tenantId")), _empty_money()), row)
        if row.get("capability") == "EXTRACTION":
            _add(by_type.setdefault(str(row.get("documentTypeKey") or "unknown"), _empty_money()), row)

    daily = [{"date": d, **_rounded(b)} for d, b in sorted(by_day.items())]
    projects = []
    for tenant, bucket in by_project.items():
        info = names.get(tenant, {})
        with_uploads = int(journeys.get(tenant) or 0)
        projects.append({
            "tenantId": tenant, "projectName": info.get("name") or tenant, "projectStatus": info.get("status"),
            **_rounded(bucket), "journeysWithUploads": with_uploads,
            "costPerJourneyUsd": _money(bucket["costUsd"] / with_uploads) if with_uploads else None,
        })
    projects.sort(key=lambda p: -p["costUsd"])
    document_types = [{
        "documentTypeKey": key, **_rounded(bucket),
        "costPerReadUsd": round(bucket["costUsd"] / bucket["reads"], 5) if bucket["reads"] else None,
    } for key, bucket in by_type.items()]
    document_types.sort(key=lambda t: -t["costUsd"])

    recorded_since = usage.get("classificationRecordedSinceUtc")
    notes = [
        "List prices in US dollars, before taxes: what the calls cost, not the provider's invoice.",
        f"Rupees are the dollar figure at ₹{rate:g} to the dollar, as an approximation.",
    ]
    if totals["costFromTokensUsd"]:
        notes.append(
            f"US${_money(totals['costFromTokensUsd']):,.2f} of this window is priced from the tokens of reads made "
            "before 16 Nov 2026, when a read kept its tokens but not its cost.")
    if totals["callsWithoutCost"]:
        notes.append(
            f"{totals['callsWithoutCost']:,} calls have no cost: sorting (classification) calls made before their "
            "tokens began to be kept, and failed reads that kept nothing. They are counted, not estimated.")
    return {
        "days": days, "firstDay": first_day.isoformat(), "timezone": IST_NAME, "currency": "USD", "usdInr": rate,
        "tenantId": tenant_id,
        "projectOptions": sorted(
            ({"tenantId": t, "projectName": i.get("name") or t, "projectStatus": i.get("status")}
             for t, i in names.items()), key=lambda p: (p["projectStatus"] != "ACTIVE", str(p["projectName"]).lower())),
        "prices": usage.get("prices") or [],
        "classificationRecordedSinceUtc": recorded_since,
        "totals": _rounded(totals),
        "today": next((d for d in daily if d["date"] == today.isoformat()), None),
        "month": month_figures(daily, today),
        "daily": daily, "projects": projects, "documentTypes": document_types,
        "notes": notes,
    }


def journeys_with_uploads(connection: Connection, tenants: list[str], since: datetime) -> dict[str, int]:
    """Per Project, the Journeys that had a page uploaded in the window: what the
    spend was for."""
    counts: dict[str, int] = {}
    for tenant in tenants:
        set_tenant_context(connection, tenant)
        counts[tenant] = int(connection.execute(text(
            "SELECT count(DISTINCT journey_id) FROM auditcore.p2_document_queue "
            "WHERE tenant_id = :t AND created_at_utc >= :s"), {"t": tenant, "s": since}).scalar_one() or 0)
    return counts


@router.get("/v1/admin/system-health/ai-cost")
def system_health_ai_cost(
    _admin: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 30,
    tenant_id: Annotated[str | None, Query(alias="tenantId", max_length=128)] = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    today = now.astimezone(IST).date()
    since = datetime.combine(today - timedelta(days=days - 1), time.min, tzinfo=IST).astimezone(UTC)
    names = project_names(connection)
    error: str | None = None
    try:
        usage = fetch_di_ai_usage(days, IST_NAME)
    except DiHealthUnavailable as exc:
        # Shown on the page; never a failed page.
        usage, error = {"rows": []}, str(exc)
    tenants = [tenant_id] if tenant_id else sorted(names)
    body = build_ai_cost(
        usage, names=names, journeys=journeys_with_uploads(connection, tenants, since),
        tenant_id=tenant_id, days=days, today=today, rate=usd_inr())
    return {"generatedAtUtc": now.isoformat(), "available": error is None, "error": error, **body}
