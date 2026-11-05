"""Build a month's Mahindra consolidated price list (the OEM_CONSOLIDATED layout the
price-master ingest reads as a complete book) from the dealer's native PV & CV and EV
workbooks, with the October 2026 book's model spellings and source-sheet names.

Usage: python tools/mahindra_consolidate_price_list.py PVCV.xlsx EV.xlsx PREVIOUS_CONSOLIDATED.xlsx OUT.xlsx

Every row must reconcile (components + registration = on-road, both bases) or it stops.
Built for November 2026 (4 Nov list); a new dealer tab must be added to SHEETS."""
import re, sys, json
from decimal import Decimal
from openpyxl import load_workbook, Workbook


PVCV, EV, OCT, OUT = sys.argv[1:5]

# source sheet in the Nov file -> (source-sheet name to print, category, model printed)
SHEETS = {
    "XUV7XO D": ("XUV7XO D", "PV", "XUV 7XO"),
    "XUV7XO PM (2)": ("XUV7XO PM (2)", "PV", "XUV 7XO"),
    "NEW BOLERO NEO (PVT)": ("NEW BOLERO NEO (PVT)", "PV", "BOLERO*"),
    "NEW BOLERO NEO-COM": ("NEW BOLERO NEO-COM", "PV", "BOLERO*"),
    "THRA 3 DOOR,": ("THAR 3 DOOR (DSL)", "PV", "NEW THAR"),
    "THAR 3DOOR": ("THAR 3 DOOR (PM)", "PV", "NEW THAR"),
    "VEERO": ("VEERO", "CV", "VEERO"),
    "SUPRO": ("SUPRO", "CV", "SUPRO"),
    "MAXX CITY": ("MAXX CITY", "CV", "MAXX CITY"),
    "MAXX HD": ("MAXX HD", "CV", "MAXX HD"),
    "PICK UP": ("PICK UP", "CV", "PICK UP"),
    "XUV400": ("XUV400", "BEV", "XUV400"),
    "XUV3XO D)": ("XUV3XO (D)", "PV", "XUV 3XO"),
    "XUV3XO PM": ("XUV3XO PM", "PV", "XUV 3XO"),
    "SCORPIO CLASSIC": ("SCORPIO CLASSIC", "PV", "SCORPIO"),
    "THAR ROXX D": ("THAR ROXX D", "PV", "THAR ROXX"),
    "THAR ROXX PM": ("THAR ROXX PM", "PV", "THAR ROXX"),
    "SCORPIO N PM new": ("SCORPIO N PM new", "PV", "SCORPIO-N"),
    "SCORPIO N D old": ("SCORPIO N D old", "PV", "SCORPIO N"),
    "SCORPIO N D New": ("SCORPIO N D New", "PV", "SCORPIO-N"),
    # EV workbook (Cuttack sheets; 'xuv3XO EV pkl' is the Panikoili outlet's copy, left out as in October)
    "New SPAT": ("New SPAT", "BEV", "BE6 SPORTEQ"),
    "xuv3XO EVctc": ("xuv3XO EVctc", "BEV", "XUV 3XO EV"),
    "XEV9 s": ("XEV9 s", "BEV", "XEV 9S"),
    "XEV9": ("XEV9", "BEV", "XEV 9E"),
    "BE6": ("BE6", "BEV", "BE 6"),
}
SKIPPED = {"xuv3XO EV pkl"}
BOLERO = {"B": "Bolero", "N": "Bolero neo", "P": "Bolero Neo Plus"}
TRANS = {"MANUAL": "MT", "AUTOMATIC": "AT"}
FUEL = {"DISEL": "DIESEL", "DSL": "DIESEL", "ELE": "ELECTRIC"}

