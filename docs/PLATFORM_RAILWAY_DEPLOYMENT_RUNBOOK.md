# Verigence Platform DEV — Railway Deployment Runbook

**Owner:** Platform / Audit Core  
**Target Railway project:** `verigence-audit-core` (`cf00a1cd-623e-4c72-8836-db95652c63db`)  
**Target Railway environment:** `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`  
**Control plane:** GitHub Actions only

## 1. Purpose

This runbook consolidates the three backend modules into one Railway project so they can use Railway private networking while preserving DI process separation:

- `verigence-audit-core` — existing Audit Core service;
- `security` — source: `verigence/verigence-security`;
- `di-api` — HTTP Document Intelligence API, source: `verigence/verigence-di`;
- `di-worker` — long-running DI processing worker, source: the same DI commit;
- `di-scheduler` — long-running DI EOD retry scheduler, source: the same DI commit;
- `platform-smoke` — operational verification service only;
- one Railway-managed PostgreSQL service used by consolidated DEV Security and DI.

Legacy standalone Security and DI Railway projects are retained during stabilization as rollback references. They are not deleted by this runbook.

## 2. Mandatory operating rule

All Railway changes for the consolidated platform are made through GitHub Actions in `verigence/verigence-audit-core`.

Do not use the Railway UI or a local Railway CLI for normal service creation, database creation, variable changes, migrations, deployments, dependency URL changes, or rollback. Railway repository auto-deploy is not the platform deployment mechanism; GitHub Actions explicitly deploys source with `railway up`.

## 3. Why there are two Railway tokens

Railway project tokens are environment/deployment scoped. They are appropriate for normal deployments and service-specific automation, but the live bootstrap test confirmed the existing project token cannot link/create new project resources.

Therefore the GitHub Actions design separates privileges:

| GitHub secret | Scope | Used by | Purpose |
|---|---|---|---|
| `RAILWAY_API_TOKEN` | Railway workspace or account token | `.github/workflows/platform-bootstrap.yml` | One-time/exceptional creation of `security`, `di-api`, `di-worker`, `di-scheduler`, `platform-smoke`, and PostgreSQL. |
| `RAILWAY_TOKEN` | Project token for `cf00a1cd-623e-4c72-8836-db95652c63db` | deployment workflows | Normal variable management, migrations, deployments, logs and redeploy operations inside the already-created project environment. |

`RAILWAY_API_TOKEN` is required only for bootstrap/recreation of project resources. Keep the narrower project token as the day-to-day deployment credential.

## 4. Target topology

```text
Railway project: verigence-audit-core
Environment: 398c3cfb-d7c7-4aaf-b5a4-3b44d3087451

verigence-audit-core
   |-- OAuth/JWKS ------> http://security.railway.internal:8001
   `-- DI --------------> http://di-api.railway.internal:8000

security
   `-- PostgreSQL

di-api
   |-- HTTP API only
   |-- JWKS ------------> http://security.railway.internal:8001/.well-known/jwks.json
   `-- PostgreSQL

di-worker
   |-- claims PENDING processing jobs
   |-- performs document processing
   `-- same DI PostgreSQL/runtime configuration

di-scheduler
   |-- APScheduler EOD retry check every 60 seconds
   |-- inserts EOD_RETRY processing jobs
   `-- same DI PostgreSQL/runtime configuration

platform-smoke
   `-- private-network verification deployment
```

Canonical private endpoints:

- Security: `http://security.railway.internal:8001`
- Token: `http://security.railway.internal:8001/oauth/token`
- JWKS: `http://security.railway.internal:8001/.well-known/jwks.json`
- DI API: `http://di-api.railway.internal:8000`
- DI liveness: `http://di-api.railway.internal:8000/health`
- DI readiness: `http://di-api.railway.internal:8000/ready`

`di-worker` and `di-scheduler` are private long-running process services and do not expose application HTTP endpoints.

## 5. Why DI is three Railway services

The DI repository contains two independent background responsibilities:

1. **Processing Worker** — polls `docintel.processing_jobs`, claims jobs with `FOR UPDATE SKIP LOCKED`, and performs document processing.
2. **EOD Retry Scheduler** — runs every 60 seconds, evaluates each Tenant's configured EOD window, and inserts `EOD_RETRY` jobs.

The DI production Dockerfile explicitly anticipates Railway command overrides for worker/scheduler process types. The unified deployment therefore does not run those processes inside `di-api`.

The API service must keep `DI_WORKER_ENABLED=false`. The separate process workflow starts exactly one worker process per `di-worker` replica and exactly one scheduler process per `di-scheduler` replica. Keep the scheduler at one replica unless the scheduler implementation is changed to use distributed leader election/advisory locking.

This avoids duplicate EOD schedulers when the API is scaled horizontally.

## 6. Bootstrap procedure

Bootstrap is required only when the consolidated services/database do not exist.

1. Add `RAILWAY_API_TOKEN` to GitHub Actions secrets in `verigence/verigence-audit-core`. Use a Railway workspace or account token authorized to create resources in the target project.
2. Run **Bootstrap unified Railway platform DEV** (`.github/workflows/platform-bootstrap.yml`).
3. The Action links explicitly to project `cf00a1cd-623e-4c72-8836-db95652c63db` and environment `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`.
4. It idempotently ensures these resource objects exist:
   - `verigence-audit-core` (already present);
   - `security`;
   - `di-api`;
   - `di-worker`;
   - `di-scheduler`;
   - `platform-smoke`;
   - one PostgreSQL service.
5. Accept bootstrap only when the log contains `PLATFORM_BOOTSTRAP=PASS`.

The bootstrap workflow creates empty application services. It does not connect them to Railway GitHub auto-deploy sources.

## 7. Core platform deployment

Normal deployment uses only the project-scoped `RAILWAY_TOKEN`.

1. Run **Deploy unified Railway platform DEV** (`.github/workflows/platform-deploy.yml`).
2. Security and DI default to their `main` branches; explicit SHAs/refs may be supplied for controlled testing or rollback.
3. The workflow records the Audit Core, Security and DI Git SHAs.
4. It verifies the target Railway project and required pre-created services.
5. It retrieves the existing Audit Core `SECURITY_CLIENT_SECRET` without printing it and derives only its SHA-256 verifier for Security.
6. It configures Security with issuer `verigence-security`, audience `verigence-platform`, the shared DEV PostgreSQL service and least-privilege `audit-core` scopes:
   - `di.document.read`
   - `di.document.upload`
7. It applies Security schema SQL and ensures tenant `dev-auth-test` exists.
8. It configures `di-api` with the shared PostgreSQL service and the private Security JWKS URL. `DI_WORKER_ENABLED=false` is mandatory on the API service.
9. It runs DI Alembic migrations.
10. It rewires Audit Core to private Security/DI URLs.
11. It deploys Security, DI API and Audit Core explicitly with `railway up`.
12. It runs the private Security → DI smoke from Railway networking.

## 8. DI worker and scheduler deployment

Workflow: `.github/workflows/platform-di-processes.yml` — **Deploy DI worker and scheduler DEV**.

It can be run manually and is also configured to follow a successful core platform deployment after these workflows are on the default branch.

The workflow:

1. verifies `di-api`, `di-worker` and `di-scheduler` exist in the consolidated project;
2. reads `DI_*` runtime variables from `di-api` inside GitHub Actions without printing secret values;
3. synchronizes those DI variables to `di-worker` and `di-scheduler`, so database, storage, Security JWKS and future DI runtime settings stay aligned;
4. forces `DI_WORKER_ENABLED=false` on the background process services so accidentally launching the FastAPI app cannot create duplicate in-process workers/schedulers;
5. builds a standalone worker command that calls `ProcessingWorker.start()` and owns only the worker lifecycle;
6. builds a standalone scheduler command that calls `EODRetryScheduler.start()` and owns only the scheduler lifecycle;
7. deploys both services from the same DI repository revision;
8. verifies runtime startup markers from Railway logs.

Required success markers:

