"""PAGE_IMAGES: a picture of a few stored pages, for review (2026-11-02).

Designing how a document is read needs the real page in front of whoever
designs it. The pages live only in storage, so this one-off job looks up the
pages asked for, renders each as a small greyscale JPEG and returns it on the
job's row (``p2_worker_jobs.detail``). Nothing is written anywhere else.

The job's tag is ``<tenant_id>:<label>``; ``reports`` lists the pages as
``<journey reference>:<page number>``, at most three per job, because a job
row keeps at most 200,000 characters of what the job returns.
"""
from __future__ import annotations

import base64
import io
import re
from typing import Any

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

MAX_PAGES = 3
# What one job may return in all, under the job row's 200,000-character limit.
_BUDGET_CHARS = 180_000
_PAGE = re.compile(r"^(?P<reference>[A-Z0-9][A-Z0-9-]{2,63}):(?P<page>[1-9][0-9]{0,3})$")
# Long side first, then JPEG quality, until the picture fits its share.
_SIZES = (1600, 1400, 1200, 1000, 850)
_QUALITIES = (70, 60, 50, 40)


def parse_request(tag: str, reports: list[str]) -> tuple[str, list[tuple[str, int]]]:
    tenant_id, _, label = (tag or "").partition(":")
    if not tenant_id or not label:
        raise ValueError("the tag must be '<tenant_id>:<label>'")
    pages: list[tuple[str, int]] = []
    for item in reports:
        match = _PAGE.match(str(item).strip().upper())
        if not match:
            raise ValueError(f"{item!r} is not '<journey reference>:<page number>'")
        pages.append((match["reference"], int(match["page"])))
    if not pages or len(pages) > MAX_PAGES:
        raise ValueError(f"ask for 1 to {MAX_PAGES} pages")
    return tenant_id, pages


def page_object_key(connection: Any, *, tenant_id: str, reference: str, page: int) -> tuple[str, int] | None:
    """The stored single page (``pages/``) of that booking's upload that holds
    the page, else the group it was read in; with the page's place in it."""
    row = connection.execute(
        text(
            """
            SELECT q.page_object_key, q.page_numbers
            FROM auditcore.journeys j
            JOIN auditcore.p2_upload_batches b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
            JOIN auditcore.p2_document_queue q ON q.tenant_id = b.tenant_id AND q.batch_id = b.batch_id
            WHERE j.tenant_id = :t AND j.journey_reference = :r AND :p = ANY(q.page_numbers)
              AND q.page_object_key IS NOT NULL
            ORDER BY (q.page_object_key LIKE '%/pages/%') DESC, cardinality(q.page_numbers), q.page_object_key
            LIMIT 1
            """
        ),
        {"t": tenant_id, "r": reference, "p": page},
    ).first()
    if row is None:
        return None
    return str(row[0]), list(row[1]).index(page)


def render_jpeg(pdf_bytes: bytes, *, budget_chars: int, page_index: int = 0) -> tuple[str, int, int]:
    """The page as a greyscale JPEG, base64, no longer than ``budget_chars``."""
    import pypdfium2

    document = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page = document[min(page_index, len(document) - 1)]
        width_pt, height_pt = page.get_size()
        image = page.render(scale=_SIZES[0] / max(width_pt, height_pt)).to_pil().convert("L")
    finally:
        document.close()
    for size in _SIZES:
        scaled = image.copy()
        scaled.thumbnail((size, size))
        for quality in _QUALITIES:
            buffer = io.BytesIO()
            scaled.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            if len(encoded) <= budget_chars:
                return encoded, scaled.width, scaled.height
    raise ValueError("the page does not fit in the space a job row keeps")


def run_page_images(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_p2_storage import get_p2_document_storage

    tenant_id, pages = parse_request(tag, reports)
    storage = get_p2_document_storage()
    budget = _BUDGET_CHARS // len(pages)
    out: dict[str, Any] = {"pages": [], "missing": []}
    for reference, page in pages:
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            found = page_object_key(connection, tenant_id=tenant_id, reference=reference, page=page)
        if found is None:
            out["missing"].append({"journey": reference, "page": page, "why": "no stored page"})
            continue
        key, index = found
        try:
            encoded, width, height = render_jpeg(storage.get_object(key), budget_chars=budget, page_index=index)
        except Exception as exc:  # noqa: BLE001 - reported; the other pages still go out
            out["missing"].append({"journey": reference, "page": page, "why": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        out["pages"].append({"journey": reference, "page": page, "objectKey": key,
                             "width": width, "height": height, "jpegBase64": encoded})
        logger.info("page_image_rendered", tag=tag, journey=reference, page=page, chars=len(encoded))
    return out
