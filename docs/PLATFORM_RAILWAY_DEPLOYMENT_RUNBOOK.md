# Verigence Platform DEV — Railway Deployment Runbook

**Owner:** Platform / Audit Core  
**Target Railway project:** `verigence-audit-core` (`cf00a1cd-623e-4c72-8836-db95652c63db`)  
**Target Railway environment:** `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`  
**Control plane:** GitHub Actions only

## 1. Purpose

This runbook consolidates the three backend modules into one Railway project so they can use Railway private networking:

- `verigence-audit-core` — existing Audit Core service;
- `security` — source: `verigence/verigence-security`;
- `di-api` — source: `verigence/verigence-di`;
- `platform-smoke` — operational verification service only;
- one Railway-managed PostgreSQL service used by consolidated DEV Security and DI.

Legacy standalone Security and DI Railway projects are retained during stabilization as rollback references. They are not deleted by this runbook.

## 2. Mandatory operating rule

All Railway changes for the consolidated platform are made through GitHub Actions in `verigence/verigence-audit-core`.

Do not use the Railway UI or a local Railway CLI for normal service creation, database creation, variable changes, migrations, deployments, dependency URL changes, or rollback. Railway repository auto-deploy is not the platform deployment mechanism; GitHub Actions explicitly deploys source with `railway up`.

## 3. Why there are two Railway tokens

Railway project tokens are intentionally environment/deployment scoped. They are appropriate for normal deployments and service-specific automation, but the live bootstrap test confirmed they cannot link/create new project resources.

Therefore the GitHub Actions design separates privileges:

| GitHub secret | Scope | Used by | Purpose |
|---|---|---|---|
| `RAILWAY_API_TOKEN` | Railway workspace or account token | `.github/workflows/platform-bootstrap.yml` | One-time/exceptional creation of `security`, `di-api`, `platform-smoke`, and PostgreSQL. |
| `RAILWAY_TOKEN` | Project token for `cf00a1cd-623e-4c72-8836-db95652c63db` | `.github/workflows/platform-deploy.yml` | Normal variable management, migrations/deployments, logs and redeploy operations inside the already-created project environment. |

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
   |-- JWKS ------------> http://security.railway.internal:8001/.well-known/jwks.json
   `-- PostgreSQL

platform-smoke
   `-- temporary deployment used to prove private routing/auth; the service object remains available for future smoke runs
```

Canonical private endpoints:

- Security: `http://security.railway.internal:8001`
- Token: `http://security.railway.internal:8001/oauth/token`
- JWKS: `http://security.railway.internal:8001/.well-known/jwks.json`
- DI: `http://di-api.railway.internal:8000`
- DI liveness: `http://di-api.railway.internal:8000/health`
- DI readiness: `http://di-api.railway.internal:8000/ready`

## 5. Bootstrap procedure

Bootstrap is required only when the consolidated services/database do not exist.

1. Add `RAILWAY_API_TOKEN` to GitHub Actions secrets in `verigence/verigence-audit-core`. Use a Railway workspace or account token authorized to create resources in the target project.
2. Run **Bootstrap unified Railway platform DEV** (`.github/workflows/platform-bootstrap.yml`).
3. The Action links explicitly to project `cf00a1cd-623e-4c72-8836-db95652c63db` and environment `398c3cfb-d7c7-4aaf-b5a4-3b44d3087451`.
4. It idempotently ensures these resource objects exist:
   - `verigence-audit-core` (already present);
   - `security`;
   - `di-api`;
   - `platform-smoke`;
   - one PostgreSQL service.
5. Accept bootstrap only when the log contains `PLATFORM_BOOTSTRAP=PASS`.

The bootstrap workflow creates empty application services. It does not connect them to Railway GitHub auto-deploy sources.

## 6. Normal deployment procedure

Normal deployment uses only the project-scoped `RAILWAY_TOKEN`.

