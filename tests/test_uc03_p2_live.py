"""Live upload status is pushed the moment a page changes, not polled."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core import uc03_p2_live as live
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2live")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _set_status(journey, queue_id, status):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_document_queue SET queue_status=:s WHERE tenant_id=:t AND queue_id=:q"),
            {"s": status, "t": journey.tenant_id, "q": queue_id},
        )


def _statuses(event_text):
    payload = json.loads(event_text.split("data: ", 1)[1])
    return [u["status"] for u in payload["units"]], payload["counts"]


def test_a_page_change_is_pushed_immediately(journey):
    _, [page] = add_batch_pages(journey, [("booking_form", "CLASSIFYING")])

    async def watch():
        events = []
        stream = live.live_events(None, tenant_id=journey.tenant_id, journey_id=journey.journey_id, max_seconds=6)
        changed_at = None
        async for chunk in stream:
            if chunk.startswith(":"):
                continue
            if "event: status" in chunk:
                events.append((time.monotonic(), chunk))
                if len(events) == 1:
                    assert live.listener.connected.wait(5)
                    # the worker moves the page on, from another connection
                    changed_at = time.monotonic()
                    threading.Thread(target=_set_status, args=(journey, page["queue_id"], "READY")).start()
                else:
                    break
        await stream.aclose()
        return events, changed_at

    events, changed_at = asyncio.run(watch())
    assert len(events) == 2
    assert _statuses(events[0][1])[0] == ["CLASSIFYING"]
    statuses, counts = _statuses(events[1][1])
    assert statuses == ["READY"] and counts["extracted"] == 1
    assert events[1][0] - changed_at < 2.0  # pushed, not on a refresh cycle


def test_unrelated_journeys_do_not_wake_the_stream(journey):
    other = create_p2_journey(journey.engine, prefix="p2live2")
    try:
        _, [page] = add_batch_pages(other, [("pan_card", "CLASSIFYING")])

        async def watch():
            chunks = []
            stream = live.live_events(None, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                      max_seconds=2)
            async for chunk in stream:
                chunks.append(chunk)
                if len(chunks) == 1:
                    assert live.listener.connected.wait(5)
                    threading.Thread(target=_set_status, args=(other, page["queue_id"], "READY")).start()
            return chunks

        chunks = asyncio.run(watch())
        assert sum("event: status" in c for c in chunks) == 1
        assert "event: reconnect" in chunks[-1]
    finally:
        delete_tenant_data(journey.engine, other.tenant_id)


def test_stream_endpoint_sends_the_status_first(journey, monkeypatch):
    add_batch_pages(journey, [("booking_form", "READY"), ("pan_card", "EXTRACTING")])
    monkeypatch.setattr(live, "_STREAM_SECONDS", 0.5)
    client = TestClient(app)
    with client.stream("GET", f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/live") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    first = body.split("event: status", 1)[1]
    payload = json.loads(first.split("data: ", 1)[1].split("\n", 1)[0])
    assert payload["counts"]["documents"] == 2 and payload["counts"]["extracted"] == 1
    assert {u["status"] for u in payload["units"]} == {"READY", "EXTRACTING"}
