import anyio
import anyio.to_thread

import audit_core.main as audit_core_main


async def _noop_sweep_loop(_engine, *, interval_seconds) -> None:
    await anyio.sleep_forever()


def test_lifespan_widens_default_thread_pool_capacity_past_the_db_pool(monkeypatch) -> None:
    """Regression test for a real, unresolved-by-PR#278 latency report.

    Every route in this app is a sync `def`, bridged through Starlette's
    run_in_threadpool -- which anyio caps at a hardcoded 40 concurrent
    worker threads per process by default, shared across every endpoint
    and every sync dependency (get_connection, get_bearer_token, ...).
    That default sits below the 30-connection DB pool PR #278 set up
    (pool_size=10 + max_overflow=20), so it silently became the real
    concurrency ceiling -- invisible to any in-handler timing log, since
    a request queues for a thread before its own code ever runs. The
    lifespan must raise it past the pool's own ceiling on startup.
    """
    monkeypatch.setattr(audit_core_main, "warm_runtime_dependencies", lambda: None)
    monkeypatch.setattr(audit_core_main, "get_engine", lambda: None)
    monkeypatch.setattr(
        audit_core_main,
        "run_stale_worker_task_recovery_loop",
        _noop_sweep_loop,
    )

    captured: dict[str, float] = {}

    async def _run() -> None:
        async with audit_core_main._lifespan(None):
            captured["tokens"] = anyio.to_thread.current_default_thread_limiter().total_tokens

    anyio.run(_run)

    assert captured["tokens"] == 100
    assert captured["tokens"] > 30  # strictly past the SQLAlchemy pool's own ceiling
