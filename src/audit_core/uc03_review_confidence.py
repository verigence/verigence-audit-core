from __future__ import annotations

"""uc03_review_confidence.py — the one confidence-review policy, for every
document extraction, Booking or Delivery alike.

Rule, stated once: a DI-extracted value at or above 90% confidence needs no PC
review; below 90% (or missing a confidence score entirely), PC review is
required. This used to exist as two copies that could disagree --
uc03_document_review_v2.py's own module-level default (92.0%) and
uc03_confidence_review_policy.py's REVIEW_THRESHOLD_PERCENT (90.0%), kept in
sync only by uc03_confidence_review_policy.py monkeypatching the first one at
app startup. They matched in practice only because the patch always ran
before any request; if it hadn't, Review would have silently graded against
92% instead of 90%.

This module has no dependents of its own (no DB access, no other uc03_*
imports) specifically so both uc03_document_review_v2.py and
uc03_confidence_review_policy.py can import it directly without creating a
cycle between them.
"""

from typing import Any, Literal

REVIEW_THRESHOLD_PERCENT = 90.0


def requires_pc_review(confidence_score: float | None) -> bool:
    """True only when a DI fact cannot satisfy the 90% trust threshold."""

    return confidence_score is None or float(confidence_score) < REVIEW_THRESHOLD_PERCENT


def has_value(value: Any) -> bool:
    return value is not None and value != ""


def field_review_state(
    *,
    value: Any,
    confidence_score: float | None,
) -> Literal["READY", "NEEDS_REVIEW"]:
    """A single field's own review state: exception-only, same as
    requires_pc_review -- an unpopulated field carries nothing to review, so
    its own reviewState follows confidence alone, not whether a value is
    present. (Every real call site already filters to populated sources
    before this matters; `value` stays a named parameter for readability at
    call sites and because callers pass it positionally-by-keyword already,
    not because this function branches on it.)"""

    del value
    return "NEEDS_REVIEW" if requires_pc_review(confidence_score) else "READY"
