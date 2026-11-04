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
leaves that line blank rather than putting a figure on the wrong line. The
working a dealer writes beside a label ("ACCESSORIES 6691 - 5000 FREE") is
read only when it agrees with the amount printed on that line.

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
_VLTD = r"V\.?L\.?T\.?D\.?"
# Other charges: the Deal has one line for them, but each printed row is a
# charge of its own, so two rows add up (a permit and a tracking device on one
# statement, 2026-11-02); the same row given two amounts is a conflict. The
# vehicle location tracking device ("VLTD", "VLTD COD") and the workshop billing
# ("WORKSHOP BILLING AMOUNT COD") have no line of their own
# (product owner 2026-11-02).
_OTHER_CHARGES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (item, re.compile(pattern)) for item, pattern in (
        # the Offer Letter's permit row carries handwritten words ("PERMIT Add Basic")
        ("PERMIT", r"PERMIT( CHARGES)?( .*)?"),
        ("IF_ANY_OTHERS", r"IF ANY OTHERS?"),
        ("MISC", r"MISC( \(IF ANY\))?"),
        ("VLTD", rf"{_VLTD}( COD)?"),
        ("WORKSHOP_BILLING", r"WORKSHOP BILLING( AMOUNT)?( COD)?"),
        # The Offer Letter / Payment Details form (2026-11-03): rows of their
        # own beside RTO, a fancy number and a coating.
        ("TEMP_REGISTRATION", r"TEMPORARY REGISTRATION"),
        ("RTO_CHOICE", r"RTO CHOICE( NO\.?)?"),
        ("3M", r"3M"),
        ("OTHERS", r"OTHERS( .*)?"),
    )
)
_CHARGES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern)) for key, pattern in (
        # "EX SHOWROOM (1-2)": the Offer Letter / Payment Details form prints
        # the vehicle price less its scheme on this row (2026-11-03).
        ("ex_showroom_price", r"EX[- ]?SHOW ?ROOM( PRICE( OF VEHICLE)?| ?\( ?1 ?- ?2 ?\))?|VEHICLE COST"),
        # The Utkal form prints its TCS row as "W/s billing TCS (1%)"; read also
        # as "W/S BILLING", "W/S BILLING TCS(1%)" and "W/S BILLING TU (1%)", and
        # every amount on it is 1% of the sale (five live statements, 2026-11-02).
        ("tcs_amount", r"TCS( ON INVOICE)?( \d+(\.\d+)?%)?|W/S BILLING( TCS| TU)? ?(\( ?1 ?%\)|1 ?%)?"),
        ("insurance_amount", r"INSURANCE( AMOUNT| COST)?( \((NORMAL|NIL DEP)\))?"),
        # "RTO BH" / "RTO (BH Series)": the Offer Letter's RTO row for a BH-series number (2026-11-03).
        ("registration_charges",
         r"REGISTRATION( COST| CHARGES)?( BH| \(BH\)| RTO)?|ONLINE RTO|RTO CHARGES.*|RTO( \(?BH( SERIES)?\)?)?"),
        # The Offer Letter's "Others" row with "FT" / "Fastag" written on it is
        # the FASTag (2026-11-03), so it is matched before the other charges.
        ("fastag_amount", r"(OTHERS )?(FAST ?T?AG( CHARGES)?|FT)"),
        ("other_charges", "|".join(f"(?:{p.pattern})" for _, p in _OTHER_CHARGES)),
        ("accessories_cost", r"ACCESSORIES|SALE OF ACCESSORIES"),
        # Not "RSA COD" / "RSA COP": their amounts are the certificate of
        # deposit bought for a scrappage (Rs 20,700 / 24,143 against an RSA of
        # Rs 2,021 on live statements, 2026-11-02), written in the RSA row.
        ("rsa_amount", r"(RSA|ROAD SIDE ASSISTANCE)( 1 ?(YEAR|YR))?"),
        ("additional_warranty_amount",
         r"(SHIELD|EXTENDED|EXT\.?) WARR?ANT(Y|IES)?( \(\d(ST|ND|RD|TH)( ?[-+&] ?\d(ST|ND|RD|TH))? ?(YR|YEAR)S?\))?"),
        ("service_package_amount", r"SERVICE PACK(AGE)?"),
        ("handling_charges", r"HANDLING CHARGES?"),
    )
)
# The canonical keys of the benefits the statement gives as a line of their
# own (uc03_masters_alignment): free accessories, and the scrappage bonus of
# the dealer or of the certificate of deposit.
FREE_ACCESSORIES_KEY = "ACCESSORIES_KIT"
_ACCESSORIES_FREE = re.compile(r"ACCESSORIES ?\(?FREE\)?")
_ADDED_KIT = re.compile(r"\bKIT\b|ACCESSOR")
SCRAPPAGE_DEALER_KEY = "SCRAPPAGE_BONUS_DEALER"
SCRAPPAGE_COD_KEY = "SCRAPPAGE_BONUS_COD"
# Discounts, to the canonical discount keys the Deal already uses
# (uc03_masters_alignment: consumer offer -> CASH_DISCOUNT, the dealer's own
# cash discount -> ADDITIONAL_DISCOUNT). The statement's figures are the final
# amounts given; no GST is added to them.
_DISCOUNTS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern)) for key, pattern in (
        ("CASH_DISCOUNT", r"(SCHEME DISCOUNT|CONSUMER OFFER|CONSUMER SCHEME)( .*)?"),
        ("CORPORATE_PRIVILEGE", r"CORPORATE (DISCOUNT|OFFER)( .*)?"),
        # "EXCHANGE CLAIM" is the exchange bonus (product owner 2026-10-29, the
        # Aditya form; written "EXCHANNGE CLAIM" on it, and read "EXCHANNG
        # CLAIM"); "EXCHANGE LOYALITY" is the loyalty bonus misspelt.
        ("EXCHANGE_BONUS", r"EXCHAN+GE? ?[ /] ?(LOYALI?TY|BONUS|CLAIM)( .*)?"),
        ("ADDITIONAL_DISCOUNT", r"(OTHER DISCOUNT( \(IF ANY\))?|CASH DISCOUNT|ADDITIONAL DISCOUNT)( .*)?"),
    )
)
# The total the statement prints for what the customer owes (product owner,
# 2026-10-26: every dealer's statement has one): "Net Value" (Aditya, Shivnath),
# "NET RECEIVABLE (A+B)" (Utkal) -- and "Final price", which he also named.
_PRINTED_TOTAL = re.compile(r"(NET VALUE|FINAL PRICE|NET RECEIVABLE( \(A ?\+ ?B\))?|ON ?ROAD PRICE)( .*)?")
# The form's own summary rows: figures the dealer wrote from the rows above
# them, never a charge or a receipt, so they are not "unmapped" and are not
# read as one. (The net value is read separately, above.)
# The Offer Letter's "VEHICLE PRICE" and "SCHEME" rows ("Scheme of Month" on
# another branch's form): the Ex Showroom (1-2) row is their difference. The
# vehicle price is the ex-showroom price and the scheme a benefit on it
# (_price_and_scheme), whether or not the dealer wrote the Ex Showroom row
# (SHI-SS-261027-024, 2026-11-03: only the vehicle price, Rs 19,28,561).
_VEHICLE_PRICE = re.compile(r"VEHICLE PRICE")
_SCHEME_ROW = re.compile(r"SCHEME( OF (THE )?MONTH)?")
_SUMMARY = re.compile(
    r"GROSS VALUE|DISCOUNT|TOTAL( COST| RECEIPTS?| \(A\))?|NET AMOUNT RECEIVED( \(E\))?|"
    r"SHORT ?/ ?EXCESS|BALANCE ?(EXCESS ?/ ?SHORTAGE)?( \(C-E\))?|DUE|"
    # The Offer Letter / Payment Details form's working rows (2026-11-03): the
    # vehicle price and its scheme (the Ex Showroom (1-2) row is their
    # difference), its sub-totals, the finance plan, the final payment.
    r"VEHICLE PRICE|SCHEME|BALANCE PAYMENT|SUB ?TOTAL.*|DOWN ?PAYMENT.*|1ST EMI|FINANCE FEES|"
    r"MISC PAYMENT|FINAL PAYMENT"
)
# The old vehicle's value (product owner 2026-10-29): Aditya's form deducts
# "LESS: EXCHANGED VEHICLE" in section A, before the net receivable. It is the
# customer paying with the old vehicle -- not a discount and not the exchange
# bonus (that is "EXCHANGE CLAIM") -- and, being already netted off the net
# receivable, not one of the statement's received lines either.
_OLD_VEHICLE = re.compile(r"EXCHANGED? VEHICLE( VALUE| AMT| AMOUNT)?")
_TRADE_IN = re.compile(r"TRADE IN VALUE( .*)?")
# Scrappage is a Deal line of its own (product owner 2026-11-02): the row that
# names the scrappage bonus is the benefit -- the certificate of deposit's
# when it says COD, else the dealer's. The handwriting reading spells the word
# "SCORPIGE". A row that names the scrappage but no bonus is not read as one:
# the certificate of deposit bought for the scrappage is written that way
# among the charges ("Scorpige" 29,143 beside "Scorpige Bonus" 20,000, whose
# printed net value only adds up with the 29,143 charged).
_SCRAPPAGE = re.compile(r".*\bSC(RA|OR|AR|RO)P+[AIE]?GE\b.*")
_SCRAPPAGE_BONUS = re.compile(r".*\b(BONUS|BONOUS|BENEFIT|DISCOUNT|INCENTIVE|CLAIM)\b.*")
_COD = re.compile(r".*(\bCOD\b|\bC\.O\.D\b|CERTIFICATE OF DEPOSIT).*")
# Lines that are what the customer paid, not what was billed. The finance D/O
# is also read run into what follows it ("FINANCE D/OMMFSL", "FINANCE
# D/O-1963"), as "FINANCE D/P", as the bank's money receipt ("FINANCE HDFC MR
# NO. PLSM/1066") or as "FINANCE" alone (product owner 2026-11-02).
_FINANCER = re.compile(
    r"FINANCER? AMOUNT( .*)?|BANK DO( .*)?|"
    r"FINANCE( D/?O( .*)?| D/O\S.*| D/P( .*)?| [A-Z][A-Z&.]*( [A-Z][A-Z&.]*)* MR NO\.?.*)?"
)
_RECEIPT = re.compile(
    r"(BOOKING AMOUNT|DEPOSITED INTO BANK|CASH|CHEQUE|ONLINE TRANSFER|TRADE IN VALUE|CASH SHORTAGE|"
    r"MR NO\.?.*|PAYMENT A/C DETAILS|REFUND|RECEIPT)( .*)?"
)
_FORM_MARKERS = tuple(re.compile(p) for p in (
    r"(EX[- ]?SHOW ?ROOM|VEHICLE COST)", r"FINANCER AMOUNT", r"DEPOSITED INTO BANK", r"BOOKING AMOUNT",
    r"FINANCE D/?O", r"MR NO", r"PAYMENT A/C", r"SCHEME DISCOUNT", r"OTHER DISCOUNT", r"CORPORATE DISCOUNT",
    r"BANK DO", r"TRADE IN VALUE", r"ONLINE TRANSFER", r"OFFER LETTER", r"ON ?ROAD PRICE", r"FINANCE AMOUNT",
))
_ERP_TOTALS = re.compile(r"(TOTAL|NET) VEHICLE COST")

