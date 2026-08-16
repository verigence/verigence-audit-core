# Verigence Platform DEV — Railway Deployment Runbook

**Owner:** Platform / Audit Core  
**Target Railway project:** `verigence-audit-core` (`cf00a1cd-623e-4c72-8836-db95652c63db`)  
**Target Railway environment:** `dev` (`398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`)  
**Control plane:** GitHub Actions only

## 1. Purpose

Consolidate Audit Core, Security and Document Intelligence into one Railway project so backend traffic uses Railway private networking and every Railway mutation is auditable through GitHub Actions.

Verified target services:

- `verigence-audit-core` — Audit Core API;
- `security` — Security/OAuth/JWKS service;
- `di-api` — Document Intelligence HTTP API;
- `di-worker` — Document Intelligence background worker **and EOD scheduler owner**;
- `platform-smoke` — temporary/private-network verification service;
- `Postgres` — consolidated DEV PostgreSQL for Security/DI where configured.

There is **no `di-scheduler` Railway service**.

Legacy standalone Railway projects remain available during stabilization and are not deleted by this runbook.

## 2. Mandatory operating rule

All Railway mutations for the consolidated platform are executed by GitHub Actions in `verigence/verigence-audit-core`.

Do not use the Railway UI or a local Railway CLI for normal service creation, variable changes, migrations, deployment, dependency binding or rollback. GitHub Actions explicitly performs Railway operations and retains run evidence.

## 3. Railway credentials

| GitHub secret | Purpose |
|---|---|
| `RAILWAY_TOKEN` | Project-scoped token for normal deployment/runtime operations in the consolidated `verigence-audit-core` project. |
| `RAILWAY_API_TOKEN` | Workspace/account token for read-only workspace discovery and exceptional resource bootstrap/migration. |

Both tokens have now been validated through GitHub Actions. Use the narrower project token for routine deployment.

## 4. Verified legacy DI topology

Read-only Railway discovery on 2026-08-16 proved the legacy `verigence-di` project (`62c22163-78d0-4a86-a2f7-dbf39e64aa4d`) contains exactly two service objects:

```text
verigence-di Railway project
├── di-api       33ce685e-27ef-4f2a-b528-8d1d479c2edd
└── verigence-di 549cba0d-eeba-43da-a6c0-c0878b53fcf8
```

The second service is the historical DI worker even though its Railway service name is `verigence-di`. Deployment metadata proves it used:

```text
Dockerfile.worker
python -m verigence.di.workers
```

The API used:

```text
Dockerfile
uvicorn verigence.di.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}
```

No third scheduler service exists in the legacy project.

The DI source contains `EODRetryScheduler`, but that is an application component, not a Railway service. The consolidated topology therefore preserves **two DI services**, with the scheduler owned by `di-worker`.

## 5. Consolidated DI runtime ownership

```text
di-api
└── FastAPI HTTP only

di-worker
├── ProcessingWorker
└── EODRetryScheduler
```

Rules:

1. `di-api` must set `DI_WORKER_ENABLED=false` so API replicas never start worker/scheduler background tasks.
2. `di-worker` runs `python -m verigence.di.workers` from `Dockerfile.worker`.
3. The worker entrypoint starts both `ProcessingWorker` and `EODRetryScheduler`.
4. There is no `di-scheduler` service.
5. Keep `di-worker` at one replica in DEV unless worker concurrency and scheduler leadership are deliberately redesigned. The worker job-claim path supports concurrent workers, but the scheduler currently has no distributed leader-election requirement documented for multiple scheduler owners.
6. `di-api` and `di-worker` must use the same DI source revision and the same database/storage/provider configuration.

Expected worker startup markers:

```text
DI_WORKER_STARTED=PASS
DI_EOD_SCHEDULER_STARTED=PASS
```

## 6. Target private endpoints

```text
SECURITY_BASE_URL=http://security.railway.internal:8001
SECURITY_TOKEN_URL=http://security.railway.internal:8001/oauth/token
SECURITY_JWKS_URL=http://security.railway.internal:8001/.well-known/jwks.json
DI_BASE_URL=http://di-api.railway.internal:8000
```

Audit Core must not depend on public Security/DI URLs after private networking is proven.

## 7. Bootstrap procedure

Workflow: `.github/workflows/platform-bootstrap.yml`.

