# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — Audit Core structural design consolidated; final-report contract blocker remains**  
Repository: **`verigence-audit-core` only**  
Investigation branch: `investigation/uc03-post-delivery-final-source`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Mode: **INVESTIGATION / DESIGN ONLY — no schema/application writes authorized**

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → continue from `NEXT ACTION`.

Do not reconstruct completed work from chat history or broadly rescan repositories.

## Locked business decisions

- One Journey; Booking and Delivery are stages of that Journey.
- Booking facts persist at Booking Review; Delivery facts persist at Delivery Review.
- Every non-null/non-empty reviewed DI field has durable Audit Core representation, even without a typed owner.
- Unchanged effective value = DI value; changed value preserves DI original/provenance plus reviewed effective value.
- Repeated Payments/Receipts and repeated/multiple Invoices remain distinct under the same Journey.
- Exact canonical document aliases/keys must come from authoritative contracts; never invent them.
- Final report is a projection of resolved Journey state and is blocked until post-Delivery final-source resolution + successful rule run.

## Completed implementation baseline

PR #135 is merged to `dev`.

- merge commit: `ab0cf4c6a3e97cf70e482d0afdb6ae4c0ada6dd1`;
- live-baseline child commit: `10701bf9968968d0efe4920b9230c2ed2664bd5f`;
- fresh migration including `0051`: PASS;
- Ruff: PASS;
- pytest: 366 passed;
- Railway DEV deployment/fresh-deploy verification/smoke: PASS.

Do not reopen the completed lossless Booking/Delivery persistence unit without contradictory evidence.

## Completed Step-1 investigation/design units

### A. Post-Delivery persistence shape

Document:
`docs/uc-003-booking-delivery-audit/UC03_POST_DELIVERY_FINAL_SOURCE_DESIGN_2026-09-01.md`

Verified/proposed structural direction:

- final resolution consumes persisted reviewed Audit Core rows, not live DI values;
- reuse `journey_attribute_resolutions` for `POST_DELIVERY`;
- smallest later extension: selected reviewed-field reference + resolved value snapshot;
- reuse `journey_stage_states.POST_DELIVERY`, workflow tasks/events and audit evaluations/findings for rule-run/readiness state;
- no generic final-state table or generic rule-run table;
- finalization gate requires Booking + Delivery Review both VERIFIED;
- existing source-comparison GET remains read-only.

### B. Stage-aware source-rule matrix

Document:
`docs/uc-003-booking-delivery-audit/UC03_POST_DELIVERY_SOURCE_RULE_MATRIX_2026-09-01.md`

Verified:

- current resolver loses Booking/Delivery stage identity;
- document type -> confidence -> tie-break is not a safe final business precedence contract;
- no blanket Delivery/latest/highest-confidence winner is authorized;
- Booking-only facts are deterministic;
- repeated Payments/Receipts and Invoices bypass scalar winner logic;
- overlapping identity/vehicle/commercial precedence remains UNKNOWN where no approved rule exists.

### C. Canonical document identity assessment

Document:
`docs/uc-003-booking-delivery-audit/UC03_CANONICAL_DOCUMENT_IDENTITY_ASSESSMENT_2026-09-01.md`

Verified current gaps:

- Booking Docket/Form: catalogue `booking_docket` vs typed materializer `booking_form`;
- Insurance: catalogue document type `insurance_cover` vs resolver `insurance_cover_note` / `insurance_policy`;
- Receipt family: Booking `minimum_booking_payment_proof`, Delivery `payment_receipt`, typed receipt path `dealer_receipt`.

Aligned/partially aligned:

- Aadhaar aligned;
- PAN canonical `pan_card` aligned; `pan` alias UNKNOWN;
- Customer DMS invoice canonical aligned;
- Tally tax invoice canonical aligned; extra invoice aliases UNKNOWN;
- Cost Sheet aligned;
- Wholesale Invoice is valid catalogue evidence; its use as a scalar source is attribute-specific and UNKNOWN where not mapped.

Corrected conclusion:

- V2 capture reconciles DI classification to requirement document type by exact equality;
- current runtime does not use `document_capture_v2_source_truth_rules` as an alias normalizer;
- that table may be reused for attribute-level source policy, but it is not proven to be the sole cross-module alias master;
- prefer one authoritative DI/catalogue canonical-key contract and align Audit Core to it after Step-2 validation;
- do not add another alias table without a proven deficiency.

### D. Final-report artifact verification

The supplied `SPR Details - Copy (2).xlsx`, `SPR_Tool_Process_SubProcess_Activity_Details (1).xlsx`, scope presentation and supplied source documents were inspected for this purpose.

They provide process/report-family evidence, but they do not establish the exact 152-field final-report workbook/column contract required by the Master.

Still UNKNOWN:

- exact 152 output fields/order;
- exact typed/resolved/repeated/computed owner per output;
- exact repeated Payment/Invoice report shape;
- whether a typed repeated Invoice entity is required.

### E. Consolidated Audit Core Step-1 design package

Document:
`docs/uc-003-booking-delivery-audit/UC03_STEP1_AUDIT_CORE_STABILIZATION_PACKAGE_2026-09-01.md`

The package records:

- structures reused unchanged;
- verified gaps/root causes;
- smallest proposed resolution-ledger extension;
- final-source confirm API design;
- canonical normalization boundary;
- rule-run/report gate;
- database/API impact;
- acceptance-test plan;
- implementation sequence;
- rollback/containment boundaries;
- external blockers/UNKNOWN.

## Structural Step-1 conclusion

### Reuse unchanged

- Journey/stage model;
- durable reviewed-field persistence;
- existing typed business owners where approved;
- repeated Payments;
- requirement/document/evidence identity structures;
- workflow tasks/events;
- audit evaluations/findings;
- POST_DELIVERY stage state.

### Smallest extension after approval

- extend `journey_attribute_resolutions` with selected reviewed-field reference + resolved value snapshot;
- additive final-source confirm/read contract;
- stage-aware approved source-rule consumption;
- post-Delivery rule-task/readiness wiring.

### New structures

**None proven.**

Typed repeated Invoice entity remains **UNKNOWN** until the actual final-report contract or approved rules prove invoice-level business rows are required.

## Corrected / withdrawn assumptions

- no second generic raw/final fact table;
- no blanket Delivery override;
- no confidence/recency business-authority rule;
- no one-final-Invoice assumption;
- 123-field matrix is not the 152-field final-report contract;
- existing `document_capture_v2_source_truth_rules` is not an already-working alias normalizer.

## Current blockers

### Blocker 1 — actual 152-field final-report workbook

This is an explicit Master/Plan Step-1 deliverable and is not present among the artifacts inspected so far.

Without it, exact output ownership and the remaining repeated-Invoice decision cannot be closed truthfully.

### Blocker 2 — authoritative DI canonical keys / aliases

Audit Core proves fragmented vocabulary but cannot determine which exact disputed keys DI publishes. This belongs to Step 2 after Step 1 structural design is accepted.

### Blocker 3 — exact overlapping-source precedence

Audit Core can implement the mechanism, but final winner rules for unresolved overlapping sources need authoritative DI/business source-truth evidence. Keep them UNKNOWN rather than inventing defaults.

## NEXT ACTION

**User input is now required for one genuine external artifact blocker:** identify or provide the actual **152-field final-report workbook** referenced by the Master Charter.

When it is available, remain in `verigence-audit-core` Step 1 and do exactly one evidence unit:

1. extract the exact 152 output fields/order;
2. map each output to `TYPED DOMAIN`, `POST_DELIVERY RESOLUTION`, `REPEATED COLLECTION`, or `COMPUTED/AUDIT` owner;
3. identify only the true unmapped outputs;
4. decide whether any output genuinely requires a typed repeated Invoice entity;
5. update the consolidated Step-1 package/checkpoint;
6. then stop at the Step-1 approval gate before schema/application implementation or Step-2 DI work.

If the workbook cannot be supplied, do not fabricate the report mapping. Record the blocker and wait for a business-approved substitute contract.

Do **not** enter DI or Web and do not write migration/application code before this gate is closed.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it UNKNOWN and pivot. Do not recursively rescan completed repositories.