# Handwritten working beside a charge (product owner 2026-11-02), read only
# when it agrees with the amount printed on the line:
#   * terms that add up to it: "ACCESSORIES (19215 + 3339)", "V.L.T.D + FITNESS
#     = 14,000+1,000", or the amount written again: "RSA 2021";
#   * free accessories taken off the accessories' value: "ACCESSORIES 6691 -
#     5000 FREE", "ACCESSORIES 20785 - SCHEME 20000/-", "ACCESSORIES 5255 - 5000
#     = 255". The amount printed is what is left to pay (the statement's net
#     value adds up with it); the accessories are billed at their value and the
#     free part is the free-accessories benefit. The free part is the term
#     marked FREE / SCHEME, else the one taken off.
# Working that does not agree ("ACCESSORIES 5000(FREE)" against 5,153,
# "ACCESSORIES SCHEME 5000 TRUE" against 112) leaves the line unmapped.
_WORKING_ITEMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("accessories_cost", re.compile(r"ACCESSORIES")),
    ("rsa_amount", re.compile(r"(MISC( \(IF ANY\))? )?RSA")),
    ("other_charges", re.compile(rf"{_VLTD}( ?\+ ?FITNESS)?")),
)
_WORKING = re.compile(r"(?P<item>.+?)(?: ?=)? (?P<working>(?:\(|FREE |SCHEME )?\d.*)")
_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_PLAIN_TERM = re.compile(rf"(?P<n>{_NUMBER})")
_FREE_TERM = re.compile(rf"(?:(?P<before>FREE|SCHEME) )?(?P<n>{_NUMBER})(?: ?(?P<after>FREE|\(FREE\)))?")


def _figure(text_: str) -> Decimal:
    return Decimal(text_.replace(",", ""))


def read_working(working: str, amount: Decimal) -> tuple[Decimal, Decimal] | None:
    """What a line's handwritten working gives -- (the value billed, the free
    part) -- when it agrees with the amount printed on the line; None when it
    does not, or is not working this reads."""
    body = re.sub(r"/[-=]+", "", working).strip()
    left, equals, right = body.partition("=")
    if equals and (_PLAIN_TERM.fullmatch(right.strip()) is None or _figure(right.strip()) != amount):
        return None
    left = left.strip()
    if left.startswith("(") and left.endswith(")"):
        left = left[1:-1].strip()
    if "+" in left and "-" not in left:
        terms = [_PLAIN_TERM.fullmatch(t.strip()) for t in left.split("+")]
        if any(t is None for t in terms) or sum((_figure(t["n"]) for t in terms if t), Decimal(0)) != amount:
            return None
        return amount, Decimal(0)
    if "-" in left and "+" not in left:
        parts = [_FREE_TERM.fullmatch(t.strip()) for t in left.split("-")]
        if len(parts) != 2 or parts[0] is None or parts[1] is None:
            return None
        marked = [bool(p["before"] or p["after"]) for p in parts]
        if all(marked):
            return None
        free_at = marked.index(True) if any(marked) else 1
        free, value = _figure(parts[free_at]["n"]), _figure(parts[1 - free_at]["n"])
        if free <= 0 or value - free != amount:
            return None
        return value, free
    single = _PLAIN_TERM.fullmatch(left)
    if single is None or _figure(single["n"]) != amount:
        return None
    return amount, Decimal(0)


