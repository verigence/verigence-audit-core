"""Which customer each uploaded file is about (found live 2026-10-31).

PCs upload one PDF per customer. On the web "New booking" screen the first
file creates the booking and the page then *is* that booking, so the next
customer's PDF dropped there went into the same booking: ADI-RD-261022-022
holds "Azizur Reheman.pdf" and, two minutes later, "Tapan kumar patra.pdf";
ADI-RD-261022-026 holds 14 customers' files. Every newer single-slot copy
supersedes the earlier one (uc03_pc_booking_documents, decision 2026-10-01)
without asking whose it is, so Tapan's PAN, Aadhaar and docket replaced
Azizur's, and Tapan's receipts counted as Azizur's payments.

Product owner: the system fixes it by itself, with no PC or Team Lead action,
and an upload is never blocked or slowed. So the decision is made per file,
inside the journey recompute (uc03_p2_worker.settle_journey), and repairs
existing bookings on their next recompute:

- Files are grouped into *units*: files whose names share a stem ("Jyotsna
  nayak delivery inscope-1(...).jpg" ... "-30(...).jpg" are one unit;
  "jagannath suppliers (04-10).pdf" and "(06-10).pdf" are one). A generic
  name (IMG_..., DOC-...-WA00xx, DocScanner, WhatsApp Image ...) is a unit of
  its own.
- The booking's customer is the person (or company) most units are about,
  then most pages, then the earliest file. Everyone named inside the
  customer's own units is accepted with them (co-applicant, nominee, spouse).
- Another unit BELONGS when any PAN / Aadhaar on it is the customer's (two
  edits of misreading allowed, or the same last five characters), when a
  name matches, or when the person is a legitimate second party: named on a
  bank approval letter (applicant, co-applicant, guarantor), an
  authorization letter or the GST certificate (signatory), or the PAN inside
  a GSTIN on file. A canteen (CSD) or financier named on a receipt is never
  anyone's identity.
- It is FOREIGN when it carries identity (PAN, Aadhaar, KYC, booking docket,
  receipt or invoice customer name) and all of it names someone else. A
  company against a person (or the reverse) that no document links is
  UNKNOWN: kept, to be checked (a director's or the owner's own file).
  Organisations without conflicting company PANs are the same organisation.
  A file with no identity BELONGS.

With ``P2_FILE_IDENTITY_ENFORCE`` on, a FOREIGN file's documents leave the
booking (evidence ``association_status='FOREIGN'``: every reader of ACTIVE
evidence stops using them), and a customer's own single-slot document that a
foreign one had superseded is made active again. With it off the decision is
recorded and nothing else changes (a dry run).
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Connection, text

from audit_core.uc03_p2_names import (
    is_firm_name,
    is_masked,
    masked_matches,
    person_tokens,
    same_organisation,
    same_person,
)

logger = structlog.get_logger(__name__)

ENFORCE_VARIABLE = "P2_FILE_IDENTITY_ENFORCE"
_ACTOR = "p2-worker"
STATUSES = ("BELONGS", "FOREIGN", "UNKNOWN")


def enforce_enabled(connection: Connection | None = None, *, tenant_id: str | None = None,
                    journey_id: UUID | None = None) -> bool:
    """Whether a FOREIGN decision takes the file's documents out of this
    booking: P2_FILE_IDENTITY_ENFORCE set (a global override), or an enabled
    row of p2_file_identity_enforcement for the journey or for its whole
    Project -- switched from the database, no redeploy (a pilot on a few
    bookings, off again at once). Off by default: the decision is recorded only."""
    if os.environ.get(ENFORCE_VARIABLE, "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if connection is None or not tenant_id:
        return False
    return bool(connection.execute(
        text(
            """
            SELECT EXISTS (SELECT 1 FROM auditcore.p2_file_identity_enforcement
                           WHERE tenant_id=:t AND enabled
                             AND (journey_id IS NULL OR journey_id = CAST(:j AS uuid)))
            """
        ),
        {"t": tenant_id, "j": str(journey_id) if journey_id else None},
    ).scalar_one())


# --------------------------------------------------------------- numbers

_PAN_SHAPE = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
_ALNUM = re.compile(r"[^A-Z0-9]")


def _edits(a: str, b: str, limit: int = 3) -> int:
    """Levenshtein distance, stopping early above ``limit``."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def normalise_pan(value: Any) -> str | None:
    """A PAN as read: letters and digits only, roughly PAN-sized (OCR drops
    or adds a character: "GNPM0756G", "GNIPM0756G")."""
    if not isinstance(value, str | int):
        return None
    pan = _ALNUM.sub("", str(value).upper())
    if not 8 <= len(pan) <= 11:
        return None
    digits = sum(c.isdigit() for c in pan)
    if digits < 3 or len(pan) - digits < 3:
        return None
    return pan


def pans_in_text(value: Any) -> list[str]:
    """PANs printed inside a name ("TAPAN KUMAR PATRA-AVFPP9596G")."""
    return _PAN_SHAPE.findall(str(value or "").upper())


def gstin_pan(value: Any) -> str | None:
    """The PAN inside a GSTIN (characters 3-12), or a bare PAN printed where
    the GSTIN goes ("AADCN5007C")."""
    code = _ALNUM.sub("", str(value or "").upper())
    if len(code) == 15 and code[:2].isdigit():
        return code[2:12]
    if len(code) == 10 and _PAN_SHAPE.fullmatch(code):
        return code
    return None


def normalise_aadhaar(value: Any) -> str | None:
    """Twelve digits, or a masked copy ("XXXX XXXX 7247")."""
    if not isinstance(value, str | int):
        return None
    number = re.sub(r"[^0-9X]", "", str(value).upper())
    if len(number) != 12 or not number[-4:].isdigit():
        return None
    return number


def is_individual_pan(pan: str | None) -> bool:
    return bool(pan) and len(pan) == 10 and pan[3] == "P"


def is_company_pan(pan: str | None) -> bool:
    """A PAN whose holder is not an individual (4th character C, F, T, H...)."""
    return bool(pan) and len(pan) == 10 and _PAN_SHAPE.fullmatch(pan) is not None and pan[3] != "P"


def same_pan(a: str | None, b: str | None) -> bool:
    """One PAN, misreads allowed: within two edits, or the same last five
    characters (BQPRP2303F / BQRPR2303F)."""
    if not a or not b:
        return False
    return a == b or _edits(a, b, 2) <= 2 or (len(a) >= 9 and len(b) >= 9 and a[-5:] == b[-5:])


