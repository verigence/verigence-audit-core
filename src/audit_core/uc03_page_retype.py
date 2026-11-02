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
_ITEM = re.compile(r"^(?P<reference>[A-Z0-9][A-Z0-9-]{2,63}):(?P<page>[1-9][0-9]{0,3}):(?P<template>[a-z0-9_]{2,64})$")
_ACTOR = "SYSTEM:PAGE_RETYPE"


def parse_request(tag: str, reports: list[str]) -> tuple[str, bool, list[tuple[str, int, str]]]:
    tenant_id, _, label = (tag or "").partition(":")
    if not tenant_id or not label:
        raise ValueError("the tag must be '<tenant_id>:<label>'")
    items: list[tuple[str, int, str]] = []
    for raw in reports:
        text_ = str(raw).strip()
        reference, _, rest = text_.partition(":")
        match = _ITEM.match(f"{reference.upper()}:{rest}")
        if not match:
            raise ValueError(f"{raw!r} is not '<journey reference>:<page>:<template key>'")
        items.append((match["reference"], int(match["page"]), match["template"]))
    if not items or len(items) > MAX_PAGES:
        raise ValueError(f"ask for 1 to {MAX_PAGES} pages")
    return tenant_id, label.endswith("-commit"), items


def _current_unit(connection: Any, *, tenant_id: str, reference: str, page: int) -> dict[str, Any] | None:
    """The booking and the live (not merged, not retired) unit holding the page."""
    row = connection.execute(
        text(
            """
            SELECT j.journey_id, q.queue_id
            FROM auditcore.journeys j
            JOIN auditcore.p2_document_queue q ON q.tenant_id = j.tenant_id AND q.journey_id = j.journey_id
            WHERE j.tenant_id = :t AND upper(j.journey_reference) = :r AND :p = ANY(q.page_numbers)
              AND q.queue_status <> 'MERGED' AND q.retired_at_utc IS NULL
            ORDER BY q.created_at_utc DESC
            LIMIT 1
            """
        ),
        {"t": tenant_id, "r": reference, "p": page},
    ).mappings().first()
    return dict(row) if row else None


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
    for reference, page, template_key in items:
        template = registry.documents.get(template_key)
        if template is None or not template.di_types:
            out["skipped"].append({"journey": reference, "page": page, "why": f"unknown template {template_key}"})
            continue
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            found = _current_unit(connection, tenant_id=tenant_id, reference=reference, page=page)
            if found is None:
                out["skipped"].append({"journey": reference, "page": page, "why": "no live page"})
                continue
            journey_id = UUID(str(found["journey_id"]))
            unit = _page_unit(connection, tenant_id=tenant_id, journey_id=journey_id,
                              queue_id=UUID(str(found["queue_id"])))
            swept = unit["queue_status"] == "CANCELLED" and _is_swept(
                connection, tenant_id=tenant_id, queue_id=UUID(str(unit["queue_id"])))
            if unit["queue_status"] not in _RETYPEABLE_PAGE_STATES and not swept:
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
            if not commit:
                out["wouldSend"].append(entry)
                continue
            new_id = _send_page_again(
                connection, tenant_id=tenant_id, journey_id=journey_id, unit=unit, template=template,
                di_type=template.di_types[0], actor_id=_ACTOR, role="SYSTEM", swept=swept,
                event_type="PAGE_TYPE_SET", override_reason=f"Re-typed by job {tag}", correlation_id=tag,
            )
            out["sent"].append({**entry, "newQueueId": str(new_id)})
        logger.info("page_retype_item", tag=tag, journey=reference, page=page, to=template_key, commit=commit)
    return out
