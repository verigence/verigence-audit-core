# UC03 Phase 2 runtime deployment

Phase 2 is additive. The existing Audit Core Railway service keeps using
`railway.toml`. The durable P2 worker must be a **separate Railway service**
pointing at the same repository/branch and configured to use
`railway.p2-worker.toml`.

## Deployment order

1. Deploy the Audit Core API service from the tested P2 SHA and allow its existing
   pre-deploy Alembic command to complete migrations.
2. Verify the P2 API health/import and that migrations 0114/0115 are present.
3. Start/update the P2 worker service from the **same tested SHA**.
4. Deploy Verigence Web P2 only after Audit Core P2 API + worker are healthy.
5. Do not merge or repoint existing UC03 routes during stabilization.

The worker intentionally does not run Alembic itself. This avoids two services
racing the same migration.

## Required shared environment

The worker needs the same database, DI and Security integration configuration as
Audit Core. It also needs P2 document storage.

P2 storage may use dedicated variables:

- `AUDIT_CORE_P2_STORAGE_ENDPOINT`
- `AUDIT_CORE_P2_STORAGE_ACCESS_KEY_ID`
- `AUDIT_CORE_P2_STORAGE_SECRET_ACCESS_KEY`
- `AUDIT_CORE_P2_STORAGE_BUCKET`
- `AUDIT_CORE_P2_STORAGE_REGION`

When these are absent, the implementation deliberately falls back to the existing
`AUDIT_CORE_PHOTO_STORAGE_*` S3-compatible configuration, but still isolates
objects under the `p2-documents/` prefix.

Operational tuning:

- `P2_WORKER_CONCURRENCY` default 4
- `P2_WORKER_MAX_ATTEMPTS` default 12
- `P2_WORKER_POLL_SECONDS` default 1.0
- `P2_RECONCILE_DELAY_SECONDS` default 2
- `P2_MAX_UPLOAD_BYTES` default 50 MiB
- `P2_MAX_PDF_PAGES` default 100

Deal-audit time windows (days after delivery; the worker re-runs the Delivery
checks the morning after each window closes):

- `P2_SETTLEMENT_GRACE_DAYS` default 7 — balance received after delivery within /
  beyond this many days (`PAYMENT_AFTER_DELIVERY_*`)
- `P2_FINANCE_DISBURSEMENT_DAYS` default 12 — financier must pay the delivery
  order within this many days (`DO_PAYMENT_NOT_RECEIVED_12D`)
- `P2_TRADE_IN_RESALE_DAYS` default 90 — an exchange vehicle must be resold within
  this many days (`TRADE_IN_NOT_SOLD_90D`); this window is scheduled only for a
  deal with an exchange vehicle

Start conservatively. Increase concurrency only after observing DB pool, DI and
object-storage latency.

## Object-storage CORS

The browser uploads the original file directly to the presigned object-storage
URL so the Audit Core API never becomes the large binary transfer path.

The P2 bucket/prefix therefore must allow the production Verigence Web origin to:

- `PUT`
- send the `Content-Type` header

Audit Core performs `HEAD` and `GET` server-side; those calls do not require
browser CORS. Do not use wildcard origins in production when the storage provider
supports an explicit Web origin.

## No-loss boundary

Upload acceptance is complete only after:

1. Web has successfully PUT the object;
2. Audit Core finalize has verified object existence and byte length; and
3. the `p2_upload_batches` + `p2_work_queue` transaction has committed.

PDF splitting, DI upload/finalize, reconciliation, stage recompute and task
verification are worker work and never execute in the upload HTTP request.

## Recovery checks

Operational checks should watch:

- `p2_work_queue.work_status='DEAD_LETTER'`
- `p2_document_queue.queue_status IN ('FAILED','DEAD_LETTER')`
- upload batches in `FAILED` or `PARTIAL_FAILURE`
- work items stuck in `CLAIMED/PROCESSING` beyond their lease
- P2 tasks overdue or stuck in `VERIFYING`

A failed page does not delete the source PDF or any successfully processed sibling
page.

## Phase 2 Web

P2 Web routes are new:

- `/p2/work-queue`
- `/p2/tasks`
- `/p2/journeys/:journeyId/overview`
- `/p2/journeys/:journeyId/documents`
- `/p2/journeys/:journeyId/tasks`

Existing Verigence routes and links stay unchanged.
