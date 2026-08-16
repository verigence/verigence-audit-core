# Verigence Platform DEV — Railway Deployment Runbook

**Owner:** Platform / Audit Core  
**Target Railway project:** `verigence-audit-core` (`cf00a1cd-623e-4c72-8836-db95652c63db`)  
**Target Railway environment:** `dev` (`398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`)  
**Control plane:** GitHub Actions only

## 1. Purpose

Consolidate Audit Core, Security and Document Intelligence into one Railway project so backend traffic uses Railway private networking.

Verified target services:

- `verigence-audit-core` — Audit Core;
- `security` — source `verigence/verigence-security`;
- `di-api` — DI HTTP API;
- `di-worker` — DI ProcessingWorker + EODRetryScheduler;
- `platform-smoke` — retained operational private-network verification service, normally scaled down;
- `Postgres` — consolidated DEV database for Security/DI.

There is no `di-scheduler` Railway service. The EOD scheduler runs inside `di-worker`.

Legacy standalone Security and DI projects are retained during stabilization and are not deleted by this deployment.

## 2. Mandatory operating rule

All Railway mutations for this consolidated platform are executed by GitHub Actions in `verigence/verigence-audit-core`.

Do not use Railway UI or local CLI for normal service creation, variable changes, migrations, deployment, dependency binding or rollback. GitHub Actions explicitly performs Railway operations and retains deployment evidence.

## 3. Railway credentials

| GitHub secret | Purpose |
|---|---|
| `RAILWAY_TOKEN` | Project-scoped token for normal deploy/runtime operations in `verigence-audit-core`. |
| `RAILWAY_API_TOKEN` | Workspace/account-level token for workspace discovery and exceptional resource bootstrap. |

The project token is used for normal deployment and verification. The broader token is reserved for workspace discovery and creation/recreation operations.

## 4. Verified DI runtime topology

Read-only Railway discovery of the legacy `verigence-di` project proved exactly two service objects:

```text
verigence-di
├── di-api
└── verigence-di   # historical worker service name
```

The historical second service used:

```text
Dockerfile.worker
python -m verigence.di.workers
```

No independent scheduler service existed.

The consolidated topology therefore preserves two DI services:

```text
di-api
└── HTTP API only; DI_WORKER_ENABLED=false

di-worker
├── ProcessingWorker
└── EODRetryScheduler
```

The worker emits explicit startup markers:

```text
DI_WORKER_STARTED=PASS
DI_EOD_SCHEDULER_STARTED=PASS
```

## 5. Private endpoints

Backend-to-backend communication uses Railway private DNS:

```text
SECURITY_BASE_URL=http://security.railway.internal:8001
SECURITY_TOKEN_URL=http://security.railway.internal:8001/oauth/token
SECURITY_JWKS_URL=http://security.railway.internal:8001/.well-known/jwks.json
DI_BASE_URL=http://di-api.railway.internal:8000
```

Audit Core does not require public URLs for Security or DI in consolidated DEV.

## 6. Database migrations

Security and DI migrations run inside Railway rather than from a GitHub-hosted runner. This keeps PostgreSQL private and avoids requiring `DATABASE_PUBLIC_URL`.

- Security schema/tenant initialization executes as a Railway pre-deploy command.
- DI runs `alembic upgrade head` as a Railway pre-deploy command.
- DI runtime includes `psycopg2-binary` because Alembic uses the synchronous PostgreSQL driver while application traffic uses `asyncpg`.

Do not expose PostgreSQL publicly merely to run deployment migrations.

## 7. DI RLS tenant context

DI tenant-scoped transactions set `app.tenant_id` using parameter-safe PostgreSQL configuration:

```sql
SELECT set_config('app.tenant_id', :tid, true)
```

The `true` argument makes the setting transaction-local. Do not use a bind parameter directly in `SET LOCAL app.tenant_id = :tid`; asyncpg/PostgreSQL rejects that form.

## 8. Security key handling

Security signing key material must never be printed to Actions logs.

During initial consolidation testing one generated DEV key appeared in an Actions log because multiline masking was ineffective. That key was immediately rotated and is no longer valid. The replacement key was written to Railway through stdin/file handling and was not printed.

Future signing-key generation/rotation must use file/stdin handling, not shell echo of multiline private-key content.

## 9. Verified DEV acceptance evidence

Final successful cutover verification:

- GitHub Actions run: `31958949627`
- Workflow: `Final unified Railway DEV cutover v9`
- Result: `success`

Required runtime markers all passed:

```text
PLATFORM_PROJECT_VERIFIED=PASS
PLATFORM_DI_DEPLOYMENT=PASS|di-api
PLATFORM_DI_DEPLOYMENT=PASS|di-worker
PLATFORM_DI_WORKER=PASS
PLATFORM_DI_EOD_SCHEDULER=PASS
PLATFORM_SECURITY_HEALTH=PASS
PLATFORM_DI_HEALTH=PASS
PLATFORM_DI_READY=PASS
PLATFORM_SECURITY_SERVICE_TOKEN=PASS
PLATFORM_SECURITY_TO_DI_JWKS_AUTH=PASS
PLATFORM_PRIVATE_E2E=PASS
FINAL_SERVICE=PASS|verigence-audit-core
FINAL_SERVICE=PASS|security
FINAL_SERVICE=PASS|di-api
FINAL_SERVICE=PASS|di-worker
FINAL_TOPOLOGY=PASS
DI_PROCESS_TOPOLOGY=API_PLUS_WORKER
DI_SCHEDULER_SERVICE=NONE
```

The private E2E path verified:

```text
platform-smoke
  -> security.railway.internal /health
  -> di-api.railway.internal /health + /ready
  -> security.railway.internal /oauth/token
  -> DI authenticated request using Security-issued JWT
  -> DI obtains Security JWKS over Railway private networking
  -> tenant-scoped DI database request succeeds
```

After verification, `platform-smoke` compute is stopped with `railway down`; the service object is retained for future operational checks.

## 10. Current persistent topology

```text
verigence-audit-core
security
di-api
di-worker
Postgres
platform-smoke   # service retained, compute normally stopped
```

`di-scheduler` must not be created.

## 11. Secret handling

No Security client secret, active JWT private key, DI secret key, database password or object-storage credential is committed to GitHub.

Secret values are written to Railway without being printed. Audit Core keeps its OAuth client secret; Security stores the configured verifier/client representation required for token issuance.

## 12. Rollback

Keep legacy standalone Security and DI Railway projects intact until the consolidated path has completed the desired stabilization period.

Rollback is executed through GitHub Actions by restoring the previously recorded service endpoints/source revisions and redeploying. Do not run destructive database down-migrations automatically.

## 13. Merge and cleanup

The runtime cutover is green, but repository PRs remain draft and unmerged until explicitly approved.

Before merge:

1. reduce the temporary diagnostic/cutover workflows created during migration to a small canonical bootstrap/deploy/verify set;
2. keep the final acceptance evidence in this runbook;
3. ensure normal source CI is green or document unrelated pre-existing failures;
4. do not delete legacy Railway projects as part of PR merge.

Legacy project deletion, if desired later, is a separate explicitly approved cleanup action.