def same_aadhaar(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if "X" in a or "X" in b:
        return a[-4:] == b[-4:]
    return _edits(a, b, 2) <= 2


def mask_pan(pan: str | None) -> str | None:
    """"AVFPP9596G" -> "AVFPP****G"."""
    if not pan:
        return None
    if len(pan) == 10:
        return f"{pan[:5]}****{pan[9:]}"
    return f"{pan[:3]}{'*' * max(len(pan) - 4, 1)}{pan[-1:]}"


# ----------------------------------------------------------------- names

# Never anyone's identity on its own: the payer a receipt names for a CSD
# purchase ("The Area Manager Canteen Stores Department") or a loan (the
# financier), an insurer.
_NOT_A_CUSTOMER = re.compile(
    r"\b(BANK|FINANC\w*|FINSERV|FINCORP|FIN\s*CORP|CAPITAL|LEASING|NBFC|CANTEEN|CSD|AREA\s+MANAGER|"
    r"STORES?\s+DEPARTMENT|INSURANCE|ASSURANCE|GENERAL\s+INS\w*)\b"
)
# Too common to make two names one person on their own.
_FILLER = frozenset({
    "KUMAR", "KUMARI", "CHANDRA", "PRASAD", "DEVI", "RANI", "MD", "MOHAMMAD", "MOHAMMED", "MOHAMAD", "SK",
    "SHEIKH", "SHAIKH", "LAL", "BALA", "BAI", "BEGUM", "BIBI", "KU", "NATH", "RANJAN",
})


def not_a_customer(name: Any) -> bool:
    return bool(_NOT_A_CUSTOMER.search(str(name or "").upper()))


def names_match(left: Any, right: Any) -> bool:
    """Two printed person names are one person: the shared name rules
    (uc03_p2_names.same_person, masked names), or the same letters with one
    misread ("CHITTARANJAN MAHIA" / "CHITTA RANJAN MATIA"), or the first name
    one letter apart with another (not filler) word shared."""
    if not left or not right:
        return False
    if is_masked(left) or is_masked(right):
        return masked_matches(left, right) or masked_matches(right, left)
    if same_person(left, right):
        return True
    a, b = person_tokens(left), person_tokens(right)
    if not a or not b:
        return False
    joined_a, joined_b = "".join(a), "".join(b)
    if min(len(joined_a), len(joined_b)) >= 8 and _edits(joined_a, joined_b, 1) <= 1:
        return True
    if len(a) >= 2 and len(b) >= 2 and min(len(a[0]), len(b[0])) >= 3 and _edits(a[0], b[0], 1) <= 1:
        return bool((set(a[1:]) & set(b[1:])) - _FILLER)
    return False


# ----------------------------------------------------------------- files

_STEM_NOISE = frozenset({
    # how the PC names a file
    "delivery", "deliveries", "booking", "bookings", "book", "files", "file", "inscope", "outscope", "in", "out",
    "scope", "pages", "page", "pg", "copy", "copies", "scan", "scanned", "scanner", "img", "image", "images",
    "doc", "docs", "document", "documents", "wa", "kyc", "pdf", "jpg", "jpeg", "png", "heic", "final", "new",
    "part", "set", "merged", "compressed", "compress", "docscanner", "camscanner", "capture", "whatsapp", "pxl",
    "photo", "photos", "pic", "screenshot", "adobe", "at", "am", "pm", "and", "of", "the", "no", "all", "full",
    "upload", "uploaded", "papers", "paper", "customer", "mr", "mrs", "ms", "smt", "sri", "shri",
    # document names
    "invoice", "invoices", "receipt", "receipts", "pan", "aadhaar", "aadhar", "adhar", "adhaar", "insurance",
    "policy", "gst", "ledger", "cost", "sheet", "do", "rc", "challan", "form", "docket", "payment", "payments",
    "front", "back", "card", "cards", "id", "proof", "bill", "bills", "voucher", "slip", "letter", "approval",
    "sanction", "statement", "accessory", "accessories", "gate", "pass", "rto", "tax", "dms", "tally", "ndc",
    "noc", "order", "cover", "note", "kit", "fastag", "with", "for",
    # dates in generated names
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})


def file_stem(file_name: Any) -> str | None:
    """The part of a file name that says whose file it is, or None for a
    generic name (each such file is a unit of its own)."""
    base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", str(file_name or "").strip())
    words = re.sub(r"[^a-z]+", " ", re.sub(r"\([^)]*\)", " ", base.lower())).split()
    kept = [w for w in words if len(w) > 1 and w not in _STEM_NOISE]
    return " ".join(kept) or None


def _unit_key(stem: str | None, units: dict[str, Any]) -> str | None:
    """The unit a stem joins: one already seen whose words start with this
    stem's, or the reverse, two words at least ("bishnu charan" is "bishnu
    charan samal"'s); else its own."""
    if not stem:
        return None
    words = stem.split()
    for key in units:
        other = key.split()
        shorter = min(len(words), len(other))
        if shorter >= 2 and words[:shorter] == other[:shorter]:
            return key
    return stem


# ----------------------------------------------------------------- model


@dataclass
class DocumentFacts:
    document_id: str
    doc_type: str
    fields: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class FileFacts:
    batch_id: str
    file_name: str
    uploaded_at: datetime
    pages: int = 1
    busy: bool = False
    documents: list[DocumentFacts] = field(default_factory=list)


@dataclass
class Party:
    key: int
    batch_id: str
    document_id: str
    doc_type: str
    role: str
    name: str | None
    pan: str | None
    aadhaar: str | None
    org: bool
    identity: bool
    weight: int


@dataclass
class FileDecision:
    batch_id: str
    status: str
    reason_code: str
    reason: str
    person_name: str | None = None
    masked_pan: str | None = None


@dataclass
class JourneyDecision:
    customer_name: str | None
    customer_masked_pan: str | None
    customer_kind: str | None
    files: dict[str, FileDecision]
    checks: list[dict[str, Any]] = field(default_factory=list)

    def foreign(self) -> list[str]:
        return [b for b, d in self.files.items() if d.status == "FOREIGN"]


_IDENTITY_FIELDS = (
    "pan_number", "pan_name", "aadhaar_number", "aadhaar_name", "customer_name", "id_number", "buyer_name",
    "buyer_gstin", "gstin", "organisation_legal_name", "organisation_trade_name", "gst_authorised_signatory_names",
    "finance_applicant_name", "finance_applicant_pan", "finance_co_applicant_name", "finance_guarantor_name",
    "insured_name", "authorized_person_name", "authorizer_name", "chassis_number", "vin_number",
)
IDENTITY_FIELDS = _IDENTITY_FIELDS
_INVOICES = frozenset({
    "customer_invoice_dms", "tax_invoice_tally", "accessory_invoice_dms", "accessory_invoice_tally", "credit_note",
    "debit_note", "ew_invoice", "rsa_invoice", "invoice_generic",
})
_WEAK_NAMED = frozenset({"delivery_order_cover", "cost_sheet", "customer_ledger", "gst_declaration"})
# Documents of the car being sold (not the exchanged or scrapped one).
_VEHICLE_DOCUMENTS = frozenset({
    "customer_invoice_dms", "tax_invoice_tally", "insurance_cover", "rto_challan", "delivery_order_cover",
    "bank_approval_letter", "gate_pass",
})
_CANONICAL = {"booking_docket": "booking_form", "payment_receipt": "dealer_receipt"}
_ROLE_TEXT = {
    "APPLICANT": "the applicant on the bank approval letter",
    "CO_APPLICANT": "the co-applicant on the bank approval letter",
    "GUARANTOR": "the guarantor on the bank approval letter",
    "SIGNATORY": "an authorised signatory on the GST certificate",
    "AUTHORIZER": "named on the authorization letter",
    "AUTHORIZED": "the person authorised on the authorization letter",
}


def _strings(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, list):
            out.extend(_strings(value))
        elif isinstance(value, str | int) and not isinstance(value, bool):
            text_ = " ".join(str(value).split())
            if text_ and text_.lower() not in ("null", "none", "na", "n/a", "nil", "-", "no"):
                out.append(text_)
    return out


def _is_org(name: str | None, pan: str | None) -> bool:
    if pan and len(pan) == 10 and _PAN_SHAPE.fullmatch(pan):
        return pan[3] != "P"
    return is_firm_name(name)


class _Builder:
    def __init__(self) -> None:
        self.parties: list[Party] = []
        # (party keys of one link document, their roles)
        self.relations: list[list[int]] = []
        self.same_entity: list[tuple[int, int]] = []

    def add(self, file: FileFacts, doc: DocumentFacts, doc_type: str, role: str, name: str | None,
            pan: str | None = None, aadhaar: str | None = None, *, identity: bool, weight: int,
            org: bool | None = None) -> int | None:
        if name is not None and role not in ("KYC", "GST") and not_a_customer(name):
            return None  # a financier or the CSD canteen named as the payer
        if name is None and pan is None and aadhaar is None:
            return None
        key = len(self.parties)
        self.parties.append(Party(
            key=key, batch_id=file.batch_id, document_id=doc.document_id, doc_type=doc_type, role=role,
            name=name, pan=pan, aadhaar=aadhaar, org=_is_org(name, pan) if org is None else org,
            identity=identity, weight=weight))
        return key


def _parties(files: list[FileFacts]) -> _Builder:
    out = _Builder()
    for file in files:
        for doc in file.documents:
            doc_type = _CANONICAL.get(doc.doc_type, doc.doc_type)
            f = {k: _strings(v) for k, v in doc.fields.items()}

            def first(key: str, values: dict[str, list[str]] = f) -> str | None:
                return values.get(key, [None])[0] if values.get(key) else None

            if doc_type == "pan_card":
                pan = normalise_pan(first("pan_number"))
                out.add(file, doc, doc_type, "KYC", first("pan_name"), pan, identity=True, weight=2)
            elif doc_type == "aadhaar":
                out.add(file, doc, doc_type, "KYC", first("aadhaar_name"), None,
                        normalise_aadhaar(first("aadhaar_number")), identity=True, weight=2, org=False)
            elif doc_type == "customer_kyc":
                number = first("id_number")
                aadhaar = normalise_aadhaar(number)
                # its number is a PAN only when shaped like one (not a voter ID)
                pan = normalise_pan(number) if not aadhaar else None
                out.add(file, doc, doc_type, "KYC", first("customer_name"),
                        pan if pan and _PAN_SHAPE.fullmatch(pan) else None, aadhaar, identity=True, weight=2)
            elif doc_type in ("booking_form", "dealer_receipt"):
                role, weight = ("DOCKET", 3) if doc_type == "booking_form" else ("RECEIPT", 1)
                for name in f.get("customer_name", []):
                    pans = pans_in_text(name)
                    out.add(file, doc, doc_type, role, name, pans[0] if pans else None, identity=True,
                            weight=weight)
            elif doc_type in _INVOICES:
                pan = gstin_pan(first("buyer_gstin"))
                for name in f.get("buyer_name", []) or [None]:
                    out.add(file, doc, doc_type, "INVOICE", name, pan, identity=True, weight=2)
            elif doc_type == "gst_certificate":
                pan = gstin_pan(first("gstin"))
                legal = first("organisation_legal_name")
                proprietor = is_individual_pan(pan)
                anchor = out.add(file, doc, doc_type, "GST", legal, pan, identity=True, weight=2,
                                 org=not proprietor if pan else None)
                members = [anchor] if anchor is not None else []
                for trade in f.get("organisation_trade_name", []):
                    if legal and same_person(trade, legal):
                        continue
                    k = out.add(file, doc, doc_type, "GST_TRADE", trade, None, identity=True, weight=1, org=True)
                    if k is not None and anchor is not None:
                        out.same_entity.append((anchor, k))
                for signatory in f.get("gst_authorised_signatory_names", []):
                    k = out.add(file, doc, doc_type, "SIGNATORY", signatory, identity=False, weight=0, org=False)
                    if k is not None:
                        members.append(k)
                if len(members) > 1:
                    out.relations.append(members)
            elif doc_type == "bank_approval_letter":
                members = [k for k in (
                    out.add(file, doc, doc_type, "APPLICANT", first("finance_applicant_name"),
                            normalise_pan(first("finance_applicant_pan")), identity=False, weight=1),
                    *(out.add(file, doc, doc_type, "CO_APPLICANT", n, identity=False, weight=0)
                      for n in f.get("finance_co_applicant_name", [])),
                    *(out.add(file, doc, doc_type, "GUARANTOR", n, identity=False, weight=0)
                      for n in f.get("finance_guarantor_name", [])),
                ) if k is not None]
                if len(members) > 1:
                    out.relations.append(members)
            elif doc_type == "authorization_letter":
                members = [k for k in (
                    *(out.add(file, doc, doc_type, "AUTHORIZER", n, identity=False, weight=0)
                      for n in f.get("authorizer_name", [])),
                    *(out.add(file, doc, doc_type, "AUTHORIZED", n, identity=False, weight=0)
                      for n in f.get("authorized_person_name", [])),
                ) if k is not None]
                if len(members) > 1:
                    out.relations.append(members)
            elif doc_type == "insurance_cover":
                for name in f.get("insured_name", []):
                    out.add(file, doc, doc_type, "WEAK", name, identity=False, weight=1)
            elif doc_type in _WEAK_NAMED:
                for name in f.get("customer_name", []):
                    pans = pans_in_text(name)
                    out.add(file, doc, doc_type, "WEAK", name, pans[0] if pans else None, identity=False, weight=1)
    return out


def _match(a: Party, b: Party) -> str | None:
    if a.pan and b.pan and same_pan(a.pan, b.pan):
        return "SAME_PAN"
    if a.aadhaar and b.aadhaar and same_aadhaar(a.aadhaar, b.aadhaar):
        return "SAME_AADHAAR"
    if a.name and b.name and a.org == b.org:
        if a.org:
            return "SAME_NAME" if same_organisation(a.name, b.name) else None
        return "SAME_NAME" if names_match(a.name, b.name) else None
    return None


class _Entities:
    """People and organisations: parties joined by a shared PAN, Aadhaar or
    name (a proprietor and the firm on their GST certificate are one)."""

    def __init__(self, parties: list[Party], same_entity: list[tuple[int, int]]) -> None:
        self.parent = list(range(len(parties)))
        for a, b in same_entity:
            self._union(a, b)
        for i, a in enumerate(parties):
            for b in parties[i + 1:]:
                if self.find(a.key) != self.find(b.key) and _match(a, b):
                    self._union(a.key, b.key)

    def find(self, key: int) -> int:
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def _union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


# A GST certificate names the customer before the KYC does (product owner
# 2026-10-27: with a GSTIN on file the customer is named as per the GSTIN).
_NAME_PRIORITY = {"GST": 0, "KYC": 1, "DOCKET": 2, "INVOICE": 3, "RECEIPT": 4, "APPLICANT": 5, "WEAK": 6}


def _best(parties: list[Party]) -> tuple[str | None, str | None]:
    named = sorted((p for p in parties if p.name),
                   key=lambda p: (is_masked(p.name), _NAME_PRIORITY.get(p.role, 9), p.key))
    pans = sorted((p for p in parties if p.pan and _PAN_SHAPE.fullmatch(p.pan)),
                  key=lambda p: (_NAME_PRIORITY.get(p.role, 9), p.key))
    pans = pans or [p for p in parties if p.pan]
    name = re.sub(r"\s*-\s*[A-Z]{1,2}\d{6,}.*$|\s*-\s*[A-Z]{5}\d{4}[A-Z].*$", "", named[0].name) if named else None
    return (" ".join(name.split()) or None) if name else None, mask_pan(pans[0].pan) if pans else None


def _principal(parties: list[Party], entities: _Entities, order: dict[str, int]) -> int | None:
    score: dict[int, list[int]] = {}
    for p in parties:
        if p.weight <= 0:
            continue
        root = entities.find(p.key)
        entry = score.setdefault(root, [0, 10**9])
        entry[0] += p.weight
        entry[1] = min(entry[1], order[p.batch_id] * 10**4 + p.key)
    if not score:
        return None
    return max(score, key=lambda r: (score[r][0], -score[r][1]))


def _vehicle_key(value: str) -> str | None:
    code = _ALNUM.sub("", value.upper())
    return code if len(code) >= 6 else None


def _same_vehicle(a: str, b: str) -> bool:
    """One car, misreads allowed (within one booking: "MA1REZTKT1J85156" is
    "MA1RE2TTKT1J85156")."""
    return _edits(a, b, 2) <= 2 or _edits(a[-5:], b[-5:], 1) <= 1


def same_serial(a: str, b: str) -> bool:
    """One car across two bookings: the same serial (last six characters).
    One misread letter is not enough there -- a dealership's cars of one
    model run in sequence (...J32590, ...J32591 are two cars)."""
    return len(a) >= 6 and len(b) >= 6 and a[-6:] == b[-6:]


def classify(files: list[FileFacts]) -> JourneyDecision:
    """The decision for every file of one booking (pure: no database)."""
    files = sorted(files, key=lambda f: (f.uploaded_at, f.batch_id))
    order = {f.batch_id: i for i, f in enumerate(files)}
    built = _parties(files)
    parties = built.parties
    entities = _Entities(parties, built.same_entity)
    by_batch: dict[str, list[Party]] = {}
    for p in parties:
        by_batch.setdefault(p.batch_id, []).append(p)

    units: dict[str, list[FileFacts]] = {}
    for f in files:
        units.setdefault(_unit_key(file_stem(f.file_name), units) or f"file:{f.batch_id}", []).append(f)
    unit_parties = {u: [p for f in fs for p in by_batch.get(f.batch_id, [])] for u, fs in units.items()}
    principal = {u: _principal(ps, entities, order) for u, ps in unit_parties.items()}

    def members(root: int) -> list[Party]:
        return [p for p in parties if entities.find(p.key) == root]

    def kind(root: int) -> str:
        return "PERSON" if any(not p.org for p in members(root)) else "ORG"

    decisions: dict[str, FileDecision] = {}

    def person_of(batch_id: str) -> tuple[str | None, str | None]:
        mine = by_batch.get(batch_id, [])
        root = _principal(mine, entities, order)
        return _best([p for p in mine if entities.find(p.key) == root]) if root is not None else (None, None)

    def decide(unit: str, status: str, code: str, reason: str) -> None:
        for f in units[unit]:
            name, pan = person_of(f.batch_id)
            decisions[f.batch_id] = FileDecision(f.batch_id, status, code, reason, name, pan)

    voters = {r for r in principal.values() if r is not None}
    if not voters:
        for unit in units:
            decide(unit, "BELONGS", "NO_IDENTITY", "No PAN, Aadhaar or customer name was read in this file.")
        return JourneyDecision(None, None, None, decisions, _checks(files, parties, entities, decisions))

    def tally(root: int) -> tuple[int, int, int]:
        mine = [u for u, r in principal.items() if r == root]
        return (len(mine), sum(max(f.pages, 1) for u in mine for f in units[u]),
                -min(order[f.batch_id] for u in mine for f in units[u]))

    customer = max(voters, key=tally)
    customer_name, customer_pan = _best(members(customer))
    customer_kind = kind(customer)
    who = customer_name or "the customer"

    accepted: dict[int, tuple[str, str]] = {customer: ("CUSTOMER", f"the customer {who}")}
    belonging: set[str] = set()
    for unit, root in principal.items():
        if root == customer:
            belonging.add(unit)
            decide(unit, "BELONGS", "CUSTOMER_FILE", f"The customer's own file ({who}).")
            for p in unit_parties[unit]:
                root = entities.find(p.key)
                if p.role in _ROLE_TEXT and p.role != "APPLICANT" and accepted.get(root, ("",))[0] != "CUSTOMER":
                    # named as such on the customer's own bank letter, GST certificate...
                    accepted[root] = ("SECOND_PARTY", _ROLE_TEXT[p.role])
                else:
                    accepted.setdefault(root, ("IN_CUSTOMER_FILE", "named in the customer's own file"))

    def second_parties() -> bool:
        grew = False
        for relation in built.relations:
            roots = [entities.find(k) for k in relation]
            anchored = any(r in accepted for r in roots) or any(
                any(f.batch_id == parties[relation[0]].batch_id for f in units[u]) for u in belonging)
            if not anchored:
                continue
            for k, root in zip(relation, roots, strict=True):
                if root not in accepted:
                    accepted[root] = ("SECOND_PARTY", _ROLE_TEXT.get(parties[k].role, "a second party"))
                    grew = True
        return grew

    def kept_batches() -> set[str]:
        return {f.batch_id for u in belonging for f in units[u]}

    _strength = {"SAME_PAN": 0, "SAME_AADHAAR": 1, "SAME_NAME": 2}
    _label = {"SAME_PAN": "the same PAN as", "SAME_AADHAAR": "the same Aadhaar as", "SAME_NAME": "the same name as"}

    def match_reason(unit: str) -> tuple[str, str] | None:
        """Why a unit belongs: the strongest tie between one of its parties
        and an accepted one (a PAN before an Aadhaar before a name)."""
        best: tuple[int, str, str] | None = None
        mine = kept_batches()
        for p in unit_parties[unit]:
            root = entities.find(p.key)
            if root not in accepted:
                continue
            code, why = accepted[root]
            if code == "SECOND_PARTY":
                candidate = (3, "SECOND_PARTY", f"Names {_best([p])[0] or 'a person'}, {why}.")
            else:
                found = min((m for m in (_match(p, q) for q in members(root)
                                         if q.batch_id != p.batch_id and q.batch_id in mine) if m),
                            key=lambda m: _strength[m], default=None)
                candidate = ((_strength[found], found, f"Names {_label[found]} {why}.") if found
                             else (4, "SAME_PERSON", f"Names {why}."))
            if best is None or candidate[0] < best[0]:
                best = candidate
        return (best[1], best[2]) if best else None

    pending = [u for u in units if u not in belonging]
    changed = True
    while changed:
        changed = second_parties()
        for unit in list(pending):
            found = match_reason(unit)
            if found is None:
                continue
            pending.remove(unit)
            belonging.add(unit)
            decide(unit, "BELONGS", *found)
            for p in unit_parties[unit]:
                accepted.setdefault(entities.find(p.key), ("BELONGING_FILE", "a file kept with this booking"))
            changed = True

    customer_company_pans = {p.pan for p in members(customer) if is_company_pan(p.pan)}
    customer_has_org = any(p.org for p in members(customer)) or any(
        any(p.org for p in unit_parties[u]) for u in belonging)
    # The cars the kept files name: a company-or-person file naming a car of
    # its own is another purchase, not a director's or owner's KYC
    # (UTK-SU-261019-001: "Progressive Odisha Pvt Ltd.pdf" invoices
    # ...J19579, the booking's own invoice is ...J35566; the same file is
    # UTK-SU-261019-002's).
    kept_vehicles = _unit_vehicles([f for u in belonging for f in units[u]])
    tentative: dict[str, tuple[str, str, str, int | None, bool]] = {}
    for unit in pending:
        identity = [p for p in unit_parties[unit] if p.identity]
        if not identity:
            decide(unit, "BELONGS", "NO_IDENTITY", "No PAN, Aadhaar or customer name was read in this file.")
            continue
        roots = {entities.find(p.key) for p in identity}
        persons = [r for r in roots if kind(r) == "PERSON"]
        orgs = [r for r in roots if kind(r) == "ORG"]
        named = _best(members(principal[unit]) if principal[unit] is not None else identity)
        label = named[0] or "another person"
        pan_text = f" (PAN {named[1]})" if named[1] else ""
        unit_company_pans = {p.pan for r in orgs for p in members(r) if is_company_pan(p.pan)}
        conflicting_company = bool(unit_company_pans) and not any(
            same_pan(u, c) for u in unit_company_pans for c in customer_company_pans)
        vehicles = _unit_vehicles(units[unit])
        own_vehicle = bool(vehicles) and not any(_same_vehicle(v, k) for v in vehicles for k in kept_vehicles)
        car = f" and its own vehicle (chassis ...{vehicles[0][-5:]})" if own_vehicle else ""
        # Proof enough to take a file out: a PAN, an Aadhaar, a KYC or a
        # booking docket in another name, or a car of its own. A receipt or
        # invoice name alone may be the payer (a relative, a company:
        # product owner 2026-10-28) or a misread: kept, to be checked.
        strong = own_vehicle or any(p.pan or p.aadhaar or p.role in ("KYC", "DOCKET", "GST") for p in identity)
        if customer_kind == "PERSON" and persons:
            status, code, reason = ("FOREIGN", "OTHER_CUSTOMER",
                                    f"Names {label}{pan_text}, not the customer {who}.")
        elif customer_kind == "ORG" and conflicting_company and customer_company_pans:
            status, code, reason = ("FOREIGN", "OTHER_COMPANY",
                                    f"Names the company {label}{pan_text}, not the customer {who}.")
        elif own_vehicle and (persons or customer_kind == "PERSON"):
            status, code, reason = ("FOREIGN", "OTHER_VEHICLE",
                                    f"Names {label}{pan_text}{car}, not the customer {who}'s.")
        elif persons:
            status, code, reason = ("UNKNOWN", "COMPANY_PERSON_UNPROVEN",
                                    (f"Names {label}{pan_text}; the customer is {who} and no document names "
                                     "this person as its director, signatory or applicant. Kept: to be checked."))
        elif customer_kind == "ORG" or (customer_has_org and not conflicting_company):
            status, code, reason = ("BELONGS", "SAME_ORGANISATION",
                                    f"Names {label}: no company PAN on it differs from {who}'s.")
        else:
            status, code, reason = ("UNKNOWN", "PERSON_COMPANY_UNPROVEN",
                                    (f"Names the firm or company {label}{pan_text}; the customer is {who} and no "
                                     "document links them. Kept: to be checked."))
        if status == "FOREIGN" and not strong:
            status, code, reason = ("UNKNOWN", "NAME_ONLY",
                                    (f"Only a receipt or invoice names {label}, not the customer {who}; it may be "
                                     "the payer. Kept: to be checked."))
        tentative[unit] = (status, code, reason, principal[unit], strong)
    # Several customers' files dropped into one booking (ADI-RD-261024-004:
    # a company's booking file beside seven people's own booking files): the
    # benefit of the doubt a director's or owner's file gets is not given
    # when two or more other parties, each with proof of their own, are here.
    others = {root for status, code, _, root, strong in tentative.values()
              if strong and status != "BELONGS" and code != "STILL_PROCESSING"}
    for unit, (status, code, reason, _root, strong) in tentative.items():
        if len(others) > 1 and strong and code in ("COMPANY_PERSON_UNPROVEN", "PERSON_COMPANY_UNPROVEN"):
            status, code = "FOREIGN", "OTHER_CUSTOMER"
            reason = (reason.split(";")[0] + f", not the customer {who}: one of {len(others)} other customers' "
                      "files in this booking.")
        if status == "FOREIGN" and any(f.busy for f in units[unit]):
            status, code, reason = ("UNKNOWN", "STILL_PROCESSING",
                                    f"{reason} Decided once every page of the file is read.")
        decide(unit, status, code, reason)

    return JourneyDecision(customer_name, customer_pan, customer_kind, decisions,
                           _checks(files, parties, entities, decisions))


def _checks(files: list[FileFacts], parties: list[Party], entities: _Entities,
            decisions: dict[str, FileDecision]) -> list[dict[str, Any]]:
    """What is reported, never acted on: two individual PANs in one kept
    file, and more than one car across the kept files."""
    out: list[dict[str, Any]] = []
    names = {f.batch_id: f.file_name for f in files}
    kept = {b for b, d in decisions.items() if d.status != "FOREIGN"}
    for batch_id in kept:
        pans: list[str] = []
        for p in parties:
            if p.batch_id == batch_id and is_individual_pan(p.pan) and not any(same_pan(p.pan, q) for q in pans):
                pans.append(p.pan)
        if len(pans) > 1:
            out.append({"check": "TWO_PANS_IN_FILE", "batchId": batch_id, "fileName": names.get(batch_id),
                        "pans": [mask_pan(p) for p in pans]})
    vehicles = _unit_vehicles([f for f in files if f.batch_id in kept])
    if len(vehicles) > 1:
        out.append({"check": "MULTIPLE_VEHICLES", "chassis": [f"...{v[-5:]}" for v in vehicles]})
    return out


def _unit_vehicles(files: list[FileFacts]) -> list[str]:
    """The cars these files name (chassis / VIN on the sold car's papers)."""
    out: list[str] = []
    for f in files:
        for doc in f.documents:
            if doc.doc_type in _VEHICLE_DOCUMENTS:
                for value in _strings(doc.fields.get("chassis_number", []) + doc.fields.get("vin_number", [])):
                    key = _vehicle_key(value)
                    if key and not any(_same_vehicle(key, v) for v in out):
                        out.append(key)
    return out


def vehicles_of(files: list[FileFacts], decisions: dict[str, FileDecision]) -> list[str]:
    """The cars named by the kept files (chassis / VIN, as read)."""
    return _unit_vehicles([f for f in files
                           if not (decisions.get(f.batch_id) and decisions[f.batch_id].status == "FOREIGN")])


# --------------------------------------------------------------- database

_BUSY_PAGE_STATES = (
    "QUEUED", "PREPARING_PAGE", "DI_UPLOAD_PREPARING", "DI_UPLOADING", "DI_FINALIZING", "CLASSIFYING",
    "EXTRACTING", "SYNCING_TO_AUDIT_CORE", "RETRY_WAIT",
)


def load_files(connection: Connection, *, tenant_id: str, journey_id: UUID) -> tuple[list[FileFacts],
                                                                                    dict[str, dict[str, Any]]]:
    """Every file of the booking with what its documents name, whatever
    their documents' status now (a superseded or foreign copy still says
    whose file it is); and each file's recorded decision."""
    rows = connection.execute(
        text(
            """
            SELECT b.batch_id, b.original_filename, b.created_at_utc, b.page_count,
                   b.identity_status, b.identity_reason_code, b.identity_reason, b.identity_person_name,
                   b.identity_masked_pan, b.identity_enforced_at_utc,
                   EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                           WHERE q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id
                             AND q.queue_status = ANY(:busy)) AS busy
            FROM auditcore.p2_upload_batches b
            WHERE b.tenant_id=:t AND b.journey_id=:j
            ORDER BY b.created_at_utc, b.batch_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "busy": list(_BUSY_PAGE_STATES)},
    ).mappings().all()
    files = {str(r["batch_id"]): FileFacts(
        batch_id=str(r["batch_id"]), file_name=str(r["original_filename"] or ""),
        uploaded_at=r["created_at_utc"], pages=int(r["page_count"] or 0), busy=bool(r["busy"]),
    ) for r in rows}
    recorded = {str(r["batch_id"]): dict(r) for r in rows}
    facts = connection.execute(
        text(
            """
            SELECT DISTINCT q.batch_id, e.di_document_id, e.document_type_key, f.field_key, f.effective_value
            FROM auditcore.p2_document_queue q
            JOIN auditcore.evidence e
              ON e.tenant_id=q.tenant_id AND e.journey_id=q.journey_id AND e.di_document_id=q.di_document_id
            JOIN auditcore.journey_document_extracted_fields f
              ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
            WHERE q.tenant_id=:t AND q.journey_id=:j AND f.field_key = ANY(:fields)
              AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
            """
        ),
        {"t": tenant_id, "j": journey_id, "fields": list(_IDENTITY_FIELDS)},
    ).mappings().all()
    documents: dict[tuple[str, str], DocumentFacts] = {}
    for r in facts:
        batch_id = str(r["batch_id"])
        if batch_id not in files:
            continue
        key = (batch_id, str(r["di_document_id"]))
        doc = documents.get(key)
        if doc is None:
            doc = documents[key] = DocumentFacts(str(r["di_document_id"]), str(r["document_type_key"] or ""))
            files[batch_id].documents.append(doc)
        doc.fields.setdefault(str(r["field_key"]), []).append(r["effective_value"])
    return list(files.values()), recorded


def _record(connection: Connection, *, tenant_id: str, journey_id: UUID, decision: JourneyDecision,
            recorded: dict[str, dict[str, Any]]) -> list[str]:
    """Store each file's decision, and the booking's, only where it changed
    (a recompute that decides the same writes nothing); the files whose
    status changed."""
    moved: list[str] = []
    for batch_id, d in decision.files.items():
        before = recorded.get(batch_id) or {}
        values = {"s": d.status, "c": d.reason_code, "r": d.reason[:1000],
                  "n": d.person_name[:300] if d.person_name else None, "p": d.masked_pan}
        if (before.get("identity_status"), before.get("identity_reason_code"), before.get("identity_reason"),
                before.get("identity_person_name"), before.get("identity_masked_pan")) == (
                values["s"], values["c"], values["r"], values["n"], values["p"]):
            continue
        if before.get("identity_status") != d.status:
            moved.append(batch_id)
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_upload_batches
                SET identity_status=:s, identity_reason_code=:c, identity_reason=:r,
                    identity_person_name=:n, identity_masked_pan=:p, identity_decided_at_utc=now()
                WHERE tenant_id=:t AND batch_id=CAST(:b AS uuid)
                """
            ),
            {"t": tenant_id, "b": batch_id, **values},
        )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.p2_journey_file_identity AS fi (
                tenant_id, journey_id, customer_name, customer_masked_pan, customer_kind,
                foreign_files, unknown_files, checks, decided_at_utc
            ) VALUES (:t, :j, :n, :p, :k, :f, :u, CAST(:c AS jsonb), now())
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                customer_name=EXCLUDED.customer_name, customer_masked_pan=EXCLUDED.customer_masked_pan,
                customer_kind=EXCLUDED.customer_kind, foreign_files=EXCLUDED.foreign_files,
                unknown_files=EXCLUDED.unknown_files, checks=EXCLUDED.checks, decided_at_utc=now()
            WHERE (fi.customer_name, fi.customer_masked_pan, fi.customer_kind, fi.foreign_files, fi.unknown_files,
                   fi.checks)
                  IS DISTINCT FROM (EXCLUDED.customer_name, EXCLUDED.customer_masked_pan, EXCLUDED.customer_kind,
                                    EXCLUDED.foreign_files, EXCLUDED.unknown_files, EXCLUDED.checks)
            """
        ),
        {"t": tenant_id, "j": journey_id, "n": decision.customer_name and decision.customer_name[:300],
         "p": decision.customer_masked_pan, "k": decision.customer_kind,
         "f": sum(1 for d in decision.files.values() if d.status == "FOREIGN"),
         "u": sum(1 for d in decision.files.values() if d.status == "UNKNOWN"),
         "c": _json(decision.checks)},
    )
    return moved


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str, sort_keys=True)


_RESTORABLE = ("REPLACED_BY_NEWER_UPLOAD", "REPLACED_BY_P2_REUPLOAD", "EARLIER_COPY_RESTORED")

# A copy ``e`` was replaced by a FOREIGN file when the newer copy is one of
# that file's documents -- whatever became of it. Found live 2026-11-01
# (ADI-RD-261022-018): a page of Sudhanshu's file superseded Bikash's
# Aadhaar, was then merged into the grouped Aadhaar document and voided as a
# page (P2_MERGED_INTO_DOCUMENT), so only the grouped document went FOREIGN;
# the superseding evidence itself stayed VOIDED and Bikash's own Aadhaar was
# never given back. A page and the document it was merged into belong to the
# same uploaded file (batch), so the file decides, not the evidence status.
_REPLACED_BY_FOREIGN_FILE = """
    EXISTS (SELECT 1 FROM auditcore.evidence s
            WHERE s.tenant_id=e.tenant_id AND s.supersedes_evidence_id=e.evidence_id
              AND (s.association_status='FOREIGN' OR EXISTS (
                    SELECT 1 FROM auditcore.p2_document_queue sq
                    WHERE sq.tenant_id=s.tenant_id AND sq.journey_id=s.journey_id
                      AND sq.di_document_id=s.di_document_id
                      AND sq.batch_id = ANY(CAST(:foreign AS uuid[])))))
"""


def _repeatable(requirement_key: str | None) -> bool:
    from audit_core.uc03_pc_booking_documents import _is_repeatable_requirement

    return _is_repeatable_requirement(requirement_key)


def _applied_before(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """Whether enforcement left anything on this booking to put back."""
    return bool(connection.execute(
        text(
            """
            SELECT EXISTS (SELECT 1 FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j
                             AND (association_status='FOREIGN' OR foreign_restore_state IS NOT NULL))
                OR EXISTS (SELECT 1 FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j
                             AND completion_result ? 'foreignFile')
                OR EXISTS (SELECT 1 FROM auditcore.p2_upload_batches WHERE tenant_id=:t AND journey_id=:j
                             AND identity_enforced_at_utc IS NOT NULL)
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).scalar_one())


