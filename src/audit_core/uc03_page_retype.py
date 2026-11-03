"""PAGE_RETYPE: read stored pages again as a named document type (2026-11-02).

The same path a PC's "Set the document type" takes (uc03_p2_api._send_page_again):
a new unit is queued with exactly that type, DI reads it again from the stored
page, and the earlier unit is retired (merged into the new one). Used to read
existing pages under a corrected type: a cost sheet as the dealer statement
reading (customer_ledger), a Form 21 or delivery receipt filed as an invoice or
receipt as a supporting document.

Tag ``<tenant_id>:<label>``; a DRY RUN (nothing sent) unless the label ends
with ``-commit``. ``reports`` lists the pages as
``<journey reference>:<page number>:<template key>``, at most 60 per job.
A page that is still being read, or whose stage a person marked reviewed, is
left alone and listed with why.

A page read as one document with other pages (a GROUP unit) is split
(2026-11-03): live, Shivnath's Offer Letter / Payment Details sheet (its NDC)
was read together with the Mahindra booking form as one booking docket
(SHI-SS-261031-008, pages 1 and 3), so the booking form's booking amount was
taken as a corporate discount and the letter's lines never reached the Deal.
The page asked for is read alone as the type asked for; every other page of
the group is read alone as the group's own type, each from its own stored
page; the group is retired (MERGED into the page asked for).
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context

logger = structlog.get_logger(__name__)

MAX_PAGES = 60
# An optional fourth part names the type the page is read as now, to pick the
# unit when the booking has two uploads with a page of that number (2026-11-03,
# SHI-SS-261031-005: page 1 of the delivery file and page 1 of the ledger file).
_ITEM = re.compile(r"^(?P<reference>[A-Z0-9][A-Z0-9-]{2,63}):(?P<page>[1-9][0-9]{0,3}):(?P<template>[a-z0-9_]{2,64})"
                   r"(:(?P<current>[a-z0-9_]{2,64}))?$")
_ACTOR = "SYSTEM:PAGE_RETYPE"


def parse_request(tag: str, reports: list[str]) -> tuple[str, bool, list[tuple[str, int, str, str | None]]]:
    tenant_id, _, label = (tag or "").partition(":")
    if not tenant_id or not label:
        raise ValueError("the tag must be '<tenant_id>:<label>'")
    items: list[tuple[str, int, str, str | None]] = []
    for raw in reports:
        text_ = str(raw).strip()
        reference, _, rest = text_.partition(":")
        match = _ITEM.match(f"{reference.upper()}:{rest}")
        if not match:
            raise ValueError(f"{raw!r} is not '<journey reference>:<page>:<template key>'")
        items.append((match["reference"], int(match["page"]), match["template"], match["current"]))
    if not items or len(items) > MAX_PAGES:
        raise ValueError(f"ask for 1 to {MAX_PAGES} pages")
    return tenant_id, label.endswith("-commit"), items


def _current_unit(connection: Any, *, tenant_id: str, reference: str, page: int,
                  current: str | None = None) -> dict[str, Any] | None:
    """The booking and the live (not merged, not retired) unit holding the page
    (read now as ``current``, when given)."""
    row = connection.execute(
        text(
            """
            SELECT j.journey_id, q.queue_id
            FROM auditcore.journeys j
            JOIN auditcore.p2_document_queue q ON q.tenant_id = j.tenant_id AND q.journey_id = j.journey_id
            WHERE j.tenant_id = :t AND upper(j.journey_reference) = :r AND :p = ANY(q.page_numbers)
              AND q.queue_status <> 'MERGED' AND q.retired_at_utc IS NULL
              AND (CAST(:c AS text) IS NULL OR COALESCE(q.template_key, q.classified_document_type) = :c)
            ORDER BY q.created_at_utc DESC
            LIMIT 1
            """
        ),
        {"t": tenant_id, "r": reference, "p": page, "c": current},
    ).mappings().first()
    return dict(row) if row else None


def _own_page_unit(connection: Any, *, tenant_id: str, journey_id: UUID, batch_id: Any,
                   page: int) -> UUID | None:
    """The unit of one page as it was uploaded (unit_kind PAGE), which holds
    that page's own stored file."""
    found = connection.execute(
        text(
            """
            SELECT queue_id FROM auditcore.p2_document_queue
            WHERE tenant_id = :t AND journey_id = :j AND batch_id = :b AND unit_kind = 'PAGE'
              AND page_numbers = ARRAY[:p]::int[]
            ORDER BY created_at_utc DESC
            LIMIT 1
            """
        ),
        {"t": tenant_id, "j": journey_id, "b": batch_id, "p": page},
    ).scalar_one_or_none()
    return UUID(str(found)) if found is not None else None


