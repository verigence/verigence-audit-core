"""uc03_account_statement.py -- the dealer's account statement as the source of
truth for a Project's Deal.

MAHINDRA ORISSA: THE ACCOUNT STATEMENT IS THE SOURCE OF TRUTH (product owner,
2026-10-25). For a Project whose ``deal_source_policy`` is ``ACCOUNT_STATEMENT``
(see uc03_deal_source_policy) the Deal's billed figures -- ex-showroom, TCS,
insurance, registration, accessories, RSA, extended warranty, FASTag and the
discounts given -- come from the account statement the dealer fills in at
delivery, not from the invoice. The invoice logic everywhere else is unchanged
and still runs for every other Project; for this one the invoices are withheld
from the Deal (the documents and what was read from them stay on file as
evidence) and a journey with no account statement shows no billed figures.

The account statement is filed by Document Intelligence as ``customer_ledger``.
Three handwritten layouts exist and one computer layout:

  * "ACCOUNTS STATEMENT" (Aditya)       EX-SHOW ROOM PRICE OF VEHICLE, TCS ON INVOICE,
                                        INSURANCE COST, ... BOOKING AMOUNT, FINANCER AMOUNT
  * "VEHICLE ACCOUNT STATEMENT" (Utkal) Vehicle cost, TCS, Insurance amount, Registration, ...
                                        Scheme / Corporate / Other discount, Finance D/O, MR No.
  * "Account Statement" (Shivnath)      Ex Showroom, Registration, Insurance, RSA, Accessories,
                                        TCS, Fast Tag ... Cash / Online transfer / Bank DO
  * a Tally ledger or ERP printout      voucher postings with a running balance. NOT an account
                                        statement: it carries one net vehicle posting, no ex-showroom
                                        and no TCS. It is only used to VALIDATE the statement.

Reading is by label, deterministic, and never guesses: a label that names two
things ("SHIELD WARANTY TCS"), a charge that two lines give different amounts
for, or a label the table below does not know, is reported as unmapped and
leaves that line blank rather than putting a figure on the wrong line.

Tally validation: when a Tally ledger and a statement are both on file for one
Journey, the Tally's vehicle posting, finance D/O and insurance posting must
agree with the statement; a deviation raises a CRITICAL task.

THE COST SHEET (product owner, 2026-10-28). This Project calls the account
statement the NDC. "Depending upon which is available cost sheet or NDC you can
pick any one": a Journey with no NDC takes its billed figures from the cost
sheet instead (the Deal shows them as the cost sheet's). "If both are available
do value check and if any difference raise a consolidated task of high
priority" -- information only, the PC closes it with a comment
(COST_SHEET_NDC_DIFFERENCE). A difference is more than Rs 100 on a line both
documents print.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_deal_source_policy import (
    ACCOUNT_STATEMENT_TYPE,
    INVOICE_SOURCE_TYPES,
    account_statement_is_source_of_truth,
)

logger = structlog.get_logger(__name__)

KIND_FORM = "FORM"
KIND_TALLY = "TALLY"
KIND_ERP = "ERP_PRINTOUT"
KIND_UNKNOWN = "UNKNOWN"

ORIGIN = "ACCOUNT_STATEMENT"
TOLERANCE = Decimal(100)  # the product owner's price tolerance

NO_EX_SHOWROOM_TASK = "ACCOUNT_STATEMENT_NO_EX_SHOWROOM"
TALLY_DEVIATION_TASK = "ACCOUNT_STATEMENT_TALLY_DEVIATION"
COST_SHEET_DIFFERENCE_TASK = "COST_SHEET_NDC_DIFFERENCE"
COST_SHEET_TYPE = "cost_sheet"

_WS = re.compile(r"\s+")


_LESS = re.compile(r"^LESS:?\s*")
_IF_APPLICABLE = re.compile(r"\s*\(IF ?APPLICABLE\)")


def _label(value: Any) -> str:
    """Upper-case, one space between words, without the form's own decoration
    ("LESS:", "(IF APPLICABLE)", the "**" on the cash discount)."""
    text_ = _WS.sub(" ", re.sub(r"\*+", "", str(value or "").upper())).strip()
    return _WS.sub(" ", _IF_APPLICABLE.sub("", _LESS.sub("", text_))).strip()


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        # handwritten amounts are closed with "/-" or "/=" ("20,45,867/-")
        return Decimal(re.sub(r"\s*/\s*[-=]*\s*$", "", str(value).replace(",", "").strip()))
    except (InvalidOperation, ValueError):
        return None


def line_rows(value: Any) -> list[dict[str, Any]]:
    """The statement's line items as a list of dicts, whatever shape DI stored
    them in (JSON, or the Python repr older extractions left behind)."""
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return []
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []


# ── the mapping ──────────────────────────────────────────────────────────────
# Each pattern is matched against the WHOLE normalised label, so a label that
# carries a second item ("FASTAG CHARGESRSA", "HANDLING CHARGES TCS") matches
# nothing and is reported, never read as the first item.
_CHARGES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern)) for key, pattern in (
        ("ex_showroom_price", r"EX[- ]?SHOW ?ROOM( PRICE( OF VEHICLE)?)?|VEHICLE COST"),
        ("tcs_amount", r"TCS( ON INVOICE)?( \d+(\.\d+)?%)?"),
        ("insurance_amount", r"INSURANCE( AMOUNT| COST)?( \((NORMAL|NIL DEP)\))?"),
        ("registration_charges", r"REGISTRATION( COST| CHARGES)?( BH)?|ONLINE RTO|RTO CHARGES.*"),
        ("other_charges", r"PERMIT( CHARGES)?|IF ANY OTHERS?|MISC( \(IF ANY\))?"),
        ("accessories_cost", r"ACCESSORIES|SALE OF ACCESSORIES"),
        ("fastag_amount", r"FAST ?T?AG( CHARGES)?"),
        ("rsa_amount", r"RSA|ROAD SIDE ASSISTANCE"),
        ("additional_warranty_amount",
         r"(SHIELD|EXTENDED|EXT\.?) WARR?ANT(Y|IES)?"),
        ("service_package_amount", r"SERVICE PACK(AGE)?"),
        ("handling_charges", r"HANDLING CHARGES"),
    )
)
# Discounts, to the canonical discount keys the Deal already uses
# (uc03_masters_alignment: consumer offer -> CASH_DISCOUNT, the dealer's own
# cash discount -> ADDITIONAL_DISCOUNT). The statement's figures are the final
# amounts given; no GST is added to them.
_DISCOUNTS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern)) for key, pattern in (
        ("CASH_DISCOUNT", r"(SCHEME DISCOUNT|CONSUMER OFFER|CONSUMER SCHEME)( .*)?"),
        ("CORPORATE_PRIVILEGE", r"CORPORATE (DISCOUNT|OFFER)( .*)?"),
        ("EXCHANGE_BONUS", r"(EXCHANGE (LOYALTY|BONUS|CLAIM)|EXCHANGED VEHICLE)( .*)?"),
        ("ADDITIONAL_DISCOUNT", r"(OTHER DISCOUNT( \(IF ANY\))?|CASH DISCOUNT)( .*)?"),
    )
)
# The total the statement prints for what the customer owes (product owner,
# 2026-10-26: every dealer's statement has one): "Net Value" (Aditya, Shivnath),
# "NET RECEIVABLE (A+B)" (Utkal) -- and "Final price", which he also named.
_PRINTED_TOTAL = re.compile(r"(NET VALUE|FINAL PRICE|NET RECEIVABLE( \(A ?\+ ?B\))?)( .*)?")
# The form's own summary rows: figures the dealer wrote from the rows above
# them, never a charge or a receipt, so they are not "unmapped" and are not
# read as one. (The net value is read separately, above.)
_SUMMARY = re.compile(
    r"GROSS VALUE|DISCOUNT|TOTAL( COST| RECEIPTS?| \(A\))?|NET AMOUNT RECEIVED( \(E\))?|"
    r"SHORT ?/ ?EXCESS|BALANCE ?(EXCESS ?/ ?SHORTAGE)?( \(C-E\))?|DUE"
)
# A discount line that names a scrappage bonus does not say whether it is the
# dealer's or the certificate of deposit's, and the two are different
# benefits: it is reported, not filed under either.
_SCRAPPAGE = re.compile(r".*SCRAPPAGE.*")
# Lines that are what the customer paid, not what was billed.
_FINANCER = re.compile(r"(FINANCER AMOUNT|FINANCE D/?O|BANK DO)( .*)?")
_RECEIPT = re.compile(
    r"(BOOKING AMOUNT|DEPOSITED INTO BANK|CASH|CHEQUE|ONLINE TRANSFER|TRADE IN VALUE|CASH SHORTAGE|"
    r"MR NO\.?.*|PAYMENT A/C DETAILS|REFUND)( .*)?"
)
_FORM_MARKERS = tuple(re.compile(p) for p in (
    r"(EX[- ]?SHOW ?ROOM|VEHICLE COST)", r"FINANCER AMOUNT", r"DEPOSITED INTO BANK", r"BOOKING AMOUNT",
    r"FINANCE D/?O", r"MR NO", r"PAYMENT A/C", r"SCHEME DISCOUNT", r"OTHER DISCOUNT", r"CORPORATE DISCOUNT",
    r"BANK DO", r"TRADE IN VALUE", r"ONLINE TRANSFER",
))
_ERP_TOTALS = re.compile(r"(TOTAL|NET) VEHICLE COST")


def statement_kind(lines: list[dict[str, Any]]) -> str:
    """Which kind of ledger document these lines are. A running balance is a
    Tally printout; a TOTAL/NET VEHICLE COST line is the ERP printout (its
    "Ex-Showroom" is before GST, so it is not read as a statement); the form
    is recognised by its own labels."""
    if any(row.get("running_balance") for row in lines):
        return KIND_TALLY
    labels = [_label(row.get("particulars_raw")) for row in lines]
    if any(_ERP_TOTALS.fullmatch(label) for label in labels):
        return KIND_ERP
    if any(marker.match(label) for label in labels for marker in _FORM_MARKERS):
        return KIND_FORM
    return KIND_UNKNOWN


@dataclass
class StatementRead:
    kind: str
    components: dict[str, Decimal] = field(default_factory=dict)
    discounts: dict[str, Decimal] = field(default_factory=dict)
    financer: Decimal | None = None
    receipts: Decimal = Decimal(0)
    # the "Net value" / "Final price" the statement prints, when it has one
    printed_total: Decimal | None = None
    # lines left blank on purpose, with why
    unmapped: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def _side_amount(row: dict[str, Any]) -> tuple[str, Decimal] | None:
    debit, credit = _amount(row.get("debit_amount")), _amount(row.get("credit_amount"))
    if debit not in (None, Decimal(0)):
        return "DR", abs(debit)
    if credit not in (None, Decimal(0)):
        return "CR", abs(credit)
    return None


def read_statement(lines: list[dict[str, Any]]) -> StatementRead:
    """Read a handwritten account statement (``FORM``) line by line."""
    out = StatementRead(kind=statement_kind(lines))
    found: dict[tuple[str, str], list[Decimal]] = {}
    for row in lines:
        label = _label(row.get("particulars_raw"))
        side = _side_amount(row)
        if not label or side is None:
            continue  # a zero or empty line is not a figure
        _, amount = side
        if _SCRAPPAGE.fullmatch(label):
            out.unmapped.append({"label": label, "amount": amount,
                                 "why": "scrappage: the statement does not say dealer or certificate of deposit"})
            continue
        key = next((("COMMERCIAL", k) for k, pattern in _CHARGES if pattern.fullmatch(label)), None)
        if key is None:
            key = next((("DISCOUNT", k) for k, pattern in _DISCOUNTS if pattern.fullmatch(label)), None)
        if key is None and _PRINTED_TOTAL.fullmatch(label):
            key = ("TOTAL", "printed_total")
        if key is not None:
            found.setdefault(key, []).append(amount)
            continue
        if _SUMMARY.fullmatch(label):
            continue
        if _FINANCER.fullmatch(label):
            out.financer = (out.financer or Decimal(0)) + amount
            out.receipts += amount
            continue
        if _RECEIPT.fullmatch(label):
            out.receipts += amount
            continue
        out.unmapped.append({"label": label, "amount": amount, "why": "label not in the mapping"})
    for (kind, key), amounts in sorted(found.items()):
        distinct = sorted(set(amounts))
        if len(distinct) > 1:
            out.conflicts.append({"kind": kind, "key": key, "amounts": distinct})
            continue
        if kind == "TOTAL":
            out.printed_total = distinct[0]
            continue
        (out.components if kind == "COMMERCIAL" else out.discounts)[key] = distinct[0]
    return out


def statement_total(fields: dict[str, Any], read: StatementRead) -> Decimal | None:
    """What the customer owes on the statement, for the receipts check (product
    owner, 2026-10-26): the net value as printed, with no calculation -- the
    ``net_value`` field Document Intelligence reads (customer_ledger schema 1.1),
    else the "Net value" / "Net receivable" / "Final price" row of the line
    items. None when the statement prints none."""
    field_value = _amount(fields.get("net_value"))
    if field_value is not None and field_value > 0:
        return field_value
    return read.printed_total if read.printed_total is not None and read.printed_total > 0 else None


# ── Tally validation (pure) ──────────────────────────────────────────────────
_TALLY_VEHICLE = re.compile(r"(VEHICLE SALES?( \d+%)?.*|SALES (?!ACCESS).*|SALE OF (?!ACCESS).*)")
_TALLY_FINANCER = re.compile(r".*(\(FINANCER\)|\(FINANCE\)|FINANCE DO|BDO).*")
_TALLY_INSURANCE = re.compile(r".*(INSURANCE|INS POLICY).*")


def tally_postings(lines: list[dict[str, Any]]) -> dict[str, Decimal]:
    """The three postings a Tally ledger can be validated on: the vehicle sale
    (a single net amount), the financer's D/O and the insurance."""
    out: dict[str, Decimal] = {}
    for row in lines:
        label = _label(row.get("particulars_raw"))
        debit, credit = _amount(row.get("debit_amount")), _amount(row.get("credit_amount"))
        if debit and "vehicle" not in out and _TALLY_VEHICLE.fullmatch(label) and "ACCESS" not in label:
            out["vehicle"] = abs(debit)
        elif credit and "financer" not in out and _TALLY_FINANCER.fullmatch(label):
            out["financer"] = abs(credit)
        elif debit and "insurance" not in out and _TALLY_INSURANCE.fullmatch(label):
            out["insurance"] = abs(debit)
    return out


