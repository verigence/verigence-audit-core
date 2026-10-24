"""A full SKU run started from outside (AUDIT_CORE_SKU_RUN_TAG on the worker)
pages through every Journey once per tag."""
from __future__ import annotations

from uuid import uuid4

from audit_core import uc03_backfill_document_sync_producers as backfill
from audit_core import uc03_p2_worker as worker


def test_the_run_pages_until_done_and_does_not_repeat_for_the_same_tag(monkeypatch):
    calls: list[tuple[str, int]] = []
    run_id = uuid4()

    flags: list[bool] = []

    def fake_backfill(engine, *, tenant_id, offset, limit, run_id: object = None, actor_id=None,
                      only_without_sku=False):
        calls.append((tenant_id, offset))
        flags.append(only_without_sku)
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
                def mappings(self_inner):
                    return self_inner

                def one_or_none(self_inner):
                    # A COMPLETE run for this tag, or none at all. The resume
                    # case has its own real-database test
                    # (test_sku_run_resumes.py).
                    if "t1" not in done:
                        return None
                    return {"run_id": run_ids[0], "status": "COMPLETE", "done_items": 25}
            return _R()

    class _Engine:
        def begin(self):
            return _Conn()

    monkeypatch.setattr(worker, "_active_tenants", lambda engine: ["t1"])
    monkeypatch.setattr(worker, "set_tenant_context", lambda connection, tenant_id: None)
    monkeypatch.setattr(backfill, "backfill_sku_resolution_for_tenant", fake_backfill)

    worker._sku_run_once(_Engine(), "2026-10-20a")
    assert calls == [("t1", 0), ("t1", 10), ("t1", 20)]

    assert not any(flags)

    done.add("t1")
    worker._sku_run_once(_Engine(), "2026-10-20a")
    assert len(calls) == 3

    # A tag starting "unresolved" leaves every pinned SKU alone.
    done.clear()
    worker._sku_run_once(_Engine(), "unresolved-2026-10-20")
    assert flags[-1] is True


def test_a_refused_commit_tag_says_so_in_plain_words_and_can_be_set_again(monkeypatch):
    """The worker's "-commit" tag is gated like Apply: refused, it logs why in
    plain words (not "sku_run_failed") and leaves no run row, so the same tag
    runs once a check exists."""
    logged: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event, **kw):
            logged.append((event, kw))

        def info(self, event, **kw):
            logged.append((event, kw))

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *_args, **_kwargs):
            class _R:
                def mappings(self_inner):
                    return self_inner

                def one_or_none(self_inner):
                    return None  # this tag has never run
            return _R()

    class _Engine:
        def begin(self):
            return _Conn()

    def refuse(engine, tenant_id, actor_id=None, commit=False):
        raise backfill.CommitNotAllowed({
            "code": "NO_CHECK_RUN",
            "message": 'There is no completed check of this Project\'s SKUs. Run "Check SKUs" first, review it, then Apply.',
            "check": None,
        })

    monkeypatch.setattr(worker, "_active_tenants", lambda engine: ["t1"])
    monkeypatch.setattr(worker, "set_tenant_context", lambda connection, tenant_id: None)
    monkeypatch.setattr(worker, "logger", _Log())
    monkeypatch.setattr(backfill, "reresolve_all_skus", refuse)
    worker._sku_run_once(_Engine(), "reresolve-2026-10-22-commit")
    events = [e for e, _ in logged]
    assert events == ["sku_run_refused"], events
    kw = logged[0][1]
    assert kw["code"] == "NO_CHECK_RUN" and kw["tag"] == "reresolve-2026-10-22-commit"
    assert "NOT run" in kw["message"] and "changed nothing" in kw["message"]
    assert "Check SKUs" in kw["message"] and "within 24 hours" in kw["message"]