def statement_kind(lines: list[dict[str, Any]]) -> str:
    """Which kind of ledger document these lines are. A running balance is a
    Tally printout; a TOTAL/NET VEHICLE COST line is the ERP printout (its
    "Ex-Showroom" is before GST, so it is not read as a statement); the form
    is recognised by its own labels."""
    # A form's own "TOTAL (A)" may come back with its figure as a running
    # balance (ADI-BM-261026-003, 2026-10-29); only a posting with one makes a
    # Tally printout.
    labels = [_label(row.get("particulars_raw")) for row in lines]
    # The ERP settlement first: its closing "Amount To Refund" carries its
    # figure as a balance (REBARANI GANTHIA, 2026-10-30), which made it read as
    # a Tally printout. A Tally printout has no TOTAL / NET VEHICLE COST line.
    if any(_ERP_TOTALS.fullmatch(label) for label in labels):
        return KIND_ERP
    if any(row.get("running_balance") and not _SUMMARY.fullmatch(_label(row.get("particulars_raw")))
           for row in lines):
        return KIND_TALLY
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
    # the old vehicle's value the statement deducts ("LESS: EXCHANGED VEHICLE")
    old_vehicle_value: Decimal | None = None
    # the "Net value" / "Final price" the statement prints, when it has one
    printed_total: Decimal | None = None
    # money paid back or adjusted against the customer (ERP settlement's
    # "Payment Refund to Customer/Other Adjustment" postings)
    refunds: Decimal = Decimal(0)
    # lines left blank on purpose, with why
    unmapped: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)


# A label ending in a whole number of three digits or more, on a row whose
# amount column is empty or zero: the number is the row's amount.
_AMOUNT_IN_LABEL = re.compile(r"(?P<label>.*[A-Z%].*?) (?P<amount>[1-9]\d?(?:,\d{2})*,\d{3}|[1-9]\d{2,})")


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
    # (kind, key, row) -> the amounts given; "row" tells apart the printed rows
    # that add up to one line (other charges) and is "" for every other line.
    found: dict[tuple[str, str, str], list[Decimal]] = {}

    def give(kind: str, key: str, amount: Decimal, row: str = "") -> None:
        found.setdefault((kind, key, row), []).append(amount)

    for row in lines:
        label = _label(row.get("particulars_raw"))
        side = _side_amount(row)
        if side is None and (written := _AMOUNT_IN_LABEL.fullmatch(label)) is not None \
                and any(pattern.fullmatch(written["label"]) for _, pattern in _CHARGES):
            # A charge's amount written into its label, the amount column
            # empty (2026-11-03, SHI-SS-261027-024: "OTHERS Fastag 600" with
            # 0): the figure is the charge's. Never a receipt's or a voucher's
            # number, which only a charge row's label is not.
            label, side = written["label"], ("DR", Decimal(written["amount"].replace(",", "")))
        if not label or side is None:
            continue  # a zero or empty line is not a figure
        _, amount = side
        if _OLD_VEHICLE.fullmatch(label):
            give("OLD_VEHICLE", "old_vehicle_value", amount)
            continue
        if _SCRAPPAGE.fullmatch(label):
            if not _SCRAPPAGE_BONUS.fullmatch(label):
                out.unmapped.append({"label": label, "amount": amount, "why": (
                    "names the scrappage but no bonus: it may be the certificate of deposit bought for it")})
                continue
            give("DISCOUNT", SCRAPPAGE_COD_KEY if _COD.fullmatch(label) else SCRAPPAGE_DEALER_KEY, amount)
            continue
        if _ACCESSORIES_FREE.fullmatch(label):
            # Accessories given free: billed at their value, the same value the
            # free-accessories benefit (the Offer Letter's "ACCESSORIES Free").
            give("COMMERCIAL", "accessories_cost", amount)
            give("DISCOUNT", FREE_ACCESSORIES_KEY, amount)
            continue
        charge = next((k for k, pattern in _CHARGES if pattern.fullmatch(label)), None)
        if charge == "other_charges" and _ADDED_KIT.search(label):
            # A kit written into another row of the Offer Letter ("PERMIT Add
            # Boss kit 18,398" beside "ACCESSORIES Free 30,000": the kit costs
            # Rs 48,398, the ledger's and the accessory invoice's figure) is
            # more accessories, not an other charge (2026-11-03).
            give("COMMERCIAL", "accessories_cost", amount, "ADDED_KIT")
            continue
        if charge is not None:
            give("COMMERCIAL", charge, amount, _other_charge_row(label) if charge == "other_charges" else "")
            continue
        discount = next((k for k, pattern in _DISCOUNTS if pattern.fullmatch(label)), None)
        if discount is not None:
            give("DISCOUNT", discount, amount)
            continue
        if row.get("discount_detail"):
            # Printed among the statement's discounts under a name no benefit
            # type matches ("Loyalty Govt Corporate", "Accessory MR"): a
            # discount all the same, counted as the statement's other discount.
            give("DISCOUNT", "OTHER_SCHEME", amount, label)
            continue
        if _PRINTED_TOTAL.fullmatch(label):
            give("TOTAL", "printed_total", amount)
            continue
        if _VEHICLE_PRICE.fullmatch(label) or _SCHEME_ROW.fullmatch(label):
            give("WORKING", "vehicle_price" if _VEHICLE_PRICE.fullmatch(label) else "scheme", amount)
            continue
        if _SUMMARY.fullmatch(label):
            continue
        if _FINANCER.fullmatch(label):
            out.financer = (out.financer or Decimal(0)) + amount
            out.receipts += amount
            continue
        if _RECEIPT.fullmatch(label):
            out.receipts += amount
            if _TRADE_IN.fullmatch(label):
                # the old vehicle taken against the price, among the received
                # lines on the Shivnath / Utkal forms: paid in kind
                give("OLD_VEHICLE", "old_vehicle_value", amount)
            continue
        working = _WORKING.fullmatch(label)
        item = next((k for k, pattern in _WORKING_ITEMS if working and pattern.fullmatch(working["item"])), None)
        if working is not None and item is not None:
            read = read_working(working["working"], amount)
            if read is None or (read[1] and item != "accessories_cost"):
                out.unmapped.append({"label": label, "amount": amount,
                                     "why": "the working written beside it does not agree with the amount"})
                continue
            value, free = read
            give("COMMERCIAL", item, value, "VLTD" if item == "other_charges" else "")
            if free:
                give("DISCOUNT", FREE_ACCESSORIES_KEY, free)
            continue
        out.unmapped.append({"label": label, "amount": amount, "why": "label not in the mapping"})
    totals: dict[tuple[str, str], Decimal] = {}
    in_conflict: set[tuple[str, str]] = set()
    for (kind, key, _), amounts in sorted(found.items()):
        distinct = sorted(set(amounts))
        if len(distinct) > 1:
            out.conflicts.append({"kind": kind, "key": key, "amounts": distinct})
            in_conflict.add((kind, key))
            continue
        totals[(kind, key)] = totals.get((kind, key), Decimal(0)) + distinct[0]
    for (kind, key), value in totals.items():
        if (kind, key) in in_conflict:
            continue  # one of the rows that add up to it is uncertain
        if kind == "TOTAL":
            out.printed_total = value
        elif kind == "OLD_VEHICLE":
            out.old_vehicle_value = value
        elif kind == "WORKING":
            continue
        else:
            (out.components if kind == "COMMERCIAL" else out.discounts)[key] = value
    _price_and_scheme(out, totals, in_conflict)
    return out


