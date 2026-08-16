# Verigence Platform DEV — Railway Deployment Runbook

**Owner:** Platform / Audit Core  
**Target Railway project:** `verigence-audit-core` (`cf00a1cd-623e-4c72-8836-db95652c63db`)  
**Deployment control plane:** GitHub Actions only  
**Primary workflow:** `.github/workflows/platform-deploy.yml`

## 1. Purpose

This runbook defines the controlled DEV deployment of the three backend modules into one Railway project so Railway private networking can be used between them:

- `verigence-audit-core` — existing Audit Core service;
- `security` — Verigence Security service sourced from `verigence/verigence-security`;
- `di-api` — Document Intelligence API sourced from `verigence/verigence-di`;
- one Railway-managed PostgreSQL service used by the consolidated DEV Security and DI services.

The existing standalone Security and DI Railway projects are deliberately not deleted during initial cutover. They remain rollback references until the consolidated platform is proven stable.

## 2. Operating rule — GitHub Actions only

All Railway mutations for this consolidated DEV platform must be executed by GitHub Actions in `verigence/verigence-audit-core`.

Do not use the Railway UI or a local Railway CLI to create services, set variables, deploy, redeploy, migrate databases, or change the Audit Core dependency URLs during normal operation. Emergency manual changes must be documented and reconciled back into the workflow before the next deployment.

The Railway services are intentionally deployed by `railway up` from GitHub Actions rather than relying on Railway repository auto-deploys. This keeps deployment orchestration and evidence in one auditable place.

## 3. Required GitHub secret

Repository: `verigence/verigence-audit-core`

| Secret | Purpose |
|---|---|
| `RAILWAY_TOKEN` | Project-scoped Railway token for `cf00a1cd-623e-4c72-8836-db95652c63db`. |

No Security client secret, JWT private key, DI secret key, or database password is committed to GitHub. Runtime secrets are generated or read inside the workflow and written directly to Railway using stdin. Secret values must never be printed.

## 4. Target topology

```text
Railway project: verigence-audit-core
Current environment

verigence-audit-core
   |-- Security OAuth/JWKS --> http://security.railway.internal:8001
   `-- Document Intelligence -> http://di-api.railway.internal:8000

security
   `-- PostgreSQL (security schema/tables)

di-api
   |-- JWKS --> http://security.railway.internal:8001/.well-known/jwks.json
   `-- PostgreSQL (docintel schema/tables)
```

Canonical private endpoints:

- Security base: `http://security.railway.internal:8001`
- Security token endpoint: `http://security.railway.internal:8001/oauth/token`
- Security JWKS: `http://security.railway.internal:8001/.well-known/jwks.json`
- DI base: `http://di-api.railway.internal:8000`
- DI liveness: `http://di-api.railway.internal:8000/health`
- DI readiness: `http://di-api.railway.internal:8000/ready`

## 5. Source repositories

The central workflow checks out the following immutable source inputs for each run:

| Service | Repository | Default ref |
|---|---|---|
| Audit Core | `verigence/verigence-audit-core` | workflow ref / `main` after merge |
| Security | `verigence/verigence-security` | `main` |
| DI API | `verigence/verigence-di` | `main` |

A deployment record must retain the Git SHAs used for all three repositories.

## 6. What the workflow does

The workflow is idempotent. On every deployment it:

1. verifies that `RAILWAY_TOKEN` resolves to Railway project `cf00a1cd-623e-4c72-8836-db95652c63db`;
2. ensures `security` and `di-api` services exist without enabling Railway GitHub auto-deploy;
3. ensures one Railway PostgreSQL database exists for consolidated DEV Security and DI;
4. retrieves the existing Audit Core `SECURITY_CLIENT_SECRET` inside the Action without printing it and derives only its SHA-256 verifier for Security;
5. generates a Security RSA private key only when the consolidated `security` service does not already have one;
6. configures the consolidated Security service with issuer `verigence-security`, audience `verigence-platform`, database connectivity, and least-privilege `audit-core` permissions (`di.document.read`, `di.document.upload`);
7. applies Security database SQL and seeds the retained DEV tenant `dev-auth-test`;
8. generates a stable DI DEV secret key only when absent, configures DI to use the shared PostgreSQL service and Security's private JWKS endpoint, and keeps Document AI in DEV mock mode until a real provider is deliberately configured;
9. applies DI Alembic migrations;
10. deploys Security from `verigence/verigence-security` and DI from `verigence/verigence-di` using `railway up`;
11. rewires the existing Audit Core service to the private Security and DI endpoints and redeploys Audit Core;
12. creates a temporary `platform-smoke` service inside the same Railway project, obtains an `audit-core` SERVICE token from Security, validates DI `/health` and `/ready`, and calls DI with the Security-issued token;
13. records PASS markers in the GitHub Actions log and deletes the temporary smoke service.

