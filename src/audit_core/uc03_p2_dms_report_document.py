"""uc03_p2_dms_report_document.py -- a generated DMS report as one professional document, in
Excel and in PDF (product owner 2026-11-22: "merge all the facts in one report ... include the
grouping of deliveries as per gate register ... a professional report which can be downloaded
... add styles to excel and pdf").

Both are made from the stored report (uc03_p2_dms_reports.compute_report), so they always agree
with what Verigence shows. Deliveries are grouped as the DMS groups outlets (its "DMS location":
Aditya's outlets other than Jajpur under Cuttack, Utkal's Anandapur under Keonjhar ...): each
location shows the DMS's figures against its outlets' Verigence and gate register figures, and
each outlet's own row sits under it.

Sections, in the order a reader takes them:
  Summary              headline figures (bookings, deliveries, gate register), one row a dealership
  By DMS location      the gate register grouping: register, dump and Verigence side by side
  Statement            the statement lines (registers missed, PC not available, the sums)
  Exceptions           bookings not in Verigence; deliveries not in Verigence; delivery not
                       uploaded; matched but the customer name differs; in Verigence, not in dump
  Delivery dump scope  every delivery row with its Inscope / Outscope (Excel only)
  PC intimations       who marked what as intimated
  How it is counted    the counting notes

Styling follows IBM Carbon (gray 100 headers, gray 10 bands, blue 60 / teal 60 / red 60 accents).
"""
from __future__ import annotations

import io
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

from audit_core.uc03_p2_dms_outlet_report import COUNTING_NOTES

IST = timezone(timedelta(hours=5, minutes=30))

# IBM Carbon tokens.
GRAY_100, GRAY_70, GRAY_50, GRAY_20, GRAY_10 = "161616", "525252", "8D8D8D", "E0E0E0", "F4F4F4"
BLUE_60, BLUE_10, TEAL_60, TEAL_10 = "0F62FE", "EDF5FF", "007D79", "D9FBFB"
RED_60, RED_10, YELLOW_10, GREEN_60, GREEN_10 = "DA1E28", "FFF1F1", "FCF4D6", "198038", "DEFBE6"
PURPLE_10 = "F6F2FF"

TITLE = "DMS Reconciliation Report"

# The gate register grouping: (header, key, band). A location row carries the DMS's figures and
# its outlets' summed Verigence / register figures; an outlet row its own Verigence / register ones.
LOCATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("Gate out register", "gateRegister", "Gate register"),
    ("Registers missed", "registersMissed", "Gate register"),
    ("Counts pending", "countsPending", "Gate register"),
    ("As per delivery dump", "deliveryDump", "Deliveries"),
    ("Synced", "deliverySynced", "Deliveries"),
    ("Delivery not uploaded", "deliveryNotUploaded", "Deliveries"),
    ("Not in Verigence", "deliveryNotInVerigence", "Deliveries"),
    ("In Verigence", "deliveryInVerigence", "Deliveries"),
    ("In Verigence, not in dump", "deliveryNotInDms", "Deliveries"),
    ("Dump less Verigence", "deliveryGap", "Deliveries"),
    ("As per DMS", "bookingDms", "Bookings"),
    ("Synced", "bookingSynced", "Bookings"),
    ("Not in Verigence", "bookingNotInVerigence", "Bookings"),
    ("In Verigence", "bookingInVerigence", "Bookings"),
    ("In Verigence, not in DMS", "bookingNotInDms", "Bookings"),
)
BAND_COLOURS = {"Gate register": GRAY_70, "Deliveries": TEAL_60, "Bookings": BLUE_60}
# Figures only the DMS has: blank on an outlet's own row (the location's row carries them).
DMS_ONLY = {"deliveryDump", "deliverySynced", "deliveryNotUploaded", "deliveryNotInVerigence", "deliveryGap",
            "bookingDms", "bookingSynced", "bookingNotInVerigence"}

ROW_COLUMNS = (("Dealer", "dealerName"), ("DMS location", "location"), ("Date", "eventDate"),
               ("Customer", "customerName"), ("Mobile", "mobile"), ("OTF / booking no.", "otf"),
               ("Chassis", "chassis"), ("Model", "model"), ("Consultant", "consultant"),
               ("Journey", "journeyReference"), ("Inscope / Outscope", "dealType"), ("Intimation", "intimation"),
               ("Marked by", "markedBy"))
NAME_DIFFERS_COLUMNS = (("Dealer", "dealerName"), ("Dump", "kind"), ("DMS location", "location"),
                        ("Date", "eventDate"), ("DMS customer", "dmsCustomer"),
                        ("Verigence customer", "journeyCustomer"), ("Journey", "journeyReference"),
                        ("Journey outlet", "journeyOutlet"), ("Matched on", "matchedOn"), ("Chassis", "chassis"),
                        ("Mobile", "mobile"), ("OTF / booking no.", "otf"))
NOT_IN_DUMP_COLUMNS = (("Dealer", "dealerName"), ("Outlet", "outletName"), ("Delivery date", "eventDate"),
                       ("Dated by", "datedBy"), ("Customer", "customerName"), ("Mobile", "mobile"),
                       ("Chassis", "chassis"), ("Model", "model"), ("Journey", "journeyReference"))
SCOPE_COLUMNS = (("Dealer", "dealerName"), ("DMS location", "location"), ("Delivery date", "eventDate"),
                 ("Customer", "customerName"), ("Chassis", "chassis"), ("Model", "model"),
                 ("Booking no.", "bookingNumber"), ("In Verigence", "inVerigence"), ("Journey", "journeyReference"),
                 ("Inscope / Outscope", "dealType"), ("Decided on", "dealTypeBasis"))

