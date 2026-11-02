"""Missing documents of delivered Journeys, day by day (product owner 2026-11-02).

"Missing documents for journeys which have deliveries, segregated day wise,
just the names": the PC sees their own mapped outlets, the Team Lead (and PM,
Executive) the whole Project, with the count of each missing document type.

    Journey with deliveries   at least one page uploaded as DELIVERY
    Missing document          an open DOCUMENT_MISSING task (DOCUMENT_MISSING or
                              BOOKING_FORM_MISSING); its name is the task title
                              without the leading "Upload (the)"
    The day                   the delivery date on the vehicle's gate pass, when it
                              is a valid date 60 days before to 1 day after the first
                              delivery upload; otherwise the IST day of that upload

Read-only.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_gate_pass import scope_outlets
from audit_core.uc03_p2_object_keys import ist_day

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-missing-documents"])

_READ = "audit.journey.read"
_ADMIN_ROLES = ("SUPER_ADMIN", "TENANT_ADMIN")
DEFAULT_DAYS = 31
MAX_DAYS = 62
# A gate pass date further from the first delivery upload than this is a misreading.
GATE_PASS_DAYS_BEFORE = 60
GATE_PASS_DAYS_AFTER = 1
_CLOSED = ("VERIFIED_COMPLETE", "CANCELLED", "FAILED", "DEAD_LETTER")
_SOURCES = ("DOCUMENT_MISSING", "BOOKING_FORM_MISSING")


def document_name(title: str | None) -> str:
    """'Upload the RSA Invoice' -> 'RSA Invoice'."""
    name = (title or "").strip()
    for prefix in ("Upload the ", "Upload "):
        if name.lower().startswith(prefix.lower()):
            return name[len(prefix):].strip() or name
    return name


def day_label(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _parse_day(value: Any) -> date | None:
    raw = str(value or "").strip()
    if len(raw) != 10:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def delivery_day(gate_pass_value: Any, upload_day: date) -> tuple[date, str]:
    """The gate pass delivery date when it is believable, else the upload day."""
    read = _parse_day(gate_pass_value)
    if read is not None and (upload_day - timedelta(days=GATE_PASS_DAYS_BEFORE)
                             <= read <= upload_day + timedelta(days=GATE_PASS_DAYS_AFTER)):
        return read, "GATE_PASS"
    return upload_day, "UPLOAD"


def _all_outlets(connection: Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT o.dealer_id, d.dealer_name, o.outlet_id, o.outlet_name, o.outlet_classification
            FROM auditcore.dealer_outlets o
            JOIN auditcore.dealers d ON d.tenant_id = o.tenant_id AND d.dealer_id = o.dealer_id
            WHERE o.tenant_id = :t
            ORDER BY lower(d.dealer_name), (o.outlet_classification = 'ONSITE') DESC, lower(o.outlet_name)
            """
        ),
        {"t": tenant_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def _delivered_journeys(connection: Connection, tenant_id: str, outlet_ids: list[str],
                        first_upload: date, last_upload: date) -> list[dict[str, Any]]:
    """Journeys of the outlets with an open missing-document task and a DELIVERY page
    whose first DELIVERY upload falls on an IST day between the two dates."""
    return [dict(r) for r in connection.execute(
        text(
            """
            WITH open_missing AS (
                SELECT DISTINCT t.journey_id
                FROM auditcore.p2_tasks t
                WHERE t.tenant_id = :t AND t.journey_id IS NOT NULL AND t.task_type = 'DOCUMENT_MISSING'
                  AND t.source_code = ANY(:sources) AND t.task_status <> ALL(:closed)
            ),
            delivered AS (
                SELECT q.journey_id, MIN(b.created_at_utc) AS first_upload
                FROM auditcore.p2_document_queue q
                JOIN auditcore.p2_upload_batches b ON b.tenant_id = q.tenant_id AND b.batch_id = q.batch_id
                WHERE q.tenant_id = :t AND q.business_stage = 'DELIVERY'
                  AND q.journey_id IN (SELECT journey_id FROM open_missing)
                GROUP BY q.journey_id
            )
            SELECT j.journey_id, j.journey_reference, j.dealer_id, j.outlet_id,
                   j.created_by_display_name AS pc_name, c.display_name AS customer_name,
                   d.dealer_name, o.outlet_name,
                   CAST(dv.first_upload AT TIME ZONE 'Asia/Kolkata' AS date) AS upload_day
            FROM delivered dv
            JOIN auditcore.journeys j ON j.tenant_id = :t AND j.journey_id = dv.journey_id
            LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            LEFT JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
            LEFT JOIN auditcore.dealer_outlets o ON o.tenant_id = j.tenant_id AND o.outlet_id = j.outlet_id
            WHERE j.outlet_id = ANY(CAST(:ids AS uuid[]))
              AND CAST(dv.first_upload AT TIME ZONE 'Asia/Kolkata' AS date) BETWEEN :a AND :b
            """
        ),
        {"t": tenant_id, "ids": outlet_ids, "a": first_upload, "b": last_upload,
         "sources": list(_SOURCES), "closed": list(_CLOSED)},
    ).mappings().all()]


def _missing_by_journey(connection: Connection, tenant_id: str,
                        journey_ids: list[str]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for r in connection.execute(
        text(
            """
            SELECT t.journey_id, t.title, t.reference ->> 'requirementKey' AS requirement_key
            FROM auditcore.p2_tasks t
            WHERE t.tenant_id = :t AND t.journey_id = ANY(CAST(:ids AS uuid[]))
              AND t.task_type = 'DOCUMENT_MISSING' AND t.source_code = ANY(:sources)
              AND t.task_status <> ALL(:closed)
            ORDER BY t.created_at_utc, t.title
            """
        ),
        {"t": tenant_id, "ids": journey_ids, "sources": list(_SOURCES), "closed": list(_CLOSED)},
    ).mappings().all():
        name = document_name(r["title"])
        key = r["requirement_key"] or name
        if (str(r["journey_id"]), key) in seen:
            continue
        seen.add((str(r["journey_id"]), key))
        out.setdefault(str(r["journey_id"]), []).append({"name": name, "requirementKey": key})
    return out


def _gate_pass_dates(connection: Connection, tenant_id: str, journey_ids: list[str]) -> dict[str, Any]:
    """The delivery date read off each Journey's vehicle gate pass: the newest reading of
    each live gate pass document (a reviewer's correction wins), the newest document first
    (the same reading vehicle_gate_pass_deliveries counts)."""
    return {str(r["journey_id"]): r["v"] for r in connection.execute(
        text(
            """
            WITH dated AS (
                SELECT DISTINCT ON (f.journey_id, f.di_document_id)
                       f.journey_id, f.effective_value #>> '{}' AS v, f.updated_at_utc
                FROM auditcore.journey_document_extracted_fields f
                JOIN auditcore.evidence e
                  ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
                 AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
                WHERE f.tenant_id = :t AND f.journey_id = ANY(CAST(:ids AS uuid[]))
                  AND f.field_key = 'delivery_date'
                  AND COALESCE(e.document_type_key, f.source_document_type_key) = 'gate_pass'
                ORDER BY f.journey_id, f.di_document_id, f.source_fact_version DESC NULLS LAST,
                         f.updated_at_utc DESC
            )
            SELECT DISTINCT ON (journey_id) journey_id, v
            FROM dated
            ORDER BY journey_id, updated_at_utc DESC
            """
        ),
        {"t": tenant_id, "ids": journey_ids},
    ).mappings().all()}


def build_missing_documents(connection: Connection, *, tenant_id: str, outlets: list[dict[str, Any]],
                            first: date, last: date) -> dict[str, Any]:
    ids = [str(o["outlet_id"]) for o in outlets]
    journeys = _delivered_journeys(
        connection, tenant_id, ids,
        first - timedelta(days=GATE_PASS_DAYS_AFTER), last + timedelta(days=GATE_PASS_DAYS_BEFORE),
    ) if ids else []
    journey_ids = [str(j["journey_id"]) for j in journeys]
    missing = _missing_by_journey(connection, tenant_id, journey_ids) if journey_ids else {}
    gate_passes = _gate_pass_dates(connection, tenant_id, journey_ids) if journey_ids else {}

    days: dict[date, list[dict[str, Any]]] = {}
    by_document: dict[str, dict[str, Any]] = {}
    by_dealer: dict[str, dict[str, Any]] = {}
    by_outlet: dict[str, dict[str, Any]] = {}
    for j in journeys:
        jid = str(j["journey_id"])
        documents = missing.get(jid)
        if not documents:
            continue
        day, basis = delivery_day(gate_passes.get(jid), j["upload_day"])
        if not first <= day <= last:
            continue
        days.setdefault(day, []).append({
            "journeyId": jid, "reference": j["journey_reference"],
            "customer": (j["customer_name"] or "").strip() or j["journey_reference"],
            "dealerId": str(j["dealer_id"]), "dealerName": j["dealer_name"],
            "outletId": str(j["outlet_id"]), "outletName": j["outlet_name"],
            "pcName": j["pc_name"], "basis": basis, "uploadDay": j["upload_day"].isoformat(),
            "documents": [d["name"] for d in documents],
            "requirementKeys": [d["requirementKey"] for d in documents],
        })
        for d in documents:
            entry = by_document.setdefault(d["requirementKey"], {"name": d["name"], "requirementKey": d["requirementKey"],
                                                                 "count": 0})
            entry["count"] += 1
        dealer = by_dealer.setdefault(str(j["dealer_id"]), {"dealerId": str(j["dealer_id"]),
                                                            "dealerName": j["dealer_name"],
                                                            "journeys": 0, "documents": 0})
        outlet = by_outlet.setdefault(str(j["outlet_id"]), {"outletId": str(j["outlet_id"]),
                                                            "outletName": j["outlet_name"],
                                                            "dealerId": str(j["dealer_id"]),
                                                            "dealerName": j["dealer_name"],
                                                            "journeys": 0, "documents": 0})
        for figures in (dealer, outlet):
            figures["journeys"] += 1
            figures["documents"] += len(documents)

    def _order(row: dict[str, Any]) -> tuple[str, str, str]:
        return (str(row["dealerName"] or "").lower(), str(row["outletName"] or "").lower(),
                str(row["customer"] or "").lower())

    day_rows = [{"date": d.isoformat(), "label": day_label(d), "journeys": sorted(days[d], key=_order)}
                for d in sorted(days, reverse=True)]
    total_journeys = sum(len(d["journeys"]) for d in day_rows)
    return {
        "from": first.isoformat(), "to": last.isoformat(),
        "totals": {"journeys": total_journeys, "documents": sum(e["count"] for e in by_document.values())},
        "byDocument": sorted(by_document.values(), key=lambda e: (-e["count"], e["name"].lower())),
        "byDealer": sorted(by_dealer.values(), key=lambda e: (-e["documents"], str(e["dealerName"] or "").lower())),
        "byOutlet": sorted(by_outlet.values(), key=lambda e: (str(e["dealerName"] or "").lower(),
                                                               -e["documents"], str(e["outletName"] or "").lower())),
        "days": day_rows,
    }


@router.get("/missing-documents")
def missing_documents(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    outletId: UUID | None = None,
    dealerId: UUID | None = None,
) -> dict[str, Any]:
    """Missing documents of the Journeys with deliveries, by delivery day (newest first),
    in the caller's scope: a PC their mapped outlets, a TL, PM or Executive with a
    Project-wide assignment the whole Project; Superadmin and Tenant admin the whole
    Project. Each journey says which day basis was used: GATE_PASS (the delivery date
    on the vehicle gate pass) or UPLOAD (the IST day of the first delivery upload)."""
    access = authorize_p2(connection, tenant_id=tenant_id, journey_id=None, human_principal=principal,
                          authorization_client=client, permission_key=_READ)
    today = ist_day().date()
    end = min(end or today, today)
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise HTTPException(status_code=422, detail="'from' is after 'to'.")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"Choose a range of at most {MAX_DAYS} days.")
    if (access.functional_role or "").upper() in _ADMIN_ROLES:
        outlets = _all_outlets(connection, tenant_id)
    else:
        outlets = scope_outlets(connection, tenant_id, principal.subject)
    chosen = outlets
    if outletId is not None:
        chosen = [o for o in outlets if str(o["outlet_id"]) == str(outletId)]
        if not chosen:
            raise HTTPException(status_code=404, detail="Outlet not found in your current Project scope.")
    elif dealerId is not None:
        chosen = [o for o in outlets if str(o["dealer_id"]) == str(dealerId)]
        if not chosen:
            raise HTTPException(status_code=404, detail="Dealership not found in your current Project scope.")
    report = build_missing_documents(connection, tenant_id=tenant_id, outlets=chosen, first=start, last=end)
    return {
        **report,
        "scope": "OUTLET" if outletId is not None else "DEALER" if dealerId is not None else "ALL",
        "outlets": [{"dealerId": str(o["dealer_id"]), "dealerName": o["dealer_name"],
                     "outletId": str(o["outlet_id"]), "outletName": o["outlet_name"]} for o in outlets],
        "notes": [
            "Journeys with at least one delivery document uploaded, and the documents still asked for on them.",
            ("Day: the delivery date on the vehicle's gate pass; journeys marked * have no believable gate pass "
             "date and are shown on the day their first delivery document was uploaded."),
        ],
    }