# The Offer Letter's right-hand benefits, one of which may be its scheme
# written again under its name (not the free accessories, which pay for the
# accessories charged, nor the scrappage bonus).
_NAMED_BENEFITS = frozenset(key for key, _ in _DISCOUNTS)
_SAME_SCHEME_TOLERANCE = Decimal(10)


def _price_and_scheme(out: StatementRead, totals: dict[tuple[str, str], Decimal],
                      in_conflict: set[tuple[str, str]]) -> None:
    """The Offer Letter's "VEHICLE PRICE" is the vehicle's ex-showroom price;
    its "SCHEME" is a benefit given on it; "EX SHOWROOM (1-2)" is the one less
    the other (product owner 2026-11-03: the benefits are the ones the NDC
    shows, of the type it shows; the price is checked against the price
    master).

    Live, SHI-SS-261027-007: Vehicle price Rs 9,99,901, Scheme Rs 30,000, Ex
    Showroom (1-2) Rs 9,69,901, and on the right "Corporate discount" Rs
    30,000. The customer paid the Ex Showroom price plus the charges: the
    corporate discount is the scheme, named. Read as the Ex Showroom row, the
    price was Rs 30,000 under the price master and the Rs 30,000 was given a
    second time.

    * The scheme, when one benefit on the letter has its amount, is that
      benefit: nothing is added.
    * Else it is the scheme discount ("Scheme Discount" on the other dealers'
      forms).
    * When the three rows do not add up (a figure misread), the Ex Showroom
      row stands as written and no scheme is read -- unless the Ex Showroom
      row is off by more than half, which is the row misread, not the price
      (SHI-SS-261027-021, 2026-11-04: Vehicle price Rs 19,28,501, Ex Showroom
      read as Rs 19,285)."""
    if ("WORKING", "vehicle_price") in in_conflict or ("WORKING", "scheme") in in_conflict \
            or ("COMMERCIAL", "ex_showroom_price") in in_conflict:
        return
    vehicle_price = totals.get(("WORKING", "vehicle_price"))
    if vehicle_price is None:
        return
    scheme = totals.get(("WORKING", "scheme"), Decimal(0))
    printed = out.components.get("ex_showroom_price")
    expected = vehicle_price - scheme
    if printed is not None and abs(expected - printed) > TOLERANCE and not (
            printed < expected / 2 or printed > expected * 2):
        if scheme > 0:
            out.unmapped.append({"label": "SCHEME", "amount": scheme, "why": (
                "the vehicle price less the scheme is not the Ex Showroom (1-2) written: "
                "the Ex Showroom row is read as written")})
        return
    out.components["ex_showroom_price"] = vehicle_price
    if scheme <= 0:
        return
    named = [key for key, amount in out.discounts.items()
             if key in _NAMED_BENEFITS and abs(amount - scheme) <= _SAME_SCHEME_TOLERANCE]
    if len(named) == 1:
        return  # the scheme, written again on the right under its name
    out.discounts["CASH_DISCOUNT"] = out.discounts.get("CASH_DISCOUNT", Decimal(0)) + scheme


def _other_charge_row(label: str) -> str:
    return next(item for item, pattern in _OTHER_CHARGES if pattern.fullmatch(label))


# ── the ERP "Settlement" printout (Aditya, 2026-10-30) ───────────────────────
# "Settlement: <customer>-<ledger code>": the deal's charges, its discounts and
# the dated postings (finance DO, customer receipts, refunds). Its Ex-Showroom
# is BEFORE GST (12,34,214.28 against a 17,39,901 price) and so are its
# discounts (Scheme 35,714 = 50,000 before GST): neither is read. The charges
# that carry no GST split -- TCS, insurance, registration, RSA, EW, FASTag,
# accessories -- and the postings are (product owner 2026-10-30, REBARANI
# GANTHIA: "TCS is there in customer ledger and ... the payment from financer,
# but deal summary doesn't have these").
SETTLEMENT_SOURCE_TYPE = "erp_settlement"
_ERP_NOT_READ = re.compile(r"EX[- ]?SHOW ?ROOM( PRICE)?")
_ERP_FINANCER = re.compile(r".*(\(FINANCE\)|\(FINANCER\)|FINANCE DO|BANK DO|\bBDO\b).*")
_ERP_SUMMARY = re.compile(
    r"(TOTAL|NET) VEHICLE COST|TOTAL DISCOUNT|DISCOUNT|EXCHANGE OF OLD CAR|ROUND OFF|"
    r"TOTAL AMOUNT (RECEIVED|REFUNDED)(/OTHER ADJUSTMENT)?|AMOUNT TO (REFUND|RECEIVE)|"
    r"PAYMENT RECEIPT FROM FINANCE DO|PAYMENT RECEIVED FROM CUSTOMER(/OTHER ADJUSTMENT)?|"
    r"PAYMENT REFUND TO CUSTOMER(/OTHER ADJUSTMENT)?"
)


def read_erp_settlement(lines: list[dict[str, Any]]) -> StatementRead:
    """Read the ERP settlement printout: the charges without a GST split and
    the dated postings. A posting is a line with a date or voucher; a credit
    is money received (from the financier when it names the finance DO), a
    debit is a refund or adjustment paid out."""
    out = StatementRead(kind=KIND_ERP)
    found: dict[str, list[Decimal]] = {}
    for row in lines:
        label = _label(row.get("particulars_raw"))
        side = _side_amount(row)
        if not label or side is None:
            continue
        direction, amount = side
        if row.get("entry_date") or row.get("voucher_number"):
            if direction == "CR":
                out.receipts += amount
                if _ERP_FINANCER.fullmatch(label):
                    out.financer = (out.financer or Decimal(0)) + amount
            else:
                out.refunds += amount
            continue
        if _ERP_NOT_READ.fullmatch(label):
            out.unmapped.append({"label": label, "amount": amount, "why": "before GST on the ERP settlement"})
            continue
        key = next((k for k, pattern in _CHARGES if pattern.fullmatch(label)), None)
        if key is not None and key != "ex_showroom_price":
            found.setdefault(key, []).append(amount)
            continue
        if _ERP_SUMMARY.fullmatch(label):
            continue
        if any(pattern.fullmatch(label) for _, pattern in _DISCOUNTS) or "DISCOUNT" in label or "CLAIM" in label:
            out.unmapped.append({"label": label, "amount": amount, "why": "discount before GST on the ERP settlement"})
            continue
        out.unmapped.append({"label": label, "amount": amount, "why": "label not in the mapping"})
    for key, amounts in sorted(found.items()):
        distinct = sorted(set(amounts))
        if len(distinct) > 1:
            out.conflicts.append({"kind": "COMMERCIAL", "key": key, "amounts": distinct})
            continue
        out.components[key] = distinct[0]
    return out


