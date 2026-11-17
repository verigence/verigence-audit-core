"""Statement re-read experiment, Document Intelligence side (2026-11-17).

Run with Document Intelligence installed (its ``dev`` branch checked out beside
this repository) and its worker's variables in a JSON file:

    python di_side.py <di-worker-vars.json> <sample.json> <readings.jsonl>

For each statement of the sample it reads the page again from storage under
each setting below, with Document Intelligence's own prompt, answer parser and
normalisers, and writes the fields each reading would have stored to
<readings.jsonl> on this machine. Nothing is written to any database or to
storage: no job, no run, no value. The file never leaves the runner.

The repository is public and so is this job's log: only counts, document ids
and failure kinds are printed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLASH_PREVIEW = "gemini-3-flash-preview"
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# US$ per million tokens (input, output), standard tier, Google's price list 2026-11-16.
PRICES = {FLASH_PREVIEW: (0.50, 3.00), "gemini-3.8-flash": (0.75, 3.75), "gemini-3.1-pro-preview": (2.00, 12.00)}
STORED = "stored_di"
MORE_OUTPUT = 32768  # thinking counts against the answer's cap: room for it
RETRY_STATUSES = {429, 500, 502, 503, 504}
REJECTED_STATUSES = {400, 404}
SPEND_LIMIT_USD = 40.0
CONCURRENCY = 12


@dataclass(frozen=True)
class Variant:
    """One provider setting. ``thinking`` None is what production uses."""
    key: str
    model: str = FLASH_PREVIEW
    thinking: str | None = None
    media_resolution: str | None = None
    max_output_tokens: int | None = None


VARIANTS = (
    Variant("rerun"),  # today's setting again: how much a reading moves by itself
    Variant("think_high", thinking="high", max_output_tokens=MORE_OUTPUT),
    Variant("res_high", media_resolution="MEDIA_RESOLUTION_HIGH"),
    Variant("res_high_think_high", thinking="high", media_resolution="MEDIA_RESOLUTION_HIGH",
            max_output_tokens=MORE_OUTPUT),
    Variant("flash_3_8", model="gemini-3.8-flash"),
    Variant("pro_3_1", model="gemini-3.1-pro-preview", max_output_tokens=MORE_OUTPUT),
)


def variant_payload(payload: dict[str, Any], variant: Variant) -> dict[str, Any]:
    """Production's request body with this setting's page resolution, if any."""
    if variant.media_resolution:
        payload["generationConfig"]["mediaResolution"] = variant.media_resolution
    return payload


def cost_usd(model: str, prompt_tokens: int, billed_output_tokens: int) -> float:
    price_in, price_out = PRICES[model]
    return (prompt_tokens * price_in + billed_output_tokens * price_out) / 1_000_000


def answer_text(data: dict[str, Any]) -> str:
    """The answer's text: the parts that are not the model's thinking."""
    parts = data["candidates"][0]["content"]["parts"]
    texts = [str(p["text"]) for p in parts if isinstance(p, dict) and "text" in p and not p.get("thought")]
    if not texts:
        raise KeyError("text")
    return "".join(texts)


def stored_fields(profile_fields: list[dict[str, Any]], results: list[Any], normalizers: dict[Any, Any],
                  run_normalizers: Any, provider_key: Any) -> dict[str, Any]:
    """What job_runner would have stored as each field's current value."""
    by_key = {result.field_key: result for result in results}
    fields: dict[str, Any] = {}
    for profile_field in profile_fields:
        result = by_key.get(provider_key(profile_field))
        raw = result.raw_value if result else None
        configs = normalizers.get(uuid.UUID(str(profile_field["profile_field_id"])), [])
        normalised = run_normalizers(raw=raw, normalizer_configs=configs)
        fields[str(profile_field["canonical_field_key"])] = normalised.normalized_value
    return fields


def load_environment(path: Path) -> None:
    for key, value in json.loads(path.read_text()).items():
        if value is not None and not key.startswith("RAILWAY_"):
            os.environ[key] = str(value)