def tally_deviations(statement: StatementRead, tally: dict[str, Decimal]) -> list[dict[str, Any]]:
    """Where the Tally ledger disagrees with the statement by more than the
    tolerance. The Tally's vehicle posting is the invoice total, so it may equal
    the ex-showroom with or without TCS, with or without the discounts given;
    it agrees when it equals any of those."""
    out: list[dict[str, Any]] = []
    exs = statement.components.get("ex_showroom_price")
    if "vehicle" in tally and exs is not None:
        tcs = statement.components.get("tcs_amount", Decimal(0))
        given = sum(statement.discounts.values(), Decimal(0))
        accepted = {exs, exs + tcs, exs - given, exs + tcs - given}
        if all(abs(tally["vehicle"] - value) > TOLERANCE for value in accepted):
            out.append({"line": "vehicle", "tally": tally["vehicle"], "statement": exs,
                        "statementWithTcsAndDiscounts": exs + tcs - given})
    if "financer" in tally and statement.financer is not None and abs(tally["financer"] - statement.financer) > TOLERANCE:
        out.append({"line": "financer", "tally": tally["financer"], "statement": statement.financer})
    ins = statement.components.get("insurance_amount")
    if "insurance" in tally and ins is not None and abs(tally["insurance"] - ins) > TOLERANCE:
        out.append({"line": "insurance", "tally": tally["insurance"], "statement": ins})
    return out