def statement_loan(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    """The loan the dealer's own statement records as received from the
    financier, and the refunds it records: the handwritten statement form's
    financer line, else the ERP settlement's finance DO posting. None when
    neither is on file."""
    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    parsed = [(d, rows, statement_kind(rows)) for d in documents for rows in [line_rows(d["fields"].get("line_items"))]]
    statement, copies = combine_statements([(d, rows) for d, rows, kind in parsed if kind == KIND_FORM])
    if statement is not None and (statement.financer is not None or statement.refunds):
        origin = copies.origin.get(("STATEMENT", "financer"), copies.primary)
        return {"loan": statement.financer, "refunds": statement.refunds, "source": "NDC (account statement)",
                "documentId": str(origin["documentId"]) if origin else None}
    for document, rows, kind in parsed:
        if kind != KIND_ERP:
            continue
        read = read_erp_settlement(rows)
        if read.financer is None and not read.refunds:
            continue
        return {"loan": read.financer, "refunds": read.refunds, "source": "Settlement (ERP printout)",
                "documentId": str(document["documentId"])}
    # The customer ledger's journal from the financier: the loan credited to
    # the customer (live 2026-11-02, a Shivnath booking: "MMFSL MAHINDRA FINANCE
    # TA (52879)" Rs 14,86,991, no sanction letter on file).
    from audit_core.uc03_ledger_receipts import ledger_financier_credit

    tallies = [(d, rows) for d, rows, kind in parsed if kind == KIND_TALLY]
    credited = ledger_financier_credit(combined_tally_rows(tallies)) if tallies else None
    if credited is not None:
        return {"loan": credited, "refunds": Decimal(0), "source": "Customer ledger (financier's posting)",
                "documentId": str(tallies[0][0]["documentId"])}
    return None


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


# The customer ledger's own charge and benefit postings (product owner
# 2026-11-02: any line item read on a billing document counts; the customer
# ledger is one). Its vehicle sale is one net figure (ex-showroom with TCS,
# less what was given off the vehicle) and is only set against the NDC
# (tally_deviations), never split into lines.
TALLY_LEDGER_TYPE = "customer_ledger_tally"
_TALLY_CHARGES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r".*(INS POLICY|INSURANCE|\(INS\b).*"), "insurance_amount"),
    (re.compile(r".*\bRTO\b.*"), "road_tax_registration"),
    (re.compile(r".*ACCESSOR.*"), "accessories_cost"),
    (re.compile(r".*\bFAST ?TAG\b.*"), "fastag_amount"),
    (re.compile(r".*(EXTENDED WARRANTY|EXT\.? ?WARRANTY|SHIELD).*"), "extended_warranty_amount"),
    (re.compile(r".*(\bRSA\b|ROAD ?SIDE).*"), "rsa_amount"),
)
_TALLY_BENEFIT_VOUCHER = re.compile(r".*CREDIT ?NOTE.*")


@dataclass
class TallyLines:
    components: dict[str, Decimal] = field(default_factory=dict)
    discounts: dict[str, Decimal] = field(default_factory=dict)
    # credit-note voucher number -> the benefit its row names
    credit_notes: dict[str, str] = field(default_factory=dict)


def read_tally_lines(lines: list[dict[str, Any]]) -> TallyLines:
    """The charges the customer ledger debits (insurance, RTO, accessories,
    FASTag, warranty, RSA) and the benefits its credit notes give, each
    summed per line. Receipts, the vehicle sale, round-off and journal
    transfers are not lines of the deal."""
    from audit_core.uc03_invoice_materialization import (
        UNCLASSIFIED_DISCOUNT,
        classify_discount,
    )

    out = TallyLines()
    for row in lines:
        label = _label(row.get("particulars_raw"))
        debit, credit = _amount(row.get("debit_amount")), _amount(row.get("credit_amount"))
        voucher = _label(row.get("voucher_type"))
        if debit and debit > 0 and not (_TALLY_VEHICLE.fullmatch(label) and "ACCESS" not in label):
            key = next((k for pattern, k in _TALLY_CHARGES if pattern.fullmatch(label)), None)
            if key is not None:
                out.components[key] = out.components.get(key, Decimal(0)) + debit
        elif credit and credit > 0 and _TALLY_BENEFIT_VOUCHER.fullmatch(voucher):
            key = classify_discount(label)
            if key == UNCLASSIFIED_DISCOUNT:
                continue
            out.discounts[key] = out.discounts.get(key, Decimal(0)) + credit
            number = str(row.get("voucher_number") or "").strip()
            if number:
                out.credit_notes[number] = key
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
        # free accessories are taken off the accessories' bill, not the vehicle's
        given = sum((v for k, v in statement.discounts.items() if k != FREE_ACCESSORIES_KEY), Decimal(0))
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
# The lines the cost sheet's reader gives (another writer records its other
# boxes, such as the declared discount total, under the same type).
COST_SHEET_KEYS = frozenset(component for _, component in _COST_SHEET_COMPONENTS)
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


# ── several copies of one document (2026-11-02, Deal defect 15) ──────────────
# The NDC and the cost sheet may be on file more than once: an offer letter and
# the final statement, two pages of one statement, a corrected copy. Every copy
# is read; copies are never added together. The latest dated copy that prints
# the vehicle line is the statement and gives every figure it prints; a copy
# without the vehicle line (another page) adds only the figures that one does
# not print; an older full copy is only compared. A figure two copies give
# differently by more than Rs 100, or one the latest full copy no longer
# prints, raises an information task for the PC (COPIES_DIFFERENCE_TASK).
COPIES_DIFFERENCE_TASK = "NDC_COPIES_DIFFERENCE"
_STATEMENT_DATE_FIELDS = ("invoice_date", "delivery_date", "period_to")
_COST_SHEET_DATE_FIELDS = ("cost_sheet_date",)
_FIGURE_LABELS = {
    ("STATEMENT", "financer"): "Finance D/O",
    ("STATEMENT", "old_vehicle_value"): "Exchanged vehicle",
    ("STATEMENT", "printed_total"): "Net value",
    ("SHEET", "discount_total"): "Total discount",
    ("SHEET", "total"): "On-road total",
}


@dataclass
class Copy:
    """One copy on file: its document, its date as printed, and its figures
    keyed (kind, key)."""
    document: dict[str, Any]
    day: Any
    figures: dict[tuple[str, str], Decimal]
    full: bool


@dataclass
class Combined:
    """What the copies give together: each figure with the copy it came from,
    the copy standing as the document, and where copies differ."""
    figures: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    origin: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    primary: dict[str, Any] | None = None
    used: list[dict[str, Any]] = field(default_factory=list)
    differences: list[dict[str, Any]] = field(default_factory=list)


def _copy_day(fields: dict[str, Any], keys: tuple[str, ...]) -> Any:
    from audit_core.uc03_p2_dates import parse_extracted_date

    days = []
    for key in keys:
        value = fields.get(key)
        if isinstance(value, str):
            value = value.strip().strip('"')
        day = parse_extracted_date(value)
        if day is not None:
            days.append(day)
    return max(days) if days else None