NOT_UPLOADED = "Booking on file, delivery not uploaded"


# --------------------------------------------------------------------------- the content
def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _ist(value: Any) -> str:
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return ""
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def _pct(part: int | None, whole: int | None) -> float | None:
    return (part or 0) / whole if whole else None


def exception_lists(report: dict[str, Any]) -> list[tuple[str, tuple[tuple[str, str], ...], list[dict[str, Any]], str]]:
    """(title, columns, rows, what it means) of each exception list, in reading order."""
    rows = report.get("rows") or []
    return [
        ("Bookings not in Verigence", ROW_COLUMNS,
         [r for r in rows if r.get("kind") == "BOOKING"],
         "In the DMS booking dump; no booking in Verigence."),
        ("Deliveries not in Verigence", ROW_COLUMNS,
         [r for r in rows if r.get("kind") == "DELIVERY" and r.get("inVerigence") != NOT_UPLOADED],
         "In the DMS delivery dump; no booking or delivery in Verigence."),
        ("Delivery not uploaded", ROW_COLUMNS,
         [r for r in rows if r.get("kind") == "DELIVERY" and r.get("inVerigence") == NOT_UPLOADED],
         "The booking is in Verigence; its delivery papers (with the vehicle's VIN) are not uploaded."),
        ("Matched, name differs", NAME_DIFFERS_COLUMNS, report.get("nameDiffers") or [],
         "Found by chassis, booking number or mobile, but the customer's name differs: review."),
        ("In Verigence, not in dump", NOT_IN_DUMP_COLUMNS, report.get("notInDump") or [],
         "Delivered in Verigence in the period; not in the DMS delivery dump."),
    ]


def summary_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for dealer in report.get("dealers") or []:
        f = dealer.get("figures") or {}
        out.append({"name": dealer["dealerName"], "figures": f,
                    "bookingDump": dealer.get("bookingDumpDate"), "deliveryDump": dealer.get("deliveryDumpDate")})
    return out


def location_rows(dealer: dict[str, Any]) -> list[dict[str, Any]]:
    """The gate register grouping of a dealership: a row a DMS location (DMS figures against its
    outlets' total), its outlets' own rows under it when it has several, then the total."""
    out: list[dict[str, Any]] = []
    for location in dealer.get("locations") or []:
        outlets = location.get("outlets") or []
        if len(outlets) <= 1:
            out.append({"kind": "location", "label": location["name"], "figures": location["figures"]})
            continue
        out.append({"kind": "location", "label": f"{location['name']} (DMS location, {len(outlets)} outlets)",
                    "figures": location["figures"]})
        for outlet in outlets:
            out.append({"kind": "outlet", "label": outlet["outletName"], "figures": outlet["figures"]})
    for row in dealer.get("unmapped") or []:
        out.append({"kind": "location", "label": f"DMS location not mapped: {row['label']}",
                    "figures": row["figures"]})
    out.append({"kind": "total", "label": f"{dealer['dealerName']} total", "figures": dealer["figures"]})
    return out


def _cell(row: dict[str, Any], key: str) -> Any:
    value = row.get(key)
    if key == "eventDate" and value:
        return _day(value) or value
    if key == "kind" and value:
        return str(value).title()
    return value


def _subtitle(report: dict[str, Any]) -> str:
    day = _day(report.get("date"))
    if day is None:
        return ""
    return f"As at {day:%d %B %Y}  |  month to date, 1 to {day:%d %b %Y}"


def _generated(report: dict[str, Any]) -> str:
    text = f"Generated by {report.get('generatedBy') or '-'} on {_ist(report.get('generatedAtUtc'))}"
    rebuilt = report.get("rebuilt") or {}
    if rebuilt.get("atUtc"):
        text += f"; recounted by the current rules on {_ist(rebuilt['atUtc'])}"
    return text