# ── the cost sheet (pure) ────────────────────────────────────────────────────
# Cost sheet field (DI cost_sheet schema 1.0) -> the Deal line it prices.
_COST_SHEET_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("ex_showroom_price", "ex_showroom_price"),
    ("rto_amount", "registration_charges"),
    ("insurance_amount", "insurance_amount"),
    ("accessories_amount", "accessories_cost"),
)
_LINE_LABELS = {
    "ex_showroom_price": "Ex-showroom price",
    "registration_charges": "Registration / RTO",
    "insurance_amount": "Insurance",
    "accessories_cost": "Accessories",
    "discount_total": "Total discount",
    "net_total": "Net payable",
}


@dataclass
class CostSheetRead:
    components: dict[str, Decimal] = field(default_factory=dict)
    # one figure for every discount and the on-road total, as printed
    discount_total: Decimal | None = None
    total: Decimal | None = None


def read_cost_sheet(fields: dict[str, Any]) -> CostSheetRead:
    """The cost sheet's figures as printed; a blank or zero box is not a figure."""
    out = CostSheetRead()
    for field_key, component in _COST_SHEET_COMPONENTS:
        amount = _amount(fields.get(field_key))
        if amount is not None and amount > 0:
            out.components[component] = amount
    discount = _amount(fields.get("discount_amount"))
    out.discount_total = abs(discount) if discount not in (None, Decimal(0)) else None
    total = _amount(fields.get("total_on_road_price"))
    out.total = total if total is not None and total > 0 else None
    return out


