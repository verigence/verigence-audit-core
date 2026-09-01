# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core lossless reviewed-DI persistence implementation**  
Mode: **WRITE APPROVED only for `UC03_IMPLEMENTATION_CONTEXT.md`; NO MERGE / NO DEPLOY**  
Repository: **`verigence-audit-core` only**  
Branch: `fix/uc03-persist-all-di-fields-booking-delivery-v2`  
Base `dev` SHA: `724ddbc4ed11ec82195763d9b10a6f9f339bb729`  
Latest application/test SHA: `8adba8b0a7ace52fad70a8b4493b3f11b7cd538a`

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → `UC03_IMPLEMENTATION_CONTEXT.md` → continue from `NEXT ACTION`.

Do not reconstruct from chat history or broadly rescan completed work.

## Locked business state

**BUSINESS DECISION**

- One Journey; Booking and Delivery are stages of that Journey.
- Persist Booking document-derived facts at Booking Review and Delivery facts at Delivery Review.
- **Every non-null/non-empty DI field participating in Review must have durable Audit Core representation after Review, even without a typed owner.**
- Unchanged: effective value = DI extracted value.
- Changed: preserve original DI value/provenance and persist confirmed effective value.
- Rejected: preserve original DI provenance; do not typed-project an effective business value.
- Existing typed Customer/Booking/Vehicle/Commercial/Payment/etc. owners remain the operational projection layer where explicitly supported.
- Unknown/new fields survive generically; do not create speculative typed columns.
- Repeated documents and multiple same-type documents remain distinct by document/fact identity.
- Payments are already repeated Journey entities; payment stage/linkage is already implemented by migration `0041`.
- Exact canonical aliases must come from authoritative catalogue/contracts; never invent them. Alias uncertainty must not cause field loss.
- No DI/Web/Security work in the current unit.
- No merge/deploy approval.

## Verified baseline facts retained

- Original stabilization branch was 9 commits behind updated `dev`; this branch was re-created from current `dev` before application writes.
- `journey_document_extracted_fields` already existed but baseline semantics were correction-oriented and not V2-compatible as-is.
- V2 DI exposes document/canonical-field/fact-version identity but not legacy `source_fact_ref`; V2 evidence ID can be absent; V2 confidence is 0–100 while legacy confidence was 0–1.
- `0048` + `uc03_v2_review_materialization.py` provide typed persistence for known Booking Form/PAN/Aadhaar/Dealer Receipt fields only; they are additional typed projection, not the lossless generic layer.
- `record_attribute_resolution(...)` allows null owning-domain/reference, so reference-only resolution does not require a fake typed owner.
- Delivery capture baseline sets Delivery `pc_verification_status='PENDING'` after submission.
- Baseline post-Delivery `/audit/source-comparison` is a GET/live DI comparison and has no durable Delivery Review commit.
- Repeated Payments/Receipts are already correctly 1:N. Insurance/Trade-In/Finance/Commercial cardinalities had no verified repeated-entity defect.
- A separate typed repeated Invoice business entity remains **UNKNOWN pending final-report/business-owner mapping**; do not invent it in this unit.
- Canonical alias mechanism remains a later verified gap; do not invent alias values here.
- Persisted final-source snapshot, rule-run placeholder and exact final-report workbook mapping remain open after this unit.

## Implementation progress

### Unit 1 — generic persistence foundation

**Status: IMPLEMENTED / SOURCE-VERIFIED; CI/DB UNVERIFIED**  
SHA: `f1b0fd57490c1e6fcc92468cb9d853d001967392`

**CHANGED:**
- Added migration `0051_uc03_lossless_review_fields.py` extending existing `journey_document_extracted_fields`; no second generic table.
- Added V2 stage/document/canonical identity, effective value, confidence scale, modification flag and review actor/time.
- Relaxed only legacy `evidence_id` / `source_fact_ref` NOT NULL requirements needed for truthful V2 identity.
- Added V2 unique identity on Journey + stage + DI document + canonical field + fact version.
- Extended `uc03_di_core_persistence.py` with shared `ReviewedDiField` / `persist_reviewed_di_fields(...)`.
- Added focused persistence contract tests.

**VERIFIED:** source inspection confirms zero/false are retained, unchanged empty/null are skipped, V1/V2 use truthful separate conflict keys, no confidence rescaling is guessed, and existing actor-context installer remains compatible.

