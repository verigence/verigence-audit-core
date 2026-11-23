"""uc03_p2_dms_report_document.py -- a generated DMS report as one document, in Excel and in PDF.

Product owner 2026-11-23: "i want to keep it simple": four groups, for bookings and for
deliveries alike, and the deliveries on the gate register:
  1. in Verigence and in DMS                                   (counts)
  2. in Verigence, not in DMS -- with the PC, to confirm each  (list)
  3. in DMS, intimated to a PC, not in Verigence               (list, with the PC who marked it)
  4. left over: in DMS, not in Verigence, not intimated         (list)

Both are made from the stored report (uc03_p2_dms_reports.compute_report), so they agree with
what Verigence shows. Sections: Summary (the groups per dealership), By DMS location (the same
per DMS location and outlet, as the DMS groups outlets), then one list per group 2-4 and kind.
Styling follows IBM Carbon (gray 100 headers, gray 10 bands, blue 60 / teal 60 accents).
(Replaces the 2026-11-22 document's statement, name-differs, cross-check, scope, PC-intimation and
counting-notes sections.)
"""
from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from xml.sax.saxutils import escape

IST = timezone(timedelta(hours=5, minutes=30))

# IBM Carbon tokens.
GRAY_100, GRAY_70, GRAY_50, GRAY_20, GRAY_10 = "161616", "525252", "8D8D8D", "E0E0E0", "F4F4F4"
BLUE_60, BLUE_10, TEAL_60, TEAL_10 = "0F62FE", "EDF5FF", "007D79", "D9FBFB"
RED_60, RED_10, YELLOW_10, GREEN_60, GREEN_10 = "DA1E28", "FFF1F1", "FCF4D6", "198038", "DEFBE6"
PURPLE_10 = "F6F2FF"

TITLE = "DMS Reconciliation Report"

BOOKINGS, DELIVERIES = "Bookings", "Deliveries"
BAND_COLOURS = {BOOKINGS: BLUE_60, DELIVERIES: TEAL_60}
BAND_LIGHT = {BOOKINGS: BLUE_10, DELIVERIES: TEAL_10}
# The four groups (and the gate register): (header, figure key, band).
GROUP_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("In Verigence and DMS", "bookingSynced", BOOKINGS),
    ("In Verigence, not in DMS", "bookingNotInDms", BOOKINGS),
    ("In DMS, intimated to PC, not in Verigence", "bookingIntimated", BOOKINGS),
    ("In DMS, not intimated, not in Verigence", "bookingNotIntimated", BOOKINGS),
    ("As per gate register", "gateRegister", DELIVERIES),
    ("In Verigence and DMS", "deliverySynced", DELIVERIES),
    ("In Verigence, not in DMS", "deliveryNotInDms", DELIVERIES),
    ("In DMS, intimated to PC, not in Verigence", "deliveryIntimated", DELIVERIES),
    ("In DMS, not intimated, not in Verigence", "deliveryNotIntimated", DELIVERIES),
)
# Figures only the DMS gives: blank on an outlet's own row (its DMS location's row carries them).
DMS_ONLY = {"bookingSynced", "bookingIntimated", "bookingNotIntimated", "deliverySynced", "deliveryIntimated",
            "deliveryNotIntimated"}
# Figures to act on: shown in red when not zero.
ACT_ON = {"bookingNotInDms", "bookingNotIntimated", "deliveryNotInDms", "deliveryNotIntimated"}

IN_VERIGENCE_BOOKING_COLUMNS = (("Dealer", "dealerName", 0.11), ("Outlet", "outletName", 0.12),
                                ("Booking date", "bookingDate", 0.07), ("Customer", "customerName", 0.2),
                                ("Mobile", "mobile", 0.08), ("OTF read", "otf", 0.1), ("Model", "model", 0.1),
                                ("Journey", "journeyReference", 0.11), ("PC", "pcName", 0.11))
