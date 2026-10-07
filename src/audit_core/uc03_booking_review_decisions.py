from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.uc03_document_registry import is_receipt_document_type
from audit_core.uc03_review_confidence import requires_pc_review
from audit_core.uc03_v2_review_materialization import (
    receipt_document_ordinals,
    receipt_review_key,
)

ReviewKind = Literal["ATTRIBUTE", "RAW_FIELD"]


@dataclass(frozen=True)
class _ReviewItem:
    review_key: str
    review_kind: ReviewKind
    decision_required: bool
    source_set_ref: str
    source_document_id: UUID
    source_canonical_field_id: str | None
    source_field_key: str
    source_fact_version: int


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _normalized_value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _source_set_ref(sources: list[Any]) -> str:
    refs = sorted(
        [
            [
                str(source.documentId),
                str(source.canonicalFieldId or ""),
                str(source.fieldKey),
                int(source.sourceFactVersion),
            ]
            for source in sources
        ]
    )
    return json.dumps(refs, separators=(",", ":"))


def _mapped_review_items(
    attributes: list[review_v2.ReviewV2Attribute],
) -> list[_ReviewItem]:
    items: list[_ReviewItem] = []
    for attribute in attributes:
        source = attribute.resolvedSource
        if source is None or not _has_value(attribute.resolvedValue):
            continue
        items.append(
            _ReviewItem(
                review_key=f"attribute:{attribute.attributeKey}",
                review_kind="ATTRIBUTE",
                decision_required=attribute.reviewState == "NEEDS_REVIEW",
                source_set_ref=_source_set_ref(attribute.sources),
                source_document_id=source.documentId,
                source_canonical_field_id=source.canonicalFieldId,
                source_field_key=source.fieldKey,
                source_fact_version=source.sourceFactVersion,
            )
        )
    return items


def _build_raw_review_item(
    review_key: str,
    sources: list[review_v2.ReviewV2UnmappedField],
) -> _ReviewItem | None:
    populated = [source for source in sources if _has_value(source.value)]
    if not populated:
        return None
    selected = min(
        populated,
        key=lambda source: (
            -(source.confidenceScore if source.confidenceScore is not None else -1.0),
            source.documentLabel.casefold(),
            str(source.documentId),
            source.canonicalFieldId,
            source.sourceFactVersion,
        ),
    )
    distinct_values = {_normalized_value(source.value) for source in populated}
    low_confidence = requires_pc_review(selected.confidenceScore)
    return _ReviewItem(
        review_key=review_key,
        review_kind="RAW_FIELD",
        decision_required=len(distinct_values) > 1 or low_confidence,
        source_set_ref=_source_set_ref(sources),
        source_document_id=selected.documentId,
        source_canonical_field_id=selected.canonicalFieldId,
        source_field_key=selected.fieldKey,
        source_fact_version=selected.sourceFactVersion,
    )


def _raw_review_items(
    unmapped: list[review_v2.ReviewV2UnmappedField],
) -> list[_ReviewItem]:
    grouped: dict[str, list[review_v2.ReviewV2UnmappedField]] = {}
    receipt_grouped: dict[
        tuple[UUID, str], list[review_v2.ReviewV2UnmappedField]
    ] = {}
    receipt_document_ids: list[UUID] = []

    for field in unmapped:
        if is_receipt_document_type(field.documentTypeKey):
            receipt_grouped.setdefault((field.documentId, field.fieldKey), []).append(field)
            receipt_document_ids.append(field.documentId)
        else:
            grouped.setdefault(field.fieldKey, []).append(field)

    items: list[_ReviewItem] = []
    for field_key, sources in grouped.items():
        item = _build_raw_review_item(f"raw:{field_key}", sources)
        if item is not None:
            items.append(item)

    ordinals = receipt_document_ordinals(receipt_document_ids)
    for (document_id, field_key), sources in receipt_grouped.items():
        item = _build_raw_review_item(
            receipt_review_key(ordinals[document_id], field_key),
            sources,
        )
        if item is not None:
            items.append(item)
    return items


def _current_review_items(
    attributes: list[review_v2.ReviewV2Attribute],
    unmapped: list[review_v2.ReviewV2UnmappedField],
) -> dict[str, _ReviewItem]:
    items = _mapped_review_items(attributes) + _raw_review_items(unmapped)
    return {item.review_key: item for item in items}




# set_booking_review_decision removed (Phase 0 monkeypatch removal):
# confirmed dead (no callers besides its own registration) -- its route was
# always discarded by install_uc03_confidence_review_policy's later
# _replace_route call. set_booking_review_decision_confidence_policy
# (uc03_confidence_review_policy.py) is the live handler -- functionally
# equivalent (same journey_attribute_review_decisions write), now decorated
# directly on review_v2.router's POST /booking/review/decision.


def _install_mismatch_review_rule() -> None:
    if getattr(review_v2, "_mismatch_review_rule_installed", False):
        return
    original = review_v2._build_attributes

    def wrapped(*args: Any, **kwargs: Any):
        attributes, unmapped = original(*args, **kwargs)
        for attribute in attributes:
            if attribute.resolvedValue is not None and attribute.comparisonState == "MISMATCH":
                attribute.reviewState = "NEEDS_REVIEW"
        return attributes, unmapped

    review_v2._build_attributes = wrapped  # type: ignore[assignment]
    review_v2._mismatch_review_rule_installed = True


# _replace_confirm_route removed (Phase 0 monkeypatch removal): there was
# never a plain @router decorator for POST /booking/review/confirm anywhere
# in the codebase -- this route was assembled entirely through a chain of
# runtime add_api_route/_replace_route calls (this one, then uc03_review_
# effective_values.py's, then uc03_booking_rule_trigger.py's, each replacing
# the last, ending at uc03_confidence_review_policy.py's
# confirm_booking_review_v2_confidence_policy, which actually won in
# production). That function is now the first-ever plain @router decorator
# for this path. confirm_booking_review_v2_with_decisions stays defined
# here as plain library code -- test_uc03_booking_review_resolution_wiring.py
# inspects its bytecode directly -- just no longer registered as a route.


def install_uc03_booking_review_decisions() -> None:
    """Install V2 Booking Review exception decisions without altering V1 flows."""

    if getattr(review_v2, "_booking_review_decisions_installed", False):
        return
    _install_mismatch_review_rule()
    # /booking/review/decision (POST) registration removed here (Phase 0
    # monkeypatch removal): set_booking_review_decision_confidence_policy
    # (uc03_confidence_review_policy.py) is decorated directly on this
    # router for that path instead.
    review_v2._booking_review_decisions_installed = True