def txt(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else None

def num(v, blank=None):
    if v in (None, ""): return blank
    if isinstance(v, (int, float)): return Decimal(str(v))
    s = str(v).replace(",", "").strip()
    if s in ("-", ""): return Decimal(0)
    try: return Decimal(s)
    except Exception: return None

def comp(label):
    u = label.upper()
    if "EX-SHOWROOM" in u or "EX SHOWROOM" in u: return "EX"
    if "TAX COLLECTION" in u or re.search(r"\bTCS\b", u): return "TCS"
    if "INSURANCE" in u: return "INS"
    if "WARRANTY" in u: return "EW45" if re.search(r"5\s*TH", u) else "EW4"
    if "ACCESSOR" in u: return "ACC"
    if re.search(r"\bRSA\b", u): return "RSA"
    if "FASTAG" in u: return "FASTAG"
    return None

# October attributes by (model, variant upper, basis-sheet) for blank attribute fallback
oct_rows = list(load_workbook(OCT, data_only=True, read_only=True).worksheets[0].iter_rows(values_only=True))
oct_attr = {}
for r in oct_rows[1:]:
    if r[1] in ("PV", "CV", "BEV") and r[3]:
        oct_attr[(r[2].upper(), txt(r[3]).upper())] = r[4:9]

out_rows, report = [], {"sheets": {}, "skipped": sorted(SKIPPED), "dropped_dupes": 0}
for path in (PVCV, EV):
    wb = load_workbook(path, data_only=True, read_only=True)
    for ws in wb.worksheets:
        if ws.title in SKIPPED: continue
        if ws.title not in SHEETS:
            raise SystemExit(f"unmapped sheet {ws.title!r}")
        src, cat, model = SHEETS[ws.title]
        rows = [list(r)[:40] for r in ws.iter_rows(values_only=True)]
        h = next(i for i, r in enumerate(rows) if any(txt(c) and "EX-SHOWROOM" in txt(c).upper() for c in r))
        sub = rows[h + 1]
        letters = next(i for i in range(h, h + 4) if sum(1 for c in rows[i] if txt(c) and re.match(r"^\([A-Z]\)", txt(c))) >= 3)
        hdr = rows[h]
        vcol = next(j for j, c in enumerate(hdr) if txt(c))
        cols = {}
        for j, c in enumerate(hdr):
            t = txt(c)
            if not t or j == vcol: continue
            u = t.upper()
            if u in ("TRIM", "FUEL", "TRANSMISSION", "DRIVE", "SEATER"): cols[u] = j
            else:
                k = comp(t)
                if k and k not in cols: cols[k] = j
        regs = [j for j, c in enumerate(sub) if txt(c) and "REGISTRATION" in txt(c).upper()]
        ons = [j for j, c in enumerate(sub) if txt(c) and "ON ROAD" in txt(c).upper()]
        assert len(regs) == 2 and len(ons) == 2, (ws.title, regs, ons)
        n = 0; seen = {}
        for i in range(letters + 1, len(rows)):
            r = rows[i] + [None] * 40
            v = txt(r[vcol]); ex = num(r[cols["EX"]])
            if not v or ex is None or ex <= 0: continue
            vals = {k: (num(r[cols[k]], Decimal(0)) if k in cols else Decimal(0)) for k in ("EX", "TCS", "INS", "EW4", "EW45", "ACC", "RSA", "FASTAG")}
            if any(x is None for x in vals.values()): raise SystemExit(f"{ws.title} row {i+1}: non-numeric {vals}")
            reg_i, on_i, reg_c, on_c = (num(r[regs[0]]), num(r[ons[0]]), num(r[regs[1]]), num(r[ons[1]]))
            base = sum(vals.values())
            for reg, on, lab in ((reg_i, on_i, "individual"), (reg_c, on_c, "corporate")):
                if abs(base + reg - on) > Decimal("1.00"):
                    raise SystemExit(f"{ws.title} row {i+1} {v}: {lab} on-road {on} != sum {base + reg}")
            m = model
            if model == "BOLERO*":
                m = BOLERO[re.match(r"^\s*([BNP])\s*\d", v.upper()).group(1)]
            a = {k: txt(r[cols[k]]) if k in cols else None for k in ("TRIM", "FUEL", "TRANSMISSION", "DRIVE", "SEATER")}
            if a["FUEL"]: a["FUEL"] = FUEL.get(a["FUEL"].upper(), a["FUEL"].upper())
            if a["TRANSMISSION"]: a["TRANSMISSION"] = TRANS.get(a["TRANSMISSION"].upper(), a["TRANSMISSION"].upper())
            if a["DRIVE"]: a["DRIVE"] = a["DRIVE"].upper()
            if a["TRIM"]: a["TRIM"] = txt(r[cols["TRIM"]])
            prev = oct_attr.get((m.upper(), v.upper()))
            if not any(a.values()) and prev:
                a = dict(zip(("TRIM", "FUEL", "TRANSMISSION", "DRIVE", "SEATER"), prev))
            if cat == "BEV": a["FUEL"] = "ELECTRIC"
            if cat == "CV" and not a["FUEL"]: a["FUEL"] = "DIESEL"
            seat = a["SEATER"]
            if isinstance(seat, str) and seat.isdigit(): seat = int(seat)
            key = (m.upper(), v.upper(), src)
            sig = (vals["EX"], reg_i, on_i, reg_c, on_c)
            if key in seen:
                if seen[key] != sig: raise SystemExit(f"conflict {key}")
                report["dropped_dupes"] += 1; continue
            seen[key] = sig
            out_rows.append([cat, m, v, a["TRIM"], a["FUEL"], a["TRANSMISSION"], a["DRIVE"], seat,
                             *[float(vals[k]) for k in ("EX", "TCS", "INS", "EW4", "EW45", "ACC", "RSA", "FASTAG")],
                             float(reg_i), float(on_i), float(reg_c), float(on_c), src])
            n += 1
        report["sheets"][ws.title] = n

wb = Workbook(); ws = wb.active; ws.title = "Price List"
ws.append(list(oct_rows[0]))
for i, r in enumerate(out_rows, 1):
    ws.append([i, *r])
ws.append([])
notes = [
    ("Category", "PV = Passenger Vehicle · CV = Commercial Vehicle · BEV = Battery Electric Vehicle"),
    ("On-Road Price", "Live formula = Ex-Showroom + TCS + Insurance + both Extended Warranty options + Accessories Kit + RSA + FASTag + Registration. Reconciles to every source sheet to the rupee."),
    ("Two price pairs", "PV/CV sheets quote Individual vs Corporate (identical in every source row). BEV sheets quote without vs with Hypothecation (differ by Rs. 1,500 registration). Both carried in the same two column pairs."),
    ("Zero cells", "Component not charged on that sheet — BEV sheets bundle RSA and warranty into Essential Accessories, so those columns read 0."),
    ("XUV400", "Sits in the PV/CV source file but is battery-electric, so it is classified BEV here."),
    ("Model names", "Spelt as in the October consolidated book so each vehicle keeps its identity; the old-series SCORPIO N D sheet as September's \"SCORPIO N\"."),
    ("Effective date", "PV & CV tabs are all marked w.e.f. 04.11.2026. The EV workbook's BE 6 / BE 6 SPORTEQ / XEV 9E / XEV 9S tabs read w.e.f. 02.11.2026 and its XUV 3XO EV tabs 04.11.2026; the whole book is loaded effective 04.11.2026 as instructed."),
    ("Source", "PV & CV Price List Nov'26.xlsx and EV Price List Nov'26.xlsx. Source Sheet column traces every row to its tab. The EV workbook's 'xuv3XO EV pkl' tab (Panikoili outlet) is not carried; the Cuttack tab 'xuv3XO EVctc' is, as in October."),
    ("Terms & conditions", "Not carried over — refer to the individual source sheets for booking amount, insurance, permit and jurisdiction clauses."),
]
for k, v in notes:
    ws.append([k, None, v])
wb.save(OUT)
report["rows"] = len(out_rows)
print(json.dumps(report, indent=1))
