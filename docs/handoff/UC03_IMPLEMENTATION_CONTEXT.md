# UC03 Implementation Context — Lossless Reviewed DI Persistence

## Mandatory read-first prompt

> Resume UC03 stabilization from repository handoff.  
> Read `docs/handoff/UC03_STABILIZATION_MASTER.md` first, then `docs/handoff/UC03_STABILIZATION_CHECKPOINT.md`, then `docs/handoff/UC03_STABILIZATION_PLAN.md`, then this implementation context.  
> Do not reconstruct decisions from chat history.  
> Work only within the approved activity scope below.  
> Verify before claiming. Use `UNKNOWN` when evidence is missing.  
> Do not broaden into unrelated repositories/files, do not recursively rescan completed work, and do not make drive-by changes.  
> If a path is unproductive, pivot to the next direct evidence source.  
> Do not merge or deploy unless separately approved.

## Activity

- Stabilization step: `Step 1 — approved Audit Core persistence implementation unit`
- Repository: `verigence-audit-core`
- Implementation branch: `fix/uc03-persist-all-di-fields-booking-delivery-v2`
- Branch starting SHA: `724ddbc4ed11ec82195763d9b10a6f9f339bb729` (`dev` at approval re-anchor)
- Approved write scope: lossless durable Audit Core persistence of every non-null/non-empty DI field participating in UC03 Booking or Delivery Review; retain original DI provenance; persist reviewed/effective value; keep existing typed business projections as the additional operational projection layer; remove confirmation blockers that discard/block accepted fields solely because no typed owner exists; add focused schema/API/tests required for that invariant.
- Approval reference/date: user explicitly approved `Approved: Audit Core implementation` on 2026-09-01, then clarified that any non-empty/non-null DI value must be updated/persisted in Audit Core.
- Merge approval: **NOT GRANTED**
- Deploy approval: **NOT GRANTED**

## Final invariant to implement

For each UC03 Booking or Delivery document reviewed from DI, every non-null/non-empty DI field has a durable Audit Core representation after Review. An unchanged field retains the original DI value as the effective reviewed value. A changed field retains original DI value/provenance and stores the confirmed effective value. An unknown/unmapped field is not silently dropped and is not required to invent a typed business owner. Existing approved typed owners continue to receive the effective value in addition to the generic durable representation. Document/fact identity and stage provenance remain traceable, including repeated documents and multiple documents of the same type.

## Verified current state

- VERIFIED FACT: `migrations/versions/0031_uc03_generic_di_review_fields.py` created `auditcore.journey_document_extracted_fields`, but its present legacy constraints require `evidence_id` and UUID `source_fact_ref`; current V2 facts do not always provide those identifiers.
- VERIFIED FACT: `src/audit_core/uc03_pc_generic_review.py` currently writes only human-corrected fields and deliberately leaves unchanged `extracted_value` / confidence unpersisted.
- VERIFIED FACT: `src/audit_core/uc03_booking_review_decisions.py` currently blocks an accepted unmapped/raw field when no typed Core owner exists and records `rawDiValuesCopied: False`.
- VERIFIED FACT: `migrations/versions/0048_uc03_di_core_field_persistence.py` plus `src/audit_core/uc03_v2_review_materialization.py` provide typed persistence for known Booking Form / PAN / Aadhaar / Dealer Receipt fields only; they are not a lossless generic DI-field layer.
- VERIFIED FACT: `src/audit_core/uc03_document_review_v2.py` exposes current Booking + Delivery DI facts; the post-Delivery source-comparison route is read-only and does not commit reviewed Delivery values.
- VERIFIED FACT: current V2 `DiFact` provides `canonical_field_id`, `field_key`, `value`, `confidence_score`, `version_no`, page/evidence data, but not the legacy UUID `source_fact_ref`.
- VERIFIED FACT: `record_attribute_resolution(...)` permits null owning-domain/reference values, so a supported review/provenance fact does not need a fabricated typed owner.
- VERIFIED FACT: Payment stage/linkage is already implemented by migration `0041_uc03_journey_stage_linkage.py`; do not reopen that defect without contradictory evidence.

## Exact gap / root cause