async def call(client: Any, api_key: str, variant: Variant, body: dict[str, Any]) -> dict[str, Any]:
    """One reading: up to three tries on a busy or failing provider."""
    import httpx
    from verigence.di.document_ai.gemini_cost import usage_from_response

    started = time.monotonic()
    error = "UNKNOWN"
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(8 * attempt * attempt)
        try:
            response = await client.post(API.format(model=variant.model), headers={"x-goog-api-key": api_key}, json=body)
        except httpx.TimeoutException:
            error = "TIMEOUT"
            continue
        except httpx.HTTPError as exc:
            error = type(exc).__name__
            continue
        if response.status_code != 200:
            error = f"HTTP_{response.status_code}"
            if response.status_code in RETRY_STATUSES:
                continue
            reason = ""
            if response.status_code in REJECTED_STATUSES:
                try:
                    detail = response.json().get("error") or {}
                    reason = f"{detail.get('status')}: {str(detail.get('message') or '')[:160]}"
                except ValueError:
                    reason = "no reason given"
            return {"ok": False, "error": error, "rejected": response.status_code in REJECTED_STATUSES,
                    "reason": reason, "attempts": attempt + 1}
        try:
            data = response.json()
            usage = usage_from_response(data)
        except ValueError:
            error = "ANSWER_NOT_JSON"
            continue
        spent = {
            "promptTokens": usage.prompt_tokens, "responseTokens": usage.response_tokens,
            "thoughtsTokens": usage.thoughts_tokens, "billedOutputTokens": usage.billed_output_tokens,
            "costUsd": cost_usd(variant.model, usage.prompt_tokens, usage.billed_output_tokens),
        }
        outcome = {"usage": spent, "truncated": usage.truncated, "attempts": attempt + 1,
                   "latencyMs": round((time.monotonic() - started) * 1000, 1)}
        if usage.blocked:
            return {"ok": False, "error": "BLOCKED", **outcome}
        try:
            return {"ok": True, "text": answer_text(data), **outcome}
        except (KeyError, IndexError, TypeError):
            return {"ok": False, "error": "NO_ANSWER_TEXT", **outcome}
    return {"ok": False, "error": error, "attempts": 3}


async def documents_to_read(tenant_id: str, ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Where each page is stored, the profile its last reading used, and the
    values that reading left (read only)."""
    from sqlalchemy import text
    from verigence.di.repositories.database import (
        AsyncSessionFactory,
        set_tenant_context,
    )
    from verigence.di.rules.runner import _load_normalizer_configs
    from verigence.di.workers.job_runner import _load_profile_fields

    wanted = [uuid.UUID(i) for i in ids]
    async with AsyncSessionFactory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await set_tenant_context(session, tenant_id)
        rows = (await session.execute(
            text(
                """
                SELECT d.document_id, pr.extraction_profile_id, a.logical_object_key, a.mime_type
                FROM docintel.documents d
                JOIN docintel.processing_runs pr
                  ON pr.tenant_id=d.tenant_id AND pr.processing_run_id=d.current_processing_run_id
                JOIN LATERAL (
                    SELECT da.logical_object_key, da.mime_type
                    FROM docintel.document_artifacts da
                    WHERE da.tenant_id=d.tenant_id AND da.document_id=d.document_id AND da.artifact_type='ORIGINAL'
                    ORDER BY da.created_at_utc
                    LIMIT 1
                ) a ON true
                WHERE d.tenant_id=:tid AND d.document_id = ANY(CAST(:ids AS uuid[]))
                  AND pr.extraction_profile_id IS NOT NULL
                """
            ),
            {"tid": tenant_id, "ids": wanted},
        )).mappings().all()
        values = (await session.execute(
            text(
                """
                SELECT v.document_id, cf.field_key, v.current_value::text AS value_text
                FROM docintel.document_field_values v
                JOIN docintel.canonical_fields cf ON cf.canonical_field_id=v.canonical_field_id
                WHERE v.tenant_id=:tid AND v.is_current AND v.document_id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"tid": tenant_id, "ids": wanted},
        )).mappings().all()
        profiles: dict[str, Any] = {}
        for profile_id in {row["extraction_profile_id"] for row in rows}:
            profiles[str(profile_id)] = {
                "fields": await _load_profile_fields(session, profile_id),
                "normalizers": await _load_normalizer_configs(session, profile_id),
            }
        await session.rollback()
    stored: dict[str, dict[str, Any]] = {}
    for value in values:
        stored.setdefault(str(value["document_id"]), {})[str(value["field_key"])] = (
            None if value["value_text"] is None else json.loads(value["value_text"]))
    documents = [{"documentId": str(row["document_id"]), "profileId": str(row["extraction_profile_id"]),
                  "key": row["logical_object_key"], "mime": row["mime_type"] or "application/octet-stream",
                  "stored": stored.get(str(row["document_id"]), {})} for row in rows]
    return documents, profiles


