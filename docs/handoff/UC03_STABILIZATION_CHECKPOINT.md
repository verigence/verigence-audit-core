# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core final-source implementation; Unit 1 implemented/source-verified**  
Repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-post-delivery-final-source-v1`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Latest application/test SHA before this checkpoint commit: `27929a95c265ece4bf0d6f0937f92a0d5fe57cd1`  
Mode: **WRITE APPROVED for final-source scope; merge/deploy NOT approved**

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → `UC03_IMPLEMENTATION_CONTEXT.md` → continue from `NEXT ACTION`.

Do not reconstruct completed work from chat history or broadly rescan completed repositories.

## Locked business decisions

- One Journey; Booking and Delivery are stages of that Journey.
- Booking facts persist at Booking Review; Delivery facts persist at Delivery Review.
- Every populated reviewed DI field remains durable in Audit Core.
- Repeated Payments/Receipts and repeated/multiple Invoice documents remain distinct.
- Final report uses the user-supplied business Final Source of Truth list; `NA` means non-document-derived.
- Exact technical DI aliases/keys must be authoritative; never invent them.
- Final report is blocked until post-Delivery final-source resolution + successful post-Delivery rule run.
- Current authoritative report contract supersedes the earlier 152-field assumption: 122 physical rows, 113 labelled outputs excluding two `-` separators, 81 unique non-separator labels.
- Current report does not require a typed repeated Invoice table.
- Typed/source-system report fields (for example Booking & Retail Dump outputs) remain in existing typed owners and do **not** require duplicate rows in the final-resolution ledger.

## Completed baseline on `dev`

PR #135 is merged/deployed and verified:

- merge `ab0cf4c6a3e97cf70e482d0afdb6ae4c0ada6dd1`;
- live baseline `10701bf9968968d0efe4920b9230c2ed2664bd5f`;
- migration through `0051`: PASS;
- Ruff: PASS;
- pytest: 366 passed;
- Railway DEV deployment/fresh verification/smoke: PASS.

Do not reopen the completed lossless Booking/Delivery reviewed-field persistence baseline without contradictory evidence.

## Approved final-source implementation scope

User approved **`Approved: Audit Core final-source implementation`** on 2026-09-01.

Allowed:

1. additive `journey_attribute_resolutions` extension;
2. persisted final-source resolver using authoritative business-source policy where technical mappings are proven;
3. additive final-source confirm/read API with authorization, If-Match, idempotency and aggregate locking;
4. reuse existing typed business/commercial structures;
5. post-Delivery workflow task/readiness gate;
6. focused migration/API/resolution/repeated-payment/report-contract tests;
7. checkpoint/context updates.

Not allowed in this unit:

- DI/Web/Security changes;
- invented aliases/field mappings;
- rule-engine internals;
- new generic final/report table;
- Invoice table;
- Payment redesign;
- merge/deploy without separate approval.

## Completed implementation units

### Unit 1 — final-resolution ledger foundation

**Status: IMPLEMENTED / SOURCE-VERIFIED; NOT YET CI/DB-TESTED**

CHANGED:

- Added migration `0052_uc03_final_source_resolution.py`.
- Reused `auditcore.journey_attribute_resolutions`; no new final-state table.
- Added `resolved_value_snapshot jsonb`.
- Added nullable `source_reviewed_field_id` with a tenant+Journey-safe composite FK to `journey_document_extracted_fields`.
- Kept existing DI source identity columns and NOT NULL constraints unchanged.
- Added `src/audit_core/uc03_final_source_persistence.py` as a narrow POST_DELIVERY helper; existing Booking resolution helper is untouched.
- Reviewed-field-backed final resolution loads `effective_value` + DI/document/fact provenance from durable Audit Core reviewed fields and snapshots it.
- Added focused tests in `tests/test_uc03_final_source_persistence.py`.

CORRECTED ASSUMPTION:

- An initial implementation draft relaxed legacy DI source columns to support typed/source-system resolution rows.
- The authoritative owner matrix disproves the need for that duplication: typed/source-system outputs are `TYPED SOURCE_SYSTEM / REUSE`, so they remain in existing domain owners and do not require ledger rows.
- That draft was removed before CI. `0052` is now strictly additive and document-derived/sparse.

VERIFIED:

- source selection query is scoped by tenant + Journey + reviewed-field id and requires an accepted `effective_value`;
- composite FK design prevents a selected reviewed field from crossing Journey/Tenant;
- final snapshot is copied from durable reviewed Core state, not a live DI call;
- no existing Booking/Delivery resolution constraints are relaxed;
- source-test errors found during inspection were corrected before CI.

NOT YET VERIFIED:

- fresh PostgreSQL migration execution;
- Ruff;
- pytest;
- real DB FK behavior.

## Remaining UNKNOWN / fail-closed items

- exact DI canonical technical keys and field keys not already proven in Audit Core;
- exact arithmetic formulas for the two payment/reconciliation report blocks;
- exact selection/concatenation semantics for multiple PC/TL/PMO remarks.

Do not invent these.

## NEXT ACTION

**Implementation Unit 2 — final-source confirm/read command.**

1. inspect only existing UC03 authorization/aggregate-lock/idempotency patterns, stage-state fields, reviewed-field rows and router installation points needed for this command;
2. define the smallest final-source policy adapter for document-derived scalar outputs whose technical Audit Core mapping is already authoritative; retain explicit unresolved policy entries for the rest;
3. implement `POST /v2/tenants/{tenant_id}/journeys/{journey_id}/audit/final-source/confirm` that:
   - requires Booking Review VERIFIED;
   - requires Delivery Review VERIFIED;
   - uses If-Match + idempotency + Journey aggregate lock;
   - reads durable Audit Core reviewed state only;
   - persists POST_DELIVERY document-derived resolutions through Unit 1 helper;
   - fails closed before partial commit when required technical source mappings are unresolved;
   - does not scalar-collapse repeated Payments/Invoices;
4. add persisted final-source/readiness GET if needed;
5. add focused tests and update this checkpoint;
6. then wire the post-Delivery workflow task/readiness gate as the next coherent unit.

Do not enter DI or Web. Do not merge/deploy.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