Bootstrap idempotently ensures:

- `security`;
- `di-api`;
- `di-worker`;
- `platform-smoke`;
- `Postgres`;
- existing `verigence-audit-core`.

A successful bootstrap must contain:

```text
PLATFORM_BOOTSTRAP=PASS
DI_PROCESS_TOPOLOGY=API_PLUS_WORKER
DI_SCHEDULER_SERVICE=NONE
```

Bootstrap run `31957228232` completed successfully on 2026-08-16 and created the missing `di-worker` and `platform-smoke` service objects. It did not create a scheduler service.

## 8. Core platform deployment

Workflow: `.github/workflows/platform-deploy.yml`.

Normal deployment uses `RAILWAY_TOKEN` and must:

1. record Audit Core, Security and DI Git SHAs;
2. verify the target Railway project;
3. configure consolidated Security without exposing raw client secrets;
4. run Security schema/tenant initialization;
5. configure `di-api` with the consolidated database and private Security JWKS URL;
6. set `DI_WORKER_ENABLED=false` on `di-api`;
7. run DI Alembic migrations;
8. synchronize required `DI_*` runtime configuration from `di-api` to `di-worker` without printing secret values;
9. deploy `security`, `di-api`, `di-worker`, and `verigence-audit-core` from GitHub Actions using pinned source revisions;
10. verify worker and EOD scheduler startup markers;
11. bind Audit Core to private Security/DI URLs;
12. run the private-network Security → DI authentication smoke from `platform-smoke`.

## 9. Required acceptance evidence

A platform cutover is accepted only when GitHub Actions retains at least:

```text
PLATFORM_PROJECT_VERIFIED=PASS
PLATFORM_SECURITY_HEALTH=PASS
PLATFORM_DI_HEALTH=PASS
PLATFORM_DI_READY=PASS
DI_WORKER_STARTED=PASS
DI_EOD_SCHEDULER_STARTED=PASS
PLATFORM_SECURITY_SERVICE_TOKEN=PASS
PLATFORM_SECURITY_TO_DI_JWKS_AUTH=PASS
PLATFORM_PRIVATE_E2E=PASS
PLATFORM_AUDIT_CORE_REBOUND=PASS
```

For a full document-processing E2E, also prove:

```text
Audit Core upload/request
→ Security authorization
→ DI API intake
→ processing job creation
→ di-worker claim/process
→ resulting DI document state
```

## 10. Configuration migration

Before declaring full document processing complete, preserve the real DEV DI provider configuration required by both API and worker, especially object storage settings (`DI_STORAGE_*`) and any deliberately enabled Document AI/provider settings.

Do not copy the legacy DI database URL or old Security/JWKS URL into the consolidated environment. Those must point to the consolidated dependencies.

Any cross-project configuration migration must be performed by GitHub Actions with `RAILWAY_API_TOKEN`; secret values must never be printed to logs.

## 11. Secret handling

No Security client secret, JWT private key, DI secret key, database password, object-storage credential, or provider credential is committed to GitHub.

Secret values are masked and written to Railway without being printed. The raw Audit Core client secret remains in Audit Core; Security stores only its verifier where supported.

## 12. Rollback

Keep legacy standalone Security and DI Railway projects intact until the consolidated path is fully green.

Rollback is executed through GitHub Actions by restoring previously recorded dependency endpoints/source revisions and redeploying. Do not run destructive database down-migrations automatically.

`di-api` and `di-worker` must always roll back to the same DI revision.

## 13. Legacy deployment retirement

Only after consolidated Security, DI, Audit Core, worker processing and EOD scheduler lifecycle are proven:

- retire old Railway mutation workflows that target legacy projects;
- keep CI/test workflows in source repos;
- point deployment documentation at this runbook;
- delete legacy Railway projects only as a separately approved cleanup action.

## 14. Current source change

DI draft PR `verigence/verigence-di#4` restores the dedicated worker image/entrypoint on current `main` and makes `python -m verigence.di.workers` own both `ProcessingWorker` and `EODRetryScheduler`.

The DI repository currently has unrelated pre-existing CI/lint failures outside this two-service runtime change. The worker-runtime change must be verified independently and must not be claimed as fully CI-green until the repository baseline is repaired or an appropriately scoped check passes.
