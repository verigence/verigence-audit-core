"""Phase 2 live status: pushed to the screen the moment it changes.

The PC sees each document being uploaded, identified and read as it
happens -- no refresh, no polling. Postgres sends a NOTIFY on channel
``p2_journey`` whenever a page, batch, task or the stage of a Journey
changes (migration 0130). One listener per Audit Core process receives
them and wakes the open streams of that Journey; each stream then sends a
fresh snapshot (upload counts, every document's status, the stage) as a
Server-Sent Event.

The stream carries only what the screen already may read (the same
permission as the documents endpoint); a keep-alive comment every 15
seconds keeps proxies from closing it, and it ends after a few minutes so
the browser reconnects with a current token.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any
from uuid import UUID

import anyio
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_engine, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_submission import upload_status

logger = structlog.get_logger(__name__)

CHANNEL = "p2_journey"
_KEEPALIVE_SECONDS = 15.0
_STREAM_SECONDS = float(os.environ.get("P2_LIVE_STREAM_SECONDS", "300"))
_COALESCE_SECONDS = 0.15

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-p2-live"])

Key = tuple[str, str]


class _Listener:
    """One LISTEN connection per process, fanning out to open streams.
    Started by the first stream and kept for the life of the process."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect or self._default_connect
        self._lock = threading.Lock()
        self._subscribers: dict[Key, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[None]]]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = threading.Event()

    @staticmethod
    def _default_connect() -> Any:
        raw = get_engine().raw_connection()
        connection = raw.driver_connection
        raw.detach()  # a dedicated connection, never returned to the pool
        connection.rollback()
        connection.autocommit = True
        return connection

    def subscribe(self, key: Key) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue()
        entry = (asyncio.get_running_loop(), queue)
        with self._lock:
            self._subscribers.setdefault(key, set()).add(entry)
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="p2-live-listener", daemon=True)
                self._thread.start()
        return queue

    def unsubscribe(self, key: Key, queue: asyncio.Queue[None]) -> None:
        with self._lock:
            entries = self._subscribers.get(key, set())
            for entry in [e for e in entries if e[1] is queue]:
                entries.discard(entry)
            if not entries:
                self._subscribers.pop(key, None)

    def _wake(self, keys: list[Key] | None = None) -> None:
        with self._lock:
            targets = [entry for key, entries in self._subscribers.items()
                       if keys is None or key in keys for entry in entries]
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:  # the stream's loop is gone
                continue

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                connection = self._connect()
            except Exception:
                logger.warning("p2_live_listen_connect_failed", exc_info=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            try:
                connection.execute(f"LISTEN {CHANNEL}")
                self.connected.set()
                backoff = 1.0
                self._wake()  # anything missed while reconnecting
                while not self._stop.is_set():
                    keys: list[Key] = []
                    for notify in connection.notifies(timeout=1.0):
                        tenant_id, _, journey_id = str(notify.payload).partition("|")
                        keys.append((tenant_id, journey_id))
                    if keys:
                        self._wake(list(dict.fromkeys(keys)))
            except Exception:
                logger.warning("p2_live_listen_failed", exc_info=True)
                time.sleep(backoff)
            finally:
                self.connected.clear()
                try:
                    connection.close()
                except Exception:
                    logger.debug("p2_live_listen_close_failed", exc_info=True)


listener = _Listener()


def live_snapshot(connection: Connection, *, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    """What the upload screen shows: counts, each document's status, stage."""
    units = connection.execute(
        text(
            """
            SELECT q.queue_id, q.batch_id, q.unit_kind, q.page_number, q.page_numbers, q.queue_status,
                   q.template_key, q.classified_document_type, q.di_document_id, q.updated_at_utc
            FROM auditcore.p2_document_queue q
            WHERE q.tenant_id=:t AND q.journey_id=:j AND q.queue_status NOT IN ('CANCELLED','MERGED')
            ORDER BY q.created_at_utc, q.page_number
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().all()
    runtime = connection.execute(
        text(
            """
            SELECT current_stage, booking_completion_state, delivery_completion_state
            FROM auditcore.p2_journey_runtime WHERE tenant_id=:t AND journey_id=:j
            """
        ),
        {"t": tenant_id, "j": journey_id},
    ).mappings().one_or_none()
    return {
        **upload_status(connection, tenant_id=tenant_id, journey_id=journey_id),
        "units": [
            {
                "queueId": str(u["queue_id"]),
                "batchId": str(u["batch_id"]),
                "kind": u["unit_kind"],
                "pageNumbers": list(u["page_numbers"] or ([u["page_number"]] if u["page_number"] else [])),
                "status": u["queue_status"],
                "templateKey": u["template_key"],
                "documentType": u["classified_document_type"],
                "documentId": str(u["di_document_id"]) if u["di_document_id"] else None,
                "updatedAtUtc": u["updated_at_utc"].isoformat() if u["updated_at_utc"] else None,
            }
            for u in units
        ],
        "stage": dict(runtime) if runtime else None,
    }


def _read_snapshot(tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    with get_engine().begin() as connection:
        set_tenant_context(connection, tenant_id)
        return live_snapshot(connection, tenant_id=tenant_id, journey_id=journey_id)


def _event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, default=str, separators=(',', ':'))}\n\n"


async def live_events(request: Request | None, *, tenant_id: str, journey_id: UUID,
                      max_seconds: float = _STREAM_SECONDS) -> AsyncIterator[str]:
    key = (tenant_id, str(journey_id))
    queue = listener.subscribe(key)
    deadline = time.monotonic() + max_seconds
    try:
        last = await anyio.to_thread.run_sync(_read_snapshot, tenant_id, journey_id)
        yield "retry: 2000\n\n" + _event("status", last)
        while time.monotonic() < deadline:
            if request is not None and await request.is_disconnected():
                return
            timeout = min(_KEEPALIVE_SECONDS, max(deadline - time.monotonic(), 0.01))
            try:
                await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            await asyncio.sleep(_COALESCE_SECONDS)  # fold a burst of page updates into one push
            while not queue.empty():
                queue.get_nowait()
            snapshot = await anyio.to_thread.run_sync(_read_snapshot, tenant_id, journey_id)
            if snapshot != last:
                last = snapshot
                yield _event("status", snapshot)
        yield _event("reconnect", {})
    finally:
        listener.unsubscribe(key, queue)


@router.get("/journeys/{journey_id}/live")
async def journey_live(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
) -> StreamingResponse:
    """Server-Sent Events: a ``status`` event now and on every change."""

    def _authorize() -> None:
        with get_engine().begin() as connection:
            authorize_p2(connection, tenant_id=tenant_id, journey_id=journey_id, human_principal=human_principal,
                         authorization_client=authorization_client, permission_key="audit.journey.read")

    await anyio.to_thread.run_sync(_authorize)
    return StreamingResponse(
        live_events(request, tenant_id=tenant_id, journey_id=journey_id, max_seconds=_STREAM_SECONDS),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