def _split(connection: Any, *, tenant_id: str, journey_id: UUID, group: dict[str, Any], page: int, template: Any,
           tag: str, registry: Any, named: dict[int, Any] | None = None) -> list[dict[str, Any]] | str:
    """The group's pages read alone: ``page`` as ``template``, each other page
    as the type the same job names for it (``named``), else as the group's own
    type. Returns what was sent, or why nothing was."""
    from audit_core.uc03_p2_api import _page_unit, _send_page_again

    own_type = registry.documents.get(group["template_key"] or "")
    if own_type is None or not own_type.di_types:
        return f"the group's own type {group['template_key']!r} is not a known template"
    pages = [int(p) for p in (group["page_numbers"] or [group["page_number"]])]
    units = {p: _own_page_unit(connection, tenant_id=tenant_id, journey_id=journey_id, batch_id=group["batch_id"],
                               page=p) for p in pages}
    missing = [p for p, unit in units.items() if unit is None]
    if missing:
        return "no stored page of its own for page " + ", ".join(map(str, missing))
    sent = []
    for each in [page] + [p for p in pages if p != page]:
        kind = template if each == page else (named or {}).get(each) or own_type
        unit = _page_unit(connection, tenant_id=tenant_id, journey_id=journey_id, queue_id=units[each])
        if not kind.di_types:
            # A type that is never read (a supporting page, a cover sheet): the
            # page stays on file on its own, as such, and is not sent.
            connection.execute(
                text(
                    """
                    UPDATE auditcore.p2_document_queue
                    SET queue_status = 'SUPPORTING', merged_into_queue_id = NULL, template_key = :k,
                        status_reason = :why, updated_at_utc = now()
                    WHERE tenant_id = :t AND queue_id = :q
                    """
                ),
                {"t": tenant_id, "q": units[each], "k": kind.key,
                 "why": f"Kept on file, not read: split from pages {pages} by job {tag}."},
            )
            sent.append({"page": each, "to": kind.key, "newQueueId": str(units[each]), "kept": True})
            continue
        new_id = _send_page_again(
            connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit, template=kind,
            di_type=kind.di_types[0], actor_id=_ACTOR, role="SYSTEM", swept=False, event_type="PAGE_TYPE_SET",
            override_reason=f"Split from pages {pages} by job {tag}", correlation_id=tag,
        )
        sent.append({"page": each, "to": kind.key, "newQueueId": str(new_id)})
    connection.execute(
        text(
            """
            UPDATE auditcore.p2_document_queue
            SET queue_status = 'MERGED', merged_into_queue_id = :into, updated_at_utc = now(),
                status_reason = :why
            WHERE tenant_id = :t AND queue_id = :q
            """
        ),
        {"t": tenant_id, "q": group["queue_id"], "into": next(x["newQueueId"] for x in sent if not x.get("kept")),
         "why": "Split into its pages, each read alone: " + ", ".join(f"page {s['page']} as {s['to']}" for s in sent)},
    )
    return sent


