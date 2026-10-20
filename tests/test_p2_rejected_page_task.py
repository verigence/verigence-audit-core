"""The processing task says which pages were rejected at upload and why
(product owner 2026-10-20)."""
from __future__ import annotations

from audit_core.uc03_p2_task_producer import _failed_pages_text


def test_rejected_pages_are_named_with_their_reason():
    title, text = _failed_pages_text(
        "RJ.pdf", ["3|DI_QUALITY_IMAGE_CONTRAST", "5|DI_QUALITY_IMAGE_BLUR_SCORE", "7|DI_QUALITY_IMAGE_BLUR_SCORE"],
        [3, 5, 7], "The page is faded.",
    )
    assert title == "RJ.pdf: 3 pages rejected at upload"
    assert "Page 3 is too faded" in text
    assert "Pages 5 and 7 are blurred" in text
    assert "Scan them again" in text


def test_rejected_and_failed_pages_are_told_apart():
    title, text = _failed_pages_text(
        "RJ.pdf", ["2|DI_QUALITY_IMAGE_CONTRAST", "12|DOCUMENT_AI_RESPONSE_INVALID"], [2, 12], None,
    )
    assert title == "RJ.pdf: 1 page rejected at upload, 1 page could not be processed"
    assert "Page 2 is too faded" in text
    assert "Page 12 could not be processed" in text


def test_a_failure_with_no_error_keeps_the_old_wording():
    title, text = _failed_pages_text("scan.pdf", [], [2], "The document service refused it.")
    assert title == "scan.pdf: 1 page could not be processed"
    assert text.startswith("The document service refused it. Page 2 could not be processed")