IN_VERIGENCE_DELIVERY_COLUMNS = (("Dealer", "dealerName", 0.11), ("Outlet", "outletName", 0.12),
                                 ("Delivery date", "eventDate", 0.07), ("Customer", "customerName", 0.2),
                                 ("Mobile", "mobile", 0.08), ("Chassis", "chassis", 0.1), ("Model", "model", 0.1),
                                 ("Journey", "journeyReference", 0.11), ("PC", "pcName", 0.11))
IN_DMS_COLUMNS = (("Dealer", "dealerName", 0.1), ("DMS location", "location", 0.1), ("DMS date", "eventDate", 0.06),
                  ("Customer", "customerName", 0.17), ("Mobile", "mobile", 0.08), ("OTF / booking no.", "otf", 0.1),
                  ("Chassis", "chassis", 0.08), ("Model", "model", 0.09), ("Consultant", "consultant", 0.1),
                  ("In Verigence", "inVerigence", 0.12))
INTIMATED_COLUMNS = (*IN_DMS_COLUMNS[:-2], ("In Verigence", "inVerigence", 0.1), ("Marked by (PC)", "markedBy", 0.12))

NOT_UPLOADED = "Booking on file, delivery not uploaded"
# Excel sheet names (at most 31 characters) of the lists.
SHEET_NAMES = {"Bookings in Verigence, not in DMS": "Bookings in Verigence not DMS",
               "Deliveries in Verigence, not in DMS": "Deliveries in Verigence not DMS",
               "Bookings intimated, not in Verigence": "Bookings intimated to PC",
               "Deliveries intimated, not in Verigence": "Deliveries intimated to PC",
               "Bookings left over": "Bookings left over", "Deliveries left over": "Deliveries left over"}


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


def _intimated(row: dict[str, Any]) -> bool:
    if "intimated" in row:
        return bool(row["intimated"])
    return str(row.get("intimation") or "").startswith("Intimated")  # reports stored before 2026-11-23