def _statement_discount_total(fields: dict[str, Any], statement: StatementRead) -> Decimal | None:
    """The statement's discount as one figure: the Less: Discount / Discount
    Details total it prints, else its discount lines added up; None when it
    gives none."""
    for key in ("discount_value", "discount_details_total"):
        printed = _amount(fields.get(key))
        if printed is not None and printed > 0:
            return printed
    return sum(statement.discounts.values(), Decimal(0)) if statement.discounts else None


def cost_sheet_differences(
    statement: StatementRead, statement_fields: dict[str, Any], sheet: CostSheetRead,
) -> list[dict[str, Any]]:
    """Every line both the NDC and the cost sheet print that differs by more
    than Rs 100. A line only one of them prints is not a difference."""
    pairs: list[tuple[str, Decimal | None, Decimal | None]] = [
        (key, statement.components.get(key), sheet.components.get(key)) for _, key in _COST_SHEET_COMPONENTS
    ]
    pairs.append(("discount_total", _statement_discount_total(statement_fields, statement), sheet.discount_total))
    pairs.append(("net_total", statement_total(statement_fields, statement), sheet.total))
    out: list[dict[str, Any]] = []
    for key, ndc, cost_sheet in pairs:
        if ndc is None or cost_sheet is None or abs(ndc - cost_sheet) <= TOLERANCE:
            continue
        out.append({"line": key, "label": _LINE_LABELS[key], "ndc": ndc, "costSheet": cost_sheet,
                    "difference": ndc - cost_sheet})
    return out