def run_page_retype(engine: Engine, tag: str, reports: list[str]) -> dict[str, Any]:
    from audit_core.uc03_p2_api import (
        _RETYPEABLE_PAGE_STATES,
        _guard_reviewed_stage,
        _is_swept,
        _page_unit,
        _send_page_again,
    )
    from audit_core.uc03_p2_registry import get_registry

    tenant_id, commit, items = parse_request(tag, reports)
    registry = get_registry()
    out: dict[str, Any] = {"commit": commit, "sent": [], "wouldSend": [], "skipped": []}
    asked = {(reference, page): registry.documents.get(key) for reference, page, key, _ in items}
    split_done: set[tuple[str, int]] = set()
    for reference, page, template_key, current in items:
        if (reference, page) in split_done:
            continue  # read alone already, as asked, when its group was split
        template = registry.documents.get(template_key)
        if template is None or not template.di_types:
            out["skipped"].append({"journey": reference, "page": page, "why": f"unknown template {template_key}"})
            continue
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            found = _current_unit(connection, tenant_id=tenant_id, reference=reference, page=page, current=current)
            if found is None:
                out["skipped"].append({"journey": reference, "page": page, "why": "no live page"})
                continue
            journey_id = UUID(str(found["journey_id"]))
            unit = _page_unit(connection, tenant_id=tenant_id, journey_id=journey_id,
                              queue_id=UUID(str(found["queue_id"])))
            swept = unit["queue_status"] == "CANCELLED" and _is_swept(
                connection, tenant_id=tenant_id, queue_id=UUID(str(unit["queue_id"])))
            grouped = len(unit["page_numbers"] or []) > 1
            # A document made from several pages that was never read (live
            # 2026-11-03, SHI-SS-261027-007: a cover sheet and the Offer
            # Letter, CANCELLED) is split as well: each page is read alone.
            never_read = grouped and unit["queue_status"] in ("CANCELLED", "DEAD_LETTER")
            if unit["queue_status"] not in _RETYPEABLE_PAGE_STATES and not swept and not never_read:
                out["skipped"].append({"journey": reference, "page": page,
                                       "why": f"page is {unit['queue_status']} (still being read)"})
                continue
            try:
                _guard_reviewed_stage(connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit,
                                      target_stage=template.stage, role=None, reason=None)
            except Exception as exc:  # noqa: BLE001 - a reviewed stage is reported, never overridden here
                out["skipped"].append({"journey": reference, "page": page, "why": str(getattr(exc, "detail", exc))})
                continue
            entry = {"journey": reference, "page": page, "from": unit["template_key"] or unit["classified_document_type"],
                     "to": template_key}
            if grouped:
                entry["splitFrom"] = [int(p) for p in unit["page_numbers"]]
            if not commit:
                out["wouldSend"].append(entry)
                continue
            if grouped:
                named = {p: kind for (ref, p), kind in asked.items() if ref == reference and kind is not None}
                split = _split(connection, tenant_id=tenant_id, journey_id=journey_id, group=unit, page=page,
                               template=template, tag=tag, registry=registry, named=named)
                if isinstance(split, str):
                    out["skipped"].append({"journey": reference, "page": page, "why": split})
                    continue
                split_done.update((reference, int(p)) for p in unit["page_numbers"])
                out["sent"].append({**entry, "newQueueId": split[0]["newQueueId"], "pages": split})
                logger.info("page_retype_split", tag=tag, journey=reference, page=page, to=template_key)
                continue
            new_id = _send_page_again(
                connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit, template=template,
                di_type=template.di_types[0], actor_id=_ACTOR, role="SYSTEM", swept=swept,
                event_type="PAGE_TYPE_SET", override_reason=f"Re-typed by job {tag}", correlation_id=tag,
            )
            out["sent"].append({**entry, "newQueueId": str(new_id)})
        logger.info("page_retype_item", tag=tag, journey=reference, page=page, to=template_key, commit=commit)
    return out