async def run(sample_path: Path, readings_path: Path) -> int:
    import logging

    import httpx
    import structlog

    # Document Intelligence's own log lines are for its workers: not this log.
    logging.disable(logging.CRITICAL)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

    from verigence.di.document_ai.adapter import ExtractionField
    from verigence.di.document_ai.gemini_adapter import (
        _build_payload,
        _build_prompt,
        _parse_response,
        capped_image,
    )
    from verigence.di.document_ai.gemini_cost import apply_service_tier
    from verigence.di.document_ai.schemas import get_schema
    from verigence.di.rules.runner import _run_normalizers
    from verigence.di.settings import get_settings
    from verigence.di.storage.adapter import get_storage_adapter
    from verigence.di.workers.job_runner import _provider_field_key

    sample = json.loads(sample_path.read_text())
    tenant_id = str(sample["tenantId"])
    settings = get_settings()
    documents, profiles = await documents_to_read(tenant_id, [str(d["documentId"]) for d in sample["documents"]])
    print(f"DOCUMENTS sample={len(sample['documents'])} readable={len(documents)} profiles={len(profiles)} "
          f"production_thinking={settings.docai_gemini_extraction_thinking_level or 'default'} "
          f"production_max_output={settings.docai_gemini_max_output_tokens}")
    if not documents:
        return 1
    schema = get_schema("customer_ledger")
    prompts: dict[str, tuple[str, list[Any]]] = {}
    for profile_id, profile in profiles.items():
        extract_fields = [ExtractionField(field_key=_provider_field_key(pf), aliases=pf.get("aliases") or [],
                                          instruction=pf.get("extraction_instruction")) for pf in profile["fields"]]
        prompts[profile_id] = (_build_prompt(schema, extract_fields), extract_fields)
    storage = get_storage_adapter()
    variants = list(VARIANTS)
    gate = asyncio.Semaphore(CONCURRENCY)
    spent = 0.0
    counts: dict[str, dict[str, int]] = {v.key: {} for v in variants}
    out = readings_path.open("w")

    def note(variant: str, outcome: str) -> None:
        counts[variant][outcome] = counts[variant].get(outcome, 0) + 1

    async def page(document: dict[str, Any]) -> tuple[bytes, str]:
        data = b"".join([chunk async for chunk in storage.get_stream(document["key"])])
        return capped_image(data, document["mime"], settings.docai_gemini_extraction_max_edge)

    async def read(client: Any, document: dict[str, Any], variant: Variant, data: bytes, mime: str) -> dict[str, Any]:
        nonlocal spent
        prompt, extract_fields = prompts[document["profileId"]]
        body = variant_payload(
            _build_payload(data, mime, prompt, thinking_level=variant.thinking,
                           max_output_tokens=variant.max_output_tokens), variant)
        apply_service_tier(body)
        async with gate:
            result = await call(client, settings.docai_gemini_api_key, variant, body)
        spent += float((result.get("usage") or {}).get("costUsd") or 0.0)
        record = {"documentId": document["documentId"], "variant": variant.key,
                  **{k: v for k, v in result.items() if k != "text"}}
        if result["ok"]:
            try:
                results = _parse_response(result["text"], schema, extract_fields)
                profile = profiles[document["profileId"]]
                record["fields"] = stored_fields(profile["fields"], results, profile["normalizers"],
                                                 _run_normalizers, _provider_field_key)
            except Exception as exc:  # noqa: BLE001 - an answer the reader cannot use is a failed reading
                record.update(ok=False, error=f"ANSWER_UNUSABLE_{type(exc).__name__}")
        out.write(json.dumps(record, default=str) + "\n")
        note(variant.key, "read" if record["ok"] else str(record["error"]))
        return record

    async def every_setting(client: Any, document: dict[str, Any]) -> None:
        if spent > SPEND_LIMIT_USD:
            return
        try:
            data, mime = await page(document)
        except Exception as exc:  # noqa: BLE001 - the page could not be fetched: counted, not fatal
            for variant in variants:
                out.write(json.dumps({"documentId": document["documentId"], "variant": variant.key, "ok": False,
                                      "error": f"PAGE_{type(exc).__name__}"}) + "\n")
                note(variant.key, f"PAGE_{type(exc).__name__}")
            return
        await asyncio.gather(*(read(client, document, variant, data, mime) for variant in variants))

    for document in documents:
        out.write(json.dumps({"documentId": document["documentId"], "variant": STORED, "ok": True,
                              "fields": document["stored"]}, default=str) + "\n")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
        # The first statement tells which settings the provider accepts at all.
        first = documents[0]
        data, mime = await page(first)
        for variant in list(variants):
            record = await read(client, first, variant, data, mime)
            if record.get("rejected"):
                variants.remove(variant)
                print(f"SETTING_NOT_ACCEPTED setting={variant.key} model={variant.model} "
                      f"status={record['error']} reason={record.get('reason')}")
            else:
                print(f"SETTING_ACCEPTED setting={variant.key} model={variant.model} first_reading="
                      f"{'read' if record['ok'] else record['error']}")
        if not variants:
            out.close()
            return 1
        started = time.monotonic()
        for offset in range(1, len(documents), 12):
            await asyncio.gather(*(every_setting(client, d) for d in documents[offset:offset + 12]))
            print(f"PROGRESS statements={min(offset + 12, len(documents))}/{len(documents)} "
                  f"spent_usd={spent:.2f} minutes={(time.monotonic() - started) / 60:.1f}")
    out.close()
    for variant in VARIANTS:
        print(f"READINGS setting={variant.key} " + " ".join(f"{k}={v}" for k, v in sorted(counts[variant.key].items())))
    print(f"SPENT usd={spent:.2f} limit_reached={spent > SPEND_LIMIT_USD}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: di_side.py <di-worker-vars.json> <sample.json> <readings.jsonl>")
        return 2
    try:
        load_environment(Path(argv[1]))
        return asyncio.run(run(Path(argv[2]), Path(argv[3])))
    except Exception as exc:  # noqa: BLE001 - a public log: the kind of failure and where, never its text
        import traceback

        frames = traceback.extract_tb(exc.__traceback__)
        where = "; ".join(f"{Path(f.filename).name}:{f.lineno}" for f in frames[-4:])
        print(f"::error::{type(exc).__name__} at {where}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