def _difference_text(rows: list[dict[str, Any]]) -> str:
    def rupees(value: Decimal) -> str:
        return f"Rs {value:,.0f}"
    return "; ".join(f"{r['label']}: NDC {rupees(r['ndc'])}, cost sheet {rupees(r['costSheet'])}" for r in rows)


# ── the journey ──────────────────────────────────────────────────────────────
def load_ledger_documents(
    connection: Connection, *, tenant_id: str, journey_id: UUID, document_type: str = ACCOUNT_STATEMENT_TYPE,
) -> list[dict[str, Any]]:
    """Every ACTIVE document of this type (the customer ledger unless told
    otherwise) of the Journey, newest first, with its fields as the PC left them."""
    rows = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_document_id, e.linked_at_utc, f.field_key, f.effective_value
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='ACTIVE'
              AND e.document_type_key = :type
            ORDER BY e.linked_at_utc DESC, e.di_document_id, f.field_key, f.source_fact_version
            """
        ),
        {"t": tenant_id, "j": journey_id, "type": document_type},
    ).mappings().all()
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc = documents.setdefault(str(row["di_document_id"]), {
            "documentId": row["di_document_id"], "evidenceId": row["evidence_id"],
            "linkedAtUtc": row["linked_at_utc"], "fields": {},
        })
        if row["effective_value"] not in (None, "", "null"):
            doc["fields"][str(row["field_key"])] = row["effective_value"]
    return list(documents.values())


def _write_discount(
    connection: Connection, *, tenant_id: str, journey_id: UUID, key: str, amount: Decimal | None,
    document_id: Any, evidence_id: Any,
) -> None:
    """Set one discount's actual from the statement (None clears an actual the
    statement set earlier). The entitlement on the row is never touched."""
    existing = connection.execute(
        text(
            """
            SELECT discount_application_id, actual_discount_amount, details
            FROM auditcore.discount_applications
            WHERE tenant_id=:t AND journey_id=:j AND discount_key=:k
            ORDER BY created_at_utc DESC LIMIT 1 FOR UPDATE
            """
        ),
        {"t": tenant_id, "j": journey_id, "k": key},
    ).mappings().one_or_none()
    details = {"origin": ORIGIN, "sourceDocumentType": ACCOUNT_STATEMENT_TYPE,
               "sourceDocumentId": str(document_id) if document_id else None}
    if existing is not None:
        current = existing["details"] if isinstance(existing["details"], dict) else {}
        if amount is None and current.get("origin") != ORIGIN:
            return  # not ours to clear
        if existing["actual_discount_amount"] == amount and current.get("origin") == ORIGIN:
            return
        connection.execute(
            text(
                """
                UPDATE auditcore.discount_applications
                SET actual_discount_amount=:amount,
                    actual_source_kind=CASE WHEN CAST(:amount AS numeric) IS NULL THEN NULL ELSE 'EVIDENCE' END,
                    source_evidence_id=CAST(:evidence AS uuid),
                    details=CAST(:details AS jsonb), updated_at_utc=now()
                WHERE tenant_id=:t AND discount_application_id=:id
                """
            ),
            {"t": tenant_id, "id": existing["discount_application_id"], "amount": amount,
             "evidence": evidence_id if amount is not None else None,
             "details": json.dumps({**current, **details})},
        )
        return
    if amount is None:
        return
    connection.execute(
        text(
            """
            INSERT INTO auditcore.discount_applications (
                tenant_id, journey_id, discount_key, actual_discount_amount, actual_source_kind,
                source_evidence_id, details
            ) VALUES (:t, :j, :k, :amount, 'EVIDENCE', CAST(:evidence AS uuid), CAST(:details AS jsonb))
            """
        ),
        {"t": tenant_id, "j": journey_id, "k": key, "amount": amount, "evidence": evidence_id,
         "details": json.dumps(details)},
    )


def _withdraw_invoice_actuals(connection: Connection, *, tenant_id: str, journey_id: UUID) -> int:
    """Clear the Deal's canonical actuals an invoice set (idempotent). The
    invoice's per-document source rows cannot be deleted by the runtime role;
    the Deal view drops them for this Project, and migration 0162 removed the
    ones that existed when the policy was set."""
    types = sorted(INVOICE_SOURCE_TYPES)
    lines = connection.execute(
        text(
            """
            UPDATE auditcore.commercial_lines
            SET actual_amount=NULL, actual_source_kind=NULL, source_evidence_id=NULL,
                source_reference=NULL, updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j AND source_reference IS NOT NULL
              AND lower(split_part(source_reference, ':', 1)) = ANY(:types)
            """
        ),
        {"t": tenant_id, "j": journey_id, "types": types},
    ).rowcount
    discounts = connection.execute(
        text(
            """
            UPDATE auditcore.discount_applications
            SET actual_discount_amount=NULL, actual_source_kind=NULL, source_evidence_id=NULL,
                updated_at_utc=now()
            WHERE tenant_id=:t AND journey_id=:j
              AND details->>'origin' IN ('INVOICE_MATERIALIZATION', 'INVOICE_WITHDRAWN')
              AND actual_discount_amount IS NOT NULL
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).rowcount
    return int(lines or 0) + int(discounts or 0)


