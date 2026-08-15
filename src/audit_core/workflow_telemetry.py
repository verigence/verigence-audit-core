from __future__ import annotations

from sqlalchemy import Connection, text

from audit_core.telemetry import record_metric, trace_span


def emit_workflow_health_metrics(connection: Connection) -> dict[str, int | float]:
    """Emit bounded-cardinality workflow health gauges from authoritative state."""
    with trace_span(
        "audit_core.workflow.health_snapshot",
        attributes={"component": "workflow"},
    ):
        status_rows = connection.execute(
            text(
                """
                SELECT task_status, count(*) AS task_count
                FROM auditcore.workflow_tasks
                GROUP BY task_status
                """
            )
        ).mappings().all()
        status_counts = {
            row["task_status"]: int(row["task_count"])
            for row in status_rows
        }
        for status, count in status_counts.items():
            record_metric(
                "audit_core.workflow.tasks",
                count,
                kind="gauge",
                labels={"status": status},
            )

        reliability = connection.execute(
            text(
                """
                SELECT
                    count(*) FILTER (WHERE task_status = 'RETRY_WAIT') AS retry_wait,
                    count(*) FILTER (
                        WHERE task_status IN ('CLAIMED','IN_PROGRESS')
                          AND lease_expires_at_utc IS NOT NULL
                          AND lease_expires_at_utc <= now()
                    ) AS stale_tasks,
                    count(*) FILTER (WHERE task_status = 'DEAD_LETTER') AS dead_letter,
                    COALESCE(
                        EXTRACT(
                            EPOCH FROM (
                                now() - min(available_at_utc) FILTER (
                                    WHERE task_status IN ('PENDING','READY','RETRY_WAIT')
                                )
                            )
                        ),
                        0
                    ) AS oldest_pending_seconds
                FROM auditcore.workflow_tasks
                """
            )
        ).mappings().one()
        retry_wait = int(reliability["retry_wait"])
        stale_tasks = int(reliability["stale_tasks"])
        dead_letter = int(reliability["dead_letter"])
        oldest_pending_seconds = max(
            0.0,
            float(reliability["oldest_pending_seconds"]),
        )

        record_metric(
            "audit_core.workflow.retry_wait",
            retry_wait,
            kind="gauge",
        )
        record_metric(
            "audit_core.workflow.stale_tasks",
            stale_tasks,
            kind="gauge",
        )
        record_metric(
            "audit_core.workflow.dead_letter",
            dead_letter,
            kind="gauge",
        )
        record_metric(
            "audit_core.workflow.oldest_pending_seconds",
            oldest_pending_seconds,
            kind="gauge",
        )

        return {
            "retry_wait": retry_wait,
            "stale_tasks": stale_tasks,
            "dead_letter": dead_letter,
            "oldest_pending_seconds": oldest_pending_seconds,
        }
