"""bank_reconciliation.py -- tie the dealership's payment receipts to the credits on its
own bank statements (product owner 2026-11-14). Pure: no database, no clock.

Scope (product owner 2026-11-17, replacing the +/- 5 days of 2026-11-14: "lets change
the time to statement transaction dates"): a receipt is checked only when its date falls
within the dates of an uploaded statement's own transactions, first to last (and the
statement is the dealership's, or that receipt's outlet's); a receipt is tied only to a
credit dated within 5 days of it. Every other receipt is left alone -- never "not found".

What the receipts on file carry (live, Mahindra Orissa, 14 Nov 2026): the amount and date
always; the payer's UTR / RRN / cheque number on about half; the mode often only "OTHERS";
the bank the money went into on most bank receipts. What a credit carries: the amount, the
date, and a narration that usually names the payer ("RTGS/ICICR1.../SAURAV BISWAL/ICICI
BANK") -- truncated to 8 letters on IMPS ("IMPS/P2A/.../LAXMINAR/").

A receipt and a credit of the same amount (to the rupee) within 5 days are tied on, in
order of strength:
  REFERENCE    the receipt's UTR / RRN / cheque number is in the credit's narration,
               reference or cheque column (or the other way round)
  NAME         the customer's name is in the narration (word order and spacing aside)
  NAME_PREFIX  the narration carries the name's first 7+ letters (IMPS truncation)
  AMOUNT_DATE  neither, the receipt prints no reference of its own (a receipt whose UTR /
               cheque is not on the credit is not that credit), the amount is distinctive
               (Rs 50,000 or more, or not a round thousand), exactly one such receipt and
               one such credit are in range, and the receipt's bank (when printed) is the
               statement's bank
A tie is made only when it is unambiguous at its strength -- one candidate each way, or
(above AMOUNT_DATE) the one clearly nearest in date each way; a receipt with several
equally good credits is AMBIGUOUS, not guessed. A receipt not tied is NOT_FOUND. (Until
17 Nov 2026 a receipt dated only in a 5-day margin around the statement was OUTSIDE_DATES;
kept for the rows of that time.) Then, by name or reference only: one receipt paid
as two or three credits (two IMPS of Rs 1,00,000 for a Rs 2,00,000 receipt), and one credit
receipted as two to four receipts of the same Journey.

Cash receipts are tied only to a cash deposit of the exact amount made the same day or up to
two days after, uniquely; the rest are CASH
-- deposited in batches, not traceable one by one (product owner: cash raises no tasks).
Card receipts the same way against card settlements (CARD).

Loans (product owner 2026-11-17, match_loans): a financier's disbursement often has no
receipt on file. A credit no receipt took -- a financier's, or a customer's that names one
of our customers -- is tied to the Journey whose loan it is, from the loan papers on file
(the D.O. cover, the bank approval letter, the finance record), in order of strength:
  LOAN_REFERENCE  the D.O.'s disbursement UTR, the D.O. number, the loan application or
                  sanction letter number is in the narration
  LOAN_NAME       the narration names the customer and the amount is the loan's
                  (disbursed, payout to the dealer, sanctioned or D.O. amount)
  LOAN_FINANCIER  a credit of the loan's amount naming its financier (or its initials:
                  PNB, IOB), the
                  loan dated near the credit (_loan_dated: disbursed within 15 days of
                  it, or sanctioned up to 60 days before it)
  LOAN_AMOUNT     a financier's credit of a distinctive amount (not a round Rs 10,000)
                  that exactly one loan of the dealership carries, that loan so dated
Each only when one loan and one credit meet it. A financier's pooled payment (one credit
for many loans: Cholamandalam, Softcell pool accounts) meets none and stays not identified.
The Journey's loan receipt of that amount, if one is on file and was not found, is then
found on that credit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Any

WINDOW_DAYS = 5
AMOUNT_TOLERANCE = Decimal("1.00")
DISTINCTIVE_AMOUNT = Decimal(50000)

MATCHED, AMBIGUOUS, NOT_FOUND, CASH, CARD = "MATCHED", "AMBIGUOUS", "NOT_FOUND", "CASH", "CARD"
# Not tied, and dated outside every covering statement's own dates (in the +/- 5 day
# margin only): the money may have come in on a day the statements do not show.
OUTSIDE_DATES = "OUTSIDE_DATES"
REFERENCE, NAME, NAME_PREFIX, AMOUNT_DATE = "REFERENCE", "NAME", "NAME_PREFIX", "AMOUNT_DATE"
LOAN_REFERENCE, LOAN_NAME, LOAN_FINANCIER, LOAN_AMOUNT = "LOAN_REFERENCE", "LOAN_NAME", "LOAN_FINANCIER", "LOAN_AMOUNT"
LOAN_TIERS = (LOAN_REFERENCE, LOAN_NAME, LOAN_FINANCIER, LOAN_AMOUNT)
CASH_DEPOSIT, NAME_SPLIT, COMBINED = "CASH_DEPOSIT", "SPLIT_CREDITS", "COMBINED_RECEIPTS"

# What a credit is.
K_CUSTOMER, K_CASH, K_CARD, K_LOAN, K_INSURANCE, K_INTERNAL = (
    "CUSTOMER", "CASH", "CARD_UPI", "LOAN", "INSURANCE", "OWN_TRANSFER")


@dataclass
class Receipt:
    key: str
    journey_id: str
    outlet_id: str | None
    amount: Decimal
    receipt_date: date | None
    mode: str | None
    reference: str | None
    bank: str | None
    customer_names: list[str] = field(default_factory=list)
    is_loan: bool = False
    source: str = "RECEIPT"
    number: str | None = None


@dataclass
class Credit:
    key: str
    statement_id: str
    outlet_id: str | None
    bank: str | None
    txn_date: date | None
    amount: Decimal
    narration: str
    reference: str | None = None
    cheque_number: str | None = None
    kind: str = K_CUSTOMER


@dataclass
class Window:
    statement_id: str
    outlet_id: str | None
    period_from: date
    period_to: date

    def within_period(self, day: date | None) -> bool:
        return day is not None and self.period_from <= day <= self.period_to

    def covers(self, day: date | None, outlet_id: str | None) -> bool:
        """The receipt is checked on this statement: dated within its transactions' dates
        (period_from..period_to are those, first to last), the outlet's own when it is one."""
        if day is None:
            return False
        if self.outlet_id is not None and self.outlet_id != outlet_id:
            return False
        return self.period_from <= day <= self.period_to


@dataclass
class ReceiptResult:
    receipt: Receipt
    outcome: str
    basis: str | None = None
    credit_keys: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class CreditResult:
    credit: Credit
    receipt_keys: list[str] = field(default_factory=list)
    basis: str | None = None
    journey_id: str | None = None
    named_journey_id: str | None = None
    loan: bool = False  # tied to the Journey's loan (match_loans), not to a receipt



# --------------------------------------------------------------------------- classifying

def _alnum(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _letters(value: Any) -> str:
    return re.sub(r"[^A-Z]", "", str(value or "").upper())


_CASH_WORDS = re.compile(r"\bCASH\b|/CASH\b|CASH ?DEP|\bBNA\b|BY CASH|CDM|\bCMS\d", re.IGNORECASE)
_CARD_WORDS = re.compile(r"\bEDC\b|^EDC/|POS[_ ]?SET|ICICIPOS|\bEZY/|CARD ?SETT|MERCHANT ?SETT|"
                         r"/PHONEPE/|PAYTM PAYMENTS|BHARATPE|PINE ?LABS|RAZORPAY|CASHFREE|MSWIPE|"
                         r"SETTLEMENT", re.IGNORECASE)
_LOAN_WORDS = re.compile(r"\bLOAN\b|DISB|DISBURS|\bFINANCE\b|FINANCIAL SERV|CHOLAMANDALAM|SOFTCELL|"
                         r"MAHINDRA ?(AND|&)? ?MAHINDRA ?FIN|MMFSL|KOTAK MAHINDRA PRIME|BAJAJ FIN|HDB FIN|"
                         r"SHRIRAM|SUNDARAM FIN|TATA CAPITAL|L ?& ?T FIN|HERO FINCORP|CFD |TERM LOAN|"
                         r"AU SMALL|EQUITAS|POONAWALLA|/LVBBS|LAPUR", re.IGNORECASE)
_INSURANCE_WORDS = re.compile(r"INSURANCE|LOMBARD|GENERALI|TATA AIG|BAJAJ ALLIANZ|RELIANCE GEN|"
                              r"\bIFFCO|ROYAL SUNDARAM|DIGIT INS|HDFC ERGO|\bSBI GEN|NATIONAL INS|"
                              r"NEW INDIA ASS|ORIENTAL INS|UNITED INDIA|MAGMA HDI|SHRIRAM GEN", re.IGNORECASE)


def holder_words(holder: str | None) -> list[str]:
    """The account holder's distinctive words (ADITYA, SHIVNATH): a credit naming them
    is the dealership moving its own money."""
    stop = {"PRIVATE", "LIMITED", "PVT", "LTD", "MOTORS", "MOTOR", "CAR", "CARS", "AUTOMOTIVES", "AUTOMOTIVE",
            "INDIA", "UNIT", "OF", "THE", "AND", "A", "M/S", "MS", "AUTO", "AUTOMOBILES"}
    return [w for w in re.findall(r"[A-Z]{4,}", str(holder or "").upper()) if w not in stop]


_ID_SEGMENT = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$|^[A-Z0-9]*\d[A-Z0-9]*$")


def remitter(narration: str) -> str:
    """Who sent the money, as the narration prints it: the first worded part after the
    channel and its reference ("NEFT/HDFCH01377355272/ADITYA MOTORS/HDFC BANK" ->
    ADITYA MOTORS; "NEFT CR-SBIN0002011-JYOTIRANJAN PATRA-ADITYA MOTORS-..." -> JYOTIRANJAN
    PATRA, the dealership there being the beneficiary)."""
    parts = [p.strip() for p in re.split(r"[/\-]", str(narration or "").upper()) if p.strip()]
    channel = {"NEFT", "RTGS", "IMPS", "UPI", "INB", "IFT", "MMT", "INF", "INFT", "NEFT CR", "NEFT DR", "P2A", "MOB",
               "TPFT", "FT", "CLG", "TRF", "TRFR", "CR", "DR"}
    for part in parts:
        if part in channel or _ID_SEGMENT.match(part.replace(" ", "")):
            continue
        if len(re.sub(r"[^A-Z]", "", part)) >= 3:
            return part
    return str(narration or "").upper()


def classify_credit(narration: str, holder: str | None) -> str:
    text = str(narration or "").upper()
    words = holder_words(holder)
    if words and any(w in _letters(remitter(text)) for w in words):
        return K_INTERNAL
    if re.search(r"\bSELF\b(?! CASH)|SWEEP|TRF FROM OWN|OWN ACCOUNT", text) and not _CASH_WORDS.search(text):
        return K_INTERNAL
    if _CASH_WORDS.search(text):
        return K_CASH
    if _CARD_WORDS.search(text):
        return K_CARD
    if _INSURANCE_WORDS.search(text):
        return K_INSURANCE
    if _LOAN_WORDS.search(text):
        return K_LOAN
    return K_CUSTOMER


def mode_class(mode: Any) -> str:
    """CASH / CARD / OTHER."""
    token = _alnum(mode)
    if token.startswith("CASH"):
        return "CASH"
    if token in {"CARD", "DEBITCARD", "CREDITCARD", "POS", "SWIPE", "EDC"} or "CARD" in token:
        return "CARD"
    return "OTHER"


# --------------------------------------------------------------------------- evidence tests

_NOISE_REF = re.compile(r"^(QR|PPAY|PHONEPAY|PHONEPE|GPAY|UPI|NEFT|RTGS|IMPS|CHEQUE|CASH|BANK|ONLINE|TRANSFER)$")


def reference_tokens(reference: Any) -> list[str]:
    """The identifiers a receipt's reference carries: the whole of it when it is one
    identifier, else each part of 6+ characters with a digit in it ("630614332059" out of
    "630614332059/MTENTERP/ICICIBAN/MTEPayme/9")."""
    raw = str(reference or "").upper()
    whole = _alnum(raw)
    parts = [p for p in re.split(r"[^A-Z0-9]+", raw) if len(p) >= 6 and any(c.isdigit() for c in p)]
    out = []
    if len(whole) >= 6 and any(c.isdigit() for c in whole) and len(parts) <= 1:
        out.append(whole)
    out += parts
    seen: set[str] = set()
    return [t for t in out if not _NOISE_REF.match(t) and not (t in seen or seen.add(t))
            and not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])\d{0,2}", t)]


