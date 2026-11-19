"""uc03_p2_dms_reports.py -- the DMS report, generated on demand in the Reports section by
the Reviewer and above (product owner 2026-11-18).

"Once DMS booking dump is uploaded we should give a button to generate the report. When
this button is clicked pls ask ... Pls make sure all PC have intimated their booking
information ... this report should move to the reports section and it can be generated only
by Reviewer and above ... the report which we need to generate can be done from all the
deterministic data."

Generating a report for a day:
1. that day's dumps are matched again (Journeys uploaded since the upload are found);
2. the outlet report is built (uc03_p2_dms_outlet_report): DMS against Verigence per DMS
   location, the PCs' intimation marks (intimated to the PC / not intimated), the gate
   register's deliveries, and the statement -- per outlet the days the gate register is
   missing (onsite and satellite apart) and the outlets whose PC was not available;
3. the rows behind it: bookings and deliveries of the dumps not in Verigence, each with
   whether a PC marked it intimated (and who), and Verigence's deliveries the dump does not
   carry;
4. all of it is kept as generated (p2_dms_reports): a report reads the same later.

``POST /p2/v1/tenants/{t}/dms-reports``                {date}   generate
``GET  /p2/v1/tenants/{t}/dms-reports?month=YYYY-MM``           the reports generated
``GET  /p2/v1/tenants/{t}/dms-reports/{report_id}``             one, as generated
``GET  /p2/v1/tenants/{t}/dms-reports/{report_id}/file``        Excel
"""
from __future__ import annotations

import io
import json
from datetime import date
from datetime import date as _Date
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_dms_dumps import (
    _READ,
    BOOKING,
    DELIVERY,
    _access,
    _dealers,
    _month,
    _name_of,
    _not_in_dump,
    _today,
    match_dump,
)
from audit_core.uc03_p2_dms_intimations import (
    INTIMATED,
    OPEN,
    REPORT_ROLES,
    counter,
    dump_rows,
    live_marks,
    row_key,
    row_state,
)
from audit_core.uc03_p2_dms_outlet_report import (
    build_outlet_report,
    build_outlet_report_excel,
    outlet_map,
    resolve_location,
    short_name,
)

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-dms-reports"])
_SCOPE = {"INSCOPE": "Inscope", "OUTSCOPE": "Outscope", "MR": "MR", "LEASE": "Lease", "BULK": "Bulk"}


def _day_dumps(connection: Connection, tenant_id: str, dealer_ids: list[str], day: date) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(
        text("""
            SELECT DISTINCT ON (u.dealer_id, u.dump_kind) u.dump_id, CAST(u.dealer_id AS text) AS dealer_id,
                   u.dump_kind, u.business_date
            FROM auditcore.p2_dms_dumps u
            WHERE u.tenant_id = :t AND u.status = 'ACTIVE' AND u.business_date = :d
              AND CAST(u.dealer_id AS text) = ANY(:ds)
            ORDER BY u.dealer_id, u.dump_kind, u.uploaded_at_utc DESC
        """),
        {"t": tenant_id, "d": day, "ds": dealer_ids},
    ).mappings()]


