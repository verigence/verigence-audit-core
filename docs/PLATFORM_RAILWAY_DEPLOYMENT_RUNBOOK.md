# Verigence Platform DEV — Railway Stable Deployment Runbook

**Owner:** Platform / Audit Core  
**Target Railway project:** `verigence-audit-core` (`cf00a1cd-623e-4c72-8836-db95652c63db`)  
**Target Railway environment:** `dev` (`398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`)  
**Control plane:** GitHub Actions only

## 1. Stable operating model

Audit Core, Security and Document Intelligence run in one Railway project so backend traffic stays on Railway private networking.

Persistent services:

- `verigence-audit-core` — Audit Core;
- `security` — Verigence Security;
- `di-api` — DI HTTP API only;
- `di-worker` — DI ProcessingWorker plus EODRetryScheduler;
- `Postgres` — consolidated DEV database;
- `platform-smoke` — retained verification service; compute is stopped after verification.

There is **no** `di-scheduler` Railway service. `EODRetryScheduler` runs inside `di-worker`.

Legacy standalone Security and DI Railway projects remain intact during the stabilization period. The stable deployment workflows do not delete them.

## 2. Canonical GitHub Actions workflows

Normal platform operation uses exactly three Railway workflows in `verigence/verigence-audit-core`:

1. `platform-bootstrap.yml` — exceptional/idempotent resource creation only;
2. `platform-deploy.yml` — normal deployment and private migration path;
3. `platform-verify.yml` — independent private-network acceptance test after deployment.

Do not restore or use versioned migration-era workflows (`*-v2`, `*-v3`, `*-v4`, `*-v5`, `*-v7`, `*-v8`, `*-v9`) for normal operation.

Audit Core source CI and the existing Neon schema workflow remain separate from these Railway platform workflows.

## 3. Mandatory operating rule

All normal Railway mutations for this consolidated platform are performed by GitHub Actions.

Do not use Railway UI or a local Railway CLI for routine variable changes, migrations, dependency binding, deployment, verification or rollback. This keeps configuration changes reproducible and leaves an Actions audit trail.

## 4. Credentials and privilege separation

| GitHub secret | Purpose |
|---|---|
| `RAILWAY_TOKEN` | Project-scoped token for normal deploy, variable, log and verification operations. |
| `RAILWAY_API_TOKEN` | Workspace/account token reserved for bootstrap/resource creation and exceptional discovery. |

Normal deploy and verify must use only the project-scoped token. Bootstrap is the only canonical workflow allowed to require the broader token.

## 5. Bootstrap workflow

Run bootstrap only when a required service/database object is missing or the consolidated project is being recreated.

Bootstrap is idempotent and ensures:

```text
verigence-audit-core   # already present as project service
security
di-api
di-worker
platform-smoke
Postgres
```

It must never create `di-scheduler`.

Bootstrap creates resource objects only. Runtime variables, migrations and application deployment belong to `platform-deploy.yml`.

## 6. Deployment workflow

`platform-deploy.yml` is the canonical deployment path.

Before changing runtime state it validates:

- the project token resolves to the expected Railway project;
- all required service objects already exist;
- no `di-scheduler` service exists;
- Audit Core has an OAuth client secret;
- Security already has signing-key material — normal deployment **must not implicitly rotate keys**;
- DI API and DI worker have the required persisted storage/secret configuration;
- shared DI API/worker secret, storage and Document-AI settings are identical.

The workflow then applies private service wiring and deploys in this order:

```text
Security -> DI API -> DI worker -> Audit Core
```

A deploy is not accepted merely because a container was submitted. The workflow requires all four latest deployments to become `SUCCESS` and verifies the Security migration plus DI worker/scheduler startup markers.

## 7. Private endpoints

Backend communication uses Railway private DNS:

```text
SECURITY_BASE_URL=http://security.railway.internal:8001
SECURITY_TOKEN_URL=http://security.railway.internal:8001/oauth/token
SECURITY_JWKS_URL=http://security.railway.internal:8001/.well-known/jwks.json
DI_BASE_URL=http://di-api.railway.internal:8000
```

Audit Core does not require public Security or DI endpoints in consolidated DEV.

## 8. Database migration policy

Security and DI database migrations execute **inside Railway** using pre-deploy commands so Postgres remains private.

- Security schema/default-role/dev-tenant initialization runs as a Railway pre-deploy command.
- DI runs `alembic upgrade head` as a Railway pre-deploy command.
- DI includes `psycopg2-binary` for Alembic's synchronous PostgreSQL path while application traffic uses `asyncpg`.

