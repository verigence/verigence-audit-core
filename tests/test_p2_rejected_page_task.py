"""The resubmit task says which pages were rejected at upload and why
(product owner 2026-10-20), page by page (2026-10-21)."""
from __future__ import annotations

from audit_core.uc03_p2_task_producer import resubmit_page_reason


def test_rejected_pages_are_named_with_their_reason():
    assert resubmit_page_reason("DI_QUALITY_IMAGE_CONTRAST", "Page rejected: contrast 0.1.") == (
        "Rejected by the scan-quality check: too faded. Scan it again in good light, flat and in focus, and upload "
        "it again.")
    assert resubmit_page_reason("DI_QUALITY_IMAGE_BLUR_SCORE", None).startswith(
        "Rejected by the scan-quality check: blurred.")
    assert resubmit_page_reason("CORRUPT", None).startswith("Rejected by the scan-quality check: unreadable.")


def test_other_failures_keep_the_reason_on_the_card():
    assert resubmit_page_reason("DOCUMENT_AI_RESPONSE_INVALID", "AI reading failed — tried 3 times.") == (
        "AI reading failed — tried 3 times.")
    assert resubmit_page_reason(None, None) == "It could not be read. Upload it again."


def test_our_setup_problem_needs_no_re_upload():
    assert resubmit_page_reason("DOCUMENT_AI_NOT_CONFIGURED", "anything") == (
        "Our setup problem — will be re-read once fixed; no need to re-upload.")