**NOT VERIFIED:** pytest execution, migration execution, DB compatibility, CI. No branch workflow/status checks exist for these direct pushes.

### Unit 2 — legacy/direct Booking Review

**Status: IMPLEMENTED / SOURCE-VERIFIED; CI/DB UNVERIFIED**  
SHA: `9d295e8eb7155f15673944b94df7065bc15e7581`

**CHANGED:**
- `uc03_pc_generic_review.py` now sends every field through lossless generic persistence before typed projection.
- Unchanged populated values persist original = effective.
- Corrections persist original + modified/effective value.
- `storedFieldCount` now means all persisted reviewed fields; correction count remains separate.
- Typed projection remains best-effort after generic persistence.
- Updated tests; only modified fields still emit correction events.

**VERIFIED:** source ordering and field construction; no indexed repo contract surfaced that requires old `rawDiValuesCopied=False`/correction-only count semantics.

### Unit 3 — Booking V2 Review Confirm

**Status: IMPLEMENTED / SOURCE-VERIFIED; CI/DB UNVERIFIED**  
SHA: `8adba8b0a7ace52fad70a8b4493b3f11b7cd538a`

**CHANGED:**
- Booking V2 confirm now persists every current DI document field generically with `BOOKING` stage before typed materialization.
- Existing attribute/raw decision keys determine effective-value acceptance without inventing new decision semantics.
- Rejected mapped/raw fields keep original extracted value/provenance but no accepted effective value.
- Accepted unknown/unmapped fields no longer require a typed Core owner.
- Supported attributes with no typed owner may retain reference-only `journey_attribute_resolutions` provenance with null owning-domain/reference.
- Existing typed owners/materializer remain active for known fields.
- Workflow metadata records persisted field count and `rawDiValuesCopied=True` without raw values.
- Updated former typed-owner-blocking tests to assert generic persistence instead.

**VERIFIED:**
- pending/failed extraction gate remains before confirmation;
- current deterministic source-set decisions/stale-decision behavior remains unchanged;
- If-Match/idempotency/aggregate lock/status checks remain;
- unknown accepted values no longer hit the removed raw-owner assertion;
- typed-owner lookup is optional when operational application returns no owner;
- rejected fields are excluded from effective/typed projection while original evidence survives generically.

**NOT VERIFIED:** pytest/DB/CI execution.

## Remaining approved implementation work

1. Explicit Delivery Review commit that persists every populated Delivery DI field with `DELIVERY` provenance and advances Delivery PC verification only after successful persistence.
2. Focused Delivery API/tests/OpenAPI contract.
3. Focused regression + migration/DB execution when an executable path is available.
4. Update checkpoint with actual test evidence.
5. Stop before merge/deploy and before DI/Web.

## Open Step-1 items outside this implementation unit

Remain **OPEN / UNKNOWN where stated**:
- exact final-report workbook field contract and owner mapping;
- typed repeated Invoice business entity necessity;
- complete canonical document alias implementation;
- persisted final-source-of-truth snapshot after Delivery;
- minimal rule-run status/reference placeholder.

Do not silently close these from the current persistence work.

## NEXT ACTION

**Current target: `verigence-audit-core` only. WRITE APPROVED for lossless reviewed-DI persistence; NO MERGE / NO DEPLOY.**

1. Inspect only `tests/test_uc03_delivery_commands.py`, Delivery stage/version helpers and route installation needed for a Delivery Review confirm command.
2. Add the smallest explicit POST Delivery Review confirm endpoint; do not mutate `/audit/source-comparison` GET.
3. Require submitted Delivery, `PENDING` verification, no pending/failed Delivery DI documents, If-Match/idempotency/authorization/aggregate locking consistent with existing UC03 commands.
4. Persist every populated Delivery DI field through the shared generic helper with `stage_code='DELIVERY'`; no typed-owner requirement and no invented aliases.
5. Mark Delivery PC verification `VERIFIED` only after persistence succeeds; append safe workflow metadata only.
6. Add focused tests, source-verify, checkpoint.
7. Then obtain executable pytest/migration/DB evidence if available; otherwise record exact unverified state and stop before merge/deploy.

## Anti-stuck rule

If a direct path does not answer the current question after a small number of attempts, mark `UNKNOWN` and pivot. Do not recursively scan the repository.