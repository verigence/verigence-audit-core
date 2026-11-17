"""Registration on a scrappage deal (product owner 2026-11-09).

Odisha's Motor Vehicles (Registration & Functions of Vehicle Scrapping
Facility) Policy 2022, following the Central Motor Vehicles (24th Amendment)
Rules 2021 (rule 51A): a new non-transport vehicle registered against a
Certificate of Deposit pays 25% less motor vehicle tax (15% for a transport
vehicle), and its registration fee is waived. On a scrappage deal the
"Road tax + registration" charged is therefore below the price master's
standard, and DEAL_UNDERCHARGED used to raise it as a shortfall.

Product owner: "check if the discount is 25% exactly (within +-100) then pls
do that and let PC know it". So, for each registration line of the deal:

- the shortfall is 25% of the line's standard, within Rs 100, and the deal
  has scrappage on record (a Certificate of Deposit on file, or a scrappage
  bonus given): the shortfall is the concession. The line's standard is taken
  as 75% of the master's, it is not a shortfall, and the journey's System
  remarks say so (2026-11-15; until then the PC got an information task);
- the shortfall is 25% within Rs 100 but no scrappage is on record: it stays
  a shortfall, with the note that it matches the concession and no
  Certificate of Deposit or scrappage benefit is on file;
- the deal has scrappage but the shortfall is not 25%: it stays a
  shortfall, with what the concession would have been.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

CONCESSION_RATE = Decimal("0.25")
TOLERANCE = Decimal(100)
TASK_TYPE = "SCRAPPAGE_REGISTRATION_CONCESSION"
POLICY = ("Odisha Motor Vehicles (Registration & Functions of Vehicle Scrapping Facility) Policy 2022: "
          "25% concession in motor vehicle tax for a non-transport vehicle registered against a Certificate "
          "of Deposit, registration fee waived")
# The deal lines that carry the road tax.
REGISTRATION_KEYS = frozenset({"road_tax_registration", "road_tax_amount", "registration_charges"})
COD_DOCUMENT_TYPES = ("scrappage_certificate_of_deposit",)


# ── the Deal's own scrappage facts (2026-11-17) ──────────────────────────────
# Product owner 2026-11-17: "there are lot of wrong calculations for RTO where
# scrappage discount is given but it is not handled in the Deal and variance
# shows the same"; "PRUTHIRAJ MUDULI has scrappage documents, but it never
# shows in Deal". Until then the concession lived only in DEAL_UNDERCHARGED:
# the Deal kept the full master RTO as the standard, so every scrappage deal
# read tens of thousands below the standard (live: 192 journeys whose RTO
# challan prints the scrappage rebate; ADI-RD-261102-003 read Rs 59,576 short
# on RTO alone), and the 25% test, run on a standard that carries the
# hypothecation fee and the registration fee, missed by more than Rs 100 on
# most of them. The Deal now owns the concession, once, and every reader
# (variance, list, DEAL_UNDERCHARGED, System remarks) takes it from the Deal.
#
# The concession is what the RTO challan itself prints as the rebate on the
# motor vehicle tax ("MV Tax ... Rebate/Waiver"): the government's own figure,
# so no percentage is guessed. With no rebate read from a challan, a
# registration charged exactly 25% (+-Rs 100) below its standard on a deal
# with scrappage on record is taken as the concession, as before.


def _rows(value: Any) -> list[dict[str, Any]]:
    from audit_core.uc03_account_statement import line_rows

    return line_rows(value)


def _num(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "", "null") else Decimal(str(value).replace(",", "").strip('"'))
    except (ArithmeticError, ValueError):
        return None


def challan_rebate(challans: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The scrappage rebate an RTO challan prints on its motor vehicle tax:
    ``{"rebate", "tax", "documentId"}`` from the challan that prints the
    largest one (copies of one challan are not added up), else None."""
    best: dict[str, Any] | None = None
    for challan in challans:
        rebate = tax = Decimal(0)
        for line in _rows((challan.get("fields") or {}).get("line_items")):
            waived = _num(line.get("rebate_waiver_amount"))
            if waived is None or waived <= 0:
                continue
            rebate += waived
            tax += _num(line.get("amount")) or Decimal(0)
        if rebate > 0 and (best is None or rebate > best["rebate"]):
            best = {"rebate": rebate, "tax": tax or None, "documentId": challan.get("documentId")}
    return best