def _rows(connection: Connection, tenant_id: str, dealers: list[dict[str, Any]], dumps: list[dict[str, Any]]
          ) -> list[dict[str, Any]]:
    """The dump rows Verigence does not hold, with the PC's mark."""
    names = {d["dealerId"]: d["dealerName"] for d in dealers}
    marks = live_marks(connection, tenant_id, [d["dealerId"] for d in dealers])
    out = []
    for dump in sorted(dumps, key=lambda d: (names.get(d["dealer_id"], ""), d["dump_kind"])):
        outlets = outlet_map(connection, tenant_id, dump["dealer_id"])
        by_id = {str(o["outlet_id"]): o for o in outlets}
        for r in dump_rows(connection, tenant_id, dump["dump_id"]):
            kind = r["dump_kind"]
            mark = marks.get((dump["dealer_id"], kind, row_key(kind, r)))
            state = row_state(r, mark)
            if state not in (INTIMATED, OPEN):
                continue
            anchor = resolve_location(r.get("outlet_label"), outlets)
            out.append({
                "kind": kind, "dealerName": names.get(dump["dealer_id"]),
                "location": short_name(by_id[anchor]["outlet_name"]) if anchor in by_id else r.get("outlet_label"),
                "eventDate": r["event_date"].isoformat() if r["event_date"] else None,
                "customerName": r["customer_name"], "mobile": r["mobile"], "otf": r["dms_booking_number"],
                "bookingNumber": r["booking_number"], "chassis": r["chassis"], "model": r["model"],
                "consultant": r["consultant"],
                "inVerigence": ("Booking on file, delivery not uploaded" if r["outcome"] == "NOT_IN_TOOL"
                                else "Not in Verigence"),
                "journeyReference": r["journey_reference"],
                "dealType": _SCOPE.get(r.get("deal_type") or "", "Pending" if kind == DELIVERY else None),
                "intimation": ("Intimated to PC - pending with PC" if state == INTIMATED
                               else "Not intimated to PC - pending with dealership"),
                "markedBy": (mark["claimed_by_name"] or mark["claimed_by"]) if mark else None,
                "markedAtUtc": mark["claimed_at_utc"].isoformat() if mark else None,
            })
    return out


_OUTCOME_WORDS = {"MATCHED": "In Verigence", "NOT_IN_TOOL": "Booking on file, delivery not uploaded",
                  "MISSING": "Not in Verigence"}


