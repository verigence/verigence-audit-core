"""Why Document Intelligence refuses some answers (2026-11-17). Reads only.

    python di_refused.py <di-worker-vars.json> <tenant-id>

Every document standing FAILED with DOCUMENT_AI_RESPONSE_INVALID ("the provider
returned an unusable response") is read once more with production's own
request, and what came back is described WITHOUT any of its text: whether it
is JSON, how many objects it holds, the page and the place on the page each
object's fields were read from, and for each requested field whether the
objects give the same value, different values, or only one of them gives it.

Nothing is written to any database or to storage. The repository is public
and so is this job's log: counts, document ids, field NAMES (the schema's own)
and the words same / differ only. No value is printed.
"""
from __future__ import annotations

import asyncio
import io
import json
import statistics
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from di_side import Variant, call, load_environment

SPEND_LIMIT_USD = 5.0
CONCURRENCY = 6


def _value(entry: Any) -> str | None:
    """A field's value as text to compare, spaces and case apart; None when not given."""
    raw = entry.get("value") if isinstance(entry, dict) else entry
    if raw is None or raw == "" or raw == []:
        return None
    text = json.dumps(raw, sort_keys=True) if isinstance(raw, (list, dict)) else str(raw)
    return " ".join(text.lower().split()) or None


def _place(item: dict[str, Any]) -> tuple[list[int], int | None, int | None]:
    """The pages an object's fields cite, and how far down the page they sit
    (box_2d is [ymin, xmin, ymax, xmax] on a 0-1000 scale): top and bottom."""
    pages, tops, bottoms = set(), [], []
    for entry in item.values():
        if not isinstance(entry, dict):
            continue
        page = entry.get("pageNo")
        if isinstance(page, int) and not isinstance(page, bool) and page >= 1:
            pages.add(page)
        box = entry.get("box_2d")
        if isinstance(box, list) and len(box) == 4 and all(isinstance(n, (int, float)) for n in box):
            tops.append(float(box[0]))
            bottoms.append(float(box[2]))
    return sorted(pages), (round(min(tops)) if tops else None), (round(max(bottoms)) if bottoms else None)


def describe(answer_text: str, wanted: list[str]) -> dict[str, Any]:
    """What an answer is, with none of its text."""
    text = answer_text.strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.split("\n") if not line.startswith("```")).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"kind": "NOT_JSON", "reason": f"{exc.msg} at char {exc.pos} of {len(text)}"}
    if isinstance(data, dict):
        return {"kind": "ONE_OBJECT", "fieldsGiven": sum(1 for k in wanted if _value(data.get(k)) is not None)}
    if not isinstance(data, list):
        return {"kind": f"OTHER_{type(data).__name__}"}
    items = [item for item in data if isinstance(item, dict)]
    out: dict[str, Any] = {"kind": "LIST", "objects": len(data), "notObjects": len(data) - len(items),
                           "places": [], "fieldsGiven": []}
    for item in items:
        pages, top, bottom = _place(item)
        out["places"].append({"pages": pages, "top": top, "bottom": bottom})
        out["fieldsGiven"].append(sum(1 for k in wanted if _value(item.get(k)) is not None))
    compared: dict[str, str] = {}
    for key in wanted:
        values = [_value(item.get(key)) for item in items]
        given = [v for v in values if v is not None]
        if not given:
            continue
        compared[key] = "one" if len(given) < len(items) else "same" if len(set(given)) == 1 else "differ"
    out["fields"] = compared
    tops = [p["top"] for p in out["places"] if p["top"] is not None]
    out["apartOnThePage"] = (len(tops) == len(items) and len(items) >= 2
                             and statistics.pstdev(tops) >= 150)
    return out


def verdict(shape: dict[str, Any]) -> str:
    """One word for what the answer is."""
    if shape["kind"] != "LIST":
        return str(shape["kind"])
    fields = shape.get("fields") or {}
    if not fields:
        return "LIST_EMPTY"
    if all(v == "same" for v in fields.values()):
        return "LIST_SAME_VALUES"          # the same document given more than once
    identity = [fields.get(k) for k in ("receipt_number", "invoice_number", "amount_paid", "receipt_date")
                if fields.get(k)]
    if any(v == "differ" for v in identity):
        return "LIST_DIFFERENT_DOCUMENTS"  # objects that name different receipts / invoices / amounts
    return "LIST_MIXED"