def _newest_first(copies: list[Copy]) -> list[Copy]:
    def order(copy: Copy) -> tuple[bool, str, float]:
        linked = copy.document.get("linkedAtUtc")
        stamp = linked.timestamp() if hasattr(linked, "timestamp") else 0.0
        return (copy.day is not None, copy.day.isoformat() if copy.day is not None else "", stamp)

    return sorted(copies, key=order, reverse=True)


def combine_copies(copies: list[Copy]) -> Combined:
    """Each figure from the copy that gives it (see the section comment)."""
    out = Combined()
    if not copies:
        return out
    ordered = _newest_first(copies)
    primary = next((c for c in ordered if c.full), ordered[0])
    out.primary = primary.document
    other_full = [c for c in ordered if c.full and c is not primary]
    pages = [c for c in ordered if not c.full and c is not primary]
    keys = list(dict.fromkeys(k for c in ordered for k in c.figures))
    used = {id(primary.document): primary.document}
    for key in keys:
        if key in primary.figures:
            chosen: Copy | None = primary
        elif any(key in c.figures for c in other_full):
            chosen = None  # the latest full copy no longer prints it
        else:
            chosen = next(c for c in pages if key in c.figures)
        taken = chosen.figures[key] if chosen is not None else None
        if chosen is not None and taken is not None:
            out.figures[key] = taken
            out.origin[key] = chosen.document
            used.setdefault(id(chosen.document), chosen.document)
        for copy in ordered:
            if copy is chosen or key not in copy.figures:
                continue
            if taken is None or abs(copy.figures[key] - taken) > TOLERANCE:
                out.differences.append({
                    "kind": key[0], "key": key[1], "label": _FIGURE_LABELS.get(key, key[1]),
                    "taken": taken, "takenFrom": str(chosen.document["documentId"]) if chosen else None,
                    "other": copy.figures[key], "otherFrom": str(copy.document["documentId"]),
                })
    out.used = list(used.values())
    return out


def _statement_figures(read: StatementRead) -> dict[tuple[str, str], Decimal]:
    figures = {("COMMERCIAL", k): v for k, v in read.components.items()}
    figures.update({("DISCOUNT", k): v for k, v in read.discounts.items()})
    for name in ("financer", "old_vehicle_value", "printed_total"):
        value = getattr(read, name)
        if value is not None:
            figures[("STATEMENT", name)] = value
    return figures


