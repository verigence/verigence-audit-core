"""uc03_p2_document_task_groups -- the Documents group of the Task Queue.

Product owner 2026-10-21: "aggregate the tasks specifically related to
documents -- manual verification, documents which get failed, low contrast,
or bad image/blur, one set of tasks for document missing". A PC or Team Lead
sees every piece of document work in one place, in four categories:

  MANUAL_VERIFICATION  a value read from a document to check or correct
  FAILED_TO_READ       a page or file that was not read (failed, refused,
                       not recognised, or still waiting)
  IMAGE_QUALITY        a page refused by the scan-quality check (blurred,
                       faded / low contrast, too small, any DI_QUALITY_*)
  MISSING_DOCUMENT     a checklist document not yet on file (or the right
                       one to upload in place of a wrong one)

Which category a task belongs to is decided only by the maps below, never
by code: a task type that is not listed (and whose template category is not
listed) is not document work and stays where it was. A new task type or
category slots in by adding its key here -- DOCUMENT_RESUBMIT (a page to
resubmit, being added on another branch) is already mapped by category.

Rule-raised tasks (FINDING_REMEDIATION, DOCUMENT_REMEDIATION, ...) carry
the check that raised them in ``source_code``; for those the check decides,
because the same task type serves checks of every kind.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from audit_core.uc03_p2_runtime import dayend_removal_notice

# The four categories, in the order the Task Queue shows them.
DOCUMENT_CATEGORIES: tuple[str, ...] = ("MANUAL_VERIFICATION", "FAILED_TO_READ", "IMAGE_QUALITY", "MISSING_DOCUMENT")

DOCUMENT_CATEGORY_LABELS: dict[str, str] = {
    "MANUAL_VERIFICATION": "Manual verification",
    "FAILED_TO_READ": "Failed to read",
    "IMAGE_QUALITY": "Image quality",
    "MISSING_DOCUMENT": "Missing documents",
}

# Task types whose meaning is fixed by the type itself.
DOCUMENT_CATEGORY_BY_TASK_TYPE: dict[str, str] = {
    # Values read from a document to confirm or correct.
    "MANUAL_VERIFICATION_REVIEW": "MANUAL_VERIFICATION",
    "PC_BOOKING_DATE_MISSING": "MANUAL_VERIFICATION",
    "FIELD_CORRECTION_REVIEW": "MANUAL_VERIFICATION",
    "FIELD_CORRECTION_REVIEW_P2": "MANUAL_VERIFICATION",
    "TL_MANUAL_VERIFICATION": "MANUAL_VERIFICATION",
    # A document whose name does not match the customer's KYC, or a receipt
    # naming another dealer: the PC checks the name on the page first.
    "WRONG_DOCUMENT_REVIEW": "MANUAL_VERIFICATION",
    "WRONG_DOCUMENT_DEALER_NOTICE": "MANUAL_VERIFICATION",
    # Two receipts that read as the same receipt: the PC compares the values
    # read on both (it was on the Documents tab before and stays there).
    "DUPLICATE_RECEIPT_NOTICE": "MANUAL_VERIFICATION",
    # Pages or files that were not read.
    "PC_UPLOAD_STATUS": "FAILED_TO_READ",
    "PC_RESOLVE_DOCUMENT_PROCESSING_FAILURE": "FAILED_TO_READ",
    "PC_VERIFY_UNRECOGNIZED_DOCUMENT": "FAILED_TO_READ",
    "TL_DOCUMENT_STUCK": "FAILED_TO_READ",
    "DOCUMENT_RESUBMIT": "FAILED_TO_READ",
    # Pages refused by the scan-quality check (one task per file).
    "PC_PAGE_QUALITY_REJECTED": "IMAGE_QUALITY",
    # Documents not yet on file.
    "DOCUMENT_MISSING": "MISSING_DOCUMENT",
    "PC_DOCUMENT_REUPLOAD": "MISSING_DOCUMENT",
    "TL_DOCUMENT_UPLOAD": "MISSING_DOCUMENT",
    "DOCUMENT_REMEDIATION": "MISSING_DOCUMENT",
    "DELIVERY_VEHICLE_PHOTOS_MISSING": "MISSING_DOCUMENT",
}

# A task's template category, for a task type not listed above.
DOCUMENT_CATEGORY_BY_TEMPLATE_CATEGORY: dict[str, str] = {
    "DOCUMENT_RESUBMIT": "FAILED_TO_READ",
}

# Task types raised by a rule: the rule (source_code) decides; a rule not
# listed falls back to the task type map (DOCUMENT_REMEDIATION is a missing
# document unless its rule says otherwise; FINDING_REMEDIATION is not
# document work unless its rule says so).
RULE_TASK_TYPES: frozenset[str] = frozenset({
    "FINDING_REMEDIATION", "DOCUMENT_REMEDIATION", "AUTO_SELF_SERVE", "FINDING_REVIEW", "RULE_DISCREPANCY_REVIEW",
    "PC_CONFIRMATION",
})
DOCUMENT_CATEGORY_BY_RULE: dict[str, str] = {
    "MANUAL_VERIFICATION": "MANUAL_VERIFICATION",
    "UC03_DI_LOW_CONFIDENCE_POST_SUBMIT": "MANUAL_VERIFICATION",
    "DOCUMENT_UNRECOGNIZED": "FAILED_TO_READ",
    "DL_V2_DOCUMENT_PROCESSING_FAILED": "FAILED_TO_READ",
    "AUTOMATED_SYNC_FAILURE": "FAILED_TO_READ",
    "DOCUMENT_MISSING": "MISSING_DOCUMENT",
    "BK_DOCKET_PRESENT": "MISSING_DOCUMENT",
    "BK_DISCOUNT_EVIDENCE_MISSING": "MISSING_DOCUMENT",
    "BK_CONDITIONAL_DOCS_ADDRESSED": "MISSING_DOCUMENT",
    "DL_V2_REQUIRED_DOCUMENT_MISSING": "MISSING_DOCUMENT",
    "DOC_REQUIRED_ANSWER_NO": "MISSING_DOCUMENT",
    # The vehicle is invoiced and no insurance cover note is on file: upload
    # it (or answer that the customer arranged their own insurance).
    "INSURANCE_INVOICE_MISSING": "MISSING_DOCUMENT",
    # External rule engine checks for a document not on file (raised as
    # RULE_DISCREPANCY_REVIEW).
    "INVOICE_MISSING": "MISSING_DOCUMENT",
    "GATE_PASS_MISSING": "MISSING_DOCUMENT",
    "RTO_CHALLAN_MISSING": "MISSING_DOCUMENT",
    "INSURANCE_COVER_NOTE_MISSING": "MISSING_DOCUMENT",
    "DELIVERY_ORDER_MISSING": "MISSING_DOCUMENT",
    "CORPORATE_PO_MISSING": "MISSING_DOCUMENT",
    "CUSTOMER_LEDGER_MISSING": "MISSING_DOCUMENT",
}

# DOCUMENT_MISSING is also raised for a document carrying the wrong name
# (source WRONG_CUSTOMER_NAME / WRONG_DEALER_NAME): that is a name to check
# on the page, not a document to find.
_WRONG_NAME_SOURCE = re.compile(r"^WRONG_[A-Z]+_NAME$")


def document_category(task_type: str | None, category: str | None = None,
                      source_code: str | None = None) -> str | None:
    """The Documents category of one task, or None when it is not document
    work (it then stays on its existing tab, unchanged)."""
    task_type = str(task_type or "").upper()
    source = str(source_code or "").upper()
    if task_type == "DOCUMENT_MISSING" and _WRONG_NAME_SOURCE.match(source):
        return "MANUAL_VERIFICATION"
    if task_type in RULE_TASK_TYPES and source in DOCUMENT_CATEGORY_BY_RULE:
        return DOCUMENT_CATEGORY_BY_RULE[source]
    if task_type in DOCUMENT_CATEGORY_BY_TASK_TYPE:
        return DOCUMENT_CATEGORY_BY_TASK_TYPE[task_type]
    return DOCUMENT_CATEGORY_BY_TEMPLATE_CATEGORY.get(str(category or "").upper())


# Why a page was refused, as the PC reads it (the daily report's reasons).
_REJECTION_WORDS = {"faded": "too faded (low contrast)", "blurred": "blurred", "tooSmall": "too small",
                    "unreadable": "empty or unreadable", "other": "not clear enough"}


def rejection_words(reason: str | None) -> str:
    return _REJECTION_WORDS.get(str(reason or ""), "not clear enough")


def _pages_phrase(pages: list[int]) -> str:
    if not pages:
        return ""
    if len(pages) == 1:
        return f"page {pages[0]}"
    return "pages " + ", ".join(str(p) for p in pages[:-1]) + f" and {pages[-1]}"


def _capital(text_: str) -> str:
    return text_[:1].upper() + text_[1:]


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            page = int(item)
        except (TypeError, ValueError):
            continue
        if page not in out:
            out.append(page)
    return out


def _first_sentence(text_: str | None, limit: int = 160) -> str | None:
    text_ = " ".join(str(text_ or "").split())
    if not text_:
        return None
    match = re.match(r"(.+?[.!?])(\s|$)", text_)
    sentence = match.group(1) if match else text_
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def _field_label(key: str) -> str:
    words = str(key).replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _template_name(registry: Any, key: Any) -> str | None:
    if not key or registry is None:
        return None
    key = str(key)
    template = registry.documents.get(key)
    if template is None:
        matches = registry.templates_for_di_type(key)
        template = matches[0] if matches else None
    return template.display_name if template is not None else None


def document_detail(item: Mapping[str, Any], category: str, registry: Any = None) -> dict[str, Any]:
    """What a Documents row shows: the file (or document), the page(s), the
    reason in plain words and the document to open when there is one. Read
    only from the task's own reference, so it never needs another query."""
    ref = dict(item.get("reference") or {})
    task_type = str(item.get("task_type") or "")
    source = str(item.get("source_code") or ref.get("sourceCode") or "")
    pages = _int_list(ref.get("pageNumbers"))
    document_ids = [str(d) for d in (ref.get("documentIds") or []) if d]
    document_id = str(ref.get("documentId") or (document_ids[0] if document_ids else "")) or None
    file_name = ref.get("filename") or ref.get("documentLabel")
    if not file_name:
        file_name = _template_name(registry, ref.get("templateKey") or ref.get("documentTypeKey"))
    reason: str | None = None

    if task_type == "PC_PAGE_QUALITY_REJECTED":
        parts = []
        for group in ref.get("rejections") or []:
            group_pages = _int_list(group.get("pages"))
            if group_pages:
                parts.append(f"{_pages_phrase(group_pages)} {'is' if len(group_pages) == 1 else 'are'} "
                             f"{rejection_words(group.get('reason'))}")
        reason = _capital("; ".join(parts) + "." if parts else "Refused by the scan-quality check.") + \
            " Re-scan and upload again. " + dayend_removal_notice(max(1, len(pages)))
    elif task_type == "PC_UPLOAD_STATUS":
        counts = ref.get("pages") or {}
        if source == "DOCUMENT_PROCESSING_SLOW" or ref.get("retrying"):
            reason = (f"Not read yet: {counts.get('read', 0)} of {counts.get('total', 0)} pages read so far. "
                      "Nothing to do yet.")
        elif pages:
            reason = f"{_capital(_pages_phrase(pages))} could not be processed."
        else:
            reason = "The file could not be processed."
    elif task_type == "PC_VERIFY_UNRECOGNIZED_DOCUMENT":
        reason = "The document type could not be recognised, so nothing was read. Set its type."
    elif task_type == "TL_DOCUMENT_STUCK":
        counts = ref.get("pages") or {}
        reason = (f"Not read for more than a day ({counts.get('read', 0)} of {counts.get('total', 0)} "
                  "pages read).")
    elif task_type == "MANUAL_VERIFICATION_REVIEW" and ref.get("fields"):
        fields = [f for f in ref["fields"] if isinstance(f, Mapping)]
        dated = [f for f in fields if "DATE_UNREADABLE" in (f.get("reasons") or ()) or f.get("value") is not None]
        labels = ", ".join(_field_label(f.get("fieldKey") or "") for f in fields[:4])
        more = f" and {len(fields) - 4} more" if len(fields) > 4 else ""
        noun = "value" if len(fields) == 1 else "values"
        reason = (f"Check {len(fields)} {noun}: {labels}{more}. "
                  + ("A date read cannot be right." if dated else "Read with low confidence."))
    elif task_type == "PC_BOOKING_DATE_MISSING":
        reason = "No booking date was read on the form. Enter the date printed on it."
    elif task_type == "DOCUMENT_MISSING" and _WRONG_NAME_SOURCE.match(source.upper()):
        reason = _first_sentence(item.get("description")) or "The name on this document does not match."
    elif category == "MISSING_DOCUMENT" and task_type == "DOCUMENT_MISSING":
        reason = "Not on file yet. " + (_first_sentence(item.get("description")) or "")
    if not reason:
        reason = _first_sentence(ref.get("reason")) or _first_sentence(item.get("description"))
    if not file_name and category == "MISSING_DOCUMENT":
        title = str(item.get("title") or "")
        file_name = re.sub(r"^(Upload|Re-?upload) (the )?", "", title, flags=re.IGNORECASE) or None
    return {
        "category": category,
        "categoryLabel": DOCUMENT_CATEGORY_LABELS[category],
        "file": str(file_name) if file_name else None,
        "pages": pages,
        "reason": " ".join(str(reason or "").split()) or None,
        "documentId": document_id,
    }


def empty_counts() -> dict[str, int]:
    return {"TOTAL": 0, **dict.fromkeys(DOCUMENT_CATEGORIES, 0)}


def count_by_category(items: Iterable[Mapping[str, Any]]) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Document task counts across the queue and per journey (only journeys
    with at least one document task are listed)."""
    totals = empty_counts()
    by_journey: dict[str, dict[str, int]] = {}
    for item in items:
        category = item.get("document_category")
        if not category:
            continue
        journey = str(item.get("journey_id"))
        bucket = by_journey.setdefault(journey, empty_counts())
        for counts in (totals, bucket):
            counts["TOTAL"] += 1
            counts[category] += 1
    return totals, by_journey