def _stale_statement_rows(
    connection: Connection, *, tenant_id: str, journey_id: UUID, keep_components: set[str],
    keep_discounts: set[str],
) -> None:
    """A figure the statement set earlier but no longer gives (the document was
    replaced, or re-read) stops standing: its canonical actual is cleared; the
    per-source row is overwritten with the next reading or filtered when its
    document is no longer active."""
    for key in connection.execute(
        text(
            """
            SELECT component_key FROM auditcore.commercial_lines
            WHERE tenant_id=:t AND journey_id=:j AND lower(split_part(source_reference, ':', 1)) = :type
            """
        ),
        {"t": tenant_id, "j": journey_id, "type": ACCOUNT_STATEMENT_TYPE},
    ).scalars().all():
        if key in keep_components:
            continue
        connection.execute(
            text(
                """
                UPDATE auditcore.commercial_lines
                SET actual_amount=NULL, actual_source_kind=NULL, source_evidence_id=NULL,
                    source_reference=NULL, updated_at_utc=now()
                WHERE tenant_id=:t AND journey_id=:j AND component_key=:k
                """
            ),
            {"t": tenant_id, "j": journey_id, "k": key},
        )
    for key in connection.execute(
        text(
            """
            SELECT discount_key FROM auditcore.discount_applications
            WHERE tenant_id=:t AND journey_id=:j AND details->>'origin' = :origin
            """
        ),
        {"t": tenant_id, "j": journey_id, "origin": ORIGIN},
    ).scalars().all():
        if key not in keep_discounts:
            _write_discount(connection, tenant_id=tenant_id, journey_id=journey_id, key=key, amount=None,
                            document_id=None, evidence_id=None)


def _task_key(kind: str, journey_id: UUID) -> str:
    return f"{kind.lower().replace('_', '-')}:{journey_id}"