def certificates(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Certificates of Deposit on file, as the Deal shows them."""
    out = []
    for document in documents:
        fields = document.get("fields") or {}

        def pick(key: str, fields: dict[str, Any] = fields) -> str | None:
            value = fields.get(key)
            text_value = str(value).strip('"') if value not in (None, "", "null") else ""
            return text_value or None

        out.append({
            "documentId": document.get("documentId"),
            "certificateNumber": pick("certificate_number"),
            "variant": pick("certificate_variant"),
            "issuedOn": pick("certificate_issue_date"),
            "validUntil": pick("certificate_valid_until_date"),
            "holder": pick("current_holder_name"),
            "oldVehicleRegistration": pick("old_vehicle_registration_number"),
            "oldVehicle": " ".join(v for v in (pick("old_vehicle_make"), pick("old_vehicle_model")) if v) or None,
            "oldVehicleType": pick("old_vehicle_type"),
        })
    return out


def journey_scrappage(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The scrappage facts on file for a Journey: its Certificates of
    Deposit and the rebate its RTO challan prints."""
    from audit_core.uc03_p2_journey360 import _document_facts

    cods = _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id, di_types=COD_DOCUMENT_TYPES)
    challans = _document_facts(connection, tenant_id=tenant_id, journey_id=journey_id, di_types=("rto_challan",))
    return {"certificates": certificates(cods), "challan": challan_rebate(challans)}


def apply_concession(rows: list[dict[str, Any]], facts: dict[str, Any], *,
                     benefits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Take the scrappage concession off the standard of the one RTO line
    that carries it, so the Deal's variance is measured against what the
    customer could be charged. Returns the concession applied, else None.

    ``benefits`` are the deal's scrappage discount lines given (bonus of the
    dealer or of the certificate of deposit)."""
    line = next((r for key in ("road_tax_registration", "road_tax_amount", "registration_charges") for r in rows
                 if r["key"] == key and not r.get("partOf") and r.get("standard") not in (None, "")), None)
    if line is None:
        return None
    standard = _num(line["standard"])
    if standard is None or standard <= 0:
        return None
    cod_on_file = bool(facts.get("certificates"))
    evidence = scrappage_evidence(cod_documents=len(facts.get("certificates") or []), scrappage_discounts=benefits)
    challan = facts.get("challan")
    concession: Decimal | None = None
    source = None
    if challan and challan["rebate"] > 0:
        concession, source = challan["rebate"], "the scrappage rebate printed on the RTO challan"
    elif evidence:
        charged = _num(line.get("billed"))
        if charged is not None:
            assessed = assess_line(standard=standard, charged=charged, scrappage=evidence)
            if assessed["explained"]:
                concession, source = assessed["concession"], "25% of the RTO standard"
    if concession is None:
        return None
    concession = min(concession, standard)
    applied = {
        "key": line["key"], "label": line.get("label"), "masterStandard": str(standard),
        "concession": str(concession), "standard": str(standard - concession), "source": source,
        "codOnFile": cod_on_file, "evidence": evidence,
        "challanTax": str(challan["tax"]) if challan and challan.get("tax") is not None else None,
    }
    line["standardMaster"] = str(standard)
    line["standard"] = str((standard - concession).quantize(Decimal("0.01")))
    basis = line.get("standardBasis") or "the price master's RTO"
    line["standardBasis"] = f"{basis}, less the scrappage concession of Rs {concession} ({source})"
    line["scrappageConcession"] = applied
    line["flags"] = [*(line.get("flags") or []), "SCRAPPAGE_CONCESSION",
                     *([] if cod_on_file else ["SCRAPPAGE_COD_MISSING"])]
    return applied


def deal_block(facts: dict[str, Any], *, applied: dict[str, Any] | None,
               benefits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What the Deal shows for scrappage: the certificates on file, the
    challan's rebate, the concession taken and the scrappage benefits given,
    and whether the Certificate of Deposit is missing. None on a deal with no
    scrappage at all."""
    challan = facts.get("challan")
    cods = facts.get("certificates") or []
    given = [{"key": b.get("key"), "label": b.get("label"), "amount": b.get("current")} for b in benefits]
    if not (cods or challan or given or applied):
        return None
    return {
        "certificates": cods,
        "challanRebate": str(challan["rebate"]) if challan else None,
        "challanTax": str(challan["tax"]) if challan and challan.get("tax") is not None else None,
        "concession": applied,
        "benefits": given,
        # The RTO took the concession, or a scrappage benefit was given, and
        # no Certificate of Deposit is on file: the PC is asked for it.
        "codMissing": not cods and bool(challan or given),
    }