def exception_lists(report: dict[str, Any]) -> list[tuple[str, tuple[tuple[str, str, float], ...], list[dict[str, Any]], str]]:
    """(title, columns, rows, what it means) of each list, in reading order: groups 2-4, bookings
    then deliveries."""
    rows = report.get("rows") or []
    in_dms = {kind: [r for r in rows if r.get("kind") == kind] for kind in ("BOOKING", "DELIVERY")}
    return [
        ("Bookings in Verigence, not in DMS", IN_VERIGENCE_BOOKING_COLUMNS, report.get("notInDmsBookings") or [],
         "Booked in Verigence in the period, not in the DMS booking dump: the PC confirms each."),
        ("Deliveries in Verigence, not in DMS", IN_VERIGENCE_DELIVERY_COLUMNS, report.get("notInDump") or [],
         "Delivered in Verigence in the period, not in the DMS delivery dump: the PC confirms each."),
        ("Bookings intimated, not in Verigence", INTIMATED_COLUMNS,
         [r for r in in_dms["BOOKING"] if _intimated(r)],
         "In the DMS booking dump, marked by a PC as intimated to them, not in Verigence: pending with the PC."),
        ("Deliveries intimated, not in Verigence", INTIMATED_COLUMNS,
         [r for r in in_dms["DELIVERY"] if _intimated(r)],
         "In the DMS delivery dump, marked by a PC as intimated to them, not in Verigence: pending with the PC."),
        ("Bookings left over", IN_DMS_COLUMNS, [r for r in in_dms["BOOKING"] if not _intimated(r)],
         "In the DMS booking dump, not in Verigence, not intimated to any PC: pending with the dealership."),
        ("Deliveries left over", IN_DMS_COLUMNS, [r for r in in_dms["DELIVERY"] if not _intimated(r)],
         "In the DMS delivery dump, not in Verigence, not intimated to any PC: pending with the dealership."),
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
    if key in ("eventDate", "bookingDate", "dmsDate") and value:
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
    font = lambda **kw: Font(name="IBM Plex Sans", **kw)
    count_fmt = "#,##0;-#,##0;\"-\""
    figures = report.get("figures") or {}

    def page_setup(ws, *, title_rows: str | None = None) -> None:
        ws.page_setup.orientation = "landscape"
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

    def group_table(ws, row: int, first_header: str, lines: list[tuple[str, str, dict[str, Any]]]) -> int:
        """A band row (Bookings / Deliveries), a header row, then one row per (label, kind, figures):
        kind "outlet" (indented, DMS-only figures blank), "location", "total" or "grand"."""
        ws.cell(row=row, column=1).fill = fill(GRAY_100)
        col = 2
        for band in (BOOKINGS, DELIVERIES):
            span = sum(1 for c in GROUP_COLUMNS if c[2] == band)
            ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)
            c = ws.cell(row=row, column=col, value=band.upper())
            c.font, c.alignment = font(bold=True, color="FFFFFF", size=9), Alignment(horizontal="center")
            for k in range(col, col + span):
                ws.cell(row=row, column=k).fill = fill(BAND_COLOURS[band])
            col += span
        row += 1
        ws.cell(row=row, column=1, value=first_header)
        for k, (h, _, _) in enumerate(GROUP_COLUMNS, start=2):
            ws.cell(row=row, column=k, value=h)
        for c in ws[row]:
            c.font, c.fill, c.border = font(bold=True, color="FFFFFF", size=9), fill(GRAY_100), box
            c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.row_dimensions[row].height = 44
        for label, kind, f in lines:
            row += 1
            c = ws.cell(row=row, column=1, value=label)
            c.alignment = Alignment(indent=2 if kind == "outlet" else 0, wrap_text=True, vertical="center")
            c.border = box
            c.font = font(size=10, bold=kind != "outlet", color=GRAY_70 if kind == "outlet" else GRAY_100)
            for k, (_, key, _) in enumerate(GROUP_COLUMNS, start=2):
                value = None if kind == "outlet" and key in DMS_ONLY else f.get(key)
                cell = ws.cell(row=row, column=k, value=value)
                cell.number_format, cell.border = count_fmt, box
                cell.font = font(size=10, bold=kind in ("total", "grand") or (key in ACT_ON and bool(value)),
                                 color=RED_60 if key in ACT_ON and value else GRAY_100)
            shade = {"location": GRAY_10, "total": BLUE_10}.get(kind)
            if kind == "grand":
                for k in range(1, len(GROUP_COLUMNS) + 2):
                    cell = ws.cell(row=row, column=k)
                    cell.font, cell.fill = font(bold=True, size=10, color="FFFFFF"), fill(BLUE_60)
            elif shade:
                for k in range(1, len(GROUP_COLUMNS) + 2):
                    ws.cell(row=row, column=k).fill = fill(shade)
            if kind == "outlet":
                ws.row_dimensions[row].outline_level = 1
        return row

    # ---- Summary: the groups per dealership
    ws = wb.active
    ws.title = "Summary"
    row = heading(ws, TITLE, width=len(GROUP_COLUMNS) + 1) + 1
    lines = [(f"{e['name']}  (booking dump {_short(e['bookingDump'])}, delivery dump {_short(e['deliveryDump'])})",
              "location", e["figures"]) for e in summary_rows(report)]
    lines.append(("Project total", "grand", figures))
    row = group_table(ws, row, "Dealership", lines) + 2
    ws.cell(row=row, column=1, value="Sheets in this report").font = font(size=11, bold=True)
    for title, _, rows, about in exception_lists(report):
        row += 1
        ws.cell(row=row, column=1, value=f"{title} ({len(rows):,})").font = font(size=10, bold=True, color=BLUE_60)
        ws.cell(row=row, column=3, value=about).font = font(size=10, color=GRAY_70)
    ws.column_dimensions["A"].width = 52
    for k in range(2, len(GROUP_COLUMNS) + 2):
        ws.column_dimensions[get_column_letter(k)].width = 14
    page_setup(ws)

    # ---- By DMS location
    ws = wb.create_sheet("By DMS location")
    row = heading(ws, "By DMS location", f"{_subtitle(report)}  |  outlets grouped as the DMS groups them; each "
                  "outlet's own Verigence and gate register figures under its DMS location",
                  width=len(GROUP_COLUMNS) + 1)
    top = row
    lines = []
    for dealer in report.get("dealers") or []:
        lines += [(e["label"], e["kind"], e["figures"]) for e in location_rows(dealer)]
    lines.append(("Project total", "grand", figures))
    group_table(ws, row, "Dealership / DMS location / outlet", lines)
    ws.sheet_properties.outlinePr.summaryBelow = False
    ws.column_dimensions["A"].width = 44
    for k in range(2, len(GROUP_COLUMNS) + 2):
        ws.column_dimensions[get_column_letter(k)].width = 13
    ws.freeze_panes = ws.cell(row=top + 2, column=2)
    page_setup(ws, title_rows=f"{top}:{top + 1}")

    # ---- the lists, as Excel tables
    used: set[str] = set()
    styles = ("TableStyleMedium6", "TableStyleMedium6", "TableStyleMedium5", "TableStyleMedium5",
              "TableStyleMedium3", "TableStyleMedium3")
    for (title, columns, rows, about), style in zip(exception_lists(report), styles, strict=True):
        ws = wb.create_sheet(SHEET_NAMES.get(title, title)[:31])
        start = heading(ws, f"{title} ({len(rows):,})", f"{_subtitle(report)}  |  {about}", width=len(columns))
        for k, (h, _, _) in enumerate(columns, start=1):
            ws.cell(row=start, column=k, value=h)
        for r in _ordered(rows):
            ws.append([_cell(r, key) for _, key, _ in columns])
        last = start + len(rows)
        widths = [len(h) for h, _, _ in columns]
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

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _short(value: Any) -> str:
    day = _day(value)
    return f"{day:%d %b}" if day else "none"