def _raise_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, task_type: str, title: str, description: str,
    reference: dict[str, Any], severity: str = "CRITICAL",
) -> None:
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    raise_or_refresh(
        connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=_task_key(task_type, journey_id),
        task_type=task_type, source_type="RESOLVER", source_code=task_type, title=title,
        description=description, reference={"sourceCode": task_type, **reference}, severity=severity,
        registry=get_registry(),
    )


def _resolve_task(connection: Connection, *, tenant_id: str, journey_id: UUID, task_type: str,
                  evidence: dict[str, Any]) -> None:
    from audit_core.uc03_p2_task_producer import resolve_if_open

    resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id,
                    dedupe_key=_task_key(task_type, journey_id), evidence=evidence)


def _json_amounts(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_amounts(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_amounts(v) for v in value]
    return value


def sync_account_statement(
    connection: Connection, *, tenant_id: str, journey_id: UUID,
    stage_code: Literal["BOOKING", "DELIVERY"] = "DELIVERY", correlation_id: str = "",
) -> dict[str, int]:
    """Set the Journey's billed figures from its account statement, for a
    Project whose policy is ACCOUNT_STATEMENT. Idempotent. No statement on file:
    no billed figures, which is the product owner's rule ("if the given document
    is unavailable keep the invoice blank")."""
    from audit_core.uc03_deal_source_history import (
        record_source_value,
        upsert_commercial_actual,
    )

    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    parsed = [(d, line_rows(d["fields"].get("line_items"))) for d in documents]
    parsed = [(d, rows, statement_kind(rows)) for d, rows in parsed]
    forms = [(d, rows) for d, rows, kind in parsed if kind == KIND_FORM]
    tallies = [(d, rows) for d, rows, kind in parsed if kind == KIND_TALLY]

    cleared = _withdraw_invoice_actuals(connection, tenant_id=tenant_id, journey_id=journey_id)
    statement = read_statement(forms[0][1]) if forms else None
    document = forms[0][0] if forms else None
    keep_components = set(statement.components) if statement else set()
    keep_discounts = set(statement.discounts) if statement else set()
    lines_written = discounts_written = 0
    if statement and document:
        for key, amount in sorted(statement.components.items()):
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL", component_key=key,
                source_document_type=ACCOUNT_STATEMENT_TYPE, amount=amount, evidence_id=document["evidenceId"],
                document_id=document["documentId"],
            )
            if upsert_commercial_actual(
                connection, tenant_id=tenant_id, journey_id=journey_id, component_key=key, amount=amount,
                document_type=ACCOUNT_STATEMENT_TYPE, document_id=document["documentId"],
                evidence_id=document["evidenceId"], record=False,
            ):
                lines_written += 1
        for key, amount in sorted(statement.discounts.items()):
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="DISCOUNT", component_key=key,
                source_document_type=ACCOUNT_STATEMENT_TYPE, amount=amount, evidence_id=document["evidenceId"],
                document_id=document["documentId"],
            )
            _write_discount(connection, tenant_id=tenant_id, journey_id=journey_id, key=key, amount=amount,
                            document_id=document["documentId"], evidence_id=document["evidenceId"])
            discounts_written += 1
    _stale_statement_rows(connection, tenant_id=tenant_id, journey_id=journey_id,
                          keep_components=keep_components, keep_discounts=keep_discounts)

    # The cost sheet: its lines are recorded for the Deal (shown as the cost
    # sheet's, standing in the billed column only when no NDC is on file -- see
    # uc03_p2_journey360), and compared with the NDC when both are in.
    sheets = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                   document_type=COST_SHEET_TYPE)
    sheet = read_cost_sheet(sheets[0]["fields"]) if sheets else None
    if sheet is not None:
        for key, amount in sorted(sheet.components.items()):
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL", component_key=key,
                source_document_type=COST_SHEET_TYPE, amount=amount, evidence_id=sheets[0]["evidenceId"],
                document_id=sheets[0]["documentId"],
            )
    differences = (cost_sheet_differences(statement, document["fields"], sheet)
                   if statement is not None and document is not None and sheet is not None else [])
    if differences:
        _raise_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, task_type=COST_SHEET_DIFFERENCE_TASK,
            title="Cost sheet and NDC differ",
            description=(
                "The cost sheet and the NDC (account statement) on this booking differ by more than Rs 100 on: "
                + _difference_text(differences) + ". For information: the NDC is the Deal's figure. Check which is "
                "right and close this task with a comment."
            ),
            reference=_json_amounts({"differences": differences,
                                     "ndcDocumentId": str(document["documentId"]) if document else None,
                                     "costSheetDocumentId": str(sheets[0]["documentId"]) if sheets else None}),
            severity="HIGH",
        )
    else:
        _resolve_task(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=COST_SHEET_DIFFERENCE_TASK,
                      evidence={"ndcOnFile": statement is not None, "costSheetOnFile": sheet is not None})

    # A statement is on file but its vehicle line was not read: CRITICAL, and it
    # says which lines were read (the Deal has no ex-showroom for this Journey).
    if statement is not None and "ex_showroom_price" not in statement.components:
        _raise_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, task_type=NO_EX_SHOWROOM_TASK,
            title="Account statement on file but its vehicle line was not read",
            description=(
                "This Project takes the Deal from the dealer's account statement. The statement on file has no "
                "ex-showroom / vehicle cost line that could be read, so the Deal has no billed vehicle price. "
                "Correct the reading of the statement."
            ),
            reference=_json_amounts({"linesRead": sorted(statement.components), "unmapped": statement.unmapped,
                                     "conflicts": statement.conflicts}),
        )
    else:
        _resolve_task(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=NO_EX_SHOWROOM_TASK,
                      evidence={"statementRead": statement is not None})

    # The Tally ledger validates the statement.
    deviations: list[dict[str, Any]] = []
    if statement is not None and tallies:
        deviations = tally_deviations(statement, tally_postings(tallies[0][1]))
    if deviations:
        _raise_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, task_type=TALLY_DEVIATION_TASK,
            title="Tally ledger disagrees with the account statement",
            description=(
                "The Tally ledger and the account statement on this Journey differ by more than Rs 100 on: "
                + ", ".join(d["line"] for d in deviations) + ". The account statement is the Deal's source of "
                "truth; find out which document is wrong."
            ),
            reference=_json_amounts({"deviations": deviations}),
        )
    else:
        _resolve_task(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=TALLY_DEVIATION_TASK,
                      evidence={"tallyOnFile": bool(tallies)})

    logger.info(
        "account_statement_synced", tenant_id=tenant_id, journey_id=str(journey_id), correlation_id=correlation_id,
        statements=len(forms), tallies=len(tallies), components=len(keep_components), discounts=len(keep_discounts),
        unmapped=len(statement.unmapped) if statement else 0, conflicts=len(statement.conflicts) if statement else 0,
        invoice_actuals_cleared=cleared, tally_deviations=len(deviations),
        cost_sheets=len(sheets), cost_sheet_differences=len(differences),
    )
    # commercialLines / discountApplications are the keys the invoice path
    # returns, which its callers read.
    return {"commercialLines": lines_written, "discountApplications": discounts_written,
            "statements": len(forms), "tallies": len(tallies), "invoiceActualsCleared": cleared,
            "unmapped": len(statement.unmapped) if statement else 0,
            "conflicts": len(statement.conflicts) if statement else 0, "tallyDeviations": len(deviations),
            "costSheetDifferences": len(differences)}