def combine_statements(
    forms: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[StatementRead | None, Combined]:
    """The statement the Deal reads, from every statement form on file. With
    one copy this is exactly read_statement of it."""
    reads = {id(document): read_statement(rows) for document, rows in forms}
    copies = [
        Copy(document=document, day=_copy_day(document["fields"], _STATEMENT_DATE_FIELDS),
             figures=_statement_figures(reads[id(document)]),
             full="ex_showroom_price" in reads[id(document)].components)
        for document, _ in forms
    ]
    combined = combine_copies(copies)
    if combined.primary is None:
        return None, combined
    out = StatementRead(kind=KIND_FORM)
    for (kind, key), value in combined.figures.items():
        if kind == "COMMERCIAL":
            out.components[key] = value
        elif kind == "DISCOUNT":
            out.discounts[key] = value
        else:
            setattr(out, key, value)
    # The money received is the statement's own, never added across copies:
    # the standing copy's, else the newest other page's that prints any.
    in_use = [reads[id(d)] for d in combined.used]
    primary = reads[id(combined.primary)]
    out.receipts = primary.receipts or next((r.receipts for r in in_use if r.receipts), Decimal(0))
    out.refunds = primary.refunds or next((r.refunds for r in in_use if r.refunds), Decimal(0))
    for read in in_use:
        out.unmapped.extend(read.unmapped)
        out.conflicts.extend(read.conflicts)
    return out, combined


def combine_cost_sheets(sheets: list[dict[str, Any]]) -> tuple[CostSheetRead | None, Combined]:
    """The cost sheet the Deal reads, from every cost sheet on file, by the
    same rule as the statement's copies; a copy that reads nothing is not one."""
    copies = []
    for document in sheets:
        read = read_cost_sheet(document["fields"])
        figures = {("COMMERCIAL", k): v for k, v in read.components.items()}
        if read.discount_total is not None:
            figures[("SHEET", "discount_total")] = read.discount_total
        if read.total is not None:
            figures[("SHEET", "total")] = read.total
        if figures:
            copies.append(Copy(document=document, day=_copy_day(document["fields"], _COST_SHEET_DATE_FIELDS),
                               figures=figures, full="ex_showroom_price" in read.components))
    combined = combine_copies(copies)
    if combined.primary is None:
        return None, combined
    out = CostSheetRead()
    for (kind, key), value in combined.figures.items():
        if kind == "COMMERCIAL":
            out.components[key] = value
        elif key == "discount_total":
            out.discount_total = value
        else:
            out.total = value
    return out, combined


def combined_tally_rows(tallies: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Every voucher row of the Tally ledger pages on file, each once (a page
    on file twice is one page), newest copy first."""
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    ordered = sorted(tallies, key=lambda t: (
        t[0].get("linkedAtUtc").timestamp() if hasattr(t[0].get("linkedAtUtc"), "timestamp") else 0.0),
        reverse=True)
    for _, lines in ordered:
        for row in lines:
            key = json.dumps(row, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def _copies_text(differences: list[dict[str, Any]]) -> str:
    def rupees(value: Decimal | None) -> str:
        return "not printed" if value is None else f"Rs {value:,.0f}"

    return "; ".join(f"{d['label']}: {rupees(d['taken'])} taken, another copy {rupees(d['other'])}"
                     for d in differences)


def chosen_copies(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, dict[tuple[str, str], str]]:
    """For the Deal: which document each statement and cost-sheet figure is
    taken from, as (line kind, key) -> document id (the sync's own choice)."""
    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    forms = [(d, rows) for d in documents for rows in [line_rows(d["fields"].get("line_items"))]
             if statement_kind(rows) == KIND_FORM]
    _, statement = combine_statements(forms)
    _, sheet = combine_cost_sheets(load_ledger_documents(
        connection, tenant_id=tenant_id, journey_id=journey_id, document_type=COST_SHEET_TYPE))
    return {
        ACCOUNT_STATEMENT_TYPE: {k: str(d["documentId"]) for k, d in statement.origin.items()},
        COST_SHEET_TYPE: {k: str(d["documentId"]) for k, d in sheet.origin.items()},
    }


def cost_sheet_discount(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[Decimal, str] | None:
    """The cost sheet's discount total (one figure, its types not printed) and
    the document it stands from, when the cost sheet prints one."""
    sheet, copies = combine_cost_sheets(load_ledger_documents(
        connection, tenant_id=tenant_id, journey_id=journey_id, document_type=COST_SHEET_TYPE))
    if sheet is None or sheet.discount_total is None or copies.primary is None:
        return None
    origin = copies.origin.get(("SHEET", "discount_total"), copies.primary)
    return sheet.discount_total, str(origin["documentId"])


def standing_figures(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, dict[tuple[str, str], str | None]]:
    """For the Deal: every figure the dealer's books give now, as (line kind,
    key) -> the document it stands from, by source type -- exactly what
    sync_account_statement records: the NDC's lines, benefits and printed
    totals; the ERP settlement's charges (no NDC); the Tally ledger's postings
    (recorded under no document); the cost sheet's lines.

    A recorded figure of these types that is not here was set by an earlier
    reading and no longer stands: the service role may not delete source rows,
    so a re-read leaves the first reading's rows behind (live 2026-11-03: an
    NDC whose reading was cleared still showed its first reading's
    Rs 21,75,000 corporate discount and Rs 1,51,000 registration)."""
    documents = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id)
    parsed = [(d, rows, statement_kind(rows)) for d in documents for rows in [line_rows(d["fields"].get("line_items"))]]
    statement, copies = combine_statements([(d, rows) for d, rows, kind in parsed if kind == KIND_FORM])
    ledger: dict[tuple[str, str], str | None] = {}
    if statement is not None and copies.primary is not None:
        for kind, figures in (("COMMERCIAL", statement.components), ("DISCOUNT", statement.discounts)):
            for key in figures:
                ledger[(kind, key)] = str(copies.origin.get((kind, key), copies.primary)["documentId"])
        for key, _amount, origin in _declared_figures(statement, copies):
            ledger[("COMMERCIAL", key)] = str(origin["documentId"])
    settlement: dict[tuple[str, str], str | None] = {}
    settlements = [(d, rows) for d, rows, kind in parsed if kind == KIND_ERP]
    if statement is None and settlements:
        erp_document, erp_rows = settlements[0]
        settlement = {("COMMERCIAL", key): str(erp_document["documentId"])
                      for key in read_erp_settlement(erp_rows).components}
    tallies = [(d, rows) for d, rows, kind in parsed if kind == KIND_TALLY]
    tally: dict[tuple[str, str], str | None] = {}
    if tallies:
        read = read_tally_lines(combined_tally_rows(tallies))
        tally = {(kind, key): None for kind, figures in (("COMMERCIAL", read.components), ("DISCOUNT", read.discounts))
                 for key in figures}
    sheet, sheet_copies = combine_cost_sheets(load_ledger_documents(
        connection, tenant_id=tenant_id, journey_id=journey_id, document_type=COST_SHEET_TYPE))
    cost: dict[tuple[str, str], str | None] = {}
    if sheet is not None:
        cost = {("COMMERCIAL", key): str(sheet_copies.origin[("COMMERCIAL", key)]["documentId"])
                for key in sheet.components}
    return {ACCOUNT_STATEMENT_TYPE: ledger, SETTLEMENT_SOURCE_TYPE: settlement, TALLY_LEDGER_TYPE: tally,
            COST_SHEET_TYPE: cost}


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
    for doc in documents.values():
        details = _discount_detail_rows(doc["fields"])
        if details:
            doc["fields"]["line_items"] = [*line_rows(doc["fields"].get("line_items")), *details]
    return list(documents.values())


def _discount_detail_rows(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """The statement's discounts read into ``discount_details`` (label and
    amount) rather than among its line items, as credit rows the reader takes
    like any other (2026-11-04, independent check: UTKAL NDCs' "Corporate
    Discount" 5,000, "Exchange Loyalty" 20,000, "Other Discount" 10,000 were
    never counted, and the invoice's matching discounts were set aside as
    not on the statement). A detail the line items already carry -- the same
    amount on a row of the same benefit -- is not added again."""
    raw = fields.get("discount_details")
    details = line_rows(raw) if raw not in (None, "", "null") else []
    if not details:
        return []
    existing = line_rows(fields.get("line_items"))

    def benefit(label: str) -> str | None:
        return next((k for k, pattern in _DISCOUNTS if pattern.fullmatch(label)), None)

    taken = {(benefit(_label(r.get("particulars_raw"))) or _label(r.get("particulars_raw")),
              _amount(r.get("credit_amount")) or _amount(r.get("debit_amount"))) for r in existing}
    out = []
    for detail in details:
        label, amount = _label(detail.get("label") or detail.get("name")), _amount(detail.get("amount"))
        if not label or amount is None or amount == 0:
            continue
        if ((benefit(label) or label), abs(amount)) in taken:
            continue
        out.append({"particulars_raw": detail.get("label") or detail.get("name"), "credit_amount": abs(amount),
                    "discount_detail": True})
    return out


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


def _latest(tallies: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> dict[str, Any]:
    """The newest Tally ledger page on file: the document its figures are
    recorded under."""
    return max((d for d, _ in tallies), key=lambda d: (
        d.get("linkedAtUtc").timestamp() if hasattr(d.get("linkedAtUtc"), "timestamp") else 0.0))


def _record_tally_lines(
    connection: Connection, *, tenant_id: str, journey_id: UUID,
    tallies: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> int:
    """One row per line, under no document id (the ledger's pages are read
    together), tied to the newest page's evidence: a re-read overwrites it,
    and it stops counting once that page is no longer on file
    (ACTIVE_SOURCE_CONDITION). Never deleted: the service role may not delete
    source rows (live 2026-11-02: a DELETE here failed every statement sync)."""
    from audit_core.uc03_deal_source_history import record_source_value

    if not tallies:
        return 0
    origin = _latest(tallies)
    read = read_tally_lines(combined_tally_rows(tallies))
    written = 0
    for kind, figures in (("COMMERCIAL", read.components), ("DISCOUNT", read.discounts)):
        for key, amount in sorted(figures.items()):
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind=kind, component_key=key,
                source_document_type=TALLY_LEDGER_TYPE, amount=amount, evidence_id=origin["evidenceId"],
                document_id=None,
            )
            written += 1
    return written


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


# The statement's printed figures recorded as the Deal's declared totals
# (uc03_p2_journey360._DECLARED_TOTAL_KEYS), from the copy standing as the
# statement.
DECLARED_NET = "net_amount"
DECLARED_RECEIVED = "received_amount"
DECLARED_BALANCE = "balance_amount"
DECLARED_OLD_VEHICLE = "old_vehicle_value"


def _declared_figures(statement: StatementRead, copies: Combined) -> list[tuple[str, Decimal, dict[str, Any]]]:
    primary = copies.primary
    if primary is None:
        return []
    fields = primary["fields"]
    out: list[tuple[str, Decimal, dict[str, Any]]] = []
    net = statement_total(fields, statement)
    if net is not None:
        out.append((DECLARED_NET, net, copies.origin.get(("STATEMENT", "printed_total"), primary)))
    received = _amount(fields.get("total_receipts"))
    if received is not None and received > 0:
        out.append((DECLARED_RECEIVED, received, primary))
    balance = _amount(fields.get("balance_amount"))
    if balance is not None:
        out.append((DECLARED_BALANCE, balance, primary))
    if statement.old_vehicle_value is not None and statement.old_vehicle_value > 0:
        out.append((DECLARED_OLD_VEHICLE, statement.old_vehicle_value,
                    copies.origin.get(("STATEMENT", "old_vehicle_value"), primary)))
    return out


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
    # Every statement copy on file is read; each figure comes from the copy
    # that gives it (combine_statements), recorded under that copy.
    statement, copies = combine_statements(forms)
    document = copies.primary
    keep_components = set(statement.components) if statement else set()
    keep_discounts = set(statement.discounts) if statement else set()
    lines_written = discounts_written = 0
    if statement and document:
        for key, amount in sorted(statement.components.items()):
            origin = copies.origin.get(("COMMERCIAL", key), document)
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL", component_key=key,
                source_document_type=ACCOUNT_STATEMENT_TYPE, amount=amount, evidence_id=origin["evidenceId"],
                document_id=origin["documentId"],
            )
            if upsert_commercial_actual(
                connection, tenant_id=tenant_id, journey_id=journey_id, component_key=key, amount=amount,
                document_type=ACCOUNT_STATEMENT_TYPE, document_id=origin["documentId"],
                evidence_id=origin["evidenceId"], record=False,
            ):
                lines_written += 1
        for key, amount in sorted(statement.discounts.items()):
            origin = copies.origin.get(("DISCOUNT", key), document)
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="DISCOUNT", component_key=key,
                source_document_type=ACCOUNT_STATEMENT_TYPE, amount=amount, evidence_id=origin["evidenceId"],
                document_id=origin["documentId"],
            )
            _write_discount(connection, tenant_id=tenant_id, journey_id=journey_id, key=key, amount=amount,
                            document_id=origin["documentId"], evidence_id=origin["evidenceId"])
            discounts_written += 1
    _stale_statement_rows(connection, tenant_id=tenant_id, journey_id=journey_id,
                          keep_components=keep_components, keep_discounts=keep_discounts)
    # The statement's own totals as it prints them, and the old vehicle's value
    # it deducts: recorded beside the Deal (its declared totals), so the Deal is
    # set against them and the old vehicle counts as paid in kind (2026-11-02,
    # Deal defects 4 and 13). Never computed, never a charge.
    if statement and document:
        for key, amount, origin in _declared_figures(statement, copies):
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL", component_key=key,
                source_document_type=ACCOUNT_STATEMENT_TYPE, amount=amount, evidence_id=origin["evidenceId"],
                document_id=origin["documentId"],
            )

    # No statement form, but the ERP settlement printout: its charges without a
    # GST split are recorded for the Deal under their own name (the Deal view
    # shows them billed; the cost sheet keeps standing in for the vehicle and
    # the discounts). Nothing canonical is set from it.
    settlements = [(d, rows) for d, rows, kind in parsed if kind == KIND_ERP]
    settlement_lines = 0
    if statement is None and settlements:
        erp_document, erp_rows = settlements[0]
        for key, amount in sorted(read_erp_settlement(erp_rows).components.items()):
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL", component_key=key,
                source_document_type=SETTLEMENT_SOURCE_TYPE, amount=amount, evidence_id=erp_document["evidenceId"],
                document_id=erp_document["documentId"],
            )
            settlement_lines += 1

    # The customer ledger's own charges and credit-note benefits, recorded for
    # the Deal under their own name: below the NDC and the cost sheet, above
    # the invoices (product owner 2026-11-02). Rewritten on every sync.
    tally_lines = _record_tally_lines(connection, tenant_id=tenant_id, journey_id=journey_id, tallies=tallies)

    # The cost sheet: its lines are recorded for the Deal (shown as the cost
    # sheet's, standing in the billed column only when no NDC is on file -- see
    # uc03_p2_journey360), and compared with the NDC when both are in.
    sheets = load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                   document_type=COST_SHEET_TYPE)
    sheet, sheet_copies = combine_cost_sheets(sheets)
    if sheet is not None:
        for key, amount in sorted(sheet.components.items()):
            origin = sheet_copies.origin[("COMMERCIAL", key)]
            record_source_value(
                connection, tenant_id=tenant_id, journey_id=journey_id, line_kind="COMMERCIAL", component_key=key,
                source_document_type=COST_SHEET_TYPE, amount=amount, evidence_id=origin["evidenceId"],
                document_id=origin["documentId"],
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
                                     "costSheetDocumentId": (str(sheet_copies.primary["documentId"])
                                                             if sheet_copies.primary else None)}),
            severity="HIGH",
        )
    else:
        _resolve_task(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=COST_SHEET_DIFFERENCE_TASK,
                      evidence={"ndcOnFile": statement is not None, "costSheetOnFile": sheet is not None})

    # Copies of the NDC, or of the cost sheet, that give a figure differently:
    # for information, the PC closes it with a comment.
    copy_differences = [{**d, "document": "NDC"} for d in copies.differences] + [
        {**d, "document": "Cost sheet"} for d in sheet_copies.differences]
    if copy_differences:
        _raise_task(
            connection, tenant_id=tenant_id, journey_id=journey_id, task_type=COPIES_DIFFERENCE_TASK,
            title="Copies of the NDC or cost sheet differ",
            description=(
                "More than one copy of the NDC or of the cost sheet is on file and they differ by more than "
                "Rs 100 on: " + "; ".join(f"{d['document']} {_copies_text([d])}" for d in copy_differences)
                + ". For information: the latest dated copy that prints the vehicle line gives the Deal's "
                "figures. Check which is right and close this task with a comment."
            ),
            reference=_json_amounts({"differences": copy_differences}),
            severity="HIGH",
        )
    else:
        _resolve_task(connection, tenant_id=tenant_id, journey_id=journey_id, task_type=COPIES_DIFFERENCE_TASK,
                      evidence={"statementCopies": len(forms), "costSheetCopies": len(sheets)})

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
        deviations = tally_deviations(statement, tally_postings(combined_tally_rows(tallies)))
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
        statements=len(forms), tallies=len(tallies), tally_lines=tally_lines,
        components=len(keep_components), discounts=len(keep_discounts),
        unmapped=len(statement.unmapped) if statement else 0, conflicts=len(statement.conflicts) if statement else 0,
        old_vehicle_value=str(statement.old_vehicle_value) if statement and statement.old_vehicle_value else None,
        invoice_actuals_cleared=cleared, tally_deviations=len(deviations),
        cost_sheets=len(sheets), cost_sheet_differences=len(differences), settlement_lines=settlement_lines,
        copy_differences=len(copy_differences),
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
    "COPIES_DIFFERENCE_TASK",
    "COST_SHEET_DIFFERENCE_TASK",
    "COST_SHEET_KEYS",
    "cost_sheet_discount",
    "COST_SHEET_TYPE",
    "DECLARED_BALANCE",
    "DECLARED_NET",
    "DECLARED_OLD_VEHICLE",
    "DECLARED_RECEIVED",
    "FREE_ACCESSORIES_KEY",
    "KIND_ERP",
    "KIND_FORM",
    "KIND_TALLY",
    "KIND_UNKNOWN",
    "NO_EX_SHOWROOM_TASK",
    "ORIGIN",
    "SCRAPPAGE_COD_KEY",
    "SCRAPPAGE_DEALER_KEY",
    "SETTLEMENT_SOURCE_TYPE",
    "TALLY_DEVIATION_TASK",
    "TALLY_LEDGER_TYPE",
    "CostSheetRead",
    "StatementRead",
    "chosen_copies",
    "combine_copies",
    "combine_cost_sheets",
    "combine_statements",
    "combined_tally_rows",
    "cost_sheet_differences",
    "line_rows",
    "load_ledger_documents",
    "read_cost_sheet",
    "read_erp_settlement",
    "read_statement",
    "read_tally_lines",
    "read_working",
    "standing_figures",
    "statement_kind",
    "statement_loan",
    "sync_account_statement",
    "sync_if_source_of_truth",
    "tally_deviations",
    "tally_postings",
]
