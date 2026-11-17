"""Statement re-read experiment, Audit Core side (2026-11-17). Reads only.

The NDC / account statement prints its own totals, so a reading can be scored
without anyone labelling it: Audit Core's own check (uc03_statement_totals)
says whether the rows read give the totals read. This picks a sample of live
statements by what that check says of today's machine reading, and scores the
readings Document Intelligence's side of the experiment (di_side.py) made of
the same pages under other provider settings.

    python ac_side.py select <sample.json>
    python ac_side.py score  <sample.json> <readings.jsonl>

and, to measure a change to the check or to the misread repair on every
statement on file (2026-11-17), with each version of Audit Core installed:

    python ac_side.py count   <verdicts.json>
    python ac_side.py compare <before.json> <after.json>

The repository is public and so is this job's log: only counts, document ids
and verdicts are printed. No amount, name or field value ever is.
"""
from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

FAIL, PASS, TOTALS_ONLY = "DOES_NOT_ADD_UP", "ADDS_UP", "TOTALS_ONLY"
NOT_JUDGED, READ_FAILED = "NOT_JUDGED", "READ_FAILED"
SAMPLE_SIZES = {FAIL: 100, PASS: 40, TOTALS_ONLY: 30}
SEED = 20261117
STORED = "stored_di"
BASELINE = "rerun"


def _clean(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value not in (None, "", "null")}


def judge(fields: dict[str, Any]) -> dict[str, Any]:
    """One copy's reading as the Deal page judges it (load_ledger_documents:
    the discounts read apart are counted as rows, a thousands comma read as a
    "1" is put back when the totals then add up), from its fields as stored."""
    from audit_core.uc03_account_statement import (
        _amount,
        _discount_detail_rows,
        line_rows,
    )
    from audit_core.uc03_statement_misreads import repair_comma_misreads
    from audit_core.uc03_statement_totals import statement_differences

    fields = _clean(fields)
    rows_read = len(line_rows(fields.get("line_items")))
    details = _discount_detail_rows(fields)
    if details:
        fields["line_items"] = [*line_rows(fields.get("line_items")), *details]
    fields, repairs = repair_comma_misreads(fields)
    differences = statement_differences(fields)
    totals = sum(1 for key in ("total_cost", "gross_value", "net_value", "total_receipts")
                 if (_amount(fields.get(key)) or Decimal(0)) != 0)
    if differences is None:
        verdict, error = NOT_JUDGED, None
    else:
        error = sum((abs(Decimal(str(d["difference"]))) for d in differences), Decimal(0))
        verdict = FAIL if differences else PASS
    amounts = [amount for row in line_rows(fields.get("line_items")) for key in ("debit_amount", "credit_amount")
               if (amount := _amount(row.get(key))) not in (None, Decimal(0))]
    return {"verdict": verdict, "error": None if error is None else str(error), "rows": rows_read,
            "totals": totals, "commaRepairs": len(repairs),
            "repairedTotals": sum(1 for r in repairs if r.get("side") == "printed_total"),
            "gaps": gaps([Decimal(str(d["difference"])) for d in differences or []], amounts)}


def gaps(differences: list[Decimal], amounts: list[Decimal]) -> str | None:
    """What the gaps of a copy that does not add up look like, as an indication
    of the cause (never proof): SMALL within Rs 100 (the dealer's rounding, or a
    last digit), ROW the amount of one of its rows (a row read twice, not read
    as the dealer counted it, or left out of the dealer's sum), DIGIT one digit
    of one place (3,000, 500: one digit misread somewhere), OTHER anything else."""
    kinds = set()
    for difference in differences:
        gap = abs(difference)
        digits = str(int(gap)) if gap == gap.to_integral_value() else ""
        if gap <= 100:
            kinds.add("SMALL")
        elif any(abs(gap - abs(amount)) <= 10 for amount in amounts):
            kinds.add("ROW")
        elif digits and digits.strip("0") and len(digits.strip("0")) == 1:
            kinds.add("DIGIT")
        else:
            kinds.add("OTHER")
    return "+".join(sorted(kinds)) or None


def stratum(judged: dict[str, Any]) -> str | None:
    """Which part of the sample a copy belongs to by today's machine reading."""
    if judged["verdict"] in (FAIL, PASS):
        return str(judged["verdict"])
    if judged["rows"] == 0 and judged["totals"] > 0:
        return TOTALS_ONLY  # the printed totals were read and none of the rows
    return None