async def run(tenant_id: str) -> int:
    import logging

    import httpx
    import structlog

    logging.disable(logging.CRITICAL)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

    from pypdf import PdfReader
    from sqlalchemy import text
    from verigence.di.document_ai.adapter import ExtractionField
    from verigence.di.document_ai.gemini_adapter import (
        _build_payload,
        _build_prompt,
        capped_image,
    )
    from verigence.di.document_ai.gemini_cost import apply_service_tier
    from verigence.di.document_ai.schemas import get_schema
    from verigence.di.repositories.database import (
        AsyncSessionFactory,
        set_tenant_context,
    )
    from verigence.di.settings import get_settings
    from verigence.di.storage.adapter import get_storage_adapter
    from verigence.di.workers.job_runner import (
        _load_profile_fields,
        _provider_field_key,
    )

    settings = get_settings()
    async with AsyncSessionFactory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await set_tenant_context(session, tenant_id)
        rows = (await session.execute(
            text(
                """
                SELECT d.document_id, dt.document_type_key, pr.extraction_profile_id,
                       a.logical_object_key, a.mime_type
                FROM docintel.documents d
                JOIN docintel.document_types dt ON dt.document_type_id=d.document_type_id
                JOIN docintel.processing_runs pr
                  ON pr.tenant_id=d.tenant_id AND pr.processing_run_id=d.current_processing_run_id
                JOIN LATERAL (
                    SELECT da.logical_object_key, da.mime_type
                    FROM docintel.document_artifacts da
                    WHERE da.tenant_id=d.tenant_id AND da.document_id=d.document_id AND da.artifact_type='ORIGINAL'
                    ORDER BY da.created_at_utc
                    LIMIT 1
                ) a ON true
                WHERE d.tenant_id=:tid AND d.processing_status='FAILED'
                  AND d.processing_failure_code='DOCUMENT_AI_RESPONSE_INVALID'
                  AND pr.extraction_profile_id IS NOT NULL
                ORDER BY d.registered_at_utc DESC
                """
            ),
            {"tid": tenant_id},
        )).mappings().all()
        profiles: dict[str, list[dict[str, Any]]] = {}
        for profile_id in {row["extraction_profile_id"] for row in rows}:
            profiles[str(profile_id)] = await _load_profile_fields(session, profile_id)
        await session.rollback()
    print(f"DOCUMENTS refused_and_still_failed={len(rows)}")
    storage = get_storage_adapter()
    gate = asyncio.Semaphore(CONCURRENCY)
    spent = 0.0
    tally: Counter[tuple[str, str]] = Counter()
    variant = Variant("production")

    async def one(client: Any, row: Any) -> None:
        nonlocal spent
        document, kind = str(row["document_id"]), str(row["document_type_key"])
        line: dict[str, Any] = {"document": document[:8], "type": kind}
        try:
            if spent > SPEND_LIMIT_USD:
                raise RuntimeError("spend limit")
            data = b"".join([chunk async for chunk in storage.get_stream(row["logical_object_key"])])
            mime = row["mime_type"] or "application/octet-stream"
            if mime == "application/pdf":
                line["pdfPages"] = len(PdfReader(io.BytesIO(data), strict=False).pages)
            data, mime = capped_image(data, mime, settings.docai_gemini_extraction_max_edge)
            fields = profiles[str(row["extraction_profile_id"])]
            wanted = [_provider_field_key(pf) for pf in fields]
            prompt = _build_prompt(get_schema(kind), [
                ExtractionField(field_key=_provider_field_key(pf), aliases=pf.get("aliases") or [],
                                instruction=pf.get("extraction_instruction")) for pf in fields])
            body = _build_payload(data, mime, prompt, media_resolution=get_schema(kind).media_resolution)
            apply_service_tier(body)
            async with gate:
                result = await call(client, settings.docai_gemini_api_key, variant, body)
            usage = result.get("usage") or {}
            spent += float(usage.get("costUsd") or 0.0)
            line.update(responseTokens=usage.get("responseTokens"), truncated=bool(result.get("truncated")))
            if not result["ok"]:
                line["answer"] = {"kind": str(result["error"])}
            else:
                line["answer"] = describe(result["text"], wanted)
            line["verdict"] = verdict(line["answer"]) if result["ok"] else str(result["error"])
        except Exception as exc:  # noqa: BLE001 - one document that cannot be read: counted, not fatal
            line["verdict"] = f"COULD_NOT_READ_{type(exc).__name__}"
        tally[(kind, line["verdict"])] += 1
        print("DOC " + json.dumps(line, sort_keys=True))

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
        await asyncio.gather(*(one(client, row) for row in rows))
    for (kind, word), count in sorted(tally.items()):
        print(f"TALLY type={kind} answer={word} documents={count}")
    print(f"SPENT usd={spent:.2f}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: di_refused.py <di-worker-vars.json> <tenant-id>")
        return 2
    try:
        uuid.UUID(argv[2])
        load_environment(Path(argv[1]))
        return asyncio.run(run(argv[2]))
    except Exception as exc:  # noqa: BLE001 - a public log: the kind of failure and where, never its text
        import traceback

        frames = traceback.extract_tb(exc.__traceback__)
        where = "; ".join(f"{Path(f.filename).name}:{f.lineno}" for f in frames[-4:])
        print(f"::error::{type(exc).__name__} at {where}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
