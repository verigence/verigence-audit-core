"""In-house or external insurance: one deterministic rule (product owner 2026-10-27).

"If we don't find the Agent code or MISP code of the Dealer on the insurance, or
any reference of the insurance premium paid in the dealer invoice, then raise a
task for the PC to confirm whether it is an in-house or external insurance.
There should be a comment with the Agent Code / MISP Code, filled manually, and
then the task is closed."

So, in order (product owner 2026-11-16: "check MISP code, if there good, if
not anyhow check NDC for insurance; if insurance amount there then it's
dealership"):

1. The PC's confirmation decides (Deal tab or this task).
2. In-house when the policy carries one of the dealer's own Agent / MISP codes
   (a character or two misread allowed, dealer_code_match); else when the NDC
   charges the insurance premium, or a dealer invoice bills it.
   Self when the NDC's insurance row says the customer arranged it ("Insurance
   amount OWN", "Insurance SELF", "Done by Customer"), whatever amount is
   written on that row and whatever a cost sheet or dealer invoice shows
   (product owner 2026-11-17: "own means self and not dealership"). Only the
   dealer's own code on the policy, or the PC, outweighs it.
2a. Self when neither: the NDC is on file and read, with no insurance amount.
3. Otherwise, once a policy has been read, a PC task INSURANCE_SOURCE_CONFIRM
   asks: in-house or external, with the Agent / MISP code in the comment. An
   in-house answer records the code as the dealer's, so later policies with
   that code are recognised without asking.

Until the PC answers, the deal keeps the premium (as before: "Inhouse" with
decidedBy UNCONFIRMED) and the page says it is not confirmed -- unless the
booking docket says the customer arranged it ("Self", "Own", "by the customer",
"done by party"): then the deal shows Self meanwhile, still unconfirmed and
still asked (product owner 2026-10-28: the add-ons said "by the customer" while
the Deal said Inhouse). A docket naming both or neither ("OWN / DLR", a bank)
does not decide.

Live 2026-10-27 (Sibaram Nahak and 75 more on Mahindra Orissa): every policy
showed as Inhouse by default, including those issued through Policybazaar,
Turtlemint, individual agents or banks; the documents were never consulted.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

TASK_TYPE = "INSURANCE_SOURCE_CONFIRM"
INHOUSE, EXTERNAL = "INHOUSE", "EXTERNAL"
# The dealer's own billing: its tax invoices and debit notes -- not a generic
# invoice (an insurer's own bill is uploaded as one), not a credit note, and
# not a proforma / quotation / estimate (Booking estimates list insurance).
_INVOICE_TYPES = ("customer_invoice_dms", "tax_invoice_tally", "tax_invoice_dms", "tax_invoice",
                  "customer_invoice_dms_v2", "debit_note")
_NOT_A_BILL = re.compile(r"pro\s*-?\s*forma|quotation|estimate|credit\s+note", re.IGNORECASE)
# What the booking docket's "Insurance by" says, written by hand: the customer
# ("Self", "Own", "by the customer", "done by party") or the dealer ("DLR").
_SELF_WORDS = re.compile(r"\b(SELF|OWN|CUSTOMER|PARTY)\b")
_DEALER_WORDS = re.compile(r"\b(DEALER|DEALERSHIP|DLR|IN-?HOUSE|SHOWROOM)\b")


def booking_says_self(value: Any) -> bool:
    """True when the booking docket says the customer arranged the insurance,
    and nothing in it names the dealer."""
    upper = str(value or "").upper()
    return bool(_SELF_WORDS.search(upper)) and not _DEALER_WORDS.search(upper)


# A code as a person writes it: letters / digits with / & - and at least one digit.
_CODE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/&\-.]{3,}[A-Za-z0-9]")


def normalize_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def codes_in(comment: str) -> list[str]:
    """The Agent / MISP codes a PC wrote in a comment."""
    out: list[str] = []
    for token in _CODE_TOKEN.findall(comment or ""):
        if any(ch.isdigit() for ch in token) and len(normalize_code(token)) >= 5 and token not in out:
            out.append(token)
    return out


def _dec(value: Any) -> float | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned not in ("", ".", "-") else None
    except ValueError:
        return None


def _policy(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT r.insurer_name, r.policy_reference, r.agent_intermediary_name, r.agent_intermediary_code,
                   r.misp_code, r.insurance_source, r.insurance_source_set_at_utc, r.insurance_by,
                   r.insurance_source_set_by_actor_id, j.dealer_id
            FROM auditcore.insurance_records r
            JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
            WHERE r.tenant_id = :t AND r.journey_id = :j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def dealer_codes(connection: Connection, *, tenant_id: str, dealer_id: Any) -> set[str]:
    if dealer_id is None:
        return set()
    return set(connection.execute(
        text("SELECT normalized_code FROM auditcore.dealer_insurance_codes WHERE tenant_id = :t AND dealer_id = :d"),
        {"t": tenant_id, "d": dealer_id},
    ).scalars().all())


def other_dealer_codes(connection: Connection, *, tenant_id: str, dealer_id: Any) -> set[str]:
    """Every other dealership's own Agent / MISP codes in the Project."""
    return set(connection.execute(
        text("SELECT normalized_code FROM auditcore.dealer_insurance_codes WHERE tenant_id = :t "
             "AND dealer_id IS DISTINCT FROM :d"),
        {"t": tenant_id, "d": dealer_id},
    ).scalars().all())


# A long code read with a character or two wrong is still the dealer's own
# (2026-11-16: Utkal's MISP code MIBL/M&M/AAACU2665C/000 is read as
# AAECU2665C, AAECA2665C, AAACU2656C or AAUC2665C on 40 policies, which then
# showed the insurance as unconfirmed, or Self where the docket said so).
MAX_CODE_MISREAD = 2
_MIN_FUZZY_CODE = 12


def _edits(a: str, b: str) -> int:
    """Characters to change, add or drop to turn one code into the other."""
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def dealer_code_match(value: Any, own: set[str], others: set[str] = frozenset()) -> tuple[str, int] | None:
    """The dealer's own code this policy code is, and how many characters it
    was misread by (0 when exact): within MAX_CODE_MISREAD of one of the
    dealer's codes of 12 or more characters, and nearer to it than to any
    other dealership's. None otherwise. Deterministic: never a guess between
    two dealers."""
    code = normalize_code(value)
    if not code:
        return None
    if code in own:
        return code, 0
    if len(code) < _MIN_FUZZY_CODE:
        return None
    near = sorted((_edits(code, o), o) for o in own if len(o) >= _MIN_FUZZY_CODE)
    if not near or near[0][0] > MAX_CODE_MISREAD:
        return None
    distance, match = near[0]
    if any(_edits(code, o) <= distance for o in others):
        return None
    return match, distance


# Every reading a dealer invoice gives of an insurance premium, for one Journey
# (``:j``) or, with ``journeys`` set, for a list of them in one statement (the
# insights reports ask for many Journeys at once, 2026-11-10).
def _invoice_rows_sql(*, many: bool) -> str:
    journeys = "f.journey_id = ANY(CAST(:journeys AS uuid[]))" if many else "f.journey_id = :j"
    return f"""
        SELECT DISTINCT ON (f.journey_id, f.di_document_id, f.field_key)
               f.journey_id, f.di_document_id, f.field_key, f.effective_value
        FROM auditcore.journey_document_extracted_fields f
        JOIN auditcore.evidence e
          ON e.tenant_id = f.tenant_id AND e.journey_id = f.journey_id
         AND e.di_document_id = f.di_document_id AND e.association_status = 'ACTIVE'
        WHERE f.tenant_id = :t AND {journeys}
          AND COALESCE(e.document_type_key, f.source_document_type_key) = ANY(CAST(:types AS text[]))
          AND f.field_key IN ('line_items', 'insurance_amount', 'invoice_nature', 'invoice_purpose',
                              'document_title')
          AND NOT EXISTS (
              SELECT 1 FROM auditcore.p2_document_queue q
              WHERE q.tenant_id = f.tenant_id AND q.di_document_id = f.di_document_id
              HAVING bool_and(q.retired_at_utc IS NOT NULL OR q.queue_status IN ('MERGED', 'CANCELLED'))
          )
        ORDER BY f.journey_id, f.di_document_id, f.field_key, f.source_fact_version DESC NULLS LAST,
                 f.updated_at_utc DESC
    """