- GAP: Audit Core does not currently guarantee durable representation of every non-null/non-empty DI field after Booking Review, and Delivery Review has no equivalent durable commit path.
- GAP: accepted unknown/unmapped V2 fields can block Booking confirmation solely because they lack a typed owner.
- ROOT CAUSE: earlier implementation optimized for DI-owned raw facts plus selected typed projections/corrections, while the governing stabilization invariant requires lossless reviewed-field persistence in Audit Core as well as typed projection.
- ROOT CAUSE: the legacy generic table schema is not fully compatible with current V2 identifiers/stage provenance and therefore needs the smallest backward-compatible extension rather than a parallel table.

## Files / structures allowed to change

- `auditcore.journey_document_extracted_fields` through one additive/backward-compatible Alembic migration.
- `src/audit_core/uc03_di_core_persistence.py` as the shared lossless persistence helper.
- `src/audit_core/uc03_pc_generic_review.py` only as needed to make the legacy/direct Review path lossless without breaking its contract.
- `src/audit_core/uc03_booking_review_decisions.py` only as needed to persist all Booking reviewed fields and remove typed-owner-only blocking.
- `src/audit_core/uc03_document_review_v2.py` and/or one narrowly scoped UC03 Delivery Review module only if required to add a proper Delivery review commit action; do not make GET endpoints mutate state.
- Existing typed materialization files only where a minimal compatibility call is required; do not redesign typed domains.
- Relevant UC03 tests and OpenAPI contract tests.
- `docs/handoff/UC03_STABILIZATION_CHECKPOINT.md` and this context for recovery updates.

## Files / areas explicitly not to touch

- Security unless separately approved.
- `verigence-di` and `verigence-web` in this implementation unit.
- unrelated UC01/UC02 modules.
- unrelated global infrastructure / CI/CD / observability.
- dependencies unless separately approved.
- rule-engine internals.
- final-report implementation, final-source-resolution platform design, or unrelated schema cleanup.
- canonical alias values that are not already authoritative in repository contracts/catalogues.
- existing Journey/Booking/Delivery/Payment cardinality unless direct contradictory evidence appears.

## Data-model rule

Reuse `journey_document_extracted_fields`; do not create a second generic raw/effective-field table unless direct implementation evidence proves the existing structure cannot be safely extended. Preserve V1/legacy rows. Use existing typed Customer/Booking/Vehicle/Commercial/Payment/etc. structures only where explicit owners already exist. Unknown fields survive generically rather than forcing speculative domain columns.

## Document identity rule

Persist actual DI document identity and original classified document type. Repeated documents and multiple documents of the same type remain distinct by document/fact identity. Canonical alias normalization is a separate verified concern; do not invent alias keys in this unit and do not let alias uncertainty cause field loss.

## Acceptance tests

The activity is not `FIXED` until final-state tests pass.

- Booking: DI supplies N populated fields; Review confirms; DB contains durable rows for all N populated fields, including unknown/unmapped fields.
- Booking unchanged value: original DI value and effective reviewed value are both durable with exact DI document/canonical-field/fact-version provenance.
- Booking accepted unknown field: confirmation succeeds without a fabricated typed owner; value remains durable generically; no unintended typed mutation occurs.
- Legacy/direct Review: unchanged non-empty fields are no longer silently omitted; corrections preserve original value plus confirmed effective value.
- Delivery: after an explicit Delivery Review commit, every populated Delivery DI field is durable with `DELIVERY` provenance; repeated documents/same-type documents remain distinct.
- Rejected values retain original DI provenance and do not project into typed business owners; exact effective-value semantics must follow the confirmed Review contract.
- Existing typed Booking Form / identity / receipt projections continue to pass their regression tests.
- Database migration is backward-compatible with existing legacy generic rows and current V2 field identifiers.
- DB-level test verifies no populated accepted field is silently lost.

## Implementation discipline

For each coherent implementation unit report only:

- `CHANGED:`
- `WHY:`
- `VERIFIED:`
- `REMAINING:`

Use `IMPLEMENTED` until DB/end-to-end acceptance passes. Do not claim `FIXED` from unit tests alone.

## Stop / escalation conditions

STOP and request a decision when:

- a required reviewed effective-value behaviour is not present in the governing Master/source or existing Review contract;
- implementing Delivery requires Web or DI changes in this unit;
- a Security change appears necessary;
- compatibility impact is materially larger than the small extension described above;
- a new generic persistence table appears necessary rather than extending the existing one;
- exact canonical document aliases would have to be invented.

## Recovery checkpoint

If interrupted, update `UC03_STABILIZATION_CHECKPOINT.md` with branch/SHA, completed coherent units, tests actually run/results, blockers/UNKNOWN, and exact next action. Then resume there rather than rescanning.