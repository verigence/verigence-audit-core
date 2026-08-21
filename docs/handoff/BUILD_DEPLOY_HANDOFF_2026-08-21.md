# Verigence Audit Core — Build & Deploy Handoff
**Document ID:** VAC-HANDOFF-CICD-001
**Date:** 2026-08-21
**Prepared by:** Bob (AI engineer)
**Status:** CURRENT — reflects exact repo state at time of writing
**Repo:** `verigence/verigence-audit-core`
**Branches covered:** `main` (HEAD `fdb3270`) · `dev` (PR #10, 43 commits ahead)

---

## 1. Where we are right now

### 1.1 Branch state

| Branch | HEAD SHA | Migrations | CI | Deploy |
|---|---|---|---|---|
| `main` | `fdb3270` | `0002_runtime_role_rls` | ❌ Failing (E501 — triggered by PR sync) | ❌ Failing (missing DB secret) |
| `dev` | `6603e1e` | `0005_uc02_admin_row_delete` | ❌ Failing (E501 lint) | ❌ Failing (missing DB secret) |

**`main` code is stable.** The CI failure shown against `main` was caused by a PR#10 sync push, which ran CI against the `dev` branch code — not a regression in `main` itself. No new code has been pushed to `main` since the last lint fix (`fdb3270`, 2026-08-20).

### 1.2 Open PR

| PR | Title | Head | Base | Status |
|---|---|---|---|---|
| [#10](https://github.com/verigence/verigence-audit-core/pull/10) | UC02 implementation verification | `dev` | `main` | 🔴 **DO NOT MERGE** — explicitly held until Security/Web UC02 packages verified |

---

## 2. CI failures — exact root causes

### 2.1 Lint (E501 — line too long > 88 chars)

`ruff` is configured in `pyproject.toml` with only `target-version = "py312"` and no `[tool.ruff.lint]` section; the default line length is **88 characters**. The `dev` branch has new UC02 implementation code with long lines that were not wrapped.

**Files and line numbers to fix:**

#### `src/audit_core/dealers.py` — 16 violations
```
L194  def _dealer_impact(connection: Connection, tenant_id: str, dealer_id: UUID) -> dict[str, int]:
L202  WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id) AS business_assignments,
L204  WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id) AS discount_eligibility,
L206  WHERE tenant_id = :tenant_id AND dealer_id = :dealer_id) AS workflow_tasks
L248  WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id) AS daily_ops_runs,
L250  WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id) AS activity_records,
L252  WHERE tenant_id = :tenant_id AND outlet_id = :outlet_id) AS pc_daily_notes
L278  @router.post("/dealers", response_model=DealerResponse, status_code=status.HTTP_201_CREATED)
L298  RETURNING dealer_id, dealer_code, dealer_name, legal_name, status, version_no
L412  RETURNING dealer_id, dealer_code, dealer_name, legal_name, status, version_no
L467  "Use the dependency impact to remove safe dependencies or use whole-Project "
L526  outlet_classification, address_text, city, state_region, postal_code,
L641  "monthlyVehicleVolume": ("monthly_vehicle_volume", payload.monthlyVehicleVolume),
L673  outlet_classification, address_text, city, state_region, postal_code,
L734  "Dealer Outlet has dependent Project data and cannot be deleted directly. "
L735  "Use the dependency impact to remove safe dependencies or use whole-Project "
```

#### `src/audit_core/dependencies.py` — 2 violations
```
L108  raise SecurityTokenError("Security administrative context is unavailable") from exc
L110  raise SecurityTokenError("Security administrative USER does not match authenticated USER")
```

#### `src/audit_core/projects.py` — 4 violations
```
L249  "OEM, Product Category and Effective Start Date cannot be changed after "
L260  patch.effectiveEndDate if "effectiveEndDate" in supplied else current["effective_end_date"]
L301  RETURNING tenant_id, project_code, project_name, oem_id, product_category_id,
L302  effective_start_date, effective_end_date, timezone_name, region_code,
```

#### `src/audit_core/security.py` — 1 violation
```
L82   raise SecurityTokenError("Security human token carries unsupported authority claims")
```

#### `src/audit_core/errors.py` — 5 violations
```
L79   async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
L89   async def authentication_error(request: Request, exc: SecurityTokenError) -> JSONResponse:
L99   async def authorization_error(request: Request, exc: AuthorizationError) -> JSONResponse:
L120  # Exception messages can contain bearer tokens, passwords, document values or other
L121  # sensitive input. Log only the safe exception classification and correlation context.
```

**Total: 28 lines to wrap across 5 files.**

**Fix command (run locally on `dev` checkout):**
```bash
pip install 'ruff==0.16.0'
ruff format src tests migrations
ruff check --fix src tests migrations
git add -p
git commit -m "fix: wrap E501 long lines for UC02 implementation (ruff)"
git push origin dev
```

Alternatively, increase the line length limit by adding this to `pyproject.toml` (discuss with team first):
```toml
[tool.ruff.lint]
# extend-ignore = ["E501"]   # or:

[tool.ruff.format]
line-length = 100
```

---

## 3. Deploy failures — exact root causes

### 3.1 Missing database secret

The `audit-core-dev-deploy.yml` workflow requires **one of**:
- `MIGRATION_DATABASE_URL` — preferred (psycopg3 URL)
- `DEV_DATABASE_URL` — fallback

Both are **not set** as GitHub Actions secrets in the repo. The deploy step exits immediately with:
```
::error::Neither MIGRATION_DATABASE_URL nor DEV_DATABASE_URL is configured.
```

**Fix:** Go to **GitHub repo Settings → Secrets and variables → Actions** and add:
```
MIGRATION_DATABASE_URL = postgresql+psycopg://<user>:<pass>@<neon-host>/<db>?sslmode=require
```
The URL must be the Neon DEV branch connection string for the `auditcore` schema.

### 3.2 Migrations pending on Neon DEV

Once the secret is fixed and `dev` is merged to `main`, the deploy job will run Alembic `upgrade head` which will apply:

| Migration | What it does |
|---|---|
| `0003_uc02_project_configuring_status` | Adds `CONFIGURING` to `projects.project_status` CHECK constraint |
| `0004_uc02_outlet_google_place` | `ALTER TABLE dealer_outlets ADD COLUMN google_place_id text` |
| `0005_uc02_admin_row_delete` | `GRANT DELETE ON dealers, dealer_outlets TO audit_core_runtime` |

Current Neon DEV state: **`0002_runtime_role_rls`** (confirmed by CI assertion in `ci.yml` on `main`).
Target state after PR#10 merge: **`0005_uc02_admin_row_delete`** (as asserted in the updated CI check in `ci.yml` on `dev`, which checks for `0004_uc02_outlet_google_place` and the new `google_place_id` column).

---

## 4. Deployment stack reference

| Component | Value |
|---|---|
| Platform | Railway |
| Builder | Railpack |
| Project ID | `cf00a1cd-623e-4c72-8836-db95652c63db` |
| Service name | `verigence-audit-core` |
| Start command | `PYTHONPATH=src uvicorn audit_core.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/health` |
| Restart policy | `ON_FAILURE` |
| Python | 3.12 |
| DB engine | PostgreSQL (Neon) via `psycopg[binary]==3.3.4` |
| Alembic schema | `auditcore` |
| Required GitHub secrets | `RAILWAY_TOKEN`, `MIGRATION_DATABASE_URL` (or `DEV_DATABASE_URL`) |

**CI workflow:** `.github/workflows/ci.yml` — triggers on `push` to `main` and all PRs.
**Deploy workflow:** `.github/workflows/audit-core-dev-deploy.yml` — triggers on `push` to `main` and `workflow_dispatch`.
**Deprecated:** `.github/workflows/platform-deploy.yml` — intentionally blocked, do not use.

---

## 5. What needs to happen before the next green build

In sequence:

```
1. Fix E501 lint violations on dev (28 lines, 5 files)
   → git commit + push to dev
   → CI re-runs against dev, lint passes
   → migration/unit-test steps run

2. Set MIGRATION_DATABASE_URL secret in GitHub repo settings
   → Deploy workflow can run migrations on Neon DEV

3. Verify all 3 UC02 modules are ready (Security + Audit Core + Web)
   → PR #10 approved for merge

4. Merge PR #10 dev → main
   → CI runs on main (all steps green)
   → Deploy fires automatically
   → Alembic applies 0003 + 0004 + 0005 to Neon DEV
   → Railway deploys new build
   → Smoke test hits /health → PASS
```

---

## 6. Source modules introduced by UC02 (`dev` branch, not yet on `main`)

| Module | File | What it does |
|---|---|---|
| Security integration client | `src/audit_core/security_integration.py` | `SecurityAdminClient` — forwards human JWT to Security admin-context endpoint |
| Updated dependencies | `src/audit_core/dependencies.py` | Adds `HumanAdminRequest`, `require_super_admin_request` dependency |
| Updated dealers | `src/audit_core/dealers.py` | Outlet geo fields (`googlePlaceId`, lat/lon), dealer/outlet hard-delete + impact endpoints |
| Updated projects | `src/audit_core/projects.py` | Full UC02 project PATCH, readiness/activation scaffolding |
| Updated security | `src/audit_core/security.py` | Human token validation with unsupported-claim guard |
| Updated errors | `src/audit_core/errors.py` | Additional UC02 error handler registrations |
| New test | `tests/test_uc02_admin_security.py` | UC02 SuperAdmin auth/admin-context boundary tests |

---

## 7. Design documents on `dev` (not yet on `main`)

All under `docs/`:

| File | Description |
|---|---|
| `AUDIT_CORE_API_CONTRACT_v1.1.md` | UC02 API contract revision (703 lines) |
| `AUDIT_CORE_CROSS_MODULE_AUTH_DESIGN_v1.1.md` | Human JWT pass-through vs ServiceIntegration rules |
| `AUDIT_CORE_PHYSICAL_DATA_MODEL_v2.2.md` | Full DDL and data model for UC02 |
| `AUDIT_CORE_SOLUTION_DESIGN_v2.2.md` | Solution design v2.2 |
| `AUDIT_CORE_UC02_ADMIN_ALIGNMENT.md` | Admin alignment notes |
| `AUDIT_CORE_UC02_MASTER_RESOLUTION_ALIGNMENT.md` | Master resolution notes |
| `AUDIT_CORE_UC02_PRODUCT_MASTER_PHASE1_ALIGNMENT.md` | Product Master Phase 1 notes |
| `handoff/UC02_CROSS_MODULE_DESIGN_HANDOFF_2026-08-21.md` | UC02 cross-module design handoff |
| `handoff/BUILD_DEPLOY_HANDOFF_2026-08-21.md` | **This document** |

---

## 8. Quick-reference links

| Resource | URL |
|---|---|
| Repo | https://github.com/verigence/verigence-audit-core |
| PR #10 | https://github.com/verigence/verigence-audit-core/pull/10 |
| CI workflow | https://github.com/verigence/verigence-audit-core/actions/workflows/ci.yml |
| Deploy workflow | https://github.com/verigence/verigence-audit-core/actions/workflows/audit-core-dev-deploy.yml |
| Last CI run (failure) | https://github.com/verigence/verigence-audit-core/actions/runs/32464390977 |
| Last Deploy run (failure) | https://github.com/verigence/verigence-audit-core/actions/runs/32340096312 |
| `main` HEAD commit | https://github.com/verigence/verigence-audit-core/commit/fdb3270429cfd507cbd0b598c38e155b35a7ebf7 |
| `dev` HEAD commit | https://github.com/verigence/verigence-audit-core/commit/6603e1e1d6070b53043d30ba6ceb2dc9904bf2b4 |