def premium_in_invoice_rows(rows: list[Any]) -> float | None:
    """The premium the dealer invoices among ``rows`` (one Journey's) bill, if any."""
    not_a_bill = {
        str(r["di_document_id"]) for r in rows
        if r["field_key"] in ("invoice_nature", "invoice_purpose", "document_title")
        and _NOT_A_BILL.search(str(r["effective_value"] or ""))
    }
    best: float | None = None
    for row in rows:
        if row["field_key"] not in ("line_items", "insurance_amount") or str(row["di_document_id"]) in not_a_bill:
            continue
        value = row["effective_value"]
        if row["field_key"] == "insurance_amount":
            amounts = [_dec(value)]
        else:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            amounts = [
                next((a for a in (_dec(item.get(k)) for k in ("net_amount", "total", "amount", "taxable_amount"))
                      if a is not None), None)
                for item in (value if isinstance(value, list) else [])
                if isinstance(item, dict) and str(item.get("line_category") or "").upper() == "INSURANCE"
            ]
        for amount in amounts:
            if amount is not None and amount > 0:
                best = max(best or 0.0, amount)
    return best


def premium_on_dealer_invoice(connection: Connection, *, tenant_id: str, journey_id: UUID) -> float | None:
    """The premium a current dealer invoice bills, if any."""
    rows = connection.execute(
        text(_invoice_rows_sql(many=False)),
        {"t": tenant_id, "j": journey_id, "types": list(_INVOICE_TYPES)},
    ).mappings().all()
    return premium_in_invoice_rows(list(rows))


def premiums_on_dealer_invoices(
    connection: Connection, *, tenant_id: str, journey_ids: list[str],
) -> dict[str, float]:
    """``premium_on_dealer_invoice`` for many Journeys in one statement: the Journeys
    (by id) whose dealer invoice bills an insurance premium, and the premium."""
    if not journey_ids:
        return {}
    rows = connection.execute(
        text(_invoice_rows_sql(many=True)),
        {"t": tenant_id, "journeys": journey_ids, "types": list(_INVOICE_TYPES)},
    ).mappings().all()
    by_journey: dict[str, list[Any]] = {}
    for row in rows:
        by_journey.setdefault(str(row["journey_id"]), []).append(row)
    out: dict[str, float] = {}
    for journey, journey_rows in by_journey.items():
        premium = premium_in_invoice_rows(journey_rows)
        if premium is not None:
            out[journey] = premium
    return out


# The dealer's own deal sheets: the NDC / account statement, the cost sheet
# and the ERP settlement printout (as the statement reader records them).
_STATEMENT_TYPES = frozenset({"customer_ledger", "cost_sheet", "erp_settlement", "customer_ledger_tally"})


def premium_on_dealer_statement(connection: Connection, *, tenant_id: str, journey_id: UUID) -> float | None:
    """The insurance premium the dealer's statement charges the customer, if
    any (2026-11-02: the NDC's insurance row shows the dealer collected the
    premium, so the insurance is in-house; it was never consulted)."""
    from audit_core.uc03_deal_source_history import active_source_rows

    amounts = [
        _dec(row["amount"]) for row in active_source_rows(connection, tenant_id=tenant_id, journey_id=journey_id)
        if row["line_kind"] == "COMMERCIAL" and str(row["component_key"]) == "insurance_amount"
        and str(row["source_document_type"]) in _STATEMENT_TYPES
    ]
    positive = [a for a in amounts if a is not None and a > 0]
    return max(positive) if positive else None


# The NDC's insurance row naming the customer as the one who arranged it
# (product owner 2026-11-17: "own means self and not dealership"). Read on the
# live NDCs: "Insurance amount OWN", "INSURANCE AMOUNT (OWN)", "Insurance SELF",
# "INSURANCE COST (NORMAL) Done By Customer", "Insurance amount by party". Not a
# posting that merely mentions the customer ("Insurance Premium Amount Debited
# to Customer", "Payment Refund to Customer"): those are the dealer's own.
_INSURANCE_ROW = re.compile(r"^\W*INSURANCE\b")
# "1st-party" / "3rd party" name the cover, not who arranged it.
_NOT_AN_OWN_ROW = re.compile(r"DEBIT|REFUND|PREMIUM|\bTO\s+(THE\s+)?CUSTOMER\b|\b(1ST|FIRST|3RD|THIRD)[\s-]*PARTY")
_MAX_OWN_ROW = 60
_IN_HOUSE = re.compile(r"\bIN\s+HOUSE\b")


def own_insurance_row(particulars: Any) -> bool:
    """True when an NDC row is the insurance row and says the customer arranged it."""
    label = " ".join(str(particulars or "").upper().split())
    return len(label) <= _MAX_OWN_ROW and bool(_INSURANCE_ROW.match(label)) \
        and not _NOT_AN_OWN_ROW.search(label) and booking_says_self(label)


