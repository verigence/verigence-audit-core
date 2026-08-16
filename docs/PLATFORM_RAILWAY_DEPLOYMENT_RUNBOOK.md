# Verigence Platform DEV — Railway Deployment Runbook

**Owner:** Platform / Audit Core  
**Target Railway project:** `verigence-audit-core` (`cf00a1cd-623e-4c72-8836-db95652c63db`)  
**Target Railway environment:** `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`  
**Control plane:** GitHub Actions only

## 1. Purpose

Consolidate Audit Core, Security and Document Intelligence into one Railway project so backend traffic can use Railway private networking.

Proven target services:

- `verigence-audit-core` — existing Audit Core service;
- `security` — source `verigence/verigence-security`;
- `di-api` — source `verigence/verigence-di`;
- `platform-smoke` — operational private-network verification service;
- one Railway PostgreSQL service for consolidated DEV Security/DI where appropriate.

**Important DI topology rule:** do not create `di-worker` or `di-scheduler` service objects merely because worker/scheduler code exists in the DI repository. Separate Railway process services must be created only if the actual deployed DI Railway topology proves they already exist or a deliberate architecture change is approved.

Legacy standalone Security and DI projects are retained during stabilization and are not deleted by bootstrap.

## 2. Mandatory operating rule

All Railway mutations for this consolidated platform are executed by GitHub Actions in `verigence/verigence-audit-core`.

Do not use Railway UI or local CLI for normal service creation, variable changes, migrations, deployment, dependency binding or rollback. GitHub Actions explicitly performs Railway operations and retains the deployment evidence.

## 3. Railway credentials

| GitHub secret | Purpose |
|---|---|
| `RAILWAY_TOKEN` | Existing project-scoped token for normal deploy/runtime operations in `verigence-audit-core`. |
| `RAILWAY_API_TOKEN` | Workspace/account-level token intended for read-only workspace discovery and exceptional resource bootstrap. |

The project token is deliberately retained for normal deployment. The broader token should only be used for workspace discovery and creation/recreation operations.

A token must be validated by a read-only GitHub Action before any bootstrap mutation. If Railway returns `Unauthorized`, bootstrap must not run.

## 4. DI runtime topology — current evidence

The DI repository definitely contains:

- a `ProcessingWorker` implementation;
- an `EODRetryScheduler` implementation.

However, repository evidence currently shows these components wired into the FastAPI application lifecycle behind `DI_WORKER_ENABLED`:

```text
di-api FastAPI lifespan
   ├── ProcessingWorker.start()
   └── EODRetryScheduler.start()
```

The existing DI DEV GitHub workflow also invokes only:

```text
railway up --service di-api --environment dev
```

although its step label refers to API + Worker + Scheduler.

Therefore the repository **does not prove** that Railway currently has independent services named `di-worker` or `di-scheduler`.

The DI Dockerfile comment says the default API command may be overridden for worker/scheduler process types. Treat that as architectural capability, not evidence that those Railway services currently exist.

## 5. Required DI discovery before cutover

Before changing DI process topology, run a read-only Railway workspace inventory through GitHub Actions and record:

1. the existing DI Railway project ID;
2. its environment(s);
3. every service name and ID;
4. whether there is a separately deployed scheduler service;
5. whether there is a separately deployed worker service;
6. the start command/runtime configuration of each DI process service where visible.

Then preserve the actual topology during consolidation unless a separate architecture decision explicitly changes it.

If the current DI deployment is one `di-api` service that starts worker + scheduler internally, the consolidated DEV deployment must initially preserve that behavior. If Railway proves a separate scheduler/worker exists, recreate those exact services in the consolidated project and bind them to the same DI database/storage configuration and same source revision as the API.

## 6. Target private endpoints

Once Security and DI are in the same Railway project/environment as Audit Core:

```text
SECURITY_BASE_URL=http://security.railway.internal:8001
SECURITY_TOKEN_URL=http://security.railway.internal:8001/oauth/token
SECURITY_JWKS_URL=http://security.railway.internal:8001/.well-known/jwks.json
DI_BASE_URL=http://di-api.railway.internal:8000
```

Audit Core must not depend on public URLs for these backend-to-backend calls after private networking is proven.

## 7. Bootstrap procedure

Workflow: `.github/workflows/platform-bootstrap.yml`.

Bootstrap is idempotent and currently creates only proven service objects:

- `security`;
- `di-api`;
- `platform-smoke`;
- PostgreSQL if no suitable consolidated PostgreSQL exists.

It does **not** create `di-worker` or `di-scheduler` until Railway discovery proves those process services are part of the existing DI deployment or a deliberate split is approved.

Acceptance marker:

```text
PLATFORM_BOOTSTRAP=PASS
DI_PROCESS_TOPOLOGY=NOT_SPLIT_BY_BOOTSTRAP
```

## 8. Core platform deployment

Workflow: `.github/workflows/platform-deploy.yml`.

Normal deployment uses `RAILWAY_TOKEN` and must:

1. record Audit Core, Security and DI Git SHAs;
2. verify the target Railway project;
3. configure consolidated Security without exposing raw client secrets;
4. run Security schema/tenant initialization;
5. configure DI with its database and private Security JWKS URL;
6. run DI Alembic migrations;
7. bind Audit Core to private Security/DI URLs;
8. deploy Security, DI and Audit Core from GitHub Actions;
9. run private-network authentication smoke tests from `platform-smoke`.

Do not alter `DI_WORKER_ENABLED` or DI process start topology merely to fit the consolidation workflow. The final setting must follow the verified existing DI runtime design.

## 9. Required acceptance evidence

At minimum retain:

```text
PLATFORM_PROJECT_VERIFIED=PASS
PLATFORM_SECURITY_HEALTH=PASS
PLATFORM_DI_HEALTH=PASS
PLATFORM_DI_READY=PASS
PLATFORM_SECURITY_SERVICE_TOKEN=PASS
PLATFORM_SECURITY_TO_DI_JWKS_AUTH=PASS
PLATFORM_PRIVATE_E2E=PASS
PLATFORM_AUDIT_CORE_REBOUND=PASS
```

For the DI background lifecycle, also retain evidence appropriate to the verified topology:

- if in-process: API logs proving ProcessingWorker and EOD scheduler startup;
- if separate services: service-specific startup/log evidence for each process.

## 10. Secret handling

No Security client secret, JWT private key, DI secret key, database password or object-storage credential is committed to GitHub.

Secret values are masked and written to Railway without being printed. The Audit Core client secret remains in Audit Core; Security stores only its verifier where supported.

## 11. Rollback

Keep legacy standalone Security and DI Railway projects intact until the consolidated path is fully green.

Rollback is executed through GitHub Actions by restoring the previously recorded service endpoints/source revisions and redeploying. Do not run destructive database down-migrations automatically.

## 12. Legacy deployment retirement

Only after consolidated Security, DI, Audit Core and the DI background processing lifecycle are proven:

- retire old Railway mutation workflows that target legacy projects;
- keep CI/test workflows in source repos;
- point deployment documentation at this runbook;
- delete legacy Railway projects only as a separately approved cleanup action.

## 13. Current open prerequisite

`RAILWAY_API_TOKEN` is present in GitHub Actions, but the first read-only workspace inventory returned Railway `Unauthorized`. Treat the broader token as not yet validated. Do not run bootstrap creation until a read-only workspace/project command succeeds with that token.