def _enforce(connection: Connection, *, tenant_id: str, journey_id: UUID, foreign: list[str]) -> dict[str, int]:
    """Make the booking hold exactly the files that are not ``foreign``.

    First everything done for a file no longer in ``foreign`` -- it was found
    to belong, or enforcement was switched off for the booking (``foreign``
    empty) -- is put back exactly as it was: its documents return to the
    status they had, the customer's copies made active for it are superseded
    again as they were, the tasks closed for it reopen as they were. Then the
    ``foreign`` files' documents leave the booking and the customer's own
    single-slot copies they had superseded become active again."""
    counts = {"documents_foreign": 0, "documents_restored": 0, "documents_returned": 0,
              "copies_superseded_again": 0, "tasks_closed": 0, "tasks_reopened": 0}
    # 1. Back as it was: the documents of a file no longer FOREIGN...
    returned = connection.execute(
        text(
            """
            UPDATE auditcore.evidence e
            SET association_status=COALESCE(e.pre_foreign_status, 'SUPERSEDED'), pre_foreign_status=NULL
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status='FOREIGN'
              AND NOT EXISTS (
                SELECT 1 FROM auditcore.p2_document_queue q
                WHERE q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id AND q.di_document_id=e.di_document_id
                  AND q.batch_id = ANY(CAST(:foreign AS uuid[])))
            RETURNING e.evidence_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "foreign": foreign},
    ).mappings().all()
    counts["documents_returned"] = len(returned)
    # ...and the copies made active because of it, superseded again exactly
    # as they were (their void reason, by whom, when; their capture row).
    again = connection.execute(
        text(
            f"""
            WITH copies AS (
                SELECT e.evidence_id, e.foreign_restore_state AS state
                FROM auditcore.evidence e
                WHERE e.tenant_id=:t AND e.journey_id=:j AND e.foreign_restore_state IS NOT NULL
                  AND NOT {_REPLACED_BY_FOREIGN_FILE}
                FOR UPDATE OF e
            )
            UPDATE auditcore.evidence e
            SET association_status=CASE WHEN e.association_status='ACTIVE' THEN 'SUPERSEDED'
                                        ELSE e.association_status END,
                void_reason=CASE WHEN e.association_status='ACTIVE' THEN c.state->>'voidReason'
                                 ELSE e.void_reason END,
                voided_by_actor_id=CASE WHEN e.association_status='ACTIVE' THEN c.state->>'voidedBy'
                                        ELSE e.voided_by_actor_id END,
                voided_at_utc=CASE WHEN e.association_status='ACTIVE' THEN (c.state->>'voidedAt')::timestamptz
                                   ELSE e.voided_at_utc END,
                foreign_restore_state=NULL
            FROM copies c
            WHERE e.tenant_id=:t AND e.evidence_id=c.evidence_id
            RETURNING e.di_document_id, e.association_status, c.state->>'captureStatus' AS capture_status
            """
        ),
        {"t": tenant_id, "j": journey_id, "foreign": foreign},
    ).mappings().all()
    for copy in again:
        if copy["association_status"] == "SUPERSEDED":
            counts["copies_superseded_again"] += 1
        if copy["capture_status"]:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.document_capture_v2_documents SET capture_status=:s, updated_at_utc=now()
                    WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND capture_status='CLASSIFIED'
                    """
                ),
                {"t": tenant_id, "j": journey_id, "d": copy["di_document_id"], "s": copy["capture_status"]},
            )
    counts["tasks_reopened"] = _reopen_tasks(connection, tenant_id=tenant_id, journey_id=journey_id,
                                             foreign=foreign)
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches SET identity_enforced_at_utc=NULL
            WHERE tenant_id=:t AND journey_id=:j AND identity_enforced_at_utc IS NOT NULL
              AND NOT (batch_id = ANY(CAST(:foreign AS uuid[])))
            """
        ),
        {"t": tenant_id, "j": journey_id, "foreign": foreign},
    )
    if not foreign:
        return counts
    # 2. A FOREIGN file's documents leave the booking.
    taken = connection.execute(
        text(
            """
            UPDATE auditcore.evidence e
            SET pre_foreign_status=e.association_status, association_status='FOREIGN'
            WHERE e.tenant_id=:t AND e.journey_id=:j AND e.association_status IN ('ACTIVE', 'SUPERSEDED')
              AND EXISTS (
                SELECT 1 FROM auditcore.p2_document_queue q
                WHERE q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id AND q.di_document_id=e.di_document_id
                  AND q.batch_id = ANY(CAST(:foreign AS uuid[])))
            RETURNING e.evidence_id
            """
        ),
        {"t": tenant_id, "j": journey_id, "foreign": foreign},
    ).mappings().all()
    counts["documents_foreign"] = len(taken)
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_upload_batches SET identity_enforced_at_utc=now()
            WHERE tenant_id=:t AND batch_id = ANY(CAST(:foreign AS uuid[])) AND identity_enforced_at_utc IS NULL
            """
        ),
        {"t": tenant_id, "foreign": foreign},
    )
    # 3. Every single-slot requirement a FOREIGN file touches, this pass or an
    # earlier one -- its documents, and its pages voided as merged into one of
    # them: the customer's own copy is given back, when nothing of the
    # booking's own is active for it. What the copy was is kept on it
    # (foreign_restore_state) so step 1 can put it back exactly.
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT e.journey_document_requirement_id, r.requirement_key
            FROM auditcore.evidence e
            JOIN auditcore.journey_document_requirements r
              ON r.tenant_id=e.tenant_id AND r.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:t AND e.journey_id=:j
              AND (e.association_status='FOREIGN' OR EXISTS (
                    SELECT 1 FROM auditcore.p2_document_queue q
                    WHERE q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id AND q.di_document_id=e.di_document_id
                      AND q.batch_id = ANY(CAST(:foreign AS uuid[]))))
            """
        ),
        {"t": tenant_id, "j": journey_id, "foreign": foreign},
    ).mappings().all()
    for row in rows:
        if _repeatable(row["requirement_key"]):
            continue
        requirement = row["journey_document_requirement_id"]
        if connection.execute(
            text(
                """
                SELECT EXISTS (SELECT 1 FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j
                               AND journey_document_requirement_id=:r AND association_status='ACTIVE')
                """
            ),
            {"t": tenant_id, "j": journey_id, "r": requirement},
        ).scalar_one():
            continue
        restored = connection.execute(
            text(
                f"""
                SELECT e.evidence_id, e.di_document_id FROM auditcore.evidence e
                WHERE e.tenant_id=:t AND e.journey_id=:j AND e.journey_document_requirement_id=:r
                  AND e.association_status='SUPERSEDED' AND e.void_reason = ANY(:reasons)
                  AND {_REPLACED_BY_FOREIGN_FILE}
                ORDER BY e.linked_at_utc DESC, e.evidence_id DESC
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"t": tenant_id, "j": journey_id, "r": requirement, "reasons": list(_RESTORABLE), "foreign": foreign},
        ).mappings().first()
        if restored is None:
            continue
        capture = connection.execute(
            text(
                """
                UPDATE auditcore.document_capture_v2_documents SET capture_status='CLASSIFIED', updated_at_utc=now()
                WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d AND capture_status='SUPERSEDED'
                RETURNING 'SUPERSEDED'
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": restored["di_document_id"]},
        ).scalar_one_or_none()
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence
                SET foreign_restore_state=jsonb_build_object(
                        'voidReason', void_reason, 'voidedBy', voided_by_actor_id, 'voidedAt', voided_at_utc,
                        'captureStatus', CAST(:capture AS text)),
                    association_status='ACTIVE', void_reason=NULL, voided_by_actor_id=NULL, voided_at_utc=NULL
                WHERE tenant_id=:t AND evidence_id=:e
                """
            ),
            {"t": tenant_id, "e": restored["evidence_id"], "capture": capture},
        )
        counts["documents_restored"] += 1
    counts["tasks_closed"] = _close_foreign_tasks(connection, tenant_id=tenant_id, journey_id=journey_id,
                                                  foreign=foreign)
    return counts


_WHOSE_FIELDS = ("pan_name", "aadhaar_name", "customer_name", "buyer_name", "insured_name",
                 "organisation_legal_name", "finance_applicant_name")


def plan_enforcement(connection: Connection, *, tenant_id: str, journey_id: UUID,
                     foreign: list[str]) -> dict[str, Any]:
    """What enforcement would do on this booking -- read only, for the scan's
    review: per FOREIGN file the documents and receipts it takes out and the
    customer's own copies it gives back, and the documents given back."""
    plan: dict[str, Any] = {"files": {b: {"documents": 0, "active": 0, "receipts": 0, "receipts_amount": 0,
                                          "restores": 0} for b in foreign}, "restored": []}
    if not foreign:
        return plan
    rows = [dict(r) for r in connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_document_id, e.document_type_key, e.association_status, e.pre_foreign_status,
                   e.journey_document_requirement_id, e.void_reason, e.linked_at_utc, e.supersedes_evidence_id,
                   e.process_area, jdr.requirement_key,
                   (SELECT q.batch_id::text FROM auditcore.p2_document_queue q
                     WHERE q.tenant_id=e.tenant_id AND q.journey_id=e.journey_id AND q.di_document_id=e.di_document_id
                     LIMIT 1) AS batch_id
            FROM auditcore.evidence e
            LEFT JOIN auditcore.journey_document_requirements jdr
              ON jdr.tenant_id=e.tenant_id AND jdr.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:t AND e.journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()]
    chosen = set(foreign)
    out = {r["evidence_id"]: r for r in rows
           if r["batch_id"] in chosen and r["association_status"] in ("ACTIVE", "SUPERSEDED", "FOREIGN")}
    active_out = {r["di_document_id"] for r in out.values()
                  if r["association_status"] == "ACTIVE"
                  or (r["association_status"] == "FOREIGN" and r["pre_foreign_status"] == "ACTIVE")}
    for r in out.values():
        plan["files"][r["batch_id"]]["documents"] += 1
        plan["files"][r["batch_id"]]["active"] += int(r["di_document_id"] in active_out)
    if active_out:
        batch_of = {r["di_document_id"]: r["batch_id"] for r in out.values()}
        for payment in connection.execute(
            text(
                """
                SELECT source_di_document_id, amount FROM auditcore.payments
                WHERE tenant_id=:t AND journey_id=:j AND amount > 0 AND source_di_document_id = ANY(:d)
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": list(active_out)},
        ).mappings().all():
            entry = plan["files"][batch_of[payment["source_di_document_id"]]]
            entry["receipts"] += 1
            entry["receipts_amount"] += float(payment["amount"])
    superseders: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        if r["supersedes_evidence_id"] is not None:
            superseders.setdefault(r["supersedes_evidence_id"], []).append(r)
    # As _enforce: any document of the file counts, a page of it voided as
    # merged into one of its documents too (_REPLACED_BY_FOREIGN_FILE).
    of_file = [r for r in rows if r["batch_id"] in chosen]
    requirements = {r["journey_document_requirement_id"] for r in of_file
                    if r["journey_document_requirement_id"] is not None and not _repeatable(r["requirement_key"])}
    restored = []
    for requirement in requirements:
        kept = [r for r in rows if r["journey_document_requirement_id"] == requirement and r["batch_id"] not in chosen]
        if any(r["association_status"] == "ACTIVE" for r in kept):
            continue
        candidates = sorted(
            (r for r in kept if r["association_status"] == "SUPERSEDED" and r["void_reason"] in _RESTORABLE
             and any(s["batch_id"] in chosen for s in superseders.get(r["evidence_id"], []))),
            key=lambda r: (r["linked_at_utc"], str(r["evidence_id"])), reverse=True)
        if candidates:
            copy = candidates[0]
            by = next(s for s in superseders[copy["evidence_id"]] if s["batch_id"] in chosen)
            plan["files"][by["batch_id"]]["restores"] += 1
            restored.append(copy)
    names: dict[Any, str] = {}
    if restored:
        for row in connection.execute(
            text(
                """
                SELECT f.di_document_id, f.field_key, f.effective_value #>> '{}' AS value
                FROM auditcore.journey_document_extracted_fields f
                WHERE f.tenant_id=:t AND f.journey_id=:j AND f.di_document_id = ANY(:d) AND f.field_key = ANY(:k)
                  AND f.effective_value IS NOT NULL AND f.effective_value <> 'null'::jsonb
                """
            ),
            {"t": tenant_id, "j": journey_id, "d": [r["di_document_id"] for r in restored],
             "k": list(_WHOSE_FIELDS)},
        ).mappings().all():
            if row["value"] and str(row["value"]).strip():
                names.setdefault(row["di_document_id"], " ".join(str(row["value"]).split()))
    from audit_core.uc03_p2_registry import get_registry

    registry = get_registry()
    for copy in sorted(restored, key=lambda r: (str(r["document_type_key"]), str(r["evidence_id"]))):
        template = registry.template_for_di_type(copy["document_type_key"],
                                                 stage=str(copy["process_area"] or "").upper() or None)
        plan["restored"].append({"document": template.display_name, "documentType": copy["document_type_key"],
                                 "whose": names.get(copy["di_document_id"]) or "no name read",
                                 "documentId": str(copy["di_document_id"])})
    return plan


def _close_foreign_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID, foreign: list[str]) -> int:
    """An open task raised only from a FOREIGN file's documents or pages is
    closed (machine-verified, so the rule that raised it can raise it again
    should the file come back). What the task was -- status, verification
    time, result -- is kept in its result, so it reopens exactly as it was
    when the file is no longer taken out (_reopen_tasks)."""
    if not foreign:
        return 0
    from audit_core.uc03_p2_task_producer import _OPEN
    from audit_core.uc03_p2_tasks import record_task_event

    pages = connection.execute(
        text(
            """
            SELECT q.queue_id::text AS queue_id, q.di_document_id::text AS document_id, q.batch_id::text AS batch_id
            FROM auditcore.p2_document_queue q
            WHERE q.tenant_id=:t AND q.journey_id=:j AND q.batch_id = ANY(CAST(:foreign AS uuid[]))
            """
        ),
        {"t": tenant_id, "j": journey_id, "foreign": foreign},
    ).mappings().all()
    batch_of_document = {p["document_id"]: p["batch_id"] for p in pages if p["document_id"]}
    batch_of_page = {p["queue_id"]: p["batch_id"] for p in pages}
    tasks = connection.execute(
        text(
            """
            SELECT task_id, dedupe_key, reference, source_code, task_status, verified_at_utc, completion_result
            FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND task_status = ANY(:open)
            """
        ),
        {"t": tenant_id, "j": journey_id, "open": list(_OPEN)},
    ).mappings().all()
    batches = set(foreign)
    closed = 0
    for task in tasks:
        reference = dict(task["reference"] or {})
        ids = {str(i) for i in (reference.get("documentIds") or []) if i}
        if reference.get("documentId"):
            ids.add(str(reference["documentId"]))
        queue_ids = {str(i) for i in (reference.get("queueIds") or []) if i}
        if reference.get("queueId"):
            queue_ids.add(str(reference["queueId"]))
        batch = str(reference.get("batchId") or "")
        if not (ids or queue_ids or batch):
            continue
        if not ((not ids or ids <= set(batch_of_document)) and (not queue_ids or queue_ids <= set(batch_of_page))
                and (not batch or batch in batches)):
            continue
        # Only a task about that document, page or file itself (its id is in
        # the task's key: "field-review:<journey>:<document>"); a booking-level
        # task that merely points at it (the booking date, from the docket on
        # file) is its own rule's to move to the document now current.
        key = str(task["dedupe_key"] or "")
        if not any(i in key for i in (*ids, *queue_ids, *([batch] if batch else []))):
            continue
        from_files = sorted({batch_of_document[i] for i in ids} | {batch_of_page[q] for q in queue_ids}
                            | ({batch} if batch else set()))
        previous = dict(task["completion_result"] or {})
        if "foreignFile" in previous:  # closed before, re-raised by its rule since
            previous = previous["foreignFile"].get("previousResult") or {}
        result = {"machineVerification": "PASS", "outcome": "FOREIGN_FILE",
                  "reason": "Raised from another customer's file, which was taken out of this booking.",
                  "verifiedAt": datetime.now(UTC).isoformat(),
                  "foreignFile": {"batchIds": from_files, "previousStatus": task["task_status"],
                                  "previousVerifiedAt": task["verified_at_utc"], "previousResult": previous}}
        closed_now = connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status='VERIFIED_COMPLETE', verified_at_utc=now(),
                    completion_result=COALESCE(completion_result, '{}'::jsonb) || CAST(:r AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:t AND task_id=:id AND task_status = ANY(:open)
                """
            ),
            {"t": tenant_id, "id": task["task_id"], "r": _json(result), "open": list(_OPEN)},
        ).rowcount
        if not closed_now:
            continue
        record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id, task_id=UUID(str(task["task_id"])),
                          event_type="MACHINE_VERIFICATION_PASS", actor_id="SYSTEM", actor_role_code="SYSTEM",
                          details={k: v for k, v in result.items() if k != "foreignFile"}
                          | {"sourceCode": task["source_code"], "batchIds": from_files})
        closed += 1
    return closed


