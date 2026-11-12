"""The message on a page no document type fits (product owner 2026-11-12): "system doesn't
understand the document. Pls recheck and if required submit again. For issues with quality,
continue with same error message"."""
from __future__ import annotations

from datetime import UTC, datetime

from audit_core import uc03_p2_worker as worker


def _outcome(di_item):
    return worker.classify_page_outcome(di_item=di_item, extracted_count=0, submitted_at=datetime.now(UTC),
                                        facts_copy=None, now=datetime.now(UTC))


def test_a_page_no_type_fits_says_the_system_does_not_understand_it() -> None:
    outcome = _outcome({"state": "UNKNOWN"})
    assert outcome.status == "SUPPORTING"
    assert outcome.reason == ("The system does not understand this document. Please recheck it and, if required, "
                              "submit it again.")


def test_a_page_refused_for_its_scan_quality_keeps_its_own_message() -> None:
    outcome = _outcome({"state": "FAILED", "failureCode": "DI_QUALITY_IMAGE_BLUR_SCORE",
                        "failureDetail": "the page is blurred"})
    assert outcome.reason.startswith("Page rejected: the page is blurred.")
    assert "does not understand" not in outcome.reason
