"""Observations on a Reviewer's journey closure (product owner 2026-11-09).

"In the closure report, I need to have a mechanism where Reviewer/TL can
select which all are the observations, and can add additional observation if
any. Then there should be justification remarks where in they can add their
comments for each observation which is not accepted and then additional
remarks if any ... option to include the severity as high, medium, low."

Every item a closure lists (uc03_reviewer_closure.closure_items: missing
documents, open findings, open tasks, plus the breaches a Team Lead already
confirmed and information notes) is a candidate observation, with a
suggested severity. The Reviewer decides each one:

- it stands as an observation, with a severity (HIGH / MEDIUM / LOW) and an
  optional note; or
- it is not accepted, with a justification (at least 10 characters), which is
  what the closure answered for an item before observations existed.

The Reviewer may add observations of their own and additional remarks; the
Team Lead accepts or rejects items as before and may add observations too
(owner choice 2026-11-09: TL accepts/rejects and adds; severity and the
selection stay the Reviewer's).

Closures written before this have only answers: they read as items not
accepted as observations, the answer being the justification.
"""
from __future__ import annotations

from typing import Any

SEVERITIES = ("HIGH", "MEDIUM", "LOW")
MIN_JUSTIFICATION = 10
MIN_TITLE = 5
MAX_ADDED = 30
# Candidates the closure lists without closing anything for them.
CONFIRMED_FINDING = "CONFIRMED_FINDING"
NOTE = "NOTE"
# Information tasks whose content belongs in the report as a note.
NOTE_TASK_TYPES = frozenset({"SCRAPPAGE_REGISTRATION_CONCESSION"})

_BY_FINDING_SEVERITY = {"CRITICAL": "HIGH", "HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW", "INFO": "LOW"}


class ObservationError(ValueError):
    """What is missing from the Reviewer's or Team Lead's observations."""


def suggested_severity(item: dict[str, Any]) -> str:
    """The severity offered for a candidate: a missing mandatory document is
    HIGH; a finding or task keeps its own; a note is LOW."""
    kind = item.get("kind")
    if kind == NOTE:
        return "LOW"
    if kind == "MISSING_DOCUMENT":
        return "HIGH" if str(item.get("requirement") or "").upper() == "MANDATORY" else "MEDIUM"
    return _BY_FINDING_SEVERITY.get(str(item.get("severity") or "").upper(), "MEDIUM")


def with_suggestions(item: dict[str, Any]) -> dict[str, Any]:
    """The candidate as the closure form shows it: suggested severity, and
    whether it stands by default (not when an answer is already accepted,
    e.g. a PC's not-mandatory claim the Reviewer accepted)."""
    return {**item, "suggestedSeverity": suggested_severity(item),
            "suggestedStands": not item.get("prefilledAnswer")}


def _clean(text: Any) -> str:
    return str(text or "").strip()


def decide_items(
    items: list[dict[str, Any]],
    *,
    observations: dict[str, dict[str, Any]],
    answers: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Each candidate with the Reviewer's decision, and the labels still
    missing one. ``answers`` is the older form (an answer per item): it reads
    as "not accepted, justified by the answer"."""
    decided: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in items:
        choice = observations.get(item["key"])
        legacy = _clean(answers.get(item["key"]))
        prefilled = _clean(item.get("prefilledAnswer"))
        typed = bool(legacy)
        if choice is not None:
            typed = typed or bool(_clean(choice.get("justification")))
        if choice is None:
            if legacy or prefilled or item.get("kind") in (CONFIRMED_FINDING, NOTE):
                # Answered the older way, an answer already accepted, or a
                # candidate that stands unless the Reviewer says otherwise.
                stands = not (legacy or prefilled)
                choice = {"stands": stands, "severity": item.get("suggestedSeverity"),
                          "justification": legacy or prefilled}
            else:
                missing.append(item["label"])
                continue
        stands = bool(choice.get("stands"))
        severity = _clean(choice.get("severity")).upper() or None
        justification = _clean(choice.get("justification")) or (legacy or prefilled)
        note = _clean(choice.get("note")) or None
        if stands:
            if severity not in SEVERITIES:
                missing.append(f"{item['label']} (choose High, Medium or Low)")
                continue
            answer = f"Stands as a {severity.lower()} severity observation" + (f": {note}" if note else ".")
            source = "REVIEWER"
        else:
            if len(justification) < MIN_JUSTIFICATION:
                missing.append(f"{item['label']} (justify why it is not an observation, at least "
                               f"{MIN_JUSTIFICATION} characters)")
                continue
            severity = None
            answer = justification
            source = ("REVIEWER" if typed else
                      "PREVIOUS_CLOSURE" if item.get("carried") else "ACCEPTED_CLAIM")
        decided.append({**item, "stands": stands, "observationSeverity": severity, "justification": None if stands else justification,
                        "note": note, "answer": answer, "answerSource": source})
    return decided, missing


def clean_added(added: list[dict[str, Any]], *, by_role: str) -> list[dict[str, Any]]:
    """Observations a person adds of their own: a title, a severity, an
    optional detail."""
    if len(added) > MAX_ADDED:
        raise ObservationError(f"At most {MAX_ADDED} added observations.")
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(added, start=1):
        title = _clean(entry.get("title"))
        severity = _clean(entry.get("severity")).upper()
        if len(title) < MIN_TITLE:
            raise ObservationError(f"Added observation {position} needs a title (at least {MIN_TITLE} characters).")
        if severity not in SEVERITIES:
            raise ObservationError(f"Added observation “{title}” needs a severity: High, Medium or Low.")
        out.append({"key": f"added:{by_role.lower()}:{position}", "kind": "ADDED", "label": title,
                    "detail": _clean(entry.get("detail")) or None, "stands": True, "observationSeverity": severity,
                    "addedBy": by_role.upper()})
    return out


def observation_rows(closure: dict[str, Any]) -> list[dict[str, Any]]:
    """Every observation of a closure in one shape, for the report and the
    Team Lead: the listed items (older closures included), what the Reviewer
    added and what the Team Lead added, each with the Team Lead's decision."""
    rejected = {r["key"]: r["reason"] for r in closure.get("rejections") or []}
    rows: list[dict[str, Any]] = []
    for item in closure.get("items") or []:
        stands = bool(item.get("stands"))  # older closures: answered, i.e. not an observation
        rows.append({
            "key": item["key"], "kind": item.get("kind"), "stage": item.get("stage"),
            "label": item.get("label"), "detail": item.get("detail"), "stands": stands,
            "severity": item.get("observationSeverity") if stands else None,
            "justification": None if stands else (item.get("justification") or item.get("answer")),
            "note": item.get("note"), "source": "SYSTEM",
            "tlRejection": rejected.get(item["key"]),
        })
    for added in (closure.get("added") or []) + (closure.get("tlAdded") or []):
        rows.append({
            "key": added["key"], "kind": "ADDED", "stage": None, "label": added["label"],
            "detail": added.get("detail"), "stands": True, "severity": added.get("observationSeverity"),
            "justification": None, "note": None, "source": added.get("addedBy") or "REVIEWER",
            "tlRejection": rejected.get(added["key"]),
        })
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, None: 3}
    return sorted(rows, key=lambda r: (not r["stands"], order.get(r["severity"], 3)))


def severity_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {s: 0 for s in SEVERITIES}
    for row in rows:
        if row["stands"] and row["severity"] in counts and not row.get("tlRejection"):
            counts[row["severity"]] += 1
    return counts
