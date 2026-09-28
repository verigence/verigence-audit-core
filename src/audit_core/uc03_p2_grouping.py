"""Group the pages of a multi-document PDF into business documents.

Pure planning logic, driven entirely by each template's page rule:

- SINGLE      every page is its own document. Six consecutive receipts are six
              receipts, never one.
- PARTS       a fixed small set of parts (Aadhaar front/back, RC front/back,
              the Booking Commitment Form + Order Taking Form pair), up to
              ``max_pages``.
- MULTI_PAGE  a variable-length document; continuation pages of the same type,
              join it (GST annexures, statement pages), up to ``max_pages``.
              A page DI could not classify joins only when it is sandwiched
              between pages of the document, or when the template sets
              ``absorb_unknown`` (untitled statement/ledger/schedule pages) --
              so an unrelated trailing page is never swallowed.

``group: ADJACENT`` requires each additional page to directly follow the
previous one; ``group: BATCH`` collects matching pages from anywhere in the
upload (the two booking forms are often not next to each other).

Failed or cancelled pages are never absorbed and always stand alone.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader, PdfWriter

from audit_core.uc03_p2_registry import SUPPORTING_TEMPLATE, Registry

_UNGROUPABLE_STATUSES = frozenset({"FAILED", "DEAD_LETTER", "CANCELLED"})


@dataclass(frozen=True)
class PageFact:
    page_number: int
    di_type: str | None
    status: str
    stage: str | None = None


@dataclass(frozen=True)
class PlannedDocument:
    template_key: str
    di_type: str | None
    page_numbers: tuple[int, ...]

    @property
    def is_multi_page(self) -> bool:
        return len(self.page_numbers) > 1


def read_once_di_types(registry: Registry) -> frozenset[str]:
    """DI types whose pages are always merged into one document (PARTS or
    MULTI_PAGE in every template that uses them).

    Gemini cost: a page of such a type is only *classified*; the merged
    document is extracted once. Extracting each page first and the merged
    document again paid for every page twice. Types also used by a SINGLE
    template (e.g. receipts) keep page-level extraction."""
    shapes: dict[str, set[str]] = {}
    for template in registry.documents.values():
        if template.is_supporting:
            continue
        for di_type in template.di_types:
            shapes.setdefault(di_type, set()).add(template.pages.shape)
    return frozenset(t for t, s in shapes.items() if "SINGLE" not in s)


def needs_document_upload(document: PlannedDocument, read_once: frozenset[str]) -> bool:
    """A planned document gets its own DI upload (extracted once) when it
    spans pages, or when its page was only classified (read_once)."""
    return document.is_multi_page or (
        document.template_key != SUPPORTING_TEMPLATE
        and document.di_type is not None
        and document.di_type in read_once
    )


def plan_documents(pages: list[PageFact], registry: Registry) -> list[PlannedDocument]:
    ordered = sorted(pages, key=lambda page: page.page_number)
    template_of = {
        page.page_number: (
            registry.template_for_di_type(page.di_type, stage=page.stage)
            if page.status not in _UNGROUPABLE_STATUSES
            else None
        )
        for page in ordered
    }
    assigned: set[int] = set()
    planned: list[PlannedDocument] = []

    # BATCH-grouped templates first: collect matching pages across the upload.
    for template in registry.documents.values():
        if template.pages.group != "BATCH" or template.pages.shape == "SINGLE":
            continue
        members = [
            page for page in ordered
            if template_of[page.page_number] is not None
            and template_of[page.page_number].key == template.key
        ]
        for start in range(0, len(members), template.pages.max_pages):
            chunk = members[start:start + template.pages.max_pages]
            planned.append(
                PlannedDocument(
                    template_key=template.key,
                    di_type=chunk[0].di_type,
                    page_numbers=tuple(page.page_number for page in chunk),
                )
            )
            assigned.update(page.page_number for page in chunk)

    # ADJACENT grouping in page order.
    current: list[PageFact] = []
    current_template = None

    def flush() -> None:
        nonlocal current, current_template
        if current:
            planned.append(
                PlannedDocument(
                    template_key=current_template.key if current_template else SUPPORTING_TEMPLATE,
                    di_type=current[0].di_type if current_template else None,
                    page_numbers=tuple(page.page_number for page in current),
                )
            )
        current, current_template = [], None

    def next_template(index: int):
        if index + 1 < len(ordered) and ordered[index + 1].page_number == ordered[index].page_number + 1:
            return template_of[ordered[index + 1].page_number]
        return None

    for index, page in enumerate(ordered):
        if page.page_number in assigned:
            flush()
            continue
        template = template_of[page.page_number]
        joins = (
            current_template is not None
            and template is not None
            and current_template.pages.shape != "SINGLE"
            and current_template.pages.group == "ADJACENT"
            and len(current) < current_template.pages.max_pages
            and page.page_number == current[-1].page_number + 1
            and (
                template.key == current_template.key
                or (
                    template.key == SUPPORTING_TEMPLATE
                    and current_template.pages.shape == "MULTI_PAGE"
                    and current_template.key != SUPPORTING_TEMPLATE
                    and (
                        current_template.pages.absorb_unknown
                        # sandwiched: the following page continues the same document
                        or (
                            next_template(index) is not None
                            and next_template(index).key == current_template.key
                        )
                    )
                )
            )
        )
        if joins:
            current.append(page)
            continue
        flush()
        current = [page]
        current_template = template
    flush()
    return sorted(planned, key=lambda document: document.page_numbers[0])


def merge_pdf_pages(page_payloads: list[bytes]) -> bytes:
    """Combine single-page PDFs (in the given order) into one PDF."""
    writer = PdfWriter()
    for payload in page_payloads:
        for page in PdfReader(io.BytesIO(payload)).pages:
            writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