def scrappage_evidence(*, cod_documents: int, scrappage_discounts: list[dict[str, Any]]) -> str | None:
    """Why the deal counts as a scrappage deal, in words, or None."""
    reasons = []
    if cod_documents:
        reasons.append("a Certificate of Deposit is on file")
    given = [d for d in scrappage_discounts if d.get("label")]
    if given:
        reasons.append(" and ".join(str(d["label"]) for d in given) + " given")
    return "; ".join(reasons) or None


def assess_line(*, standard: Decimal, charged: Decimal, scrappage: str | None) -> dict[str, Any]:
    """The registration line against the concession: ``explained`` when the
    shortfall is the concession on a scrappage deal; a ``note`` otherwise."""
    short = standard - charged
    concession = (standard * CONCESSION_RATE).quantize(Decimal("0.01"))
    off_by = short - concession
    matches = abs(off_by) <= TOLERANCE
    result: dict[str, Any] = {"concession": concession, "offBy": off_by, "matches": matches, "explained": False}
    if matches and scrappage:
        result["explained"] = True
        result["note"] = (f"matches the 25% scrappage concession on the standard ({_rupees(concession)}; "
                          f"{scrappage})")
    elif matches:
        result["note"] = ("matches the 25% scrappage concession, but no Certificate of Deposit or scrappage "
                          "benefit is on file")
    elif scrappage:
        result["note"] = (f"scrappage deal ({scrappage}): the 25% concession would be {_rupees(concession)}; "
                          f"the shortfall is {_rupees(abs(off_by))} {'more' if off_by > 0 else 'less'} than that")
    return result


def _rupees(value: Decimal) -> str:
    from audit_core.uc03_p2_audit_rules import _rupees as fmt

    return fmt(value)


def _task_key(journey_id: UUID | str) -> str:
    return f"scrappage-registration:{journey_id}"


def refresh_pc_task(connection: Connection, *, tenant_id: str, journey_id: UUID,
                    explained: list[dict[str, Any]]) -> None:
    """The concession is no longer a PC task (product owner 2026-11-15: "it
    should come up in the journey observation or system remarks"): Journey
    360's System remarks show it (uc03_system_remarks). An information task
    still open from before is closed, saying where the note went."""
    from audit_core.uc03_p2_task_producer import resolve_if_open

    resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_task_key(journey_id),
                    evidence={"scrappageConcession": [{k: str(v) for k, v in e.items()} for e in explained],
                              "movedTo": "System remarks (product owner 2026-11-15)"})


def concession_remarks(explained: list[dict[str, Any]]) -> list[str]:
    """The concession in words, one line per registration line."""
    return [
        f"{e['label']}: {_rupees(Decimal(str(e['charged'])))} charged; the price master's "
        f"{_rupees(Decimal(str(e['standard'])))} less the scrappage concession of "
        f"{_rupees(Decimal(str(e['concession'])))} ({e.get('source') or 'scrappage'}) is the standard; "
        f"not counted as an undercharge."
        f"{'' if e.get('codOnFile') in (None, True, 'True') else ' No Certificate of Deposit is on file.'} {POLICY}."
        for e in explained
    ]
