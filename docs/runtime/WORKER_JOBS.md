# Starting a one-off P2 worker job (2026-10-27)

A one-off job (statement re-read, diagnostics report, invoice re-scan, SKU run, ...)
is started by adding a row to `auditcore.p2_worker_jobs`. The running worker picks
it up within a minute and runs it beside its normal work. **Nothing restarts.**

```sql
INSERT INTO auditcore.p2_worker_jobs (job_kind, tag, reports, requested_by)
VALUES ('STATEMENT_REREAD', 'statement-1027-dry', '{}', 'who asked');
-- diagnostics: job_kind 'DIAG_REPORTS', reports '{receipts_failing_detail}'
SELECT job_kind, tag, status, started_at_utc, finished_at_utc, detail
FROM auditcore.p2_worker_jobs ORDER BY requested_at_utc DESC LIMIT 10;
```

Kinds: `DIAG_REPORTS`, `MASTER_RELOAD`, `PAGE_RESEND`, `INVOICE_RESCAN`,
`ACCOUNT_STATEMENT`, `STATEMENT_REREAD`, `INVOICE_TASK_CLEANUP`, `VARIANCE_AUDIT`,
`SKU_RUN`, `PEOPLE_NAMES` (names from Security now, instead of waiting for the six-hourly copy). Each job keeps its own rules (dry run unless the tag ends with `-commit`,
once per tag).

Why: on 26 Oct 2026 the worker was restarted 34 times, none of them a crash. 16
were deploys of merges, 8 were Railway deploying the bot's "live baseline" commits
(the GitHub source is now disconnected by the deploy workflows), and 10 were
variable changes made only to start a job: Railway redeploys a service whenever a
variable changes. The `AUDIT_CORE_*_TAG` and `AUDIT_CORE_DIAG_REPORTS` variables
still work but restart the worker; do not use them during working hours.