def sync_if_source_of_truth(
    connection: Connection, *, tenant_id: str, journey_id: UUID,
    stage_code: Literal["BOOKING", "DELIVERY"] = "DELIVERY", correlation_id: str = "",
) -> dict[str, int] | None:
    """The hook the invoice materialiser calls: None for a Project that still
    takes its Deal from invoices, else the statement sync's counts."""
    if not account_statement_is_source_of_truth(connection, tenant_id=tenant_id):
        return None
    return sync_account_statement(connection, tenant_id=tenant_id, journey_id=journey_id,
                                  stage_code=stage_code, correlation_id=correlation_id)


__all__ = [
    "COST_SHEET_DIFFERENCE_TASK",
    "COST_SHEET_TYPE",
    "KIND_ERP",
    "KIND_FORM",
    "KIND_TALLY",
    "KIND_UNKNOWN",
    "NO_EX_SHOWROOM_TASK",
    "ORIGIN",
    "TALLY_DEVIATION_TASK",
    "CostSheetRead",
    "StatementRead",
    "cost_sheet_differences",
    "line_rows",
    "load_ledger_documents",
    "read_cost_sheet",
    "read_statement",
    "statement_kind",
    "sync_account_statement",
    "sync_if_source_of_truth",
    "tally_deviations",
    "tally_postings",
]
