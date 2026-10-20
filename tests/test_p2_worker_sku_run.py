"""A full SKU run started from outside (AUDIT_CORE_SKU_RUN_TAG on the worker)
pages through every Journey once per tag."""
from __future__ import annotations

from uuid import uuid4

from audit_core import uc03_backfill_document_sync_producers as backfill
from audit_core import uc03_p2_worker as worker


def test_the_run_pages_until_done_and_does_not_repeat_for_the_same_tag(monkeypatch):
    calls: list[tuple[str, int]] = []
    run_id = uuid4()

    def fake_backfill(engine, *, tenant_id, offset, limit, run_id: object = None, actor_id=None):
        calls.append((tenant_id, offset))
        return {"runId": run_id or run_ids[0], "nextOffset": None if offset >= 20 else offset + limit,
                "journeysTotal": 25, "withSkuBefore": 1, "withSkuAfter": 2, "withoutSkuAfter": 3,
                "journeysFailed": []}

    run_ids = [run_id]
    done: set[str] = set()

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *_args, **_kwargs):
            class _R:
                def first(self_inner):
                    return (1,) if "t1" in done else None
            return _R()

    class _Engine:
        def begin(self):
            return _Conn()

    monkeypatch.setattr(worker, "_active_tenants", lambda engine: ["t1"])
    monkeypatch.setattr(worker, "set_tenant_context", lambda connection, tenant_id: None)
    monkeypatch.setattr(backfill, "backfill_sku_resolution_for_tenant", fake_backfill)

    worker._sku_run_once(_Engine(), "2026-10-20a")
    assert calls == [("t1", 0), ("t1", 10), ("t1", 20)]

    done.add("t1")
    worker._sku_run_once(_Engine(), "2026-10-20a")
    assert len(calls) == 3
