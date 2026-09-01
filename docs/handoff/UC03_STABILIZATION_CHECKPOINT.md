# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core final-source implementation**  
Repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-post-delivery-final-source-v1`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Mode: **WRITE APPROVED for the narrow final-source scope below; merge/deploy NOT approved**

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → `UC03_IMPLEMENTATION_CONTEXT.md` → continue from `NEXT ACTION`.

Do not reconstruct completed work from chat history or broadly rescan completed repositories.

## Locked business decisions

- One Journey; Booking and Delivery are stages of that Journey.
- Booking facts persist at Booking Review; Delivery facts persist at Delivery Review.
- Every non-null/non-empty reviewed DI field has durable Audit Core representation, even without a typed owner.
- Unchanged effective value = DI value; changed value preserves original DI provenance + reviewed effective value.
- Repeated Payments/Receipts and repeated/multiple Invoice documents remain distinct under one Journey.
- Exact canonical technical aliases/keys must come from authoritative contracts; never invent them.
- Final report is a projection of resolved Journey state and is blocked until post-Delivery final-source resolution + successful post-Delivery rule run.
- The user-supplied Final Source of Truth list is authoritative at business-source-label level for the final report.
- `NA` in that list means the output is not document-derived; it does not mean no value.

## Completed implementation baseline

PR #135 is merged to `dev` and DEV verification passed.

- merge commit: `ab0cf4c6a3e97cf70e482d0afdb6ae4c0ada6dd1`;
- live baseline child: `10701bf9968968d0efe4920b9230c2ed2664bd5f`;
- migration through `0051`: PASS;
- Ruff: PASS;
- pytest: 366 passed;
- Railway DEV deployment/fresh-deploy verification/smoke: PASS.

Completed persistence baseline:

1. lossless reviewed-field persistence via `journey_document_extracted_fields`;
2. legacy/direct Booking Review persists all populated reviewed fields;
3. Booking V2 Review Confirm persists all populated fields and accepts unknown fields without fabricated typed owners;
4. Delivery V2 Review Confirm persists all populated Delivery fields before Delivery Review becomes VERIFIED.

Do not reopen this baseline without contradictory evidence.

## Completed Step-1 investigation/design

The investigation source branch is `investigation/uc03-post-delivery-final-source`.

Completed design conclusions:

- final resolution must consume persisted reviewed Audit Core rows, not live DI;
- reuse `journey_attribute_resolutions` for `POST_DELIVERY`;
- smallest extension: `resolved_value_snapshot` + nullable selected reviewed-field reference;
- source-reviewed-field reference must be nullable because some approved final sources are typed/source-system values such as Booking & Retail Dump;
- reuse `journey_stage_states.POST_DELIVERY`, workflow tasks/events, audit evaluations/findings;
- no generic final-state table and no generic rule-run table;
- existing `/audit/source-comparison` remains read-only;
- no blanket Delivery/latest/highest-confidence precedence;
- approved report business source labels determine source authority; unresolved technical DI keys stay UNKNOWN/fail closed;
- no new alias table is approved;
- no typed repeated Invoice entity is required for the current report contract;
- repeated payment/reconciliation sections remain repeated/aggregate outputs and must not be collapsed into scalar winners.

Authoritative current report contract supplied by the user:

- 122 physical rows including structural blank/separator rows;
- 113 labelled output rows excluding two `-` separators;
- 81 unique non-separator labels because Standard/Actual and payment sections deliberately repeat labels.

This supersedes the earlier assumption that the active report must contain exactly 152 outputs.

## Approved write scope

User approved: **`Approved: Audit Core final-source implementation`** on 2026-09-01.

Allowed implementation scope:

1. additive `journey_attribute_resolutions` extension with `resolved_value_snapshot` + nullable `source_reviewed_field_id`;
2. persisted stage-aware/business-source-aware final-source resolver using approved report source policy where technical mapping is already authoritative;
3. additive final-source confirm/read API with authorization, If-Match, idempotency and aggregate locking;
4. reuse existing typed commercial/domain projections — no new generic table and no Invoice table;
5. post-Delivery workflow task/readiness gate;
6. focused migration/API/resolution/repeated-payment/report-contract tests;
7. checkpoint/context updates required for recovery.

Explicitly out of scope:

- DI repository changes or invented DI aliases;
- Web changes;
- Security redesign/change;
- rule-engine internals;
- new generic final-state/report table;
- new Invoice table;
- Payment cardinality redesign;
- deployment or merge without separate approval.

## Remaining UNKNOWN / fail-closed items

- exact DI canonical technical keys for business labels not already proven in Audit Core;
- exact DI field keys for any source not already authoritative in current contracts;
- exact arithmetic formulas for the two payment/reconciliation report blocks;
- exact multi-remark selection/concatenation semantics for PC/TL/PMO report remarks.

Do not invent these in this implementation.

## NEXT ACTION

**Implementation Unit 1 — resolution ledger foundation.**

1. inspect current `journey_attribute_resolutions` schema/helper and current migration head only;
2. implement one additive backward-compatible migration adding `resolved_value_snapshot` and nullable `source_reviewed_field_id` with tenant/Journey-safe integrity;
3. extend the existing resolution helper so `POST_DELIVERY` resolutions can store either:
   - a reviewed-field-backed source; or
   - a typed/source-system owner with no reviewed-field reference;
4. add focused migration/helper tests;
5. update this checkpoint after verification;
6. then proceed to the final-source confirm/read command unit.

Do not enter DI or Web. Do not merge/deploy. If a technical source mapping is not authoritative in Audit Core, leave it unresolved/fail closed rather than inventing an alias.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