def reference_hit(receipt: Receipt, credit: Credit) -> bool:
    haystack = _alnum(f"{credit.narration} {credit.reference or ''} {credit.cheque_number or ''}")
    for token in reference_tokens(receipt.reference):
        stripped = token.lstrip("0")
        if len(stripped) >= 6 and stripped in haystack:
            return True
    # the statement's own reference inside the receipt's ("HDFCH01364 633517/LULU BULU ...")
    receipt_ref = _alnum(receipt.reference)
    for value in (credit.reference, credit.cheque_number):
        token = _alnum(value).lstrip("0")
        if len(token) >= 6 and receipt_ref and token in receipt_ref:
            return True
    return False


_NAME_STOP = {"MR", "MRS", "MS", "MISS", "DR", "SHRI", "SRI", "SMT", "KUMARI", "M/S", "MESSRS", "THE",
              "PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "CO", "COMPANY", "AND", "INDIA"}


def _name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[A-Z]+", str(name or "").upper()) if w not in _NAME_STOP]


def name_hit(names: list[str], narration: str) -> str | None:
    """NAME / NAME_PREFIX / None: whether the narration names one of these people."""
    tokens = [t for t in re.split(r"[^A-Z]+", str(narration or "").upper()) if t]
    compact = "".join(tokens)
    # where the narration's words start and end in the joined letters: a name must
    # begin and end on them ("SATY ARANJAN SAHOO" carries RANJANSAHOO mid-word)
    starts, ends, at = set(), set(), 0
    for t in tokens:
        starts.add(at)
        at += len(t)
        ends.add(at)

    def aligned(v: str) -> bool:
        i = compact.find(v)
        while i >= 0:
            if i in starts and i + len(v) in ends:
                return True
            i = compact.find(v, i + 1)
        return False

    best = None
    for name in names:
        words = _name_words(name)
        if not words:
            continue
        joined = "".join(words)
        if len(joined) < 6:
            continue
        variants = {joined, "".join(reversed(words))}
        if len(words) >= 3:
            variants.add(words[0] + words[-1])
            variants.add(words[-1] + words[0])
        if any(len(v) >= 6 and aligned(v) for v in variants):
            return NAME
        # an IMPS / UPI payee field cut to 7-10 letters: LAXMINAR, SUSILKUM, CHINUTRA
        if any(7 <= len(t) <= 12 and joined.startswith(t) for t in tokens):
            best = NAME_PREFIX
    return best


def _same_amount(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= AMOUNT_TOLERANCE


def _distinctive(amount: Decimal) -> bool:
    return amount >= DISTINCTIVE_AMOUNT or amount % 1000 != 0


def _bank_key(name: Any) -> str:
    text = _letters(name).replace("BANK", "").replace("LIMITED", "").replace("LTD", "")
    for long_, short in (("STATEOFINDIA", "SBI"), ("HOUSINGDEVELOPMENTFINANCE", "HDFC")):
        text = text.replace(long_, short)
    return text[:6]


def _bank_conflict(receipt: Receipt, credit: Credit) -> bool:
    if not receipt.bank or not credit.bank:
        return False
    return _bank_key(receipt.bank) != _bank_key(credit.bank)


def _within(receipt: Receipt, credit: Credit) -> bool:
    if receipt.receipt_date is None or credit.txn_date is None:
        return False
    return abs((credit.txn_date - receipt.receipt_date).days) <= WINDOW_DAYS


# --------------------------------------------------------------------------- matching

def in_scope(receipt: Receipt, windows: list[Window]) -> list[Window]:
    return [w for w in windows if w.covers(receipt.receipt_date, receipt.outlet_id)]


def reconcile(receipts: list[Receipt], credits: list[Credit], windows: list[Window]
              ) -> tuple[list[ReceiptResult], list[CreditResult]]:
    """Every receipt in scope gets a verdict; every credit, the receipts it stands for."""
    statements_of: dict[str, Window] = {w.statement_id: w for w in windows}
    scoped = [r for r in receipts if in_scope(r, windows) and r.amount > 0]
    results = {r.key: ReceiptResult(receipt=r, outcome=NOT_FOUND) for r in scoped}
    credit_results = {c.key: CreditResult(credit=c) for c in credits}
    free_receipts = {r.key for r in scoped}
    free_credits = {c.key for c in credits if c.kind != K_INTERNAL}
    by_key_r = {r.key: r for r in scoped}
    by_key_c = {c.key: c for c in credits}

    def eligible(r: Receipt, c: Credit) -> bool:
        window = statements_of.get(c.statement_id)
        if window is None or not window.covers(r.receipt_date, r.outlet_id):
            return False
        return _within(r, c) and _same_amount(r.amount, c.amount)

    def tier_of(r: Receipt, c: Credit) -> str | None:
        rc = mode_class(r.mode)
        if reference_hit(r, c):
            return REFERENCE
        hit = name_hit(r.customer_names, c.narration)
        if hit and not (rc == "CASH" and c.kind not in (K_CASH, K_CUSTOMER)):
            return hit
        # Without a name or a reference: the exact amount only (Rs 21,001 is not Rs 21,000).
        if r.amount != c.amount:
            return None
        if rc == "CASH":
            # cash goes to the bank the same day or within two days after the receipt
            gap = (c.txn_date - r.receipt_date).days if c.txn_date and r.receipt_date else None
            return CASH_DEPOSIT if c.kind == K_CASH and gap is not None and 0 <= gap <= 2 else None
        if rc == "CARD":
            return AMOUNT_DATE if c.kind == K_CARD else None
        if c.kind in (K_CUSTOMER, K_LOAN) and _distinctive(r.amount) and not _bank_conflict(r, c) \
                and not reference_tokens(r.reference):
            return AMOUNT_DATE
        return None

    pairs: dict[str, dict[str, str]] = {}
    for r in scoped:
        pairs[r.key] = {}
        for c in credits:
            if c.key in free_credits and eligible(r, c):
                tier = tier_of(r, c)
                if tier is not None:
                    pairs[r.key][c.key] = tier

    def assign(rk: str, cks: list[str], basis: str) -> None:
        result = results[rk]
        result.outcome, result.basis, result.credit_keys = MATCHED, basis, list(cks)
        free_receipts.discard(rk)
        for ck in cks:
            free_credits.discard(ck)
            cr = credit_results[ck]
            cr.receipt_keys.append(rk)
            cr.basis = basis
            cr.journey_id = by_key_r[rk].journey_id

    ambiguous: set[str] = set()
    for tier in (REFERENCE, NAME, NAME_PREFIX, CASH_DEPOSIT, AMOUNT_DATE):
        changed = True
        while changed:
            changed = False
            for rk in sorted(free_receipts - ambiguous):
                options = [ck for ck, t in pairs[rk].items() if t == tier and ck in free_credits]
                if not options:
                    continue
                rivals = {ck: [ok for ok in free_receipts - ambiguous if pairs[ok].get(ck) == tier] for ck in options}
                if len(options) == 1 and rivals[options[0]] == [rk]:
                    assign(rk, options, tier)
                    changed = True
                    continue
                if tier == AMOUNT_DATE:
                    continue
                # The one clearly nearest in date, each way (a same-day cash deposit
                # against a receipt of the same amount four days earlier).
                near = _nearest(by_key_r[rk], [by_key_c[ck] for ck in options])
                if near is not None:
                    contenders = [by_key_r[ok] for ok in rivals[near.key]]
                    back = _nearest_receipt(near, contenders)
                    if back is not None and back.key == rk:
                        assign(rk, [near.key], tier)
                        changed = True
                        continue
                # One customer, several receipts and credits of one amount (two IMPS of
                # Rs 1,00,000 for two receipts): tie them in date order.
                journey = by_key_r[rk].journey_id
                if all(by_key_r[ok].journey_id == journey for ck in options for ok in rivals[ck]):
                    mine = sorted({ok for ck in options for ok in rivals[ck]},
                                  key=lambda k: (by_key_r[k].receipt_date or date.max, k))
                    theirs = sorted(options, key=lambda k: (by_key_c[k].txn_date or date.max, k))
                    # only as many credits as receipts: one receipt with two such
                    # credits is ambiguous, not the earlier one
                    if mine and mine[0] == rk and len(mine) == len(theirs):
                        assign(rk, [theirs[0]], tier)
                        changed = True
        # whatever still has several options at this strength is ambiguous: a weaker
        # test must not decide what a stronger one could not
        for rk in free_receipts - ambiguous:
            options = [ck for ck, t in pairs[rk].items() if t == tier and ck in free_credits]
            if len(options) > 1 and tier != AMOUNT_DATE:
                ambiguous.add(rk)
                results[rk].candidates = options

    # One receipt paid as two or three credits; one credit for two to four receipts of the
    # same Journey -- only on a name or a reference.
    for rk in sorted(free_receipts - ambiguous, key=lambda k: -by_key_r[k].amount):
        r = by_key_r[rk]
        if mode_class(r.mode) == "CASH":
            continue
        linked = [c for c in credits if c.key in free_credits and _within(r, c)
                  and statements_of.get(c.statement_id) and statements_of[c.statement_id].covers(r.receipt_date, r.outlet_id)
                  and c.amount < r.amount and (reference_hit(r, c) or name_hit(r.customer_names, c.narration))]
        found = _subset(linked, r.amount, sizes=(2, 3), amount_of=lambda c: c.amount)
        if found:
            assign(rk, [c.key for c in found], NAME_SPLIT)
    for ck in sorted(free_credits, key=lambda k: -by_key_c[k].amount):
        c = by_key_c[ck]
        linked = [r for r in scoped if r.key in free_receipts and r.key not in ambiguous and _within(r, c)
                  and statements_of.get(c.statement_id) and statements_of[c.statement_id].covers(r.receipt_date, r.outlet_id)
                  and r.amount < c.amount and mode_class(r.mode) != "CASH"
                  and (reference_hit(r, c) or name_hit(r.customer_names, c.narration))]
        by_journey: dict[str, list[Receipt]] = {}
        for r in linked:
            by_journey.setdefault(r.journey_id, []).append(r)
        for group in by_journey.values():
            found = _subset(group, c.amount, sizes=(2, 3, 4), amount_of=lambda r: r.amount)
            if found:
                for r in found:
                    result = results[r.key]
                    result.outcome, result.basis, result.credit_keys = MATCHED, COMBINED, [ck]
                    free_receipts.discard(r.key)
                free_credits.discard(ck)
                cr = credit_results[ck]
                cr.receipt_keys = [r.key for r in found]
                cr.basis, cr.journey_id = COMBINED, found[0].journey_id
                break

    for rk in free_receipts:
        result = results[rk]
        rc = mode_class(result.receipt.mode)
        if rk in ambiguous:
            result.outcome = AMBIGUOUS
            result.note = f"{len(result.candidates)} credits of this amount could be it; none names the customer " \
                          "or carries the receipt's reference."
        elif rc == "CASH":
            result.outcome = CASH
            result.note = "Cash: deposited in batches, not traceable receipt by receipt."
        elif rc == "CARD":
            result.outcome = CARD
            result.note = "Card: settled by the card company in daily batches, net of charges."
        elif not any(w.within_period(result.receipt.receipt_date) for w in in_scope(result.receipt, windows)):
            result.outcome = OUTSIDE_DATES
            result.note = "Dated outside the statement's transaction dates."
        else:
            same = [c for c in credits if c.key in free_credits and _within(result.receipt, c)
                    and _same_amount(c.amount, result.receipt.amount)]
            result.note = (f"{len(same)} credit(s) of this amount within 5 days, none tied by reference or name."
                           if same else "No credit of this amount within 5 days on the statements on file.")
    return list(results.values()), list(credit_results.values())


def _gap(a: date | None, b: date | None) -> int:
    return abs((a - b).days) if a and b else 10_000


def _nearest(receipt: Receipt, options: list[Credit]) -> Credit | None:
    ranked = sorted(options, key=lambda c: _gap(receipt.receipt_date, c.txn_date))
    if len(ranked) == 1 or _gap(receipt.receipt_date, ranked[0].txn_date) < _gap(receipt.receipt_date,
                                                                                ranked[1].txn_date):
        return ranked[0]
    return None


def _nearest_receipt(credit: Credit, options: list[Receipt]) -> Receipt | None:
    ranked = sorted(options, key=lambda r: _gap(r.receipt_date, credit.txn_date))
    if not ranked:
        return None
    if len(ranked) == 1 or _gap(ranked[0].receipt_date, credit.txn_date) < _gap(ranked[1].receipt_date,
                                                                               credit.txn_date):
        return ranked[0]
    return None


def _subset(items: list[Any], total: Decimal, *, sizes: tuple[int, ...], amount_of: Any) -> list[Any] | None:
    if len(items) > 12:
        items = sorted(items, key=amount_of, reverse=True)[:12]
    for size in sizes:
        for combo in combinations(items, size):
            if _same_amount(sum((amount_of(x) for x in combo), Decimal(0)), total):
                return list(combo)
    return None


def name_journeys(credit: Credit, people: dict[str, list[str]]) -> list[str]:
    """The Journeys whose customer this credit's narration names (full name only)."""
    if credit.kind in (K_INTERNAL, K_CASH, K_CARD, K_INSURANCE):
        return []
    return [jid for jid, names in people.items() if name_hit(names, credit.narration) == NAME]


# --------------------------------------------------------------------------- loans

@dataclass
class Loan:
    """One Journey's loan as its papers give it."""
    journey_id: str
    outlet_id: str | None
    amounts: set[Decimal] = field(default_factory=set)
    financiers: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    disbursed: list[date] = field(default_factory=list)
    sanctioned: list[date] = field(default_factory=list)
    customer_names: list[str] = field(default_factory=list)


_FINANCIER_STOP = {"BANK", "LIMITED", "LTD", "FINANCE", "FINANCIAL", "FINANCIERS", "SERVICES", "SERVICE", "INDIA",
                   "OF", "THE", "AND", "PRIVATE", "PVT", "COMPANY", "CORPORATION", "CO", "BRANCH", "MAIN", "NBFC",
                   "CREDIT", "FINCORP", "FIN", "LOANS", "LOAN"}


def financier_words(name: Any) -> list[str]:
    """The distinctive words of a financier's name (ICICI, KOTAK, CENTRAL, CHOLAMANDALAM)."""
    return [w for w in re.findall(r"[A-Z]{3,}", str(name or "").upper()) if w not in _FINANCIER_STOP]


def _financier_hit(loan: Loan, narration: str) -> bool:
    """The narration names the loan's financier: its first distinctive word (ICICI, KOTAK,
    CENTRAL), or its initials as a word of their own (PNB, IOB, SBI)."""
    letters = _letters(narration)
    tokens = set(re.findall(r"[A-Z]+", str(narration or "").upper()))
    for name in loan.financiers:
        words = financier_words(name)
        if words and words[0] in letters:
            return True
        initials = "".join(w[0] for w in re.findall(r"[A-Z]+", str(name or "").upper())
                           if w not in {"OF", "THE", "AND", "LTD", "LIMITED", "PVT", "PRIVATE", "CO"})
        if len(initials) >= 3 and initials in tokens:
            return True
    return False


def _loan_reference_hit(loan: Loan, narration: str) -> bool:
    """A loan paper's number in the narration: as a whole token, or (10+ characters, a UTR)
    anywhere in it."""
    text_ = str(narration or "").upper()
    tokens = set(re.findall(r"[A-Z0-9]+", text_))
    flat = _alnum(text_)
    for ref in loan.references:
        key = _alnum(ref)
        if len(key) < 6 or not re.search(r"\d", key):
            continue
        if key in tokens or (len(key) >= 10 and key in flat):
            return True
    return False


def _loan_dated(loan: Loan, day: date | None) -> bool:
    """The loan was disbursed within 15 days of the credit, or sanctioned up to 60 days before it."""
    if day is None:
        return False
    return (any(abs((day - d).days) <= 15 for d in loan.disbursed)
            or any(0 <= (day - d).days <= 60 for d in loan.sanctioned))


def _loan_amount(loan: Loan, amount: Decimal) -> bool:
    return any(_same_amount(a, amount) for a in loan.amounts)


def match_loans(loans: list[Loan], receipts: list[ReceiptResult], credits: list[CreditResult],
                windows: list[Window]) -> None:
    """Tie the credits no receipt took to the Journey whose loan they are (see the module
    docstring); a tied Journey's loan receipt of that amount, not found so far, is found on
    it. Changes ``receipts`` and ``credits`` in place."""
    statements_of = {w.statement_id: w for w in windows}
    taken = {cr.journey_id for cr in credits if cr.loan}
    free = [cr for cr in credits if not cr.receipt_keys and not cr.loan
            and cr.credit.kind in (K_LOAN, K_CUSTOMER) and cr.credit.amount >= DISTINCTIVE_AMOUNT]

    def in_reach(loan: Loan, credit: Credit) -> bool:
        window = statements_of.get(credit.statement_id)
        return window is not None and (window.outlet_id is None or window.outlet_id == loan.outlet_id)

    def tier_of(loan: Loan, credit: Credit) -> str | None:
        if _loan_reference_hit(loan, credit.narration):
            return LOAN_REFERENCE
        if not _loan_amount(loan, credit.amount):
            return None
        if name_hit(loan.customer_names, credit.narration) == NAME:
            return LOAN_NAME
        if not _loan_dated(loan, credit.txn_date):
            return None
        if _financier_hit(loan, credit.narration):
            return LOAN_FINANCIER
        # the amount alone: a financier's credit only, of an amount no round figure
        return LOAN_AMOUNT if credit.kind == K_LOAN and credit.amount % 10000 != 0 else None

    options: dict[str, dict[str, str]] = {}
    for cr in free:
        options[cr.credit.key] = {}
        for loan in loans:
            if loan.journey_id not in taken and in_reach(loan, cr.credit):
                tier = tier_of(loan, cr.credit)
                if tier is not None:
                    options[cr.credit.key][loan.journey_id] = tier
    by_key = {cr.credit.key: cr for cr in free}
    with_amount = {k: [ln.journey_id for ln in loans if _loan_amount(ln, by_key[k].credit.amount)] for k in by_key}
    for tier in LOAN_TIERS:
        open_credits = [k for k in by_key if not by_key[k].loan]
        for key in open_credits:
            mine = [j for j, t in options[key].items() if t == tier and j not in taken]
            if len(mine) != 1:
                continue
            journey = mine[0]
            rivals = [k for k in open_credits if k != key and options[k].get(journey) == tier]
            if rivals:
                continue
            if tier == LOAN_AMOUNT and len(with_amount[key]) != 1:
                continue  # another loan of the dealership, dated or not, carries the amount
            cr = by_key[key]
            cr.loan, cr.basis, cr.journey_id = True, tier, journey
            taken.add(journey)
            for result in receipts:
                if (result.receipt.journey_id == journey and result.outcome in (NOT_FOUND, AMBIGUOUS)
                        and _same_amount(result.receipt.amount, cr.credit.amount)):
                    result.outcome, result.basis, result.credit_keys = MATCHED, tier, [key]
                    result.candidates, result.note = [], None
                    cr.receipt_keys.append(result.receipt.key)
                    break


__all__ = [
    "AMBIGUOUS",
    "CARD",
    "CASH",
    "LOAN_AMOUNT",
    "LOAN_FINANCIER",
    "LOAN_NAME",
    "LOAN_REFERENCE",
    "LOAN_TIERS",
    "MATCHED",
    "NOT_FOUND",
    "OUTSIDE_DATES",
    "WINDOW_DAYS",
    "Credit",
    "CreditResult",
    "Loan",
    "Receipt",
    "ReceiptResult",
    "Window",
    "classify_credit",
    "financier_words",
    "in_scope",
    "match_loans",
    "mode_class",
    "name_hit",
    "name_journeys",
    "reconcile",
    "reference_hit",
    "reference_tokens",
]
