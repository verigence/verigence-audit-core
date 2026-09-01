# UC03 Implementation Context — Post-Delivery Final Source of Truth

## Mandatory read-first prompt

> Resume UC03 stabilization from repository handoff.  
> Read `docs/handoff/UC03_STABILIZATION_MASTER.md` first, then `docs/handoff/UC03_STABILIZATION_CHECKPOINT.md`, then `docs/handoff/UC03_STABILIZATION_PLAN.md`, then this implementation context.  
> Do not reconstruct decisions from chat history.  
> Work only within the approved activity scope below.  
> Verify before claiming. Use `UNKNOWN` when evidence is missing.  
> Do not broaden into unrelated repositories/files or recursively rescan completed work.  
> Do not merge or deploy unless separately approved.

## Activity

- Stabilization step: `Step 1 — approved Audit Core final-source implementation`
- Repository: `verigence-audit-core`
- Implementation branch: `fix/uc03-post-delivery-final-source-v1`
- Branch starting SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`
- Approval reference/date: user explicitly approved `Approved: Audit Core final-source implementation` on 2026-09-01.
- Merge approval: **NOT GRANTED**
- Deploy approval: **NOT GRANTED**

## Final invariant to implement

After Booking Review and Delivery Review are both VERIFIED, Audit Core can commit durable post-Delivery final-source rows for approved **document-derived scalar** report/business attributes without re-reading live DI as final business state. Each committed row retains exact reviewed-field/document/fact provenance and a stable resolved value snapshot.

Typed/source-system outputs continue to come directly from their existing Audit Core business owners; they are not duplicated into the resolution ledger. Repeated Payments/Receipts and multiple Invoice documents remain distinct collections/documents and are not collapsed into scalar winners. The final report remains blocked until the post-Delivery rule-run boundary completes successfully.

## Authoritative business inputs

The user supplied the final report field list and corresponding Final Source of Truth list directly. That business-source list is authoritative at label level.

Examples:

- DMS Invoice Date/Number -> `Tax Invoice — DMS`;
- Delivery Date -> `Gate Pass`;
- Customer/KYC outputs -> `Customer KYC (PAN, Aadhaar, address proof)`;
- Type of customer / Model / Model Variant -> `Booking & Retail Dump`;
- registration outputs -> `RTO Paper`;
- chassis -> `Tax Invoice — DMS`;
- Finance Type -> `Bank DO`;
- Bank Name -> `Bank Statement`;
- First receipt date -> `Money Receipt`;
- actual Insurance -> `Insurance Cover Note`;
- actual Accessories -> `Accessory Invoice — Tally / bookkeeping software`;
- actual EW -> `EW Tally Invoice`;
- many actual discounts -> `Customer Ledger`.

`NA` means the output is not document-derived; it does not mean no value.

The active contract is 122 physical rows, 113 labelled outputs excluding two `-` separators, and 81 unique non-separator labels. This supersedes the earlier unverified 152-field assumption.

Technical DI/canonical keys not proven by current Audit Core evidence remain `UNKNOWN` and must fail closed. Do not invent aliases.

## Verified current state

- `journey_document_extracted_fields` is the durable reviewed-field layer for BOOKING and DELIVERY after migration `0051`.
- `journey_attribute_resolutions` already supports `POST_DELIVERY`, one resolution per Journey/stage/attribute, selected-source provenance, resolution rule/mapping version, optional owning domain/reference, actor/time, and append-only runtime semantics.
- Before `0052` it had no stable final value snapshot and no direct reference to the selected reviewed-field row.
- Existing `/audit/source-comparison` computes transient values and remains read-only.
- `journey_stage_states` already supports `POST_DELIVERY` with audit state/status.
- `workflow_tasks` already provides idempotent task lifecycle/retry/dead-letter behavior; no generic rule-run table is needed.
- `audit_evaluations` / findings already provide rule-result structures.
- Existing typed domains cover Booking, Customer, Product/Vehicle, Registration, Finance, Insurance, Addons, Trade-In, Commercial lines, Discounts and repeated Payments.
- Current report contract does not require a typed repeated Invoice entity.
- Typed/source-system report fields such as `Booking & Retail Dump` outputs already have existing typed owners and must not be duplicated into final-resolution rows.

## Exact gap / root cause

- GAP: no durable post-Delivery final value snapshot existed for document-derived scalar outputs.
- GAP: the resolution ledger could not directly point to the reviewed Audit Core row selected as final source.
- GAP: current transient resolver does not carry Booking/Delivery stage identity and must not be reused unchanged as final business precedence.
- ROOT CAUSE: the existing comparison flow predates the durable reviewed-field persistence/final-report source contract.

## Approved data-model change

Reuse `journey_attribute_resolutions`. Add only:

1. `resolved_value_snapshot` — JSON value snapshot selected at finalization;
2. nullable `source_reviewed_field_id` — reference to `journey_document_extracted_fields.extracted_field_id` for new document-derived `POST_DELIVERY` resolution rows.

The column is nullable only for backward compatibility with pre-0052 rows. New document-derived final-source rows must populate it.

Do **not** relax existing DI document/field/fact identity requirements. Do **not** create ledger rows for typed/source-system outputs. Do not create another generic final-value/report table.

## Files / structures allowed to change

- one additive Alembic migration after current head;
- narrow final-source persistence/policy/command modules;
- router registration/OpenAPI only for additive final-source confirm/read endpoints;
- existing workflow task helper only where minimal reuse/integration is required;
- focused UC03 migration/API/resolution/report-contract tests;
- `docs/handoff/UC03_STABILIZATION_CHECKPOINT.md` and this context.

## Files / areas explicitly not to touch

- `verigence-di`, `verigence-web`, Security;
- unrelated UC01/UC02 code;
- dependency upgrades or CI/CD redesign;
- rule-engine evaluator internals;
- canonical alias values not already authoritative in Audit Core;
- Payment/Invoice cardinality redesign;
- generic report persistence table;
- new Invoice table.

## Final-source command boundary

Design target:

`POST /v2/tenants/{tenant_id}/journeys/{journey_id}/audit/final-source/confirm`

Responsibilities:

1. authorize Journey/project scope;
2. If-Match + aggregate lock;
3. require Booking Review VERIFIED;
4. require Delivery Review VERIFIED;
5. idempotency;
6. load durable reviewed Booking/Delivery rows only; no DI call;
7. resolve only document-derived scalar outputs whose business source and technical document/field mapping are both authoritative;
8. fail closed before any final-source write when required technical mappings remain unresolved;
9. preflight all candidate disagreements before inserting the first final resolution;
10. persist `POST_DELIVERY` resolutions with reviewed-field reference + stable snapshot;
11. expose status/readiness without mutating the existing comparison GET.

Typed/source-system report outputs are read later from their existing domain owners rather than inserted into `journey_attribute_resolutions`.

## Candidate rules

- Read only reviewed rows with non-null `effective_value`.
- For each stage/document/field, use the latest persisted reviewed fact version.
- If no legitimate current source exists, record the attribute as missing; do not manufacture a value.
- If multiple legitimate current sources agree, choose deterministic provenance only; this is not a business-precedence rule.
- If legitimate current sources disagree, fail closed. Confidence, stage and recency must not choose the business winner unless an explicit later contract says so.
- Unknown/unmapped reviewed fields remain durable but are not guessed into a final report attribute.

## Repeated collections

Do not scalar-resolve:

- Payments/Receipts;
- multiple Invoice documents;
- other genuinely repeated business/document collections.

The two payment/reconciliation report blocks require aggregation rules; exact arithmetic is still `UNKNOWN` and is not to be invented in this implementation.

## Acceptance tests

### Migration / ledger

- existing resolution rows remain valid;
- `resolved_value_snapshot` persists exact selected reviewed effective value;
- selected reviewed-field FK cannot cross tenant/Journey;
- existing DI source NOT NULL constraints remain unchanged;
- runtime append-only behavior remains.

### Finalization gates

- fails if Booking Review != VERIFIED once technical mapping gate is clear;
- fails if Delivery Review != VERIFIED once technical mapping gate is clear;
- current unresolved technical mappings return `MAPPING_BLOCKED` / conflict with no partial write;
- stale Delivery If-Match fails;
- idempotent replay returns same committed result;
- concurrent conflicting finalization cannot create a second winner set.

### Source selection

- uses persisted reviewed effective values, not live DI;
- obsolete fact versions are not compared as current sources;
- rejected/no-effective reviewed fields are ineligible;
- unknown/unmapped reviewed fields remain durable but are not guessed into a report attribute;
- unresolved technical canonical mapping fails closed;
- source disagreement does not resolve by confidence/stage/recency.

### Repeated data

- multiple payments remain multiple rows;
- multiple invoices/same-type invoices remain distinct documents;
- finalization never merges/deletes sibling repeated records.

### Rule/report gate

- final-source commit will create/reuse exactly one post-Delivery rule task per finalization version/effect key in the next coherent unit;
- readiness remains false while rule task is not successfully completed;
- report readiness becomes true only after successful rule task + POST_DELIVERY audit completion;
- rule evaluator internals are not implemented here.

## Implementation sequence

1. resolution-ledger additive migration + helper tests;
2. durable candidate/source adapter for only proven Audit Core mappings;
3. final-source confirm/read command + tests;
4. workflow task/readiness wiring + tests;
5. report-contract ownership/shape tests that do not invent unresolved payment formulas/DI aliases;
6. CI + fresh migration + full pytest;
7. update checkpoint and stop for merge approval.

## Stop / escalation conditions

STOP and mark `UNKNOWN` when:

- a business source label requires a DI canonical key not proven in Audit Core;
- exact report arithmetic would have to be invented;
- a new table/domain appears necessary beyond the approved additive ledger extension;
- implementation would require DI/Web/Security changes;
- rule-engine internals would have to be designed.

## Recovery checkpoint

After each coherent unit update `UC03_STABILIZATION_CHECKPOINT.md` with branch/SHA, changes, tests actually run/results, remaining UNKNOWN/blockers, and exact next action.