def ndc_says_own(connection: Connection, *, tenant_id: str, journey_id: UUID) -> str | None:
    """The NDC's insurance row as printed, when a current NDC copy says the
    customer arranged the insurance ("Insurance amount OWN") and no copy names
    the dealer on it; else None."""
    from audit_core.uc03_account_statement import line_rows, load_ledger_documents

    own: list[str] = []
    for document_type in sorted(_STATEMENT_TYPES):
        for document in load_ledger_documents(connection, tenant_id=tenant_id, journey_id=journey_id,
                                              document_type=document_type):
            for row in line_rows(document["fields"].get("line_items")):
                label = " ".join(str(row.get("particulars_raw") or "").split())
                if not _INSURANCE_ROW.match(label.upper()) or len(label) > _MAX_OWN_ROW:
                    continue
                if _DEALER_WORDS.search(label.upper()) or _IN_HOUSE.search(label.upper()):
                    return None
                if own_insurance_row(label):
                    own.append(label)
    return own[0] if own else None


def document_decision(connection: Connection, *, tenant_id: str, journey_id: UUID,
                      policy: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """In-house from the documents, with the reason, or None when they do not
    show it. Product owner 2026-11-16, "simple": the dealer's MISP / Agent code
    on the policy first; if not, the NDC's insurance amount (or a dealer
    invoice billing the premium)."""
    policy = policy if policy is not None else _policy(connection, tenant_id=tenant_id, journey_id=journey_id)
    if policy is not None:
        known = dealer_codes(connection, tenant_id=tenant_id, dealer_id=policy["dealer_id"])
        others: set[str] | None = None
        for label, key in (("MISP code", "misp_code"), ("Agent code", "agent_intermediary_code")):
            if not policy.get(key):
                continue
            if normalize_code(policy[key]) in known:
                return {"source": INHOUSE, "basis": f"The policy carries the dealer's {label} {policy[key]}."}
            if others is None:
                others = other_dealer_codes(connection, tenant_id=tenant_id, dealer_id=policy["dealer_id"])
            near = dealer_code_match(policy[key], known, others)
            if near is not None:
                return {"source": INHOUSE,
                        "basis": f"The policy carries the dealer's {label} (read as {policy[key]}, "
                                 f"{near[1]} character{'s' if near[1] > 1 else ''} from the dealer's code)."}
    # The NDC saying the customer arranged it outweighs an amount written on
    # that row, a cost sheet's premium and a dealer invoice (2026-11-17).
    if ndc_says_own(connection, tenant_id=tenant_id, journey_id=journey_id) is not None:
        return None
    charged = premium_on_dealer_statement(connection, tenant_id=tenant_id, journey_id=journey_id)
    if charged is not None:
        return {"source": INHOUSE, "basis": f"The NDC charges the insurance premium (Rs {charged:,.0f})."}
    # The NDC decides (product owner 2026-11-17: "very simple rules: the policy
    # has the dealership code or the NDC mentions an amount for insurance"): a
    # dealer invoice's premium counts only while no NDC has been read.
    from audit_core.uc03_account_statement import statement_reading

    if statement_reading(connection, tenant_id=tenant_id, journey_id=journey_id)["read"]:
        return None
    premium = premium_on_dealer_invoice(connection, tenant_id=tenant_id, journey_id=journey_id)
    if premium is not None:
        return {"source": INHOUSE, "basis": f"The dealer invoice bills the insurance premium (Rs {premium:,.0f})."}
    return None


def ndc_without_premium(connection: Connection, *, tenant_id: str, journey_id: UUID) -> bool:
    """The NDC is on file and read, and charges no insurance premium."""
    from audit_core.uc03_account_statement import statement_reading

    return bool(statement_reading(connection, tenant_id=tenant_id, journey_id=journey_id)["read"]) \
        and premium_on_dealer_statement(connection, tenant_id=tenant_id, journey_id=journey_id) is None


def resolve(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """The insurance source and who decided it: PC, DOCUMENT, UNCONFIRMED (a
    policy is on file and the PC is asked) or DEFAULT (no policy yet). Only
    SELF takes the premium out of the deal, as before."""
    policy = _policy(connection, tenant_id=tenant_id, journey_id=journey_id)
    if policy and policy["insurance_source"]:
        return {"source": str(policy["insurance_source"]), "decidedBy": "PC",
                "decidedAt": policy["insurance_source_set_at_utc"],
                "actorId": policy["insurance_source_set_by_actor_id"], "basis": None}
    decided = document_decision(connection, tenant_id=tenant_id, journey_id=journey_id, policy=policy)
    if decided is not None:
        return {"source": INHOUSE, "decidedBy": "DOCUMENT", "decidedAt": None, "actorId": None,
                "basis": decided["basis"]}
    # The NDC's insurance row says the customer arranged it ("Insurance amount
    # OWN"): Self, policy on file or not (product owner 2026-11-17: "own means
    # self and not dealership").
    own_row = ndc_says_own(connection, tenant_id=tenant_id, journey_id=journey_id)
    if own_row is not None:
        return {"source": "SELF", "decidedBy": "DOCUMENT", "decidedAt": None, "actorId": None,
                "basis": f"The NDC's insurance row says \"{own_row}\": the customer's own insurance."}
    # Neither the dealer's code on the policy nor an insurance amount on the NDC
    # on file: the customer's own insurance (product owner 2026-11-16), policy
    # on file or not (2026-11-17). Until the NDC is read, the PC is asked as before.
    if ndc_without_premium(connection, tenant_id=tenant_id, journey_id=journey_id):
        has_policy = bool(policy) and bool(policy["insurer_name"] or policy["policy_reference"])
        return {"source": "SELF", "decidedBy": "DOCUMENT", "decidedAt": None, "actorId": None,
                "basis": ("The policy does not carry the dealer's MISP / Agent code and the NDC charges no "
                          "insurance premium: the customer's own insurance.") if has_policy else
                         ("The NDC charges no insurance premium and no policy with the dealer's code is on file: "
                          "the customer's own insurance.")}
    # Meanwhile the booking docket's own word stands, when it plainly says the
    # customer arranged it (2026-10-28).
    said_self = bool(policy) and booking_says_self(policy.get("insurance_by"))
    meanwhile = "SELF" if said_self else INHOUSE
    docket = (f" The booking docket says the insurance is by {policy['insurance_by']!s}, so the premium is kept "
              "out of the deal meanwhile.") if said_self else ""
    if policy and (policy["insurer_name"] or policy["policy_reference"]):
        return {"source": meanwhile, "decidedBy": "UNCONFIRMED", "decidedAt": None, "actorId": None,
                "basis": "Neither the dealer's Agent / MISP code nor a premium on the dealer invoice was found; "
                         "the PC is asked to confirm." + docket}
    return {"source": meanwhile, "decidedBy": "DEFAULT", "decidedAt": None, "actorId": None,
            "basis": docket.strip() or None}


def _dedupe_key(journey_id: UUID) -> str:
    return f"insurance-source:{journey_id}"


def sync_insurance_source_task(
    connection: Connection, *, tenant_id: str, journey_id: UUID, evaluation_started_at: datetime | None = None,
) -> dict[str, int]:
    """Raise the PC's task while the rule cannot decide; close it once it can."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_stage import delivery_has_started
    from audit_core.uc03_p2_task_producer import raise_or_refresh, resolve_if_open

    decision = resolve(connection, tenant_id=tenant_id, journey_id=journey_id)
    key = _dedupe_key(journey_id)
    counts: dict[str, int] = {}
    # A question for the PC: asked once delivery has started (2026-10-28).
    asking = decision["decidedBy"] == "UNCONFIRMED" and delivery_has_started(
        connection, tenant_id=tenant_id, journey_id=journey_id)
    if asking:
        policy = _policy(connection, tenant_id=tenant_id, journey_id=journey_id) or {}
        shows = "; ".join(f"{label} {policy[k]}" for label, k in (
            ("insurer", "insurer_name"), ("agent", "agent_intermediary_name"),
            ("Agent code", "agent_intermediary_code"), ("MISP code", "misp_code")) if policy.get(k)) or "no agent details"
        _, outcome = raise_or_refresh(
            connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key, task_type=TASK_TYPE,
            source_type="DOCUMENT_FIELD", source_code="INSURANCE_SOURCE_UNCONFIRMED",
            title="Confirm the insurance: in-house or external",
            description=(
                f"The policy shows {shows}. Neither the dealer's Agent / MISP code nor an insurance premium on "
                "the dealer invoice was found, so it is not known whether the dealership arranged this "
                "insurance. Choose in-house or external and write the Agent code / MISP code in the comment. "
                + ("The booking docket says it is by the customer, so until then the premium is kept out of "
                   "the deal." if decision["source"] == "SELF" else "Until then the premium stays in the deal.")),
            reference={
                "generatedBy": "SYSTEM", "sourceType": "DOCUMENT_FIELD", "sourceCode": "INSURANCE_SOURCE_UNCONFIRMED",
                "insurerName": policy.get("insurer_name"), "agentName": policy.get("agent_intermediary_name"),
                "agentCode": policy.get("agent_intermediary_code"), "mispCode": policy.get("misp_code"),
                "question": "Is this insurance in-house (through the dealership) or external?",
                "answers": [
                    {"value": INHOUSE, "label": "In-house: write the dealer's Agent / MISP code",
                     "requiresComment": True},
                    {"value": EXTERNAL, "label": "External: write the Agent / MISP code on the policy",
                     "requiresComment": True},
                ],
            },
            severity="MEDIUM", registry=get_registry(), evaluation_started_at=evaluation_started_at,
        )
        counts[outcome] = 1
    elif resolve_if_open(connection, tenant_id=tenant_id, journey_id=journey_id, dedupe_key=key,
                         evidence={"decidedBy": decision["decidedBy"], "source": decision["source"],
                                   "basis": decision["basis"], "verifiedAt": datetime.now(UTC).isoformat()}):
        counts["VERIFIED"] = 1
    return counts


def answer_task(connection: Connection, *, tenant_id: str, task: dict[str, Any], actor_id: str,
                comment: str | None, details: dict[str, Any] | None) -> dict[str, Any]:
    """The PC's answer: in-house or external, with the Agent / MISP code."""
    from audit_core.uc03_p2_deal_actions import set_insurance_source

    answer = str((details or {}).get("answer") or "").upper()
    if answer not in (INHOUSE, EXTERNAL):
        raise ValueError("Choose in-house or external.")
    comment = (comment or "").strip()
    codes = codes_in(comment)
    if not codes:
        raise ValueError("Write the Agent code / MISP code in the comment, as printed on the policy.")
    journey_id = UUID(str(task["journey_id"]))
    set_insurance_source(
        connection, tenant_id=tenant_id, journey_id=journey_id, source=INHOUSE if answer == INHOUSE else "SELF",
        actor_id=actor_id, reason=comment, correlation_id=None, via="TASK",
    )
    if answer == INHOUSE:
        dealer_id = connection.execute(
            text("SELECT dealer_id FROM auditcore.journeys WHERE tenant_id = :t AND journey_id = :j"),
            {"t": tenant_id, "j": journey_id},
        ).scalar_one_or_none()
        # Remember as the dealer's only a code this policy carries (never a date,
        # a phone or policy number from the comment). When the policy's codes
        # were not read, the PC's code is all there is.
        policy = _policy(connection, tenant_id=tenant_id, journey_id=journey_id) or {}
        on_policy = {normalize_code(policy.get(k)) for k in ("agent_intermediary_code", "misp_code")} - {""}
        remembered = [c for c in codes if normalize_code(c) in on_policy] if on_policy else codes
        for code in [c for c in remembered if len(normalize_code(c)) <= 120]:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealer_insurance_codes (
                        tenant_id, dealer_id, normalized_code, code_as_entered, confirmed_by_actor_id,
                        journey_id, task_id
                    ) VALUES (:t, :d, :n, :c, :a, :j, :task)
                    ON CONFLICT (tenant_id, dealer_id, normalized_code) DO NOTHING
                    """
                ),
                {"t": tenant_id, "d": dealer_id, "n": normalize_code(code), "c": code[:200], "a": actor_id,
                 "j": journey_id, "task": task["task_id"]},
            )
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_tasks
            SET task_status = 'VERIFIED_COMPLETE', verified_at_utc = now(), updated_at_utc = now(),
                completion_result = CAST(:result AS jsonb)
            WHERE tenant_id = :t AND task_id = :id
            """
        ),
        {"t": tenant_id, "id": task["task_id"], "result": json.dumps({
            "outcome": answer, "codes": codes, "answeredBy": actor_id, "answeredAt": datetime.now(UTC).isoformat(),
            "comment": comment})},
    )
    return {"taskId": str(task["task_id"]), "status": "VERIFIED_COMPLETE", "outcome": answer, "codes": codes}