Do not create or depend on `DATABASE_PUBLIC_URL` merely to run migrations from a GitHub-hosted runner.

Do not execute destructive database down-migrations automatically during rollback.

## 9. DI runtime topology

The stable DI process model is:

```text
di-api
└── HTTP API only
    DI_WORKER_ENABLED=false

di-worker
├── ProcessingWorker
└── EODRetryScheduler
    DI_WORKER_ENABLED=true
```

Both are built from the same DI revision. The worker must emit:

```text
DI_WORKER_STARTED=PASS
DI_EOD_SCHEDULER_STARTED=PASS
```

## 10. DI tenant RLS context

Tenant-scoped transactions set `app.tenant_id` with parameter-safe PostgreSQL configuration:

```sql
SELECT set_config('app.tenant_id', :tid, true)
```

The final `true` makes the setting transaction-local. Do not parameterize `SET LOCAL app.tenant_id = :tid`; asyncpg/PostgreSQL rejects that syntax.

## 11. Security signing-key rule

Normal deployment must preserve existing signing-key material and fail if it is unexpectedly absent. Signing-key generation/rotation is a deliberate security operation, not a deployment side effect.

Private key values must never be printed in Actions logs. Use stdin/file handling for secret writes.

An earlier migration-era DEV key that appeared in an Actions log was immediately rotated and invalidated; the replacement was stored without printing it.

## 12. Verification workflow

Run `platform-verify.yml` after deployment or whenever platform connectivity/authentication needs independent confirmation.

The private smoke container verifies:

```text
platform-smoke
  -> Security /health
  -> DI /health
  -> DI /ready
  -> Security /oauth/token using Audit Core client credentials
  -> authenticated DI tenant request
  -> DI validates the Security JWT using Security JWKS over private DNS
  -> tenant-scoped database query succeeds
```

Required acceptance markers:

```text
PLATFORM_SECURITY_HEALTH=PASS
PLATFORM_DI_HEALTH=PASS
PLATFORM_DI_READY=PASS
PLATFORM_SECURITY_SERVICE_TOKEN=PASS
PLATFORM_SECURITY_TO_DI_JWKS_AUTH=PASS
PLATFORM_PRIVATE_E2E=PASS
PLATFORM_DI_WORKER=PASS
PLATFORM_DI_EOD_SCHEDULER=PASS
FINAL_TOPOLOGY=PASS
DI_PROCESS_TOPOLOGY=API_PLUS_WORKER
DI_SCHEDULER_SERVICE=NONE
```

`platform-smoke` compute is always stopped after verification, including failure paths. The service object remains for future checks.

## 13. Canonical stable evidence

The migration-era final cutover was first proven by Actions run `31958949627`.

The cleaned canonical workflows were then executed independently and passed:

- `31959607760` — **Deploy unified Railway platform DEV** — `success`;
- `31959629154` — **Verify unified Railway platform DEV** — `success`;
- `31959614313` — Audit Core source **CI** — `success`.

These canonical runs supersede the versioned diagnostic/cutover workflows as the operational reference.

## 14. DI repository quality gate

DI CI must run the full pytest suite. Lint/type checking is applied to files changed relative to the PR base so unrelated historical Ruff debt does not make every focused runtime change impossible.

The repository previously declared a 70% coverage gate while its measured suite covered about 49%, making `main` permanently red. The stable branch enforces the measured 49% baseline so coverage cannot regress. Raising coverage toward 70% remains an explicit engineering backlog objective; do not lower the 49% floor to make a change pass.

The placeholder `ops-ui` is not treated as a buildable frontend until `package.json` and `package-lock.json` exist. Once implementation starts, the same CI workflow automatically enables Node install/build checks.

## 15. Rollback

Rollback is performed through GitHub Actions by deploying a previously known-good source revision and restoring previously recorded private endpoint/configuration values if necessary.

Rules:

- do not delete or alter legacy standalone projects as part of an application rollback;
- do not automatically down-migrate the consolidated database;
- do not rotate Security signing keys merely because an application revision is rolled back;
- after rollback, run `platform-verify.yml` and require the complete private E2E markers again.

## 16. Release checklist

A platform revision is considered stable only when all of the following are true:

- repository CI for the changed components is green;
- canonical `platform-deploy.yml` succeeds;
- canonical `platform-verify.yml` succeeds;
- all four persistent application services show latest deployment `SUCCESS`;
- DI worker and EOD scheduler markers are present;
- private OAuth/JWKS/DI E2E passes;
- `platform-smoke` compute is stopped afterward;
- no temporary diagnostic workflows are required for operation;
- legacy Railway projects remain untouched unless a separate deletion change is explicitly approved.
