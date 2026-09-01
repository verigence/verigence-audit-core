# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core final-source implementation; Unit 1 implemented/source-verified**  
Repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-post-delivery-final-source-v1`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Latest application/test SHA before this checkpoint commit: `309a208f8b33c0fb91da55cc6576407e594ea35f`  
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
- Added source-contract constraints preserving mandatory DI identity for existing BOOKING/DELIVERY rows.
- Corrected a design assumption exposed by implementation: legacy `source_di_document_id`, `source_field_key`, `source_fact_version` were NOT NULL, so typed/source-system POST_DELIVERY resolutions could not be represented truthfully. `0052` relaxes them only under a POST_DELIVERY typed-owner contract; no fake DI values are allowed.
- Added `src/audit_core/uc03_final_source_persistence.py` rather than changing the existing Booking helper.
- Reviewed-field-backed final resolution loads `effective_value` + provenance from durable Audit Core reviewed fields and snapshots it.
- Typed/source-system final resolution requires explicit owning domain/reference and leaves DI identifiers + reviewed-field reference NULL.
- Added focused tests in `tests/test_uc03_final_source_persistence.py`.

WHY:

- final source must be stable/reproducible and must not re-read live DI;
- approved source-system values such as Booking & Retail Dump need a truthful non-DI source representation;
- cross-Journey reviewed-field references must be impossible.

VERIFIED:

- source inspection confirms the migration is additive and preserves existing BOOKING/DELIVERY source-reference requirements;
- source inspection confirms reviewed winner query is scoped by tenant + Journey + reviewed-field id and requires an accepted `effective_value`;
- source inspection confirms typed winner writes no fake DI identifiers and preserves valid `0` values;
- source test typo found during verification was corrected before CI.

NOT YET VERIFIED:

- fresh PostgreSQL migration execution;
- Ruff;
- pytest;
- DB FK/check behavior under a real database.

## Remaining UNKNOWN / fail-closed items

- exact DI canonical technical keys and field keys not already proven in Audit Core;
- exact arithmetic formulas for the two payment/reconciliation report blocks;
- exact selection/concatenation semantics for multiple PC/TL/PMO remarks.

Do not invent these.

## NEXT ACTION

**Implementation Unit 2 — final-source confirm/read command.**

1. inspect only the existing UC03 authorization/aggregate-lock/idempotency patterns, stage-state fields, reviewed-field rows and router installation points needed for this command;
2. define the smallest in-repo final-source policy for sources whose technical Audit Core mapping is already authoritative;
3. implement `POST /v2/tenants/{tenant_id}/journeys/{journey_id}/audit/final-source/confirm` that:
   - requires Booking Review VERIFIED;
   - requires Delivery Review VERIFIED;
   - uses If-Match + idempotency + Journey aggregate lock;
   - reads durable Audit Core reviewed/typed state only;
   - persists `POST_DELIVERY` resolutions through Unit 1 helpers;
   - fails closed for unresolved technical source mappings;
   - does not scalar-collapse repeated Payments/Invoices;
4. add a persisted read/status endpoint if required by the command contract;
5. add focused tests and update this checkpoint;
6. then wire the post-Delivery workflow task/readiness gate as the next coherent unit.

Do not enter DI or Web. Do not merge/deploy.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