1. Run **Deploy unified Railway platform DEV** (`.github/workflows/platform-deploy.yml`).
2. Security and DI default to their `main` branches; explicit SHAs/refs may be supplied for controlled testing or rollback.
3. The workflow records the Audit Core, Security and DI Git SHAs.
4. It verifies the target Railway project and required pre-created services.
5. It retrieves the existing Audit Core `SECURITY_CLIENT_SECRET` without printing it and derives only its SHA-256 verifier for Security.
6. It creates a stable Security RSA signing key only if the consolidated Security service does not already have one.
7. It configures Security with issuer `verigence-security`, audience `verigence-platform`, the shared DEV PostgreSQL service and least-privilege `audit-core` scopes:
   - `di.document.read`
   - `di.document.upload`
8. It applies Security schema SQL and ensures tenant `dev-auth-test` exists.
9. It configures DI with the shared PostgreSQL service and the private Security JWKS URL. The API worker is disabled in the API service and Document AI stays in DEV mock mode until a real provider is deliberately configured.
10. It runs DI Alembic migrations.
11. It rewires Audit Core to private Security/DI URLs.
12. It deploys Security, DI and Audit Core explicitly with `railway up` from the three checked-out repositories.
13. It deploys a smoke image to the pre-created `platform-smoke` service to prove actual private networking and authentication.
14. It removes the smoke deployment after verification while retaining the service object for the next run.

## 7. Expected acceptance markers

A deployment is accepted only when the GitHub Actions log contains:

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

## 8. Secret handling

No Security client secret, JWT private key, DI secret key, or database password is committed to GitHub.

The workflow writes secret values to Railway through stdin, masks temporary values immediately in GitHub Actions, and never prints raw variable JSON containing secret values. The raw Audit Core client secret remains stored in the Audit Core Railway service; Security stores only its SHA-256 verifier.

## 9. Database model

Audit Core keeps its existing `DATABASE_URL` and database boundary.

The consolidated DEV Security and DI services share one Railway-managed PostgreSQL instance for infrastructure efficiency while retaining their existing application schema/table boundaries. GitHub Actions uses the database public migration endpoint only for migrations and masks it before use; runtime services use Railway private/reference variables.

Destructive automatic down-migrations are not part of rollback.

## 10. What the private smoke proves

GitHub-hosted runners cannot directly resolve Railway private DNS, so a deployment inside the pre-created `platform-smoke` service performs verification from the Railway private network.

The smoke proves:

1. Security private liveness;
2. DI private liveness and DB readiness;
3. the real `audit-core` confidential client can obtain a Security SERVICE token for `dev-auth-test`;
4. DI retrieves Security JWKS through the private network;
5. DI accepts the Security-issued token on a tenant-scoped API request.

This is a backend SERVICE-path verification. Interactive USER OAuth/Clerk migration is intentionally separate because legacy Security secret values are outside this project token's scope. Do not claim positive end-to-end USER delegation until Clerk/user state is provisioned into the consolidated Security service and tested.

## 11. Rollback

During stabilization, keep the legacy standalone Security and DI Railway projects intact.

Rollback must also be executed through GitHub Actions. Re-run a known-good platform workflow revision or a controlled rollback revision that restores previously recorded dependency endpoints and redeploys Audit Core. Do not patch Railway variables manually.

Database rollback is forward-compatible only; do not automatically execute destructive down-migrations.

## 12. Legacy deployment retirement

Only after the consolidated run is green:

- retire Railway mutation workflows in `verigence/verigence-security` that deploy to the old Security project;
- retire `.github/workflows/deploy-dev.yml` in `verigence/verigence-di` as a direct Railway deployer;
- remove temporary inspection workflows;
- retain CI/test workflows in each source repository;
- point each repository's deployment documentation to this runbook.

## 13. DEV limitations before production

This setup is DEV only. Production requires a separate Railway project/environment, production tokens, production databases/storage, real Document AI configuration, Clerk/interactive auth provisioning, approval gates, and production secret management. DEV-generated credentials and mock Document AI settings must not be promoted.

## 14. Evidence to retain

For each accepted deployment retain:

- bootstrap run ID when resources are created/recreated;
- platform deployment run ID;
- Audit Core, Security and DI commit SHAs;
- Railway project/environment IDs and service IDs;
- migration completion evidence without secret values;
- all acceptance markers;
- any rollback or exception record.
