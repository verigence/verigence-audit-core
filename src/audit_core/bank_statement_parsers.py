"""bank_statement_parsers.py -- read a dealership's own bank account statement,
whatever bank it comes from, into its transactions (product owner 2026-11-14).

"bank statement payment reconcile have to done.. this could be per outlet or per
dealership, file could be .pdf, or excel.. different bank will have different
format."

No bank-specific template: every format is read by what any account statement has.

* Excel (.xlsx, .xls, the HTML ".xls" some banks export, .csv): the header row is
  found by its names -- a date, a narration and either debit / credit columns or one
  amount with a Cr/Dr column -- and each column is taken by any of its known names
  (ICICI CIB "OpTransactionHistory": Value Date, Description, Cr/Dr, Transaction
  Amount(INR)).
* PDF with text (Axis "Account Statement Report", HDFC "Statement of account"): the
  page's words are grouped into lines; a line printing a running balance starts a
  transaction, and the lines after it (until the next) are its narration -- Axis
  prints the amount on the narration's first line and the date on its middle line,
  HDFC the date, narration and amounts on one line with the narration running on.
  Whether an amount is a credit or a debit is read off the balance, not the column
  it sits in: previous balance + amount = this balance is a credit, - amount a
  debit. The same arithmetic checks the reading: a chain that does not add up is
  reported, never silently accepted.
* A PDF with no text (a scan) cannot be read here: ``ScannedPdf`` is raised so the
  caller can say so.

Only what is printed is kept: the statement's own account number, holder, period and
totals; per transaction the date, narration, reference / cheque number, amount and
direction, and balance.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

CREDIT, DEBIT = "CR", "DR"
MAX_TRANSACTIONS = 20000


class StatementUnreadable(ValueError):
    """The file is not a bank statement this reader understands (message says why)."""


class ScannedPdf(StatementUnreadable):
    """A PDF with no text layer (a scan or a photo)."""


@dataclass
class Transaction:
    txn_date: date | None
    value_date: date | None
    narration: str
    reference: str | None
    cheque_number: str | None
    amount: Decimal
    direction: str  # CR / DR
    balance: Decimal | None
    bank_txn_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Statement:
    file_format: str
    bank_name: str | None
    account_number: str | None
    account_holder: str | None
    ifsc: str | None
    period_from: date | None
    period_to: date | None
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    transactions: list[Transaction] = field(default_factory=list)
    # The statement's own totals, when it prints them, and whether the reading agrees.
    printed_credit_total: Decimal | None = None
    printed_debit_total: Decimal | None = None
    balance_breaks: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def credit_total(self) -> Decimal:
        return sum((t.amount for t in self.transactions if t.direction == CREDIT), Decimal(0))

    @property
    def debit_total(self) -> Decimal:
        return sum((t.amount for t in self.transactions if t.direction == DEBIT), Decimal(0))

    def check(self) -> str:
        """VERIFIED: the statement's printed totals (or its balances) agree with what
        was read; UNCHECKED: nothing printed to check against; MISMATCH otherwise."""
        checks: list[bool] = []
        if self.printed_credit_total is not None:
            checks.append(abs(self.printed_credit_total - self.credit_total) < Decimal("0.01"))
        if self.printed_debit_total is not None:
            checks.append(abs(self.printed_debit_total - self.debit_total) < Decimal("0.01"))
        if self.opening_balance is not None and self.closing_balance is not None and self.transactions:
            expected = self.opening_balance + self.credit_total - self.debit_total
            checks.append(abs(expected - self.closing_balance) < Decimal("0.01"))
        if self.balance_breaks:
            checks.append(False)
        if not checks:
            return "UNCHECKED"
        return "VERIFIED" if all(checks) else "MISMATCH"


# --------------------------------------------------------------------------- shared helpers

_MONEY = re.compile(r"^\(?-?(?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2}\)?(?:\s*\(?(?:CR|DR|Cr|Dr|cr|dr)\)?)?$")
_DATE_TOKEN = re.compile(
    r"^(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}[\-/ ](?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*[\-/ ]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2})$",
    re.IGNORECASE,
)
_DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y", "%d-%b-%Y", "%d-%b-%y",
                 "%d %b %Y", "%d %b %y", "%d/%b/%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S",
                 "%d/%m/%Y %I:%M:%S %p", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S")
_IFSC = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
_IFSC_BANKS = {
    "UTIB": "Axis Bank", "HDFC": "HDFC Bank", "ICIC": "ICICI Bank", "SBIN": "State Bank of India",
    "KKBK": "Kotak Mahindra Bank", "PUNB": "Punjab National Bank", "BARB": "Bank of Baroda",
    "CNRB": "Canara Bank", "UBIN": "Union Bank of India", "IOBA": "Indian Overseas Bank", "UCBA": "UCO Bank",
    "IDIB": "Indian Bank", "BKID": "Bank of India", "CBIN": "Central Bank of India", "INDB": "IndusInd Bank",
    "YESB": "Yes Bank", "FDRL": "Federal Bank", "IDFB": "IDFC First Bank", "MAHB": "Bank of Maharashtra",
    "PSIB": "Punjab & Sind Bank", "UTKS": "Utkarsh Small Finance Bank", "AUBL": "AU Small Finance Bank",
    "RATN": "RBL Bank", "KARB": "Karnataka Bank", "SIBL": "South Indian Bank", "DBSS": "DBS Bank",
    "ESFB": "Equitas Small Finance Bank", "JAKA": "J&K Bank", "TMBL": "Tamilnad Mercantile Bank",
}
_BANK_WORDS = (
    ("AXIS BANK", "Axis Bank"), ("HDFC BANK", "HDFC Bank"), ("ICICI BANK", "ICICI Bank"),
    ("STATE BANK OF INDIA", "State Bank of India"), ("KOTAK MAHINDRA BANK", "Kotak Mahindra Bank"),
    ("PUNJAB NATIONAL BANK", "Punjab National Bank"), ("BANK OF BARODA", "Bank of Baroda"),
    ("CANARA BANK", "Canara Bank"), ("UNION BANK OF INDIA", "Union Bank of India"),
    ("INDIAN OVERSEAS BANK", "Indian Overseas Bank"), ("UCO BANK", "UCO Bank"), ("INDUSIND BANK", "IndusInd Bank"),
    ("YES BANK", "Yes Bank"), ("FEDERAL BANK", "Federal Bank"), ("IDFC FIRST", "IDFC First Bank"),
    ("BANK OF INDIA", "Bank of India"), ("INDIAN BANK", "Indian Bank"), ("CENTRAL BANK OF INDIA", "Central Bank of India"),
)


def parse_money(value: Any) -> tuple[Decimal | None, str | None]:
    """(amount, 'CR' / 'DR' / None) of a printed amount: '1,08,93,028.88', '547,986.67',
    '1,234.00 Cr', '(500.00)'."""
    if value is None or value == "":
        return None, None
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = Decimal(str(value))
        return (abs(number), DEBIT if number < 0 else None)
    raw = str(value).strip()
    side = None
    found = re.search(r"\b(CR|DR)\b\.?$", raw, re.IGNORECASE)
    if found:
        side = found.group(1).upper()
        raw = raw[: found.start()]
    negative = raw.startswith("(") and raw.endswith(")") or raw.startswith("-")
    raw = raw.strip("()- ").replace(",", "").replace("₹", "").replace("INR", "").strip()
    if not raw or raw in {".", "-"}:
        return None, None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None, None
    if negative and side is None:
        side = DEBIT
    return abs(number), side


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = re.sub(r"\s+", " ", str(value)).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()  # noqa: DTZ007 -- a calendar day
        except ValueError:
            continue
    head = raw.split(" ")[0]
    if head != raw:
        return parse_date(head)
    return None


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def _norm_header(value: Any) -> str:
    return re.sub(r"[^A-Z/ ]", "", _norm(value).replace(".", " ")).strip()


def bank_from_text(text: str, file_name: str = "") -> tuple[str | None, str | None]:
    """(bank name, IFSC) from the statement's own text: the account's IFSC first (the
    first IFSC printed in the header block, not a counterparty's), then the bank's name."""
    head = text.upper()
    ifsc_found = re.search(r"IFSC(?:\s*CODE)?\s*[:\-]?\s*([A-Z]{4}0[A-Z0-9]{6})", head) \
        or re.search(r"(?:RTGS|NEFT)[^A-Z0-9]{0,20}IFSC\s*[:\-]?\s*([A-Z]{4}0[A-Z0-9]{6})", head)
    ifsc = ifsc_found.group(1) if ifsc_found else None
    if ifsc and ifsc[:4] in _IFSC_BANKS:
        return _IFSC_BANKS[ifsc[:4]], ifsc
    for word, name in _BANK_WORDS:
        if re.search(rf"STATEMENT OF {word}|{word}\s+(?:LTD|LIMITED)?\s*(?:ACCOUNT|STATEMENT)", head):
            return name, ifsc
    if "OPTRANSACTIONHISTORY" in file_name.upper().replace(" ", ""):
        return "ICICI Bank", ifsc  # ICICI Corporate Internet Banking's export name
    return None, ifsc


def account_from_text(text: str) -> str | None:
    head = text.upper()
    for pattern in (r"ACCOUNT\s*(?:NO|NUMBER)\.?\s*[:\-]?\s*([0-9X*]{6,20})",
                    r"A/?C\.?\s*(?:NO|NUMBER)\.?\s*[:\-]?\s*([0-9X*]{6,20})",
                    r"\(INR\)\s*-\s*([0-9]{8,20})"):
        found = re.search(pattern, head)
        if found:
            return found.group(1)
    runs = re.findall(r"(?<![0-9])([0-9]{11,18})(?![0-9])", head)
    return runs[0] if runs else None


def period_from_text(text: str) -> tuple[date | None, date | None]:
    found = re.search(r"FROM\s*:?\s*([0-9]{1,2}[/\-.][0-9]{1,2}[/\-.][0-9]{2,4}|[0-9]{1,2}[\- ][A-Z]{3}[\- ][0-9]{2,4})"
                      r"\s*(?:TO|-)\s*:?\s*([0-9]{1,2}[/\-.][0-9]{1,2}[/\-.][0-9]{2,4}|[0-9]{1,2}[\- ][A-Z]{3}[\- ][0-9]{2,4})",
                      text.upper())
    if not found:
        found = re.search(r"PERIOD\s*:?\s*([0-9/\-.]{8,10})\s*(?:TO|-)\s*([0-9/\-.]{8,10})", text.upper())
    if not found:
        return None, None
    return parse_date(found.group(1).title()), parse_date(found.group(2).title())


def holder_from_lines(lines: list[str]) -> str | None:
    """The account holder: the first line of the statement's head that reads like an
    entity's name (M/S. ADITYA MOTORS, ADITYA CAR AUTOMOTIVES PRIVATE LIMITED)."""
    for line in lines[:40]:
        text = _norm(line)
        found = re.search(r"TRANSACTIONS LIST\s*-[\s-]*(.+?)\s*\(INR\)", text)
        if found:
            return found.group(1).strip(" -")[:200]
        found = re.search(r"\b(M/S\.?\s+[A-Z0-9&.,()' -]{3,})", text)
        if found:
            return re.split(r"\s{2,}|ACCOUNT BRANCH|CITY|STATE", found.group(1))[0].strip()[:200]
        if re.search(r"\b(PRIVATE LIMITED|PVT\.? LTD|LIMITED|MOTORS|AUTOMOTIVES?|ENTERPRISES)\b", text) \
                and not re.search(r"BANK|STATEMENT|BRANCH|REGISTERED OFFICE", text):
            return re.split(r"\s{3,}", text)[0][:200]
    return None


# Reference numbers the narration carries: NEFT / RTGS UTRs (SBIN526308595478,
# UCBAR52026110400724965), IMPS / UPI RRNs (12 digits), cheque numbers.
_UTR = re.compile(r"\b([A-Z]{4}[A-Z0-9]?[0-9]{8,22}|[A-Z]{4}H[0-9]{8,14})\b")
_RRN = re.compile(r"(?<![0-9])([0-9]{12})(?![0-9])")


def references_in(narration: str) -> list[str]:
    text = _norm(narration)
    out = [m.group(1) for m in _UTR.finditer(text)]
    out += [m.group(1) for m in _RRN.finditer(text)]
    seen: set[str] = set()
    return [r for r in out if not (r in seen or seen.add(r))]


def statement_from_rows(file_format: str, rows: list[list[Any]], file_name: str) -> Statement:
    """A tabular statement (Excel, CSV, HTML table): find the header, read the rows."""
    header_at, columns = _find_header(rows)
    if header_at is None:
        raise StatementUnreadable(
            "No transaction table was found: a bank statement has a header row naming the date, the narration "
            "(description / particulars) and the amounts (debit / credit, or an amount with Cr/Dr).")
    preamble = [" ".join(str(c) for c in r if c not in (None, "")) for r in rows[:header_at]]
    head_text = "\n".join(preamble)
    bank, ifsc = bank_from_text(head_text, file_name)
    period = period_from_text(head_text)
    statement = Statement(
        file_format=file_format, bank_name=bank, account_number=account_from_text(head_text),
        account_holder=holder_from_lines(preamble), ifsc=ifsc, period_from=period[0], period_to=period[1],
        opening_balance=None, closing_balance=None,
    )
    for r in rows[header_at + 1:]:
        if len(statement.transactions) >= MAX_TRANSACTIONS:
            break
        txn = _txn_from_row(r, columns)
        if txn is not None:
            statement.transactions.append(txn)
    _finish(statement)
    return statement


_HEADER_NAMES: dict[str, tuple[str, ...]] = {
    "txn_date": ("TRANSACTION DATE", "TXN DATE", "TRAN DATE", "DATE", "TXN POSTED DATE", "POST DATE", "POSTING DATE",
                 "BOOK DATE"),
    "value_date": ("VALUE DATE", "VALUE DT", "VAL DATE"),
    "narration": ("DESCRIPTION", "NARRATION", "PARTICULARS", "TRANSACTION REMARKS", "REMARKS", "DETAILS",
                  "TRANSACTION DETAILS", "TRANSACTION DESCRIPTION", "TRANSACTION PARTICULARS"),
    "reference": ("REF NO", "REFERENCE NO", "REFERENCE NUMBER", "CHQ/REF NO", "CHQ / REF NO", "REF NO/CHEQUE NO",
                  "UTR NO", "UTR NUMBER", "CHEQUE/REFERENCE NO"),
    "cheque": ("CHEQUENO", "CHEQUE NO", "CHQ NO", "CHEQUE NUMBER", "INSTRUMENT NO", "CHQNO"),
    "bank_txn_id": ("TRANSACTION ID", "TRAN ID", "TXN ID"),
    "debit": ("DEBIT", "WITHDRAWAL", "WITHDRAWALS", "WITHDRAWAL AMT", "DEBIT AMOUNT", "DEBIT AMOUNTINR",
              "WITHDRAWAL AMOUNT", "DR AMOUNT", "DR"),
    "credit": ("CREDIT", "DEPOSIT", "DEPOSITS", "DEPOSIT AMT", "CREDIT AMOUNT", "CREDIT AMOUNTINR",
               "DEPOSIT AMOUNT", "CR AMOUNT", "CR"),
    "amount": ("TRANSACTION AMOUNT", "TRANSACTION AMOUNTINR", "AMOUNT", "AMOUNTINR", "TXN AMOUNT", "AMOUNT INR"),
    "side": ("CR/DR", "DR/CR", "TYPE", "CR DR", "DR CR", "DEBIT/CREDIT"),
    "balance": ("BALANCE", "BALANCEINR", "CLOSING BALANCE", "RUNNING BALANCE", "AVAILABLE BALANCE", "BALANCE AMT"),
}


def _header_key(name: str) -> str | None:
    compact = name.replace(" ", "")
    for key, names in _HEADER_NAMES.items():
        for alias in names:
            if name == alias or compact == alias.replace(" ", ""):
                return key
    return None


def _find_header(rows: list[list[Any]]) -> tuple[int | None, dict[str, int]]:
    for at, row in enumerate(rows[:60]):
        columns: dict[str, int] = {}
        for i, cell in enumerate(row):
            key = _header_key(_norm_header(cell))
            if key and key not in columns:
                columns[key] = i
        has_date = "txn_date" in columns or "value_date" in columns
        has_amount = ("debit" in columns or "credit" in columns) or ("amount" in columns and "side" in columns) \
            or "amount" in columns
        if has_date and has_amount and "narration" in columns:
            return at, columns
    return None, {}


def _cell(row: list[Any], columns: dict[str, int], key: str) -> Any:
    i = columns.get(key)
    return row[i] if i is not None and i < len(row) else None


def _txn_from_row(row: list[Any], columns: dict[str, int]) -> Transaction | None:
    txn_date = parse_date(_cell(row, columns, "txn_date"))
    value_date = parse_date(_cell(row, columns, "value_date"))
    if txn_date is None and value_date is None:
        return None
    narration = re.sub(r"\s+", " ", str(_cell(row, columns, "narration") or "")).strip()
    if re.search(r"OPENING BALANCE|CLOSING BALANCE|BROUGHT FORWARD|CARRIED FORWARD", narration.upper()):
        return None
    debit, _ = parse_money(_cell(row, columns, "debit"))
    credit, _ = parse_money(_cell(row, columns, "credit"))
    amount, side = None, None
    if credit:
        amount, side = credit, CREDIT
    elif debit:
        amount, side = debit, DEBIT
    elif "amount" in columns:
        amount, printed_side = parse_money(_cell(row, columns, "amount"))
        side_text = _norm(_cell(row, columns, "side"))
        side = CREDIT if side_text.startswith("C") else DEBIT if side_text.startswith("D") else printed_side
    if not amount or side is None:
        return None
    balance, balance_side = parse_money(_cell(row, columns, "balance"))
    if balance is not None and balance_side == DEBIT:
        balance = -balance
    cheque = _clean_ref(_cell(row, columns, "cheque"))
    reference = _clean_ref(_cell(row, columns, "reference"))
    return Transaction(
        txn_date=txn_date or value_date, value_date=value_date or txn_date, narration=narration[:1000],
        reference=reference, cheque_number=cheque, amount=amount, direction=side, balance=balance,
        bank_txn_id=_clean_ref(_cell(row, columns, "bank_txn_id")),
    )


def _clean_ref(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = re.sub(r"\s+", "", str(value)).strip(" -")
    return text[:80] if text and text not in {"-", "0", "NA", "N/A"} else None


def _finish(statement: Statement) -> None:
    dates = [t.txn_date for t in statement.transactions if t.txn_date]
    if statement.period_from is None and dates:
        statement.period_from = min(dates)
    if statement.period_to is None and dates:
        statement.period_to = max(dates)


# --------------------------------------------------------------------------- Excel / CSV / HTML

def read_excel_rows(data: bytes, file_name: str) -> tuple[str, list[list[Any]]]:
    """(format, rows) of the statement's sheet: the sheet that holds a transaction table."""
    head = data[:8]
    lower = file_name.lower()
    if head.startswith(b"PK"):
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise StatementUnreadable("The file could not be read as an Excel workbook.") from exc
        try:
            best: list[list[Any]] = []
            for sheet in workbook.worksheets:
                rows = [list(r) for _, r in zip(range(MAX_TRANSACTIONS + 80), sheet.iter_rows(values_only=True))]
                if _find_header(rows)[0] is not None and len(rows) > len(best):
                    best = rows
            return "XLSX", best
        finally:
            workbook.close()
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        try:
            import xlrd
        except ImportError as exc:  # pragma: no cover -- a deployment without xlrd
            raise StatementUnreadable("Old Excel (.xls) files cannot be read on this server yet.") from exc
        try:
            book = xlrd.open_workbook(file_contents=data)
        except Exception as exc:
            raise StatementUnreadable("The file could not be read as an Excel (.xls) workbook.") from exc
        best = []
        for sheet in book.sheets():
            rows = []
            for r in range(min(sheet.nrows, MAX_TRANSACTIONS + 80)):
                row = []
                for c in sheet.row(r):
                    value = c.value
                    if c.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate.xldate_as_datetime(c.value, book.datemode)
                        except (ValueError, OverflowError, xlrd.xldate.XLDateError):
                            value = c.value  # keep the raw number
                    row.append(value)
                rows.append(row)
            if _find_header(rows)[0] is not None and len(rows) > len(best):
                best = rows
        return "XLS", best
    text = _decode(data)
    if re.search(r"<\s*(table|html)", text[:4000], re.IGNORECASE):
        return "HTML_XLS", _html_rows(text)
    if lower.endswith((".csv", ".txt")) or "," in text[:2000] or "\t" in text[:2000]:
        dialect = "excel-tab" if text[:2000].count("\t") > text[:2000].count(",") else "excel"
        return "CSV", [list(r) for r in csv.reader(io.StringIO(text), dialect=dialect)]
    raise StatementUnreadable("The file is not an Excel, CSV or PDF statement.")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


class _Table(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _html_rows(text: str) -> list[list[Any]]:
    parser = _Table()
    parser.feed(text)
    return parser.rows


# --------------------------------------------------------------------------- PDF

@dataclass
class _Line:
    page: int
    top: float
    words: list[dict[str, Any]]

    @property
    def text(self) -> str:
        return " ".join(w["text"] for w in self.words)


_FOOTER = re.compile(r"TRANSACTION TOTAL|STATEMENT SUMMARY|CLOSING BALANCE|END OF REPORT|END OF STATEMENT|"
                     r"CHEQUE RETURN DETAILS|\*{3,} ?END|TOTAL\s+DR/CR", re.IGNORECASE)


def _pdf_lines(pdf: Any) -> list[_Line]:
    out: list[_Line] = []
    for number, page in enumerate(pdf.pages, start=1):
        # Some banks print each character twice (a bold effect): drop the copies.
        page = page.dedupe_chars() if hasattr(page, "dedupe_chars") else page
        words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
        words.sort(key=lambda w: (round(w["top"]), w["x0"]))
        current: list[dict[str, Any]] = []
        top = None
        for w in words:
            if top is None or abs(w["top"] - top) > 2.5:
                if current:
                    out.append(_Line(number, top or 0.0, sorted(current, key=lambda x: x["x0"])))
                current, top = [w], w["top"]
            else:
                current.append(w)
        if current:
            out.append(_Line(number, top or 0.0, sorted(current, key=lambda x: x["x0"])))
    return out


def _is_money(word: str) -> bool:
    return bool(_MONEY.match(word.strip()))


def _pdf_header(lines: list[_Line]) -> dict[str, tuple[float, float]] | None:
    """The transaction table's columns, from its header line(s): name -> (x0, x1)."""
    for i, line in enumerate(lines):
        text = line.text.upper()
        if not re.search(r"\b(DATE|DT)\b", text):
            continue
        if not re.search(r"PARTICULARS|NARRATION|DESCRIPTION|DETAILS|REMARKS", text):
            continue
        if not re.search(r"DEBIT|WITHDRAWAL|CREDIT|DEPOSIT|AMOUNT|BALANCE", text):
            continue
        cols: dict[str, tuple[float, float]] = {}
        for w in line.words:
            word = re.sub(r"[^A-Z./]", "", w["text"].upper())
            key = None
            if word.startswith(("PARTICULAR", "NARRATION", "DESCRIPTION", "DETAILS", "REMARKS")):
                key = "narration"
            elif word.startswith(("CHQ", "CHEQUE", "REF", "INSTRUMENT")):
                key = "reference"
            elif word.startswith(("DEBIT", "WITHDRAWAL")):
                key = "debit"
            elif word.startswith(("CREDIT", "DEPOSIT")):
                key = "credit"
            elif word.startswith(("BALANCE", "CLOSING")):
                key = "balance"
            elif word.startswith("BRANCH"):
                key = "branch"
            if key and key not in cols:
                cols[key] = (w["x0"], w["x1"])
        if "narration" in cols:
            left = [w["x1"] for w in line.words if w["x1"] < cols["narration"][0]]
            out = {"__line__": (float(i), float(line.page)), **cols}
            if left:
                out["__left__"] = (max(left) + 2, 0.0)
            return out
    return None


def _first_money_after(lines: list[_Line], pattern: str) -> Decimal | None:
    for i, line in enumerate(lines):
        upper = line.text.upper()
        found = re.search(pattern, upper)
        if not found:
            continue
        tail = upper[found.end():]
        for token in tail.split():
            if _is_money(token):
                return parse_money(token)[0]
        if i + 1 < len(lines):
            # the label row of a summary table, the figures on the next line, same column
            label_x = next((w["x0"] for w in line.words if re.search(pattern.split("\\s")[0], w["text"].upper())), None)
            below = [w for w in lines[i + 1].words if _is_money(w["text"])]
            if below:
                if label_x is not None:
                    below.sort(key=lambda w: abs(w["x0"] - label_x))
                return parse_money(below[0]["text"])[0]
    return None


def _narration_range(header: dict[str, tuple[float, float]]) -> tuple[float, float]:
    # The narration column starts just after the column to its left (its label is often
    # centred over the column: HDFC's "Narration" sits well right of the text's start).
    x0 = header.get("__left__", (header["narration"][0] - 4, 0.0))[0]
    rights = [v[0] for k, v in header.items()
              if k not in ("narration", "__line__", "__left__") and v[0] > header["narration"][0]]
    x1 = min(rights) - 2 if rights else 10_000.0
    return x0, x1


def statement_from_pdf(data: bytes, file_name: str) -> Statement:
    import pdfplumber

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        raise StatementUnreadable("The PDF could not be opened (is it password protected?).") from exc
    with pdf:
        if not pdf.pages:
            raise StatementUnreadable("The PDF has no pages.")
        lines = _pdf_lines(pdf)
    if sum(len(line.words) for line in lines) < 20:
        raise ScannedPdf("This PDF has no readable text (a scan or photo). Upload the bank's downloaded "
                         "PDF or Excel statement instead.")
    header = _pdf_header(lines)
    if header is None:
        raise StatementUnreadable("No transaction table was found in this PDF (no header row with the date, "
                                  "particulars / narration and the amounts).")
    head_end = int(header["__line__"][0])
    head_text = "\n".join(line.text for line in lines[:head_end])
    whole_text = "\n".join(line.text for line in lines)
    bank, ifsc = bank_from_text(head_text or whole_text, file_name)
    period = period_from_text(head_text or whole_text)
    statement = Statement(
        file_format="PDF", bank_name=bank, account_number=account_from_text(head_text),
        account_holder=holder_from_lines([line.text for line in lines[:head_end]]), ifsc=ifsc,
        period_from=period[0], period_to=period[1],
        opening_balance=_first_money_after(lines, r"OPENING\s*BAL(?:ANCE)?"),
        closing_balance=None,
    )
    nx0, nx1 = _narration_range(header)
    ref_range = None
    if "reference" in header:
        rx0 = header["reference"][0] - 6
        rights = [v[0] for k, v in header.items()
                  if k not in ("__line__", "__left__") and v[0] > header["reference"][0] + 1]
        ref_range = (rx0, min(rights) - 2 if rights else 10_000.0)

    # Lines of the table: after each page's header (a page with none continues the
    # table from its top), up to the statement's totals.
    header_pages: dict[int, int] = {}
    for i, line in enumerate(lines):
        upper = line.text.upper()
        if line.page not in header_pages and re.search(r"\b(DATE|DT)\b", upper) \
                and re.search(r"PARTICULARS|NARRATION|DESCRIPTION|DETAILS|REMARKS", upper) \
                and re.search(r"DEBIT|WITHDRAWAL|CREDIT|DEPOSIT|BALANCE", upper):
            header_pages[line.page] = i
    blocks: list[list[_Line]] = []
    stopped = False
    for i, line in enumerate(lines):
        if stopped:
            break
        start = header_pages.get(line.page)
        if start is not None and i <= start + 2 and (i <= start or _is_header_continuation(line)):
            continue
        if start is None and line.page < lines[head_end].page:
            continue
        upper = line.text.upper()
        if _FOOTER.search(upper):
            closing = _money_on_line(line, r"CLOSING\s*BAL(?:ANCE)?")
            if closing is not None:
                statement.closing_balance = closing
            totals = [parse_money(w["text"])[0] for w in line.words if _is_money(w["text"])]
            if re.search(r"TRANSACTION TOTAL|TOTAL\s+DR/CR", upper):
                if len(totals) < 2 and blocks and len(blocks[-1]) == 1 and not any(
                        _DATE_TOKEN.match(w["text"]) for w in blocks[-1][0].words):
                    # The totals' figures printed on the line above their label
                    # (Axis, when the branch name wraps): not a transaction.
                    totals = [parse_money(w["text"])[0] for w in blocks.pop()[0].words if _is_money(w["text"])]
                if len(totals) >= 2:
                    statement.printed_debit_total, statement.printed_credit_total = totals[0], totals[1]
            if re.search(r"TRANSACTION TOTAL|STATEMENT SUMMARY|END OF|CHEQUE RETURN", upper):
                stopped = True
            continue
        if re.search(r"^PAGE\s*(NO)?\.?\s*:?\s*\d+|GENERATED (ON|BY)|THIS IS A (SYSTEM|COMPUTER)", upper):
            continue
        monies = [w for w in line.words if _is_money(w["text"]) and w["x0"] > nx1 - 2]
        if monies:
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    if statement.closing_balance is None:
        statement.closing_balance = _summary_closing(lines)
    # HDFC's summary prints the totals under its labels.
    summary_debits = _summary_figure(lines, "DEBITS")
    summary_credits = _summary_figure(lines, "CREDITS")
    if statement.printed_credit_total is None and summary_credits is not None:
        statement.printed_credit_total = summary_credits
    if statement.printed_debit_total is None and summary_debits is not None:
        statement.printed_debit_total = summary_debits

    previous = statement.opening_balance
    for block in blocks:
        if len(statement.transactions) >= MAX_TRANSACTIONS:
            break
        first = block[0]
        figures = [w for w in first.words if _is_money(w["text"]) and w["x0"] > nx1 - 2]
        values = [(parse_money(w["text"]), w) for w in figures]
        narration = " ".join(
            w["text"] for line in block for w in line.words
            if w["x0"] >= nx0 and w["x1"] <= nx1 + 2 and not _is_money(w["text"])
        )
        narration = re.sub(r"\s+", " ", narration).strip()
        dates = [parse_date(w["text"]) for line in block for w in line.words
                 if w["x1"] < nx0 + 2 and _DATE_TOKEN.match(w["text"])]
        dates += [parse_date(w["text"]) for line in block for w in line.words
                  if w["x0"] >= nx0 and _DATE_TOKEN.match(w["text"]) and not dates]
        dates = [d for d in dates if d]
        if len(values) == 1:
            # A balance alone: the opening balance (or a brought-forward line).
            balance = values[0][0][0]
            if balance is not None and ("OPENING" in narration.upper() or previous is None
                                        or "FORWARD" in narration.upper()):
                if statement.opening_balance is None:
                    statement.opening_balance = balance
                previous = balance
            continue
        nonzero = [(v, w) for v, w in values if v[0]]
        if len(nonzero) < 2:
            continue
        (balance, balance_side), _ = nonzero[-1]
        if balance_side == DEBIT and balance is not None:
            balance = -balance
        amount_entries = nonzero[:-1]
        amount = amount_entries[-1][0][0]
        direction = None
        if previous is not None and amount is not None and balance is not None:
            if abs(previous + amount - balance) < Decimal("0.01"):
                direction = CREDIT
            elif abs(previous - amount - balance) < Decimal("0.01"):
                direction = DEBIT
        if direction is None and amount is not None:
            direction = _side_by_column(amount_entries[-1][1], header)
            if previous is not None:
                statement.balance_breaks += 1
        if amount is None or direction is None:
            continue
        reference = None
        if ref_range is not None:
            refs = [w["text"] for line in block for w in line.words
                    if w["x0"] >= ref_range[0] and w["x1"] <= ref_range[1] + 4 and not _is_money(w["text"])
                    and not _DATE_TOKEN.match(w["text"])]
            reference = _clean_ref("".join(refs)) if refs else None
        cheque = reference if reference and reference.isdigit() and len(reference) <= 10 else None
        statement.transactions.append(Transaction(
            txn_date=dates[0] if dates else None, value_date=dates[1] if len(dates) > 1 else (dates[0] if dates else None),
            narration=narration[:1000], reference=None if cheque else reference, cheque_number=cheque,
            amount=amount, direction=direction, balance=balance,
        ))
        previous = balance
    if not statement.transactions:
        raise StatementUnreadable("No transactions could be read from this PDF.")
    if statement.closing_balance is None:
        statement.closing_balance = statement.transactions[-1].balance
    _finish(statement)
    return statement


def _money_on_line(line: _Line, pattern: str) -> Decimal | None:
    found = re.search(pattern, line.text.upper())
    if not found:
        return None
    for token in line.text.upper()[found.end():].split():
        if _is_money(token):
            return parse_money(token)[0]
    return None


def _summary_closing(lines: list[_Line]) -> Decimal | None:
    """The closing balance a summary table prints under its label (HDFC: "Opening
    Balance ... Closing Bal" with the figures on the next line)."""
    for i, line in enumerate(lines):
        upper = line.text.upper()
        if "OPENING" not in upper or "CLOSING" not in upper or i + 1 >= len(lines):
            continue
        if any(_is_money(w["text"]) for w in line.words):
            continue
        below = [w for w in lines[i + 1].words if _is_money(w["text"])]
        if below:
            return parse_money(below[-1]["text"])[0]
    return None


def _is_header_continuation(line: _Line) -> bool:
    text = line.text.upper()
    return bool(re.search(r"\(DD/MM/YYYY\)|AMOUNT\(INR\)|^DATE\b|NUMBER$|\(INR\)", text)) and not any(
        _is_money(w["text"]) for w in line.words)


def _summary_figure(lines: list[_Line], label: str) -> Decimal | None:
    for i, line in enumerate(lines):
        labels = [w for w in line.words if w["text"].upper() == label]
        if not labels or i + 1 >= len(lines):
            continue
        anchor = labels[0]
        centre = (anchor["x0"] + anchor["x1"]) / 2
        below = [w for w in lines[i + 1].words if _is_money(w["text"])]
        if below:
            best = min(below, key=lambda w: abs((w["x0"] + w["x1"]) / 2 - centre))
            if abs((best["x0"] + best["x1"]) / 2 - centre) < 60:
                return parse_money(best["text"])[0]
    return None


def _side_by_column(word: dict[str, Any], header: dict[str, tuple[float, float]]) -> str | None:
    """Only when the balance cannot say: the nearer of the debit / credit headers."""
    centre = (word["x0"] + word["x1"]) / 2
    options = [(abs(centre - (v[0] + v[1]) / 2), k) for k, v in header.items() if k in ("debit", "credit")]
    if not options:
        return None
    return CREDIT if min(options)[1] == "credit" else DEBIT


# --------------------------------------------------------------------------- entry point

def read_statement(data: bytes, file_name: str) -> Statement:
    """Any supported statement file -> Statement. Raises StatementUnreadable / ScannedPdf."""
    if data[:5] == b"%PDF-" or file_name.lower().endswith(".pdf"):
        return statement_from_pdf(data, file_name)
    file_format, rows = read_excel_rows(data, file_name)
    return statement_from_rows(file_format, rows, file_name)


__all__ = [
    "CREDIT", "DEBIT", "ScannedPdf", "Statement", "StatementUnreadable", "Transaction", "bank_from_text",
    "parse_date", "parse_money", "read_statement", "references_in",
]