def _ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (str(r.get("dealerName") or ""), str(r.get("location") or r.get("outletName")
                                                                           or ""),
                                       str(r.get("eventDate") or r.get("bookingDate") or "")))


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
    head = s("head", font="Helvetica-Bold", fontSize=7, leading=8.5, textColor=colors.white)

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

    # Headline: one card for bookings, one for deliveries.
    def card(band: str) -> Table:
        accent, light = BAND_COLOURS[band], BAND_LIGHT[band]
        rows = [[Paragraph(f"<b>{band.upper()}</b>", s(band, fontSize=9, textColor=colors.white)), ""]]
        for label, key, b in GROUP_COLUMNS:
            if b != band:
                continue
            value = figures.get(key)
            colour = RED_60 if key in ACT_ON and value else accent
            rows.append([p(label, body), Paragraph(f"<b>{n(value)}</b>", s(band + key, fontSize=12, leading=14,
                                                                           alignment=TA_RIGHT,
                                                                           textColor=hexc(colour)))])
        t = Table(rows, colWidths=[width / 2 * 0.72 - 6, width / 2 * 0.28 - 6])
        t.setStyle(TableStyle([
            ("SPAN", (0, 0), (1, 0)), ("BACKGROUND", (0, 0), (-1, 0), hexc(accent)),
            ("BACKGROUND", (0, 1), (-1, -1), hexc(light)), ("LINEBELOW", (0, 1), (-1, -2), 0.5, colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        return t

    cards = Table([[card(BOOKINGS), card(DELIVERIES)]], colWidths=[width / 2] * 2)
    cards.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    story += [cards, Spacer(1, 10)]

    head_sm = s("headsm", font="Helvetica-Bold", fontSize=6.5, leading=7.5, textColor=colors.white)
    label_w = width * 0.2
    col_w = (width - label_w) / len(GROUP_COLUMNS)

    def group_table(first_header: str, lines: list[tuple[str, str, dict[str, Any]]]) -> Table:
        band_row: list[Any] = [p("", head)]
        styles: list[Any] = [("BACKGROUND", (0, 0), (-1, 1), hexc(GRAY_100)),
                             ("GRID", (0, 1), (-1, -1), 0.4, hexc(GRAY_20)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("LEFTPADDING", (1, 0), (-1, -1), 2.5), ("RIGHTPADDING", (1, 0), (-1, -1), 2.5)]
        at = 1
        for band in (BOOKINGS, DELIVERIES):
            span = sum(1 for c in GROUP_COLUMNS if c[2] == band)
            band_row += [p(band.upper(), head)] + [""] * (span - 1)
            styles += [("SPAN", (at, 0), (at + span - 1, 0)),
                       ("BACKGROUND", (at, 0), (at + span - 1, 0), hexc(BAND_COLOURS[band]))]
            at += span
        rows = [band_row, [p(first_header, head)] + [p(h, head_sm) for h, _, _ in GROUP_COLUMNS]]
        for label, kind, f in lines:
            label_style = s("l" + kind, font="Helvetica" if kind == "outlet" else "Helvetica-Bold", fontSize=7.5,
                            leading=9, leftIndent=10 if kind == "outlet" else 0,
                            textColor=colors.white if kind == "grand"
                            else hexc(GRAY_70 if kind == "outlet" else GRAY_100))
            line = [Paragraph(escape(label), label_style)]
            for _, key, _ in GROUP_COLUMNS:
                value = None if kind == "outlet" and key in DMS_ONLY else f.get(key)
                colour = ("FFFFFF" if kind == "grand" else RED_60 if key in ACT_ON and value else GRAY_100)
                text = "" if value is None else n(value)
                if kind != "outlet" and value:
                    text = f"<b>{text}</b>"
                line.append(Paragraph(text, s("v", fontSize=7.5, leading=9, alignment=TA_RIGHT,
                                              textColor=hexc(colour))))
            rows.append(line)
            r = len(rows) - 1
            shade = {"location": GRAY_10, "total": BLUE_10, "grand": BLUE_60}.get(kind)
            if shade:
                styles.append(("BACKGROUND", (0, r), (-1, r), hexc(shade)))
        t = Table(rows, colWidths=[label_w] + [col_w] * len(GROUP_COLUMNS), repeatRows=2)
        t.setStyle(TableStyle(styles))
        return t

    story.append(Paragraph("By dealership", h1))
    lines = [(f"{e['name']} (booking dump {_short(e['bookingDump'])}, delivery dump {_short(e['deliveryDump'])})",
              "location", e["figures"]) for e in summary_rows(report)]
    lines.append(("Project total", "grand", figures))
    story += [group_table("Dealership", lines), PageBreak()]

    story.append(Paragraph("By DMS location", h1))
    story.append(Paragraph("Outlets are grouped as the DMS groups them. A grey row is a DMS location; indented rows "
                           "are each outlet's own Verigence and gate register figures (the DMS does not split its "
                           "own).", note))
    story.append(Spacer(1, 4))
    for dealer in report.get("dealers") or []:
        story += [CondPageBreak(50 * mm),
                  group_table(dealer["dealerName"], [(e["label"], e["kind"], e["figures"])
                                                     for e in location_rows(dealer)]), Spacer(1, 10)]

    accents = (TEAL_60, TEAL_60, "8E6A00", "8E6A00", RED_60, RED_60)
    for (title_, cols, rows, about), accent in zip(exception_lists(report), accents, strict=True):
        story.append(PageBreak())
        story.append(Paragraph(f"{escape(title_)} <font color='#{GRAY_50}'>({len(rows):,})</font>", h1))
        story.append(Paragraph(escape(about), note))
        story.append(Spacer(1, 4))
        if not rows:
            story.append(Paragraph("None.", body))
            continue
        data = [[p(h, head) for h, _, _ in cols]]
        for r in _ordered(rows):
            line = []
            for _, key, _ in cols:
                value = _cell(r, key)
                if isinstance(value, date):
                    value = f"{value:%d %b}"
                line.append(p(value))
            data.append(line)
        t = Table(data, colWidths=[width * w for _, _, w in cols], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), hexc(accent)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, hexc(GRAY_10)]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, hexc(GRAY_20)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story.append(t)

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=14 * mm,
                            bottomMargin=13 * mm, title=TITLE, author="Verigence", subject=_subtitle(report))
    doc.build(story, canvasmaker=Numbered)
    return out.getvalue()


def file_name(report: dict[str, Any], extension: str) -> str:
    return f"DMS-Reconciliation-Report_{report.get('date') or 'report'}.{extension}"


__all__ = ["build_report_pdf", "build_report_xlsx", "exception_lists", "file_name", "location_rows"]