def pick(population: list[dict[str, Any]], sizes: dict[str, int] = SAMPLE_SIZES, seed: int = SEED) -> list[dict[str, Any]]:
    """A fixed-seed sample of each part, each dealer in proportion to its share
    of that part (largest remainders)."""
    chosen: list[dict[str, Any]] = []
    for part, size in sizes.items():
        by_dealer: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in sorted(population, key=lambda i: str(i["documentId"])):
            if item["stratum"] == part:
                by_dealer[str(item["dealer"])].append(item)
        total = sum(len(items) for items in by_dealer.values())
        if total == 0:
            continue
        size = min(size, total)
        exact = {dealer: size * len(items) / total for dealer, items in by_dealer.items()}
        quota = {dealer: int(share) for dealer, share in exact.items()}
        for dealer in sorted(exact, key=lambda d: (quota[d] - exact[d], d))[: size - sum(quota.values())]:
            quota[dealer] += 1
        for dealer in sorted(by_dealer):
            chosen.extend(random.Random(f"{seed}:{part}:{dealer}").sample(by_dealer[dealer], quota[dealer]))
    return chosen


def _moved(before: str | None, after: str | None) -> str:
    if before is None or after is None:
        return "n/a"
    was, now = Decimal(before), Decimal(after)
    return "closer" if now < was else "further" if now > was else "same"