```text
DI_PROCESS_SERVICES_PRESENT=PASS
DI_PROCESS_VARIABLE_SYNC=PASS
DI_WORKER_STARTED=PASS
DI_SCHEDULER_STARTED=PASS
DI_PROCESS_SEPARATION=PASS
```

## 9. Expected core-platform acceptance markers

A core platform deployment is accepted only when the GitHub Actions log contains:

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

A complete DI runtime acceptance additionally requires all five DI process markers in section 8.

## 10. Secret handling

No Security client secret, JWT private key, DI secret key, storage credential, or database password is committed to GitHub.

Workflows write secret values to Railway through stdin and do not print raw DI variable JSON. The DI process workflow uses a runner-local temporary variable snapshot solely to synchronize the already-configured `di-api` runtime into `di-worker` and `di-scheduler`.

The raw Audit Core client secret remains stored in the Audit Core Railway service; Security stores only its SHA-256 verifier.

## 11. DI storage/config prerequisite

The worker is the process that actually reads uploaded content and invokes the document-processing adapter. Therefore the consolidated `di-api` must contain the correct DEV object-storage configuration before a real document-upload/processing E2E can be declared complete (`DI_STORAGE_PROVIDER`, endpoint, access credentials, bucket and region as applicable).

The process workflow deliberately clones these values from `di-api`; it does not invent separate worker/scheduler storage credentials.

Document AI remains in DEV mock mode until the real provider is deliberately enabled.

## 12. Database model

Audit Core keeps its existing `DATABASE_URL` and database boundary.

The consolidated DEV Security and DI services share one Railway-managed PostgreSQL instance for infrastructure efficiency while retaining their existing application schema/table boundaries. GitHub Actions uses the database public migration endpoint only for migrations and masks it before use; runtime services use Railway private/reference variables.

Destructive automatic down-migrations are not part of rollback.

## 13. What the private smoke proves

GitHub-hosted runners cannot directly resolve Railway private DNS, so a deployment inside `platform-smoke` performs verification from the Railway private network.

The core smoke proves:

1. Security private liveness;
2. DI private liveness and DB readiness;
3. the real `audit-core` confidential client can obtain a Security SERVICE token for `dev-auth-test`;
4. DI retrieves Security JWKS through the private network;
5. DI accepts the Security-issued token on a tenant-scoped API request.

The DI process verification separately proves the worker and scheduler processes start as independent services. A later full document E2E must prove upload → job creation → worker processing → resulting DI document state.

Interactive USER OAuth/Clerk migration remains separate until Clerk/user state is provisioned into the consolidated Security service and tested.

## 14. Rollback

During stabilization, keep the legacy standalone Security and DI Railway projects intact.

Rollback must also be executed through GitHub Actions. Re-run a known-good platform workflow revision or a controlled rollback revision that restores previously recorded dependency endpoints and redeploys Audit Core. `di-worker` and `di-scheduler` must roll back to the same DI source revision as `di-api`.

Database rollback is forward-compatible only; do not automatically execute destructive down-migrations.

## 15. Legacy deployment retirement

Only after the consolidated API, worker, scheduler and Security/Audit Core path are green:

- retire Railway mutation workflows in `verigence/verigence-security` that deploy to the old Security project;
- retire direct DEV Railway deployment ownership in `verigence/verigence-di`;
- remove temporary inspection workflows;
- retain CI/test workflows in each source repository;
- point each repository's deployment documentation to this runbook.

## 16. DEV limitations before production

This setup is DEV only. Production requires a separate Railway project/environment, production tokens, production databases/storage, real Document AI configuration, Clerk/interactive auth provisioning, approval gates, production secret management, and an HA-safe scheduler strategy before more than one scheduler replica is allowed.

## 17. Evidence to retain

For each accepted deployment retain:

- bootstrap run ID when resources are created/recreated;
- core platform deployment run ID;
- DI process deployment run ID;
- Audit Core, Security and DI commit SHAs;
- Railway project/environment IDs and all service IDs;
- migration completion evidence without secret values;
- core-platform and DI-process acceptance markers;
- any rollback or exception record.