## 7. Deployment procedure

### Normal deployment

1. Open GitHub Actions in `verigence/verigence-audit-core`.
2. Select **Deploy unified Railway platform DEV**.
3. Run the workflow from `main`.
4. Keep the default Security and DI refs unless a controlled rollback or candidate test requires a specific commit SHA.
5. Do not run old Security or DI Railway deployment workflows; platform deployment is centralized here.

### Expected verification markers

A successful run must contain all of the following:

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

A missing marker means the deployment is not accepted even if an individual Railway deployment shows `SUCCESS`.

## 8. Security model

The raw Audit Core OAuth client secret remains stored in the Audit Core Railway service. The central workflow reads it only within the ephemeral GitHub runner, masks it immediately, and registers only its SHA-256 verifier in Security.

The consolidated Security service is initially bootstrapped for backend SERVICE-token operation. The workflow does not copy Clerk credentials from the legacy Security Railway project because those secret values are intentionally outside this project's token scope. Interactive USER OAuth/Clerk cutover must be performed separately through an approved secret-provisioning Action if/when the consolidated Security service becomes the interactive login endpoint.

The `audit-core` confidential client is restricted to:

- `di.document.read`
- `di.document.upload`

## 9. Database model

Audit Core continues using its existing `DATABASE_URL` and database boundary.

Security and DI share one managed PostgreSQL instance for DEV infrastructure efficiency but keep application objects isolated by their existing schema/table conventions. Migrations are executed from GitHub Actions against the database public migration endpoint; runtime services use the Railway private database reference.

Never print database URLs in Actions logs. The workflow masks the temporary migration URL before use.

## 10. Private-network verification

GitHub-hosted runners cannot directly resolve Railway private DNS. Therefore the workflow deploys a temporary `platform-smoke` service inside the project to verify actual private routing.

The smoke proves:

1. `security.railway.internal:8001` is reachable;
2. `di-api.railway.internal:8000` is reachable and database-ready;
3. the real `audit-core` confidential client can obtain a SERVICE token;
4. DI can retrieve Security JWKS through the private network;
5. DI accepts a correctly scoped Security-issued token for tenant `dev-auth-test`.

The temporary smoke service is deleted after verification.

## 11. Rollback

During the stabilization period, do not delete the legacy standalone Security or DI Railway projects.

If the consolidated deployment fails before Audit Core is rebound, no Audit Core rollback is required.

If Audit Core has already been rebound and the consolidated Security/DI path subsequently fails, rollback must also be performed by GitHub Actions. Re-run a known-good platform workflow or use a dedicated rollback revision of the same workflow that restores the previously recorded Security/DI endpoints and redeploys Audit Core. Do not patch variables manually in the Railway UI.

Database migrations must be treated as forward-compatible. Do not automatically run destructive down-migrations during rollback.

## 12. Legacy workflow retirement

After the first successful consolidated deployment:

- Security Railway mutation workflows in `verigence/verigence-security` must be disabled or converted to informational/manual guard workflows;
- DI `.github/workflows/deploy-dev.yml` must no longer deploy directly to its legacy Railway project;
- the temporary DI Railway inspection workflow must be removed;
- CI workflows remain in their source repositories and continue to validate code before central deployment.

## 13. Promotion and production

This runbook is DEV-only. Production must use a separate Railway project/environment token, explicit production databases/storage/Document AI configuration, a production secret-management policy, and approval gates. Do not promote DEV mock Document AI configuration or DEV-generated credentials into production.

## 14. Evidence to retain

For every accepted deployment retain:

- central GitHub Actions run ID;
- Audit Core, Security, and DI commit SHAs;
- Railway project ID;
- Railway service IDs;
- migration completion output without secret values;
- all PASS markers from section 7;
- any rollback or exception record.