def score(sample: list[dict[str, Any]], readings: list[dict[str, Any]]) -> dict[str, Any]:
    """Per setting and part of the sample: how many readings add up, do not,
    are not judged or failed; how the distance from the printed totals moved
    against today's reading; and each setting against the plain re-run."""
    by_document = {str(item["documentId"]): item for item in sample}
    verdicts: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    usage: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "failed": 0, "truncated": 0, "promptTokens": 0, "outputTokens": 0, "thoughtsTokens": 0,
        "costUsd": 0.0, "latencyMs": [], "errors": Counter()})
    for reading in readings:
        document, variant = str(reading["documentId"]), str(reading["variant"])
        if document not in by_document:
            continue
        if reading.get("ok"):
            judged = judge(reading.get("fields") or {})
        else:
            judged = {"verdict": READ_FAILED, "error": None, "rows": 0, "totals": 0, "commaRepairs": 0}
        verdicts[variant][document] = judged
        if variant == STORED:
            continue
        figures = usage[variant]
        figures["calls"] += 1
        figures["failed"] += 0 if reading.get("ok") else 1
        figures["truncated"] += 1 if reading.get("truncated") else 0
        if not reading.get("ok"):
            figures["errors"][str(reading.get("error") or "UNKNOWN")] += 1
        spent = reading.get("usage") or {}
        figures["promptTokens"] += int(spent.get("promptTokens") or 0)
        figures["outputTokens"] += int(spent.get("billedOutputTokens") or 0)
        figures["thoughtsTokens"] += int(spent.get("thoughtsTokens") or 0)
        figures["costUsd"] += float(spent.get("costUsd") or 0.0)
        if reading.get("latencyMs") is not None:
            figures["latencyMs"].append(float(reading["latencyMs"]))

    table: dict[str, dict[str, Any]] = {}
    for variant, judged_by_document in verdicts.items():
        parts: dict[str, Any] = {}
        for part in SAMPLE_SIZES:
            documents = [d for d, item in by_document.items() if item["stratum"] == part and d in judged_by_document]
            counts = Counter(judged_by_document[d]["verdict"] for d in documents)
            moved = Counter(_moved(by_document[d]["stored"]["error"], judged_by_document[d]["error"]) for d in documents)
            dealers: dict[str, Counter[str]] = defaultdict(Counter)
            for d in documents:
                dealers[str(by_document[d]["dealer"])][judged_by_document[d]["verdict"]] += 1
            parts[part] = {
                "documents": len(documents), **{v: counts.get(v, 0) for v in (PASS, FAIL, NOT_JUDGED, READ_FAILED)},
                "errorCloser": moved.get("closer", 0), "errorSame": moved.get("same", 0),
                "errorFurther": moved.get("further", 0),
                "rowsRead": sum(judged_by_document[d]["rows"] for d in documents),
                "totalsRead": sum(judged_by_document[d]["totals"] for d in documents),
                "commaRepairs": sum(judged_by_document[d]["commaRepairs"] for d in documents),
                "byDealer": {dealer: dict(c) for dealer, c in sorted(dealers.items())},
            }
        table[variant] = parts

    against: dict[str, dict[str, int]] = {}
    baseline = verdicts.get(BASELINE, {})
    for variant, judged_by_document in verdicts.items():
        if variant in (BASELINE, STORED) or not baseline:
            continue
        both = [d for d in judged_by_document if d in baseline]
        against[variant] = {
            "addsUpHereOnly": sum(1 for d in both if judged_by_document[d]["verdict"] == PASS and baseline[d]["verdict"] != PASS),
            "addsUpRerunOnly": sum(1 for d in both if judged_by_document[d]["verdict"] != PASS and baseline[d]["verdict"] == PASS),
            "addsUpBoth": sum(1 for d in both if judged_by_document[d]["verdict"] == PASS and baseline[d]["verdict"] == PASS),
            "addsUpNeither": sum(1 for d in both if judged_by_document[d]["verdict"] != PASS and baseline[d]["verdict"] != PASS),
        }

    spend: dict[str, Any] = {}
    for variant, figures in usage.items():
        latencies = sorted(figures["latencyMs"])
        spend[variant] = {
            "calls": figures["calls"], "failed": figures["failed"], "truncated": figures["truncated"],
            "errors": dict(figures["errors"]), "promptTokens": figures["promptTokens"],
            "outputTokens": figures["outputTokens"], "thoughtsTokens": figures["thoughtsTokens"],
            "costUsd": round(figures["costUsd"], 4),
            "costPerReadUsd": round(figures["costUsd"] / figures["calls"], 5) if figures["calls"] else None,
            "latencyMedianMs": latencies[len(latencies) // 2] if latencies else None,
            "latencyP90Ms": latencies[int(len(latencies) * 0.9) - 1] if len(latencies) >= 10 else None,
        }

    stored = verdicts.get(STORED, {})
    agree = sum(1 for d, judged in stored.items() if stratum(judged) == by_document[d]["stratum"])
    documents = {
        d: {"stratum": item["stratum"], "dealer": item["dealer"],
            **{variant: judged_by_document[d]["verdict"] for variant, judged_by_document in sorted(verdicts.items())
               if d in judged_by_document}}
        for d, item in sorted(by_document.items())
    }
    return {"table": table, "againstRerun": against, "spend": spend,
            "storedReadingCheck": {"documents": len(stored), "sameAsAuditCore": agree}, "documents": documents}


# ── the two commands ─────────────────────────────────────────────────────────
def _population(tenant_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import create_engine, text

    from audit_core.db import set_tenant_context

    engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, tenant_id)
        rows = connection.execute(
            text(
                """
                SELECT e.di_document_id, j.dealer_id, f.field_key, f.extracted_value
                FROM auditcore.evidence e
                JOIN auditcore.journeys j ON j.tenant_id=e.tenant_id AND j.journey_id=e.journey_id
                JOIN auditcore.journey_document_extracted_fields f
                  ON f.tenant_id=e.tenant_id AND f.journey_id=e.journey_id AND f.di_document_id=e.di_document_id
                WHERE e.tenant_id=:t AND e.association_status='ACTIVE' AND e.document_type_key='customer_ledger'
                ORDER BY e.di_document_id, f.field_key, f.source_fact_version
                """
            ),
            {"t": tenant_id},
        ).mappings().all()
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        document = documents.setdefault(str(row["di_document_id"]), {
            "documentId": str(row["di_document_id"]), "dealer": str(row["dealer_id"])[:8], "fields": {}})
        document["fields"][str(row["field_key"])] = row["extracted_value"]
    population = []
    for document in documents.values():
        judged = judge(document["fields"])
        population.append({"documentId": document["documentId"], "dealer": document["dealer"],
                           "stratum": stratum(judged), "stored": judged})
    return population


def select(sample_path: Path) -> None:
    tenant_id = os.environ["EXPERIMENT_TENANT_ID"]
    population = _population(tenant_id)
    counts = Counter((str(item["stratum"]), item["dealer"]) for item in population)
    print(f"POPULATION statements={len(population)}")
    for (part, dealer), count in sorted(counts.items()):
        print(f"POPULATION part={part} dealer={dealer} statements={count}")
    limit = int(os.environ.get("EXPERIMENT_LIMIT") or 0)
    sizes = {part: min(size, limit) for part, size in SAMPLE_SIZES.items()} if limit else SAMPLE_SIZES
    sample = pick([item for item in population if item["stratum"]], sizes)
    for (part, dealer), count in sorted(Counter((item["stratum"], item["dealer"]) for item in sample).items()):
        print(f"SAMPLE part={part} dealer={dealer} statements={count}")
    sample_path.write_text(json.dumps({"tenantId": tenant_id, "documents": sample}))
    print(f"SAMPLE statements={len(sample)}")


def count(out_path: Path) -> None:
    """Every statement on file judged by the Audit Core installed here."""
    population = _population(os.environ["EXPERIMENT_TENANT_ID"])
    out_path.write_text(json.dumps({item["documentId"]: {"dealer": item["dealer"], **item["stored"]}
                                    for item in population}))
    verdicts = Counter(item["stored"]["verdict"] for item in population)
    print(f"COUNT statements={len(population)} " + " ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))


def changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What a change to the check or the repair did to each statement both
    versions judged: verdicts before and after, each move with what made it
    (a figure put back, a printed total put back, or a row no longer needed
    by name), and what the gaps of those still not adding up look like."""
    both = sorted(set(before) & set(after))
    moves: Counter[tuple[str, str, str, str]] = Counter()
    verdicts: Counter[tuple[str, str, str]] = Counter()
    left: Counter[tuple[str, str]] = Counter()
    for document in both:
        was, now = before[document], after[document]
        dealer = str(now["dealer"])
        verdicts[("before", dealer, was["verdict"])] += 1
        verdicts[("after", dealer, now["verdict"])] += 1
        if was["verdict"] != now["verdict"]:
            how = ("PRINTED_TOTAL_PUT_BACK" if now.get("repairedTotals") else
                   "FIGURE_PUT_BACK" if int(now.get("commaRepairs") or 0) > int(was.get("commaRepairs") or 0) else
                   "ROW_NOT_NEEDED_BY_NAME")
            moves[(dealer, was["verdict"], now["verdict"], how)] += 1
        if now["verdict"] == FAIL:
            left[(dealer, str(now.get("gaps")))] += 1
    return {"statements": len(both), "onlyBefore": len(set(before) - set(after)),
            "onlyAfter": len(set(after) - set(before)),
            "verdicts": [{"when": w, "dealer": d, "verdict": v, "statements": n} for (w, d, v), n in sorted(verdicts.items())],
            "moves": [{"dealer": d, "from": a, "to": b, "how": h, "statements": n} for (d, a, b, h), n in sorted(moves.items())],
            "stillNotAddingUp": [{"dealer": d, "gaps": g, "statements": n} for (d, g), n in sorted(left.items())]}


def compare(before_path: Path, after_path: Path) -> None:
    result = changes(json.loads(before_path.read_text()), json.loads(after_path.read_text()))
    print(f"COMPARED statements={result['statements']} only_before={result['onlyBefore']} only_after={result['onlyAfter']}")
    for key, name in (("verdicts", "VERDICT"), ("moves", "MOVE"), ("stillNotAddingUp", "STILL")):
        for row in result[key]:
            print(f"{name} " + json.dumps(row, sort_keys=True))


def report(sample_path: Path, readings_path: Path) -> None:
    sample = json.loads(sample_path.read_text())["documents"]
    readings = [json.loads(line) for line in readings_path.read_text().splitlines() if line.strip()]
    result = score(sample, readings)
    check = result["storedReadingCheck"]
    print(f"STORED_READING_CHECK documents={check['documents']} same_as_audit_core={check['sameAsAuditCore']}")
    for variant, parts in sorted(result["table"].items()):
        for part, figures in parts.items():
            print("RESULT " + json.dumps({"setting": variant, "part": part, **figures}, sort_keys=True))
    for variant, figures in sorted(result["againstRerun"].items()):
        print("AGAINST_RERUN " + json.dumps({"setting": variant, **figures}, sort_keys=True))
    for variant, figures in sorted(result["spend"].items()):
        print("SPEND " + json.dumps({"setting": variant, **figures}, sort_keys=True))
    for document, verdicts in result["documents"].items():
        print("DOC " + json.dumps({"document": document, **verdicts}, sort_keys=True))


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "select":
            select(Path(argv[2]))
        elif len(argv) == 4 and argv[1] == "score":
            report(Path(argv[2]), Path(argv[3]))
        elif len(argv) == 3 and argv[1] == "count":
            count(Path(argv[2]))
        elif len(argv) == 4 and argv[1] == "compare":
            compare(Path(argv[2]), Path(argv[3]))
        else:
            print("usage: ac_side.py select <sample.json> | score <sample.json> <readings.jsonl> | "
                  "count <verdicts.json> | compare <before.json> <after.json>")
            return 2
    except Exception as exc:  # noqa: BLE001 - a public log: the kind of failure and where, never its text
        import traceback

        frames = traceback.extract_tb(exc.__traceback__)
        where = "; ".join(f"{Path(f.filename).name}:{f.lineno}" for f in frames[-4:])
        print(f"::error::{type(exc).__name__} at {where}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