def _delivery_scope_rows(connection: Connection, tenant_id: str, dealers: list[dict[str, Any]],
                         dumps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every row of the day's delivery dumps with its Inscope / Outscope (2026-11-19)."""
    names = {d["dealerId"]: d["dealerName"] for d in dealers}
    out = []
    for dump in sorted((d for d in dumps if d["dump_kind"] == DELIVERY), key=lambda d: names.get(d["dealer_id"], "")):
        outlets = outlet_map(connection, tenant_id, dump["dealer_id"])
        by_id = {str(o["outlet_id"]): o for o in outlets}
        for r in dump_rows(connection, tenant_id, dump["dump_id"]):
            anchor = resolve_location(r.get("outlet_label"), outlets)
            out.append({
                "dealerName": names.get(dump["dealer_id"]),
                "location": short_name(by_id[anchor]["outlet_name"]) if anchor in by_id else r.get("outlet_label"),
                "eventDate": r["event_date"].isoformat() if r["event_date"] else None,
                "customerName": r["customer_name"], "chassis": r["chassis"], "model": r["model"],
                "bookingNumber": r["booking_number"], "inVerigence": _OUTCOME_WORDS.get(r["outcome"], r["outcome"]),
                "journeyReference": r["journey_reference"],
                "dealType": _SCOPE.get(r.get("deal_type") or "", "Pending"), "dealTypeBasis": r.get("deal_type_basis"),
            })
    return out


def generate_report(connection: Connection, *, tenant_id: str, dealers: list[dict[str, Any]], day: date,
                    actor_id: str) -> dict[str, Any]:
    today = _today(connection, tenant_id)
    dealer_ids = [d["dealerId"] for d in dealers]
    dumps = _day_dumps(connection, tenant_id, dealer_ids, day)
    if not dumps:
        raise HTTPException(status_code=409, detail=f"No DMS dump was uploaded for {day:%d %b %Y}.")
    for dump in dumps:
        match_dump(connection, tenant_id=tenant_id, dump_id=dump["dump_id"])
    report = build_outlet_report(connection, tenant_id=tenant_id, dealers=dealers, day=day, today=today)
    report["rows"] = _rows(connection, tenant_id, dealers, dumps)
    report["deliveryScope"] = _delivery_scope_rows(connection, tenant_id, dealers, dumps)
    report["notInDump"] = [
        {k: row.get(k) for k in ("dealerName", "outletName", "eventDate", "customerName", "mobile", "chassis",
                                 "model", "journeyReference", "datedBy")}
        for row in _not_in_dump(connection, tenant_id, [d["dump_id"] for d in dumps if d["dump_kind"] == DELIVERY])]
    report["intimations"] = [
        {**counter(connection, tenant_id, d["dealer_id"], d["dump_kind"], upto=day),
         "dealerName": next((x["dealerName"] for x in dealers if x["dealerId"] == d["dealer_id"]), None)}
        for d in sorted(dumps, key=lambda x: (x["dealer_id"], x["dump_kind"]))]
    report_id = uuid4()
    by_name = _name_of(connection, actor_id)
    connection.execute(
        text("""
            INSERT INTO auditcore.p2_dms_reports (tenant_id, report_id, report_date, period_month, dealer_ids,
              generated_by, generated_by_name, figures, report)
            VALUES (:t, :id, :d, :m, :ds, :by, :byn, CAST(:f AS jsonb), CAST(:r AS jsonb))
        """),
        {"t": tenant_id, "id": str(report_id), "d": day, "m": day.replace(day=1),
         "ds": sorted({d["dealer_id"] for d in dumps}), "by": actor_id, "byn": by_name,
         "f": json.dumps(report["figures"]), "r": json.dumps(report, default=str)},
    )
    return {"reportId": str(report_id), "date": day.isoformat(), "generatedBy": by_name or actor_id,
            "figures": report["figures"], "statementLines": report["statementLines"]}


class GenerateCommand(BaseModel):
    date: _Date | None = None


def _reviewer(connection: Connection, tenant_id: str, principal: HumanPrincipal,
              client: SecurityAuthorizationClient) -> list[dict[str, Any]]:
    held = _access(connection, tenant_id, principal, client, _READ, REPORT_ROLES)
    return _dealers(connection, tenant_id, principal.subject, "ADMIN" in held)


@router.post("/dms-reports", status_code=201)
def post_dms_report(
    tenant_id: str,
    command: GenerateCommand,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    """Generate the DMS report of a day's dumps (Reviewer and above)."""
    dealers = _reviewer(connection, tenant_id, principal, client)
    today = _today(connection, tenant_id)
    day = command.date or today
    if day > today:
        raise HTTPException(status_code=422, detail="The report's date cannot be in the future.")
    return generate_report(connection, tenant_id=tenant_id, dealers=dealers, day=day, actor_id=principal.subject)


def _covered(row: Any, dealer_ids: set[str]) -> bool:
    return set(row["dealer_ids"] or []) <= dealer_ids


@router.get("/dms-reports")
def list_dms_reports(
    tenant_id: str,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
    month: str | None = None,
) -> dict[str, Any]:
    dealer_ids = {d["dealerId"] for d in _reviewer(connection, tenant_id, principal, client)}
    first = _month(month, _today(connection, tenant_id))
    rows = connection.execute(
        text("""
            SELECT report_id, report_date, dealer_ids, generated_by, generated_by_name, generated_at_utc, figures
            FROM auditcore.p2_dms_reports
            WHERE tenant_id = :t AND period_month = :m
            ORDER BY report_date DESC, generated_at_utc DESC
        """),
        {"t": tenant_id, "m": first},
    ).mappings().all()
    return {"month": first.strftime("%Y-%m"), "items": [
        {"reportId": str(r["report_id"]), "date": r["report_date"].isoformat(),
         "generatedBy": r["generated_by_name"] or r["generated_by"], "generatedAtUtc": r["generated_at_utc"].isoformat(),
         "figures": r["figures"]} for r in rows if _covered(r, dealer_ids)]}


def _report(connection: Connection, tenant_id: str, report_id: UUID, dealer_ids: set[str]) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT * FROM auditcore.p2_dms_reports WHERE tenant_id = :t AND report_id = :id"),
        {"t": tenant_id, "id": str(report_id)},
    ).mappings().one_or_none()
    if row is None or not _covered(row, dealer_ids):
        raise HTTPException(status_code=404, detail="No such report.")
    return {**row["report"], "reportId": str(row["report_id"]),
            "generatedBy": row["generated_by_name"] or row["generated_by"],
            "generatedAtUtc": row["generated_at_utc"].isoformat()}


@router.get("/dms-reports/{report_id}")
def get_dms_report(
    tenant_id: str,
    report_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    dealer_ids = {d["dealerId"] for d in _reviewer(connection, tenant_id, principal, client)}
    return _report(connection, tenant_id, report_id, dealer_ids)


_ROW_COLUMNS = (("Dealer", "dealerName"), ("DMS location", "location"), ("Date", "eventDate"),
                ("Customer", "customerName"), ("Mobile", "mobile"), ("OTF", "otf"), ("Booking no.", "bookingNumber"),
                ("Chassis", "chassis"), ("Model", "model"), ("Consultant", "consultant"),
                ("In Verigence", "inVerigence"), ("Journey", "journeyReference"), ("Inscope / Outscope", "dealType"),
                ("Intimation", "intimation"),
                ("Marked by", "markedBy"), ("Marked at (UTC)", "markedAtUtc"))
_SCOPE_COLUMNS = (("Dealer", "dealerName"), ("DMS location", "location"), ("Delivery date", "eventDate"),
                  ("Customer", "customerName"), ("Chassis", "chassis"), ("Model", "model"),
                  ("Booking no.", "bookingNumber"), ("In Verigence", "inVerigence"), ("Journey", "journeyReference"),
                  ("Inscope / Outscope", "dealType"), ("Decided on", "dealTypeBasis"))
_NOT_IN_DUMP_COLUMNS = (("Dealer", "dealerName"), ("Outlet", "outletName"), ("Delivery date", "eventDate"),
                        ("Dated by", "datedBy"), ("Customer", "customerName"), ("Mobile", "mobile"),
                        ("Chassis", "chassis"), ("Model", "model"), ("Journey", "journeyReference"))


def build_report_excel(report: dict[str, Any]) -> bytes:
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill

    wb = load_workbook(io.BytesIO(build_outlet_report_excel(report)))
    head, white = PatternFill("solid", fgColor="161616"), Font(bold=True, color="FFFFFF")

    def sheet(title: str, columns: tuple[tuple[str, str], ...], rows: list[dict[str, Any]]) -> None:
        ws = wb.create_sheet(title)
        ws.append([h for h, _ in columns])
        for cell in ws[1]:
            cell.fill, cell.font = head, white
        for row in rows:
            ws.append([row.get(k) for _, k in columns])
        ws.freeze_panes = "A2"
        for i, (h, _) in enumerate(columns):
            ws.column_dimensions[chr(ord("A") + i)].width = max(12, min(40, len(h) + 8))

    rows = report.get("rows") or []
    sheet("Bookings not in Verigence", _ROW_COLUMNS, [r for r in rows if r["kind"] == BOOKING])
    sheet("Deliveries not in Verigence", _ROW_COLUMNS, [r for r in rows if r["kind"] == DELIVERY])
    sheet("Delivery dump scope", _SCOPE_COLUMNS, report.get("deliveryScope") or [])
    sheet("In Verigence, not in dump", _NOT_IN_DUMP_COLUMNS, report.get("notInDump") or [])
    pcs = wb.create_sheet("PC intimations")
    pcs.append(["Dealer", "Dump", "PC", "Outlets", "Rows marked", "of which since last dump", "Last marked (UTC)"])
    for cell in pcs[1]:
        cell.fill, cell.font = head, white
    for entry in report.get("intimations") or []:
        for pc in entry["pcs"]:
            pcs.append([entry["dealerName"], entry["kind"].title(), pc["name"], ", ".join(pc["outlets"]),
                        pc["marked"], pc["markedNew"], pc["lastAtUtc"]])
    for col, width in zip("ABCDEFG", (24, 10, 28, 30, 12, 14, 24), strict=True):
        pcs.column_dimensions[col].width = width
    # The statement first after the summary: the order a reader takes it in.
    if "Statement" in wb.sheetnames:
        wb.move_sheet("Statement", offset=1 - wb.sheetnames.index("Statement"))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@router.get("/dms-reports/{report_id}/file")
def export_dms_report(
    tenant_id: str,
    report_id: UUID,
    principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> StreamingResponse:
    dealer_ids = {d["dealerId"] for d in _reviewer(connection, tenant_id, principal, client)}
    report = _report(connection, tenant_id, report_id, dealer_ids)
    name = f"DMS-Report_{report['date']}.xlsx"
    return StreamingResponse(io.BytesIO(build_report_excel(report)), media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


__all__ = ["build_report_excel", "generate_report", "router"]