def _reopen_tasks(connection: Connection, *, tenant_id: str, journey_id: UUID, foreign: list[str]) -> int:
    """A task closed because its file was taken out (outcome FOREIGN_FILE)
    reopens exactly as it was -- status, verification time, result -- once
    none of its files is taken out any more (found to belong, or enforcement
    switched off for the booking). One its own rule raised again meanwhile
    keeps the status the rule gave it and gets its result back."""
    from audit_core.uc03_p2_tasks import record_task_event

    tasks = connection.execute(
        text(
            """
            SELECT task_id, task_status, completion_result FROM auditcore.p2_tasks
            WHERE tenant_id=:t AND journey_id=:j AND completion_result ? 'foreignFile'
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    still = set(foreign)
    reopened = 0
    for task in tasks:
        result = dict(task["completion_result"] or {})
        memo = dict(result.get("foreignFile") or {})
        if set(memo.get("batchIds") or []) & still:
            continue
        closed_by_us = task["task_status"] == "VERIFIED_COMPLETE" and result.get("outcome") == "FOREIGN_FILE"
        previous = memo.get("previousResult")
        connection.execute(
            text(
                """
                UPDATE auditcore.p2_tasks
                SET task_status=CASE WHEN :ours THEN :s ELSE task_status END,
                    verified_at_utc=CASE WHEN :ours THEN CAST(:v AS timestamptz) ELSE verified_at_utc END,
                    completion_result=COALESCE(CAST(:r AS jsonb), '{}'::jsonb), updated_at_utc=now()
                WHERE tenant_id=:t AND task_id=:id
                """
            ),
            {"t": tenant_id, "id": task["task_id"], "ours": closed_by_us, "s": memo.get("previousStatus") or "READY",
             "v": memo.get("previousVerifiedAt"), "r": _json(previous) if previous is not None else None},
        )
        if closed_by_us:
            record_task_event(connection, tenant_id=tenant_id, journey_id=journey_id,
                              task_id=UUID(str(task["task_id"])), event_type="REOPENED", actor_id="SYSTEM",
                              actor_role_code="SYSTEM",
                              details={"reason": "Its file is no longer taken out of this booking.",
                                       "batchIds": memo.get("batchIds") or []})
            reopened += 1
    return reopened


def apply_file_identity(connection: Connection, *, tenant_id: str, journey_id: UUID,
                        enforce: bool | None = None) -> dict[str, Any]:
    """Decide every file of the booking, record it and -- with enforcement
    on for the booking (enforce_enabled) -- act on it. Runs first in the
    journey recompute, so the canonical tables it then rebuilds already leave
    a foreign file out.

    With enforcement off only the decision is written, and only when it
    changed -- unless enforcement had been applied on the booking and was
    switched off since: then this recompute puts everything back exactly as
    it was (the documents, the copies given back, the tasks closed)."""
    enforce = enforce_enabled(connection, tenant_id=tenant_id, journey_id=journey_id) if enforce is None else enforce
    files, recorded = load_files(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not files:
        return {"files": 0}
    decision = classify(files)
    moved = _record(connection, tenant_id=tenant_id, journey_id=journey_id, decision=decision, recorded=recorded)
    summary: dict[str, Any] = {
        "files": len(files), "foreign": len(decision.foreign()),
        "unknown": sum(1 for d in decision.files.values() if d.status == "UNKNOWN"),
        "changed": len(moved), "enforce": enforce,
    }
    for batch_id in moved:
        d = decision.files[batch_id]
        logger.info("p2_file_identity_decided", tenant_id=tenant_id, journey_id=str(journey_id), batch_id=batch_id,
                    status=d.status, reason_code=d.reason_code, person=d.person_name, masked_pan=d.masked_pan,
                    customer=decision.customer_name, enforce=enforce)
    target = decision.foreign() if enforce else []
    if not target and not _applied_before(connection, tenant_id=tenant_id, journey_id=journey_id):
        return summary
    counts = _enforce(connection, tenant_id=tenant_id, journey_id=journey_id, foreign=target)
    summary.update(counts)
    if any(counts.values()):
        reviewed = connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM auditcore.p2_stage_document_reviews "
                 "WHERE tenant_id=:t AND journey_id=:j)"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one()
        event = "p2_file_identity_applied" if enforce else "p2_file_identity_switched_off"
        logger.info(event, tenant_id=tenant_id, journey_id=str(journey_id), customer=decision.customer_name,
                    reviewed=bool(reviewed), **counts)
        if reviewed:
            # Applied on a reviewed booking too (product owner: fixed by us).
            logger.warning("p2_file_identity_reviewed_journey", tenant_id=tenant_id, journey_id=str(journey_id),
                           foreign_files=len(target), enforce=enforce)
        from audit_core.uc03_p2_runtime import record_activity

        record_activity(connection, tenant_id=tenant_id, journey_id=journey_id,
                        event_type="FILE_IDENTITY_APPLIED" if enforce else "FILE_IDENTITY_SWITCHED_OFF",
                        subject_type="JOURNEY", subject_id=str(journey_id),
                        details={"customer": decision.customer_name, "foreignFiles": target, **counts})
    return summary


def _shown_identity(row: dict[str, Any]) -> dict[str, Any]:
    """A file's identity as the web shows it. The web says "taken out of this
    booking" for FOREIGN, so FOREIGN is shown only for a file whose documents
    really are out; a file decided another customer's but not taken out
    (enforcement off, or the recompute not yet run) is UNKNOWN."""
    status = row["identity_status"] if row["identity_status"] in STATUSES else "UNKNOWN"
    reason = row["identity_reason"]
    if status == "FOREIGN" and not row["taken_out"]:
        status = "UNKNOWN"
        reason = f"Looks like {row['identity_person_name'] or 'another customer'}'s file; not yet taken out"
    return {"status": status, "personName": row["identity_person_name"], "maskedPan": row["identity_masked_pan"],
            "reason": reason}


def files_view(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The booking's files and whose each one is, for the documents API."""
    rows = connection.execute(
        text(
            """
            SELECT b.batch_id, b.original_filename, b.created_at_utc, b.identity_status, b.identity_reason,
                   b.identity_person_name, b.identity_masked_pan,
                   EXISTS (SELECT 1 FROM auditcore.p2_document_queue q
                           JOIN auditcore.evidence e
                             ON e.tenant_id=q.tenant_id AND e.journey_id=q.journey_id
                            AND e.di_document_id=q.di_document_id
                           WHERE q.tenant_id=b.tenant_id AND q.batch_id=b.batch_id
                             AND e.association_status='FOREIGN') AS taken_out,
                   -- where it went (uc03_file_move, 2026-11-01): the customer's own booking
                   moved.target_journey_id AS moved_to_journey_id, tj.journey_reference AS moved_to_reference
            FROM auditcore.p2_upload_batches b
            LEFT JOIN auditcore.p2_file_moves moved
              ON moved.tenant_id=b.tenant_id AND moved.source_batch_id=b.batch_id AND moved.status='DONE'
            LEFT JOIN auditcore.journeys tj
              ON tj.tenant_id=moved.tenant_id AND tj.journey_id=moved.target_journey_id
            WHERE b.tenant_id=:t AND b.journey_id=:j
            ORDER BY b.created_at_utc DESC, b.batch_id
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    customer = connection.execute(
        text(
            """
            SELECT fi.customer_name AS decided, c.legal_name, c.legal_name_status, c.display_name
            FROM auditcore.journeys j
            JOIN auditcore.customers c ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.p2_journey_file_identity fi ON fi.tenant_id=j.tenant_id AND fi.journey_id=j.journey_id
            WHERE j.tenant_id=:t AND j.journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first() or {}
    name = (customer.get("decided")
            or (customer.get("legal_name") if customer.get("legal_name_status") == "VERIFIED" else None))
    return {
        "customerName": name,
        "enforced": enforce_enabled(connection, tenant_id=tenant_id, journey_id=journey_id),
        "files": [
            {
                "batchId": str(r["batch_id"]),
                "fileName": r["original_filename"],
                "uploadedAtUtc": r["created_at_utc"],
                "identity": _shown_identity(dict(r)),
                # Only for a file taken out of this booking, and only while
                # the booking it went to is still there.
                "movedTo": ({"journeyId": str(r["moved_to_journey_id"]), "reference": r["moved_to_reference"]}
                            if r["taken_out"] and r["moved_to_reference"] else None),
            }
            for r in rows
        ],
    }


# Payments a FOREIGN file's receipt left behind never count (for the readers
# that do not already require the receipt's evidence to be ACTIVE).
def payment_not_foreign_sql(alias: str = "p") -> str:
    return (f"NOT EXISTS (SELECT 1 FROM auditcore.evidence fe WHERE fe.tenant_id={alias}.tenant_id "
            f"AND fe.di_document_id={alias}.source_di_document_id AND fe.association_status='FOREIGN')")
