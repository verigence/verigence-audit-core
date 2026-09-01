# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — lossless Booking + Delivery reviewed-DI persistence completed and CI-verified**  
Repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-persist-all-di-fields-booking-delivery-v2`  
Base `dev` SHA: `724ddbc4ed11ec82195763d9b10a6f9f339bb729`  
Latest application/test SHA before this checkpoint-only commit: `10c29da3a372ec4dee9369b4e9bc3e705bff456c`  
PR: **#135 — UC03: persist all reviewed DI fields for Booking and Delivery**  
Merge status: **MERGE TO `dev` EXPLICITLY APPROVED BY USER on 2026-09-01; merge only after final PR CI remains green.**

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → `UC03_IMPLEMENTATION_CONTEXT.md` → continue from `NEXT ACTION`.

Do not reconstruct completed work from chat history or broadly rescan repositories.

## Locked business decisions

- One Journey; Booking and Delivery are stages of that Journey.
- Persist Booking document-derived facts at Booking Review and Delivery document-derived facts at Delivery Review.
- **Every non-null/non-empty DI field participating in Review must have durable Audit Core representation after Review, even without a typed owner.**
- Unchanged reviewed value: effective value = DI extracted value.
- Changed reviewed value: preserve original DI value/provenance and persist confirmed effective value.
- Rejected Booking value: preserve original DI provenance; do not create an accepted effective/typed business mutation.
- Existing typed Audit Core business owners remain the operational projection layer where explicitly supported.
- Unknown/new fields survive generically; do not create speculative typed columns.
- Repeated documents and multiple same-type documents remain distinct by DI document/fact identity.
- Exact canonical aliases must come from authoritative contracts/catalogues; never invent aliases.

## Completed implementation

### Unit 1 — common lossless persistence foundation

**Status: IMPLEMENTED + CI/DB VERIFIED**

- Migration `0051_uc03_lossless_review_fields.py` extends existing `auditcore.journey_document_extracted_fields`; no parallel generic field table.
- Adds Booking/Delivery stage provenance, V2 canonical-field identity, source document type, reviewed effective value, confidence scale, modification flag, reviewed actor/time.
- Legacy `evidence_id` / `source_fact_ref` become nullable only where V2 truthfully lacks them.
- V2 identity is Journey + stage + DI document + canonical field + fact version.
- Existing V1 identity/upsert remains supported.
- `uc03_di_core_persistence.py` provides shared `ReviewedDiField` and `persist_reviewed_di_fields(...)`.
- Valid `0` / `False` values survive; empty/null unchanged fields are skipped; confidence values are not guessed or rescaled.

### Unit 2 — legacy/direct Booking Review

**Status: IMPLEMENTED + CI VERIFIED**

- `uc03_pc_generic_review.py` now persists every populated reviewed field generically before best-effort typed projection.
- Unchanged values persist original = effective.
- Corrections persist original + modified/effective value.
- `storedFieldCount` represents all persisted reviewed fields; correction count remains separate.
- Only human-modified fields continue to emit correction events.

### Unit 3 — Booking V2 Review Confirm

**Status: IMPLEMENTED + CI VERIFIED**

- Booking V2 confirm persists every current populated DI field generically with `stage_code='BOOKING'` before typed materialization.
- Existing decision keys determine accepted/rejected effective-value semantics; no new review semantics were invented.
- Accepted unknown/unmapped fields no longer fail solely because no typed Core owner exists.
- Supported fields may retain reference-only resolution with null owning domain/reference when no approved typed owner exists.
- Rejected mapped/raw fields keep original DI provenance but no accepted effective value/typed projection.
- Existing stale-decision, pending/failed extraction, If-Match, idempotency, aggregate locking and typed projections remain.

### Unit 4 — Delivery V2 Review Confirm

**Status: IMPLEMENTED + CI VERIFIED**

- Added explicit `POST /v2/tenants/{tenant_id}/journeys/{journey_id}/delivery/review/confirm`.
- Existing `/audit/source-comparison` remains GET/read-only; no state mutation was added to GET.
- Confirm requires Delivery submitted, `pc_verification_status='PENDING'`, no pending/failed Delivery extraction, If-Match, idempotency, authorization and aggregate locking.
- Loads Delivery-stage DI documents only and persists every populated field through the shared helper with `stage_code='DELIVERY'`.
- No typed-owner requirement or invented canonical aliases.
- Delivery PC verification advances to `VERIFIED` only after field persistence succeeds.
- Workflow event contains safe counts/metadata, not raw field values.
- Added focused route/order/state tests in `tests/test_uc03_delivery_review_confirm.py`.

## CI evidence

PR #135 CI run **#1140**, head `10c29da3a372ec4dee9369b4e9bc3e705bff456c`, completed successfully:

- package build: **PASS**;
- `ruff check src tests migrations`: **PASS**;
- fresh PostgreSQL `alembic upgrade head`: **PASS**, including `0051_uc03_lossless_review_fields`; exactly one Alembic head;
- pytest: **366 passed, 1 unrelated Starlette/httpx deprecation warning, 18.04s**.

The immediately preceding CI run #1139 failed only on Ruff import formatting in the newly added Delivery module; migration and tests were skipped in that run. The import formatting was corrected and run #1140 passed fully. Do not hide or reinterpret that history.

## Scope boundaries retained

This completed persistence unit did **not** implement:

- DI or Web changes;
- Security redesign/change;
- rule-engine internals;
- final-report generation;
- speculative Invoice/domain redesign;
- invented canonical aliases;
- persisted final post-Delivery source-of-truth snapshot.

## Remaining Step-1 items

Still OPEN / UNKNOWN where stated:

1. persisted final source-of-truth/resolution model after Booking-vs-Delivery comparison;
2. exact final-report workbook field contract and Audit Core owner mapping;
3. whether a typed repeated Invoice collection is genuinely required — **UNKNOWN pending workbook/business-owner mapping**;
4. complete canonical document alias/family implementation across current modules;
5. minimal rule-run status/reference placeholder before final report generation.

Do not silently close these because reviewed-field persistence is complete.

## NEXT ACTION

1. Allow the checkpoint-only commit to complete PR CI.
2. If green, merge PR #135 to `dev` as explicitly approved by the user.
3. Verify the post-merge `dev` CI result. Repository CI automatically deploys a tested `dev` push to Railway DEV; record that outcome rather than manually changing deployment behavior.
4. After `dev` is healthy, begin the next Audit Core stabilization unit with **post-Delivery final source-of-truth persistence**: define the smallest durable resolution/snapshot contract that consumes Booking + Delivery reviewed values and does not re-read volatile DI as the final business state.
5. In parallel with that design, verify the supplied final-report workbook field/owner mapping before deciding whether any typed repeated Invoice entity is actually necessary.
6. Only after Audit Core Step-1 gaps are closed should Step 2 revalidate DI contracts and then Web integration.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.