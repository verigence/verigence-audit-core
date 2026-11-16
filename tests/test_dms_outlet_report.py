"""The DMS outlet report's location map (product owner 2026-11-16): Shivnath's two
locations as they are, Utkal's ANANDAPUR under KEONJHAR, Aditya's JAJPUR alone and
everything else under CUTTACK. Pure: no database."""
from __future__ import annotations

import io

from openpyxl import load_workbook

from audit_core import uc03_p2_dms_outlet_report as m

UTKAL = [
    {"outlet_id": "ana", "outlet_name": "Utkal Mahindra - ANANDAPUR", "dms_outlet_id": "keo",
     "dms_labels": ["ANANDAPUR"], "catch_all": False},
    {"outlet_id": "keo", "outlet_name": "Utkal Mahindra - KEONJHAR", "dms_outlet_id": "keo", "dms_labels": [],
     "catch_all": False},
    {"outlet_id": "cda", "outlet_name": "Utkal Mahindra - CDA", "dms_outlet_id": "cda",
     "dms_labels": ["CDA", "CSA_SEC_10_SH"], "catch_all": False},
    {"outlet_id": "man", "outlet_name": "Utkal Mahindra - MANCHESWAR FULLRANGE SR NPAHAL WS 2ND FL",
     "dms_outlet_id": "man", "dms_labels": ["MANCHESWAR & CUBE"], "catch_all": False},
    {"outlet_id": "cub", "outlet_name": "Utkal Mahindra - CUBE_UNIT3", "dms_outlet_id": "cub", "dms_labels": [],
     "catch_all": False},
]
ADITYA = [
    {"outlet_id": "jaj", "outlet_name": "Aditya Motors - Jajpur", "dms_outlet_id": "jaj", "dms_labels": ["JAJPUR"],
     "catch_all": False},
    {"outlet_id": "ctc", "outlet_name": "Aditya Motors - Cuttack", "dms_outlet_id": "ctc", "dms_labels": ["CUTTACK"],
     "catch_all": True},
    {"outlet_id": "khd", "outlet_name": "Aditya Motors - Khordha", "dms_outlet_id": "ctc", "dms_labels": [],
     "catch_all": False},
    {"outlet_id": "cube", "outlet_name": "Aditya Motors - Bhubaneswar_Cube", "dms_outlet_id": "ctc",
     "dms_labels": ["BHUBANESHWAR_SH"], "catch_all": False},
]
SHIVNATH = [
    {"outlet_id": "kes", "outlet_name": "Shivnath Motors - Kesora / Puri Bypass", "dms_outlet_id": "kes",
     "dms_labels": ["PURI_BY_PASS_SH", "PURIBYPASS"], "catch_all": False},
    {"outlet_id": "tam", "outlet_name": "Shivnath Motors - Tamando", "dms_outlet_id": "tam", "dms_labels": [],
     "catch_all": False},
]


def test_location_names_are_compared_on_letters_and_digits() -> None:
    assert m.label_key("Puri By-Pass") == "PURIBYPASS"
    assert m.resolve_outlet("puri by pass", SHIVNATH) == "kes"  # spaces and case do not matter
    assert m.resolve_outlet("PURI_BY_PASS_SH", SHIVNATH) == "kes"
    assert m.resolve_outlet("PURIBYPASS", SHIVNATH) == "kes"
    assert m.resolve_outlet("TAMANDO_3S", SHIVNATH) == "tam"  # the outlet's own name finds it


def test_anandapur_is_counted_under_keonjhar() -> None:
    assert m.resolve_location("ANANDAPUR", UTKAL) == "keo"
    assert m.resolve_location("KEONJHAR", UTKAL) == "keo"
    assert m.resolve_location("CSA_SEC_10_SH", UTKAL) == "cda"
    assert m.resolve_location("MANCHESWAR & CUBE", UTKAL) == "man"
    assert m.resolve_location("CUBE", UTKAL) == "cub"
    assert m.resolve_location("SOMEWHERE ELSE", UTKAL) is None  # no catch-all: shown as not mapped


def test_aditya_is_jajpur_and_everything_else_cuttack() -> None:
    assert m.resolve_location("JAJPUR", ADITYA) == "jaj"
    for label in ("CUTTACK", "KHORDHA_SZS", "BHUBANESHWAR_SH", "KENDRAPARA_SZS", "", "ANYTHING"):
        assert m.resolve_location(label, ADITYA) == "ctc", label


def test_dump_figures_by_location() -> None:
    rows = [
        {"dump_kind": "BOOKING", "outlet_label": "KEONJHAR", "outcome": "MATCHED"},
        {"dump_kind": "BOOKING", "outlet_label": "ANANDAPUR", "outcome": "MISSING"},
        {"dump_kind": "BOOKING", "outlet_label": "KEONJHAR", "outcome": "CANCELLED"},
        {"dump_kind": "DELIVERY", "outlet_label": "CSA_SEC_10_SH", "outcome": "NOT_IN_TOOL"},
        {"dump_kind": "DELIVERY", "outlet_label": "NOWHERE", "outcome": "MISSING"},
    ]
    located, unmapped = m.dms_figures(rows, UTKAL)
    assert located["keo"]["bookingDms"] == 2 and located["keo"]["bookingSynced"] == 1
    assert located["keo"]["bookingNotInVerigence"] == 1 and located["keo"]["bookingCancelled"] == 1
    assert located["cda"]["deliveryDump"] == 1 and located["cda"]["deliveryNotUploaded"] == 1
    assert unmapped["NOWHERE"]["deliveryNotInVerigence"] == 1


def _figures(**values: int) -> dict[str, int]:
    out = dict.fromkeys(m.DMS_KEYS + m.VERIGENCE_KEYS, 0)
    out.update(values)
    out["bookingGap"] = out["bookingDms"] - out["bookingInVerigence"]
    out["deliveryGap"] = out["deliveryDump"] - out["deliveryInVerigence"]
    return out


def test_the_excel_shows_member_outlets_and_the_shared_location() -> None:
    keo = _figures(bookingInVerigence=5)
    ana = _figures(bookingInVerigence=2)
    report = {
        "date": "2026-11-10", "month": "2026-11", "today": "2026-11-16",
        "figures": _figures(bookingDms=14, bookingInVerigence=7),
        "dealers": [{
            "dealerId": "u", "dealerName": "Utkal Mahindra", "bookingDumpDate": "2026-11-10", "deliveryDumpDate": None,
            "bookingsTo": "2026-11-09", "deliveriesTo": "2026-11-10",
            "locations": [{"dmsOutletId": "keo", "name": "KEONJHAR",
                           "outlets": [{"outletId": "keo", "outletName": "KEONJHAR", "figures": keo},
                                       {"outletId": "ana", "outletName": "ANANDAPUR", "figures": ana}],
                           "figures": _figures(bookingDms=14, bookingInVerigence=7)}],
            "unmapped": [], "figures": _figures(bookingDms=14, bookingInVerigence=7),
        }],
    }
    ws = load_workbook(io.BytesIO(m.build_outlet_report_excel(report))).active
    values = [[c.value for c in row] for row in ws.iter_rows(min_row=3)]
    assert values[0][:4] == ["Utkal Mahindra", "KEONJHAR", None, 5]  # no DMS on a member outlet
    assert values[1][:4] == ["Utkal Mahindra", "ANANDAPUR", None, 2]
    assert values[2][1].startswith("KEONJHAR - DMS location (KEONJHAR + ANANDAPUR)")
    assert values[2][2:4] == [14, 7]
    assert values[-1][0] == "Project - Total" and values[-1][2] == 14