# --------------------------------------------------------------------------- Excel
def build_report_xlsx(report: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = Workbook()
    wb.properties.title = TITLE
    wb.properties.creator = "Verigence"
    fill = lambda colour: PatternFill("solid", fgColor=colour)
    thin = Side(style="thin", color=GRAY_20)
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    base = "IBM Plex Sans"
    font = lambda **kw: Font(name=base, **kw)
    count_fmt, pct_fmt = "#,##0;-#,##0;\"-\"", "0.0%"
    figures = report.get("figures") or {}

    def page_setup(ws, *, landscape: bool = True, title_rows: str | None = None) -> None:
        ws.page_setup.orientation = "landscape" if landscape else "portrait"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_options.horizontalCentered = True
        ws.page_margins.left = ws.page_margins.right = 0.4
        ws.oddFooter.left.text = f"Verigence | {TITLE}"
        ws.oddFooter.right.text = "Page &P of &N"
        if title_rows:
            ws.print_title_rows = title_rows

    def heading(ws, text: str, sub: str | None = None, width: int = 8) -> int:
        ws.sheet_view.showGridLines = False
        ws["A1"] = text
        ws["A1"].font = font(size=16, bold=True, color=GRAY_100)
        ws["A2"] = sub or _subtitle(report)
        ws["A2"].font = font(size=10, color=GRAY_70)
        ws["A3"] = _generated(report)
        ws["A3"].font = font(size=9, italic=True, color=GRAY_50)
        for c in range(1, width + 1):
            ws.cell(row=4, column=c).border = Border(top=Side(style="medium", color=BLUE_60))
        return 5

    # ---- Summary
    ws = wb.active
    ws.title = "Summary"
    row = heading(ws, TITLE, width=12)
    tiles = (
        ("BOOKINGS", BLUE_60, BLUE_10, (("As per DMS", figures.get("bookingDms")),
                                         ("In Verigence", figures.get("bookingInVerigence")),
                                         ("Synced", figures.get("bookingSynced")),
                                         ("Not in Verigence", figures.get("bookingNotInVerigence")),
                                         ("In Verigence, not in DMS", figures.get("bookingNotInDms")))),
        ("DELIVERIES", TEAL_60, TEAL_10, (("As per delivery dump", figures.get("deliveryDump")),
                                           ("In Verigence", figures.get("deliveryInVerigence")),
                                           ("Synced", figures.get("deliverySynced")),
                                           ("Delivery not uploaded", figures.get("deliveryNotUploaded")),
                                           ("Not in Verigence", figures.get("deliveryNotInVerigence")))),
        ("GATE REGISTER", GRAY_70, GRAY_10, (("Gate out register", figures.get("gateRegister")),
                                             ("Registers missed", figures.get("registersMissed")),
                                             ("Counts pending", figures.get("countsPending")),
                                             ("Inscope (dump)", figures.get("dumpInscope")),
                                             ("Outscope (dump)", figures.get("dumpOutscope")))),
    )
    row += 1
    col = 1
    for name, accent, light, items in tiles:
        ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + 2)
        top = ws.cell(row=row, column=col, value=name)
        top.font, top.fill = font(bold=True, size=10, color="FFFFFF"), fill(accent)
        top.alignment = Alignment(horizontal="left", indent=1)
        for c in range(col, col + 3):
            ws.cell(row=row, column=c).fill = fill(accent)
        for i, (label, value) in enumerate(items, start=1):
            ws.merge_cells(start_row=row + i, start_column=col, end_row=row + i, end_column=col + 1)
            a = ws.cell(row=row + i, column=col, value=label)
            a.font, a.alignment = font(size=10, color=GRAY_100), Alignment(indent=1)
            b = ws.cell(row=row + i, column=col + 2, value=value)
            b.font, b.number_format = font(size=12, bold=True, color=accent), count_fmt
            b.alignment = Alignment(horizontal="right")
            for c in range(col, col + 3):
                ws.cell(row=row + i, column=c).fill = fill(light)
                ws.cell(row=row + i, column=c).border = Border(bottom=Side(style="thin", color="FFFFFF"))
        col += 4
    row += len(tiles[0][3]) + 3
    rates = (("Booking sync rate (synced / as per DMS)", _pct(figures.get("bookingSynced"), figures.get("bookingDms"))),
             ("Delivery sync rate (synced / as per dump)",
              _pct(figures.get("deliverySynced"), figures.get("deliveryDump"))))
    for label, value in rates:
        ws.cell(row=row, column=1, value=label).font = font(size=10, color=GRAY_70)
        c = ws.cell(row=row, column=5, value=value)
        c.number_format, c.font = pct_fmt, font(bold=True, size=11)
        row += 1
    row += 1
    ws.cell(row=row, column=1, value="By dealership").font = font(size=12, bold=True)
    row += 1
    headers = ("Dealership", "Booking dump", "Delivery dump", "Bookings (DMS)", "Booking synced", "Booking sync %",
               "Bookings not in Verigence", "Deliveries (dump)", "Delivery synced", "Delivery sync %",
               "Delivery not uploaded", "Deliveries not in Verigence", "Gate out register", "Registers missed")
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font, c.fill, c.border = font(bold=True, color="FFFFFF", size=9), fill(GRAY_100), box
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[row].height = 32
    for n, entry in enumerate(summary_rows(report)):
        row += 1
        f = entry["figures"]
        values = (entry["name"], _day(entry["bookingDump"]), _day(entry["deliveryDump"]), f.get("bookingDms"),
                  f.get("bookingSynced"), _pct(f.get("bookingSynced"), f.get("bookingDms")),
                  f.get("bookingNotInVerigence"), f.get("deliveryDump"), f.get("deliverySynced"),
                  _pct(f.get("deliverySynced"), f.get("deliveryDump")), f.get("deliveryNotUploaded"),
                  f.get("deliveryNotInVerigence"), f.get("gateRegister"), f.get("registersMissed"))
        for i, v in enumerate(values, start=1):
            c = ws.cell(row=row, column=i, value=v)
            c.border, c.font = box, font(size=10)
            if n % 2:
                c.fill = fill(GRAY_10)
            if isinstance(v, date):
                c.number_format = "dd-mmm-yy"
            elif i in (6, 10):
                c.number_format = pct_fmt
            elif i > 3:
                c.number_format = count_fmt
    row += 1
    f = figures
    total = ("Project total", None, None, f.get("bookingDms"), f.get("bookingSynced"),
             _pct(f.get("bookingSynced"), f.get("bookingDms")), f.get("bookingNotInVerigence"), f.get("deliveryDump"),
             f.get("deliverySynced"), _pct(f.get("deliverySynced"), f.get("deliveryDump")),
             f.get("deliveryNotUploaded"), f.get("deliveryNotInVerigence"), f.get("gateRegister"),
             f.get("registersMissed"))
    for i, v in enumerate(total, start=1):
        c = ws.cell(row=row, column=i, value=v)
        c.font, c.fill, c.border = font(bold=True, size=10), fill(BLUE_10), box
        c.number_format = pct_fmt if i in (6, 10) else count_fmt
    row += 2
    ws.cell(row=row, column=1, value="Sheets in this report").font = font(size=11, bold=True)
    for name, about in (("By DMS location", "deliveries grouped as the DMS groups outlets, against the gate register"),
                        ("Statement", "registers missed, PC not available, how the figures add up"),
                        *[(t, about) for t, _, _, about in exception_lists(report)],
                        ("Delivery dump scope", "every delivery row with its Inscope / Outscope"),
                        ("PC intimations", "rows each PC marked as intimated"),
                        ("How it is counted", "the counting rules")):
        row += 1
        ws.cell(row=row, column=1, value=name).font = font(size=10, bold=True, color=BLUE_60)
        ws.cell(row=row, column=4, value=about).font = font(size=10, color=GRAY_70)
    widths = (26, 12, 12, 12, 12, 11, 14, 12, 12, 11, 14, 14, 12, 12)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    page_setup(ws)

    # ---- By DMS location (the gate register grouping)
    ws = wb.create_sheet("By DMS location")
    row = heading(ws, "Deliveries by DMS location, against the gate register",
                  f"{_subtitle(report)}  |  outlets grouped as the DMS groups them; each outlet's gate register "
                  "and Verigence counts under its DMS location", width=len(LOCATION_COLUMNS) + 1)
    band_row, head_row = row, row + 1
    ws.cell(row=band_row, column=1, value="")
    ws.cell(row=head_row, column=1, value="Dealership / DMS location / outlet")
    i = 0
    while i < len(LOCATION_COLUMNS):
        j = i
        while j + 1 < len(LOCATION_COLUMNS) and LOCATION_COLUMNS[j + 1][2] == LOCATION_COLUMNS[i][2]:
            j += 1
        band = LOCATION_COLUMNS[i][2]
        ws.merge_cells(start_row=band_row, start_column=i + 2, end_row=band_row, end_column=j + 2)
        c = ws.cell(row=band_row, column=i + 2, value=band.upper())
        c.font, c.alignment = font(bold=True, color="FFFFFF", size=9), Alignment(horizontal="center")
        for k in range(i + 2, j + 3):
            ws.cell(row=band_row, column=k).fill = fill(BAND_COLOURS[band])
        i = j + 1
    ws.cell(row=band_row, column=1).fill = fill(GRAY_100)
    for k, (h, _, _) in enumerate(LOCATION_COLUMNS, start=2):
        c = ws.cell(row=head_row, column=k, value=h)
    for c in ws[head_row]:
        c.font, c.fill, c.border = font(bold=True, color="FFFFFF", size=9), fill(GRAY_100), box
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[head_row].height = 42
    row = head_row
    for dealer in report.get("dealers") or []:
        row += 1
        c = ws.cell(row=row, column=1, value=dealer["dealerName"])
        c.font = font(bold=True, size=11, color="FFFFFF")
        dumps = "; ".join(f"{kind} dump {f'{d:%d %b %Y}' if (d := _day(dealer.get(key))) else 'none'}"
                          for kind, key in (("booking", "bookingDumpDate"), ("delivery", "deliveryDumpDate")))
        ws.cell(row=row, column=2, value=dumps).font = font(size=9, italic=True, color="FFFFFF")
        for k in range(1, len(LOCATION_COLUMNS) + 2):
            ws.cell(row=row, column=k).fill = fill(GRAY_70)
        for entry in location_rows(dealer):
            row += 1
            kind = entry["kind"]
            c = ws.cell(row=row, column=1, value=entry["label"])
            c.alignment = Alignment(indent=2 if kind == "outlet" else 0, wrap_text=True, vertical="center")
            c.font = font(size=10, bold=kind != "outlet", color=GRAY_70 if kind == "outlet" else GRAY_100)
            for k, (_, key, _) in enumerate(LOCATION_COLUMNS, start=2):
                value = None if kind == "outlet" and key in DMS_ONLY else entry["figures"].get(key)
                cell = ws.cell(row=row, column=k, value=value)
                cell.number_format, cell.border = count_fmt, box
                cell.font = font(size=10, bold=kind == "total")
                if key in ("deliveryNotInVerigence", "bookingNotInVerigence") and value:
                    cell.font = font(size=10, bold=True, color=RED_60)
                elif key == "deliveryNotUploaded" and value:
                    cell.font = font(size=10, bold=kind != "outlet", color="8E6A00")
            ws.cell(row=row, column=1).border = box
            shade = {"location": GRAY_10, "total": BLUE_10}.get(kind)
            if shade:
                for k in range(1, len(LOCATION_COLUMNS) + 2):
                    ws.cell(row=row, column=k).fill = fill(shade)
            if kind == "outlet":
                ws.row_dimensions[row].outline_level = 1
    row += 1
    c = ws.cell(row=row, column=1, value="Project total")
    for k, (_, key, _) in enumerate(LOCATION_COLUMNS, start=2):
        cell = ws.cell(row=row, column=k, value=figures.get(key))
        cell.number_format = count_fmt
    for k in range(1, len(LOCATION_COLUMNS) + 2):
        cell = ws.cell(row=row, column=k)
        cell.font, cell.fill, cell.border = font(bold=True, size=10, color="FFFFFF"), fill(BLUE_60), box
    row += 2
    for line in (("Grey rows are DMS locations: the DMS's figures against the total of their outlets. Indented "
                  "rows are each outlet's own gate register and Verigence figures (the DMS does not split them)."),
                 "Use the + / - buttons on the left to show or hide the outlets."):
        ws.cell(row=row, column=1, value=line).font = font(size=9, italic=True, color=GRAY_70)
        row += 1
    ws.sheet_properties.outlinePr.summaryBelow = False
    ws.column_dimensions["A"].width = 44
    for k in range(2, len(LOCATION_COLUMNS) + 2):
        ws.column_dimensions[get_column_letter(k)].width = 11.5
    ws.freeze_panes = ws.cell(row=head_row + 1, column=2)
    page_setup(ws, title_rows=f"{band_row}:{head_row}")

    # ---- Statement
    ws = wb.create_sheet("Statement")
    row = heading(ws, "Statement", width=1)
    for line in report.get("statementLines") or []:
        row += 1
        is_head = not str(line).startswith("*")
        c = ws.cell(row=row, column=1, value=line if is_head else "• " + str(line).lstrip("* ").strip())
        c.alignment = Alignment(wrap_text=True, vertical="top", indent=0 if is_head else 1)
        if is_head:
            c.font, c.fill = font(bold=True, size=11, color="FFFFFF"), fill(GRAY_70)
        else:
            c.font = font(size=10)
            ws.row_dimensions[row].height = max(15, 15 * (len(str(line)) // 150 + 1))
    ws.column_dimensions["A"].width = 150
    page_setup(ws)

    # ---- the lists, as Excel tables
    used: set[str] = set()

    def table_sheet(title: str, columns: tuple[tuple[str, str], ...], rows: list[dict[str, Any]], about: str,
                    style: str) -> None:
        ws = wb.create_sheet(title[:31])
        start = heading(ws, f"{title} ({len(rows):,})", f"{_subtitle(report)}  |  {about}", width=len(columns))
        for k, (h, _) in enumerate(columns, start=1):
            ws.cell(row=start, column=k, value=h)
        for r in rows:
            ws.append([_cell(r, key) for _, key in columns])
        last = start + len(rows)
        widths = [len(h) for h, _ in columns]
        for r in ws.iter_rows(min_row=start + 1, max_row=last):
            for k, c in enumerate(r):
                c.font = font(size=10)
                if isinstance(c.value, date):
                    c.number_format = "dd-mmm-yy"
                    widths[k] = max(widths[k], 10)
                else:
                    widths[k] = max(widths[k], min(45, len(str(c.value or ""))))
        for c in ws[start]:
            c.font = font(bold=True, color="FFFFFF", size=10)
        for k, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(k)].width = w + 3
        if rows:
            name = "".join(ch for ch in title.title() if ch.isalnum())[:30] or "List"
            while name in used:
                name += "X"
            used.add(name)
            table = Table(displayName=name, ref=f"A{start}:{get_column_letter(len(columns))}{last}")
            table.tableStyleInfo = TableStyleInfo(name=style, showRowStripes=True)
            ws.add_table(table)
        else:
            ws.cell(row=start + 1, column=1, value="None.").font = font(italic=True, color=GRAY_50)
            for c in ws[start]:
                c.fill = fill(GRAY_100)
        ws.freeze_panes = ws.cell(row=start + 1, column=1)
        page_setup(ws, title_rows=f"{start}:{start}")

    styles = {"Bookings not in Verigence": "TableStyleMedium2", "Deliveries not in Verigence": "TableStyleMedium3",
              "Delivery not uploaded": "TableStyleMedium5", "Matched, name differs": "TableStyleMedium4",
              "In Verigence, not in dump": "TableStyleMedium6"}
    for title, columns, rows, about in exception_lists(report):
        table_sheet(title, columns, rows, about, styles.get(title, "TableStyleMedium2"))
    table_sheet("Delivery dump scope", SCOPE_COLUMNS, report.get("deliveryScope") or [],
                "every delivery row of the dump with its Inscope / Outscope", "TableStyleLight9")
    pcs = []
    for entry in report.get("intimations") or []:
        for pc in entry.get("pcs") or []:
            pcs.append({"dealerName": entry.get("dealerName"), "kind": str(entry.get("kind") or "").title(),
                        "pc": pc.get("name"), "outlets": ", ".join(pc.get("outlets") or []),
                        "marked": pc.get("marked"), "markedNew": pc.get("markedNew"),
                        "last": _ist(pc.get("lastAtUtc")) if pc.get("lastAtUtc") else None})
    table_sheet("PC intimations", (("Dealer", "dealerName"), ("Dump", "kind"), ("PC", "pc"), ("Outlets", "outlets"),
                                   ("Rows marked", "marked"), ("Since last dump", "markedNew"),
                                   ("Last marked", "last")), pcs, "rows each PC marked as intimated",
                "TableStyleLight1")

    # ---- How it is counted
    ws = wb.create_sheet("How it is counted")
    row = heading(ws, "How it is counted", width=2)
    for n, line in enumerate(COUNTING_NOTES, start=1):
        row += 1
        ws.cell(row=row, column=1, value=n).font = font(bold=True, color=BLUE_60)
        c = ws.cell(row=row, column=2, value=line)
        c.alignment, c.font = Alignment(wrap_text=True, vertical="top"), font(size=10)
        ws.row_dimensions[row].height = 15 * (len(line) // 140 + 1)
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 150
    page_setup(ws)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --------------------------------------------------------------------------- PDF
def build_report_pdf(report: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.platypus import (
        CondPageBreak,
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    hexc = lambda h: colors.HexColor("#" + h)
    page = landscape(A4)
    margin = 12 * mm
    width = page[0] - 2 * margin
    figures = report.get("figures") or {}
    day = _day(report.get("date"))
    as_at = f"As at {day:%d %b %Y}" if day else ""
    footer_left = _generated(report)

    s = lambda name, **kw: ParagraphStyle(name, fontName=kw.pop("font", "Helvetica"), **kw)
    title = s("title", font="Helvetica-Bold", fontSize=20, leading=24, textColor=hexc(GRAY_100))
    sub = s("sub", fontSize=10, leading=13, textColor=hexc(GRAY_70))
    h1 = s("h1", font="Helvetica-Bold", fontSize=13, leading=16, textColor=hexc(GRAY_100), spaceBefore=6,
           spaceAfter=4)
    note = s("note", fontSize=8, leading=10, textColor=hexc(GRAY_70))
    body = s("body", fontSize=9, leading=12, textColor=hexc(GRAY_100))
    cell = s("cell", fontSize=7, leading=8.5, textColor=hexc(GRAY_100))
    cell_b = s("cellb", font="Helvetica-Bold", fontSize=7.5, leading=9, textColor=hexc(GRAY_100))
    head = s("head", font="Helvetica-Bold", fontSize=7, leading=8.5, textColor=colors.white)
    num = s("num", fontSize=7.5, leading=9, alignment=TA_RIGHT, textColor=hexc(GRAY_100))

    def p(text: Any, style: ParagraphStyle = cell) -> Paragraph:
        return Paragraph(escape(str(text)) if text not in (None, "") else "", style)

    def n(value: Any) -> str:
        return f"{value:,}" if isinstance(value, int) and value else ("-" if value in (0, None) else str(value))

    class Numbered(pdf_canvas.Canvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._pages: list[dict[str, Any]] = []

        def showPage(self) -> None:
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total: int) -> None:
            w, h = page
            self.setFillColor(hexc(BLUE_60))
            self.rect(0, h - 4, w, 4, stroke=0, fill=1)
            self.setFillColor(hexc(GRAY_100))
            self.setFont("Helvetica-Bold", 8)
            self.drawString(margin, h - 9 * mm, f"Verigence  |  {TITLE}")
            self.setFont("Helvetica", 8)
            self.drawRightString(w - margin, h - 9 * mm, as_at)
            self.setStrokeColor(hexc(GRAY_20))
            self.line(margin, 9 * mm, w - margin, 9 * mm)
            self.setFillColor(hexc(GRAY_50))
            self.setFont("Helvetica", 7)
            self.drawString(margin, 5.5 * mm, footer_left)
            self.drawRightString(w - margin, 5.5 * mm, f"Page {self._pageNumber} of {total}")

    story: list[Any] = [Paragraph(TITLE, title), Spacer(1, 2), Paragraph(escape(_subtitle(report)), sub),
                        Paragraph(escape(_generated(report)), note), Spacer(1, 8)]

    # Headline figures: three cards.
    def card(name: str, accent: str, light: str, items: Iterable[tuple[str, Any]]) -> Table:
        rows = [[Paragraph(f"<b>{name}</b>", s(name, fontSize=9, textColor=colors.white)), ""]]
        for label, value in items:
            rows.append([p(label, body), Paragraph(f"<b>{n(value)}</b>",
                                                   s(name + label, fontSize=12, leading=14, alignment=TA_RIGHT,
                                                     textColor=hexc(accent)))])
        t = Table(rows, colWidths=[width / 3 * 0.68 - 6, width / 3 * 0.32 - 6])
        t.setStyle(TableStyle([
            ("SPAN", (0, 0), (1, 0)), ("BACKGROUND", (0, 0), (-1, 0), hexc(accent)),
            ("BACKGROUND", (0, 1), (-1, -1), hexc(light)),
            ("LINEBELOW", (0, 1), (-1, -2), 0.5, colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        return t

    booking_rate = _pct(figures.get("bookingSynced"), figures.get("bookingDms"))
    delivery_rate = _pct(figures.get("deliverySynced"), figures.get("deliveryDump"))
    cards = Table([[
        card("BOOKINGS", BLUE_60, BLUE_10, (("As per DMS", figures.get("bookingDms")),
                                            ("In Verigence", figures.get("bookingInVerigence")),
                                            ("Synced", figures.get("bookingSynced")),
                                            ("Not in Verigence", figures.get("bookingNotInVerigence")),
                                            ("In Verigence, not in DMS", figures.get("bookingNotInDms")))),
        card("DELIVERIES", TEAL_60, TEAL_10, (("As per delivery dump", figures.get("deliveryDump")),
                                              ("In Verigence", figures.get("deliveryInVerigence")),
                                              ("Synced", figures.get("deliverySynced")),
                                              ("Delivery not uploaded", figures.get("deliveryNotUploaded")),
                                              ("Not in Verigence", figures.get("deliveryNotInVerigence")))),
        card("GATE REGISTER", GRAY_70, GRAY_10, (("Deliveries on the gate out register", figures.get("gateRegister")),
                                                 ("Registers missed", figures.get("registersMissed")),
                                                 ("Counts pending", figures.get("countsPending")),
                                                 ("Inscope (dump)", figures.get("dumpInscope")),
                                                 ("Outscope (dump)", figures.get("dumpOutscope")))),
    ]], colWidths=[width / 3] * 3)
    cards.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    story += [cards, Spacer(1, 6)]
    rates = []
    if booking_rate is not None:
        rates.append(f"Booking sync rate <b>{booking_rate:.1%}</b> (synced / as per DMS)")
    if delivery_rate is not None:
        rates.append(f"Delivery sync rate <b>{delivery_rate:.1%}</b> (synced / as per dump)")
    if rates:
        story.append(Paragraph("&nbsp;&nbsp;|&nbsp;&nbsp;".join(rates), body))
    story.append(Spacer(1, 8))

    # By dealership.
    story.append(Paragraph("By dealership", h1))
    heads = ["Dealership", "Booking dump", "Delivery dump", "Bookings (DMS)", "Synced", "Sync %",
             "Not in Verigence", "Deliveries (dump)", "Synced", "Sync %", "Not uploaded", "Not in Verigence",
             "Gate register", "Registers missed"]
    data = [[p(h, head) for h in heads]]

    def dealer_line(name: str, f: dict[str, Any], booking_day: Any, delivery_day: Any, bold: bool) -> list[Any]:
        st = cell_b if bold else cell
        br, dr = _pct(f.get("bookingSynced"), f.get("bookingDms")), _pct(f.get("deliverySynced"), f.get("deliveryDump"))
        return [p(name, st), p(f"{_day(booking_day):%d %b}" if _day(booking_day) else "", st),
                p(f"{_day(delivery_day):%d %b}" if _day(delivery_day) else "", st),
                *[p(n(f.get(k)), num) for k in ("bookingDms", "bookingSynced")],
                p(f"{br:.1%}" if br is not None else "-", num), p(n(f.get("bookingNotInVerigence")), num),
                *[p(n(f.get(k)), num) for k in ("deliveryDump", "deliverySynced")],
                p(f"{dr:.1%}" if dr is not None else "-", num),
                *[p(n(f.get(k)), num) for k in ("deliveryNotUploaded", "deliveryNotInVerigence", "gateRegister",
                                                  "registersMissed")]]

    for entry in summary_rows(report):
        data.append(dealer_line(entry["name"], entry["figures"], entry["bookingDump"], entry["deliveryDump"], False))
    data.append(dealer_line("Project total", figures, None, None, True))
    first = width * 0.16
    t = Table(data, colWidths=[first] + [(width - first) / (len(heads) - 1)] * (len(heads) - 1), repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), hexc(GRAY_100)), ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white,
                                                                                               hexc(GRAY_10)]),
        ("BACKGROUND", (0, -1), (-1, -1), hexc(BLUE_10)), ("GRID", (0, 0), (-1, -1), 0.4, hexc(GRAY_20)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [t, PageBreak()]

    # By DMS location: the gate register grouping.
    story.append(Paragraph("Deliveries by DMS location, against the gate register", h1))
    story.append(Paragraph("Outlets are grouped as the DMS groups them. A grey row is a DMS location: the DMS's "
                           "figures against the total of its outlets. Indented rows are each outlet's own gate "
                           "register and Verigence figures, which the DMS does not split.", note))
    story.append(Spacer(1, 4))
    bands = [("", 1)]
    for band in ("Gate register", "Deliveries", "Bookings"):
        bands.append((band.upper(), sum(1 for c in LOCATION_COLUMNS if c[2] == band)))
    label_w = width * 0.17
    col_w = (width - label_w) / len(LOCATION_COLUMNS)
    head_sm = s("headsm", font="Helvetica-Bold", fontSize=6.5, leading=7.5, textColor=colors.white)
    for dealer in report.get("dealers") or []:
        band_row, at = [], 0
        spans = []
        for name, count in bands:
            band_row += [p(name, head)] + [""] * (count - 1)
            if count > 1:
                spans.append(("SPAN", (at, 0), (at + count - 1, 0)))
            at += count
        rows = [band_row, [p(dealer["dealerName"], head)] + [p(h, head_sm) for h, _, _ in LOCATION_COLUMNS]]
        styles: list[Any] = [*spans, ("BACKGROUND", (0, 0), (0, 1), hexc(GRAY_100)),
                             ("BACKGROUND", (1, 1), (-1, 1), hexc(GRAY_100)),
                             ("GRID", (0, 1), (-1, -1), 0.4, hexc(GRAY_20)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("LEFTPADDING", (1, 0), (-1, -1), 2.5), ("RIGHTPADDING", (1, 0), (-1, -1), 2.5)]
        at = 1
        for name, count in bands[1:]:
            colour = BAND_COLOURS.get(name.title(), GRAY_70)
            styles.append(("BACKGROUND", (at, 0), (at + count - 1, 0), hexc(colour)))
            at += count
        for entry in location_rows(dealer):
            kind = entry["kind"]
            label_style = s("loc" + kind, font="Helvetica-Bold" if kind != "outlet" else "Helvetica", fontSize=7.5,
                            leading=9, leftIndent=10 if kind == "outlet" else 0,
                            textColor=hexc(GRAY_70 if kind == "outlet" else GRAY_100))
            line = [Paragraph(escape(entry["label"]), label_style)]
            for _, key, _ in LOCATION_COLUMNS:
                value = None if kind == "outlet" and key in DMS_ONLY else entry["figures"].get(key)
                colour = (RED_60 if key in ("deliveryNotInVerigence", "bookingNotInVerigence") and value
                          else "8E6A00" if key == "deliveryNotUploaded" and value else GRAY_100)
                text = "" if value is None else n(value)
                if kind != "outlet" and value:
                    text = f"<b>{text}</b>"
                line.append(Paragraph(text, s("v", fontSize=7.5, leading=9, alignment=TA_RIGHT,
                                              textColor=hexc(colour))))
            rows.append(line)
            r = len(rows) - 1
            if kind == "location":
                styles.append(("BACKGROUND", (0, r), (-1, r), hexc(GRAY_10)))
            elif kind == "total":
                styles.append(("BACKGROUND", (0, r), (-1, r), hexc(BLUE_10)))
        t = Table(rows, colWidths=[label_w] + [col_w] * len(LOCATION_COLUMNS), repeatRows=2)
        t.setStyle(TableStyle(styles))
        story += [CondPageBreak(60 * mm), t, Spacer(1, 10)]
    story.append(PageBreak())

    # Statement.
    story.append(Paragraph("Statement", h1))
    for line in report.get("statementLines") or []:
        text = str(line)
        if text.startswith("*"):
            story.append(Paragraph(escape(text.lstrip("* ").strip()), s("bullet", fontSize=8.5, leading=11,
                                                                        leftIndent=12, bulletIndent=2,
                                                                        textColor=hexc(GRAY_100)),
                                   bulletText="•"))
        else:
            story.append(Spacer(1, 4))
            story.append(Paragraph(f"<b>{escape(text)}</b>", s("dealerhead", fontSize=10, leading=13,
                                                               textColor=hexc(BLUE_60))))
    story.append(Spacer(1, 8))
    story.append(KeepTogether([Paragraph("How it is counted", h1)] + [
        Paragraph(f"{i}. {escape(line)}", s(f"howto{i}", fontSize=8, leading=10.5, textColor=hexc(GRAY_70),
                                           spaceAfter=2))
        for i, line in enumerate(COUNTING_NOTES, start=1)]))

    # The exceptions.
    pdf_columns = {
        "Bookings not in Verigence": (("Dealer", "dealerName", 0.11), ("DMS location", "location", 0.11),
                                      ("Date", "eventDate", 0.06), ("Customer", "customerName", 0.19),
                                      ("Mobile", "mobile", 0.08), ("OTF / booking no.", "otf", 0.1),
                                      ("Model", "model", 0.1), ("Consultant", "consultant", 0.13),
                                      ("Intimation", "intimation", 0.12)),
        "Deliveries not in Verigence": (("Dealer", "dealerName", 0.1), ("DMS location", "location", 0.1),
                                        ("Date", "eventDate", 0.06), ("Customer", "customerName", 0.19),
                                        ("Mobile", "mobile", 0.08), ("Booking no.", "otf", 0.09),
                                        ("Chassis", "chassis", 0.08), ("Model", "model", 0.1),
                                        ("Scope", "dealType", 0.08), ("Intimation", "intimation", 0.12)),
        "Delivery not uploaded": (("Dealer", "dealerName", 0.1), ("DMS location", "location", 0.1),
                                  ("Date", "eventDate", 0.06), ("Customer", "customerName", 0.19),
                                  ("Booking no.", "otf", 0.09), ("Chassis", "chassis", 0.08),
                                  ("Model", "model", 0.1), ("Journey", "journeyReference", 0.12),
                                  ("Scope", "dealType", 0.07), ("Intimation", "intimation", 0.09)),
        "Matched, name differs": (("Dealer", "dealerName", 0.1), ("Dump", "kind", 0.06), ("Date", "eventDate", 0.06),
                                  ("DMS customer", "dmsCustomer", 0.18), ("Verigence customer", "journeyCustomer", 0.18),
                                  ("Journey", "journeyReference", 0.12), ("Matched on", "matchedOn", 0.1),
                                  ("Chassis", "chassis", 0.08), ("OTF / booking no.", "otf", 0.12)),
        "In Verigence, not in dump": (("Dealer", "dealerName", 0.11), ("Outlet", "outletName", 0.12),
                                      ("Delivery date", "eventDate", 0.07), ("Dated by", "datedBy", 0.09),
                                      ("Customer", "customerName", 0.2), ("Chassis", "chassis", 0.09),
                                      ("Model", "model", 0.12), ("Journey", "journeyReference", 0.12),
                                      ("Mobile", "mobile", 0.08)),
    }
    accents = {"Bookings not in Verigence": BLUE_60, "Deliveries not in Verigence": RED_60,
               "Delivery not uploaded": "8E6A00", "Matched, name differs": "6929C4",
               "In Verigence, not in dump": TEAL_60}
    for title_, _columns, rows, about in exception_lists(report):
        story.append(PageBreak())
        story.append(Paragraph(f"{escape(title_)} <font color='#{GRAY_50}'>({len(rows):,})</font>", h1))
        story.append(Paragraph(escape(about), note))
        story.append(Spacer(1, 4))
        if not rows:
            story.append(Paragraph("None.", body))
            continue
        cols = pdf_columns[title_]
        data = [[p(h, head) for h, _, _ in cols]]
        ordered = sorted(rows, key=lambda r: (str(r.get("dealerName") or ""), str(r.get("location") or
                                                                                  r.get("outletName") or ""),
                                              str(r.get("eventDate") or "")))
        for r in ordered:
            line = []
            for _, key, _ in cols:
                value = _cell(r, key)
                if isinstance(value, date):
                    value = f"{value:%d %b}"
                line.append(p(value))
            data.append(line)
        t = Table(data, colWidths=[width * w for _, _, w in cols], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), hexc(accents.get(title_, GRAY_100))),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, hexc(GRAY_10)]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, hexc(GRAY_20)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story.append(t)

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=14 * mm,
                            bottomMargin=13 * mm, title=TITLE, author="Verigence",
                            subject=_subtitle(report))
    doc.build(story, canvasmaker=Numbered)
    return out.getvalue()


def file_name(report: dict[str, Any], extension: str) -> str:
    return f"DMS-Reconciliation-Report_{report.get('date') or 'report'}.{extension}"


__all__ = ["build_report_pdf", "build_report_xlsx", "exception_lists", "file_name", "location_rows"]
