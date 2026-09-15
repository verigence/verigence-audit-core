"""uc03_payment_mode.py — canonical payment-mode classification.

DI's own extraction fields (``payment_receipt``/``dealer_receipt``'s
``payment_mode``, ``bank_statement_extract``'s ``transaction_description`` +
``reference_no``) stay free text -- audit evidence, exactly as printed, never
constrained at the extraction step. This module is the one place raw text
gets classified into a small, closed set of canonical payment mode types for
reporting/analytics, backed by ``auditcore.payment_mode_types`` (see
migration 0096_uc03_payment_mode_types.py). Every payment and every bank
statement line always resolves to exactly one of these codes -- ``OTHERS``
when nothing else matches, never null/unclassified.

Not a canonical type here: UPI, card, POS -- none appeared in the requested
list. They fall through to OTHERS today; add a dedicated token+code pair
here (and a matching row in the migration/backfill) if that turns out to be
wrong. DD/Demand Draft is folded into CHEQUE (a negotiable bank instrument in
the same practical category), also not its own line in the requested list.
"""
from __future__ import annotations

import re
from typing import Any

# Ordered (code, label) pairs -- also the seed order for payment_mode_types
# and the row order shown wherever this list is rendered (e.g. a dropdown).
PAYMENT_MODE_TYPES: tuple[tuple[str, str], ...] = (
    ("IMPS", "IMPS"),
    ("RTGS", "RTGS"),
    ("NEFT", "NEFT"),
    ("BANK_TRANSFER", "Bank Transfer"),
    ("BANKERS_ORDER", "Banker's Order (BO)"),
    ("PAY_ORDER", "Pay Order (PO)"),
    ("CASH", "Cash"),
    ("CHEQUE", "Cheque"),
    ("TRADE_IN", "Trade-In"),
    ("REFUND", "Refund"),
    ("OTHERS", "Others"),
)

PAYMENT_MODE_CODES: frozenset[str] = frozenset(code for code, _ in PAYMENT_MODE_TYPES)

_OTHERS = "OTHERS"

_WORD_SPLIT = re.compile(r"[^A-Z0-9]+")

# Checked in this order -- IMPS/RTGS/NEFT are checked before the generic
# BANK_TRANSFER tokens so "NEFT transfer" resolves to NEFT, not BANK_TRANSFER.
# Each rule's tokens are matched as a SUBSTRING of the compact (punctuation-
# and space-stripped) form, EXCEPT tokens listed in `whole_word` for that
# rule, which are matched only as a standalone word -- "PO"/"DD"/"BO" are
# too short to safely match as a substring (a reference number's own digits
# or an unrelated word could contain them), so those are only recognized
# when they stand alone as a full word once punctuation/spacing is treated
# as a word boundary (e.g. "Mode: PO" -> word "PO"; "PO12345" as one fused
# alphanumeric token is deliberately NOT matched -- ambiguous either way).
_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("IMPS", ("IMPS",), ()),
    ("RTGS", ("RTGS",), ()),
    ("NEFT", ("NEFT",), ()),
    ("BANKERS_ORDER", ("BANKERSORDER", "BANKERORDER", "BANKERSCHEQUE"), ("BO",)),
    ("PAY_ORDER", ("PAYORDER",), ("PO",)),
    ("CHEQUE", ("CHEQUE", "CHQ", "DEMANDDRAFT"), ("DD",)),
    ("CASH", ("CASH",), ()),
    ("TRADE_IN", ("TRADEIN",), ()),
    ("REFUND", ("REFUND",), ()),
    ("BANK_TRANSFER", ("BANKTRANSFER", "NETBANKING", "ONLINETRANSFER", "FUNDTRANSFER", "TRANSFER"), ()),
)


def _compact(value: Any) -> str:
    """Uppercased, punctuation/space stripped -- for substring matching of
    tokens long/specific enough that fusing words together is safe and
    actually desirable ("Pay Order" and "PayOrder" should match the same
    way)."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _words(value: Any) -> set[str]:
    """Uppercased and split on any run of non-alphanumeric characters -- for
    whole-word matching of short, ambiguous tokens ("PO", "DD", "BO")."""
    return set(_WORD_SPLIT.split(str(value or "").upper()))


def classify_payment_mode(*raw_values: Any) -> str:
    """Classify one or more raw text fields (payment_mode, a bank line's
    transaction_description + reference_no, etc.) into a canonical
    ``payment_mode_types.code``. Checks each value in order and returns the
    first match; ``OTHERS`` when none match or every value is empty."""
    for raw in raw_values:
        compact = _compact(raw)
        if not compact:
            continue
        words = _words(raw)
        for code, substr_tokens, whole_word_tokens in _RULES:
            if any(token in compact for token in substr_tokens):
                return code
            if any(token in words for token in whole_word_tokens):
                return code
    return _OTHERS
