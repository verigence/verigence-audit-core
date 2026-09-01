# UC03 Step 1 — Audit Core Stabilization Design Package

Date: 2026-09-01  
Repository: `verigence-audit-core`  
Branch: `investigation/uc03-post-delivery-final-source`  
Status: **STEP-1 DESIGN CONSOLIDATION — NO SCHEMA/APPLICATION IMPLEMENTATION AUTHORIZED**

## 1. Final business state

The Audit Core target for UC03 is one evolving Journey with durable reviewed facts at Booking and Delivery, deterministic post-Delivery final-source resolution, a post-Delivery rule-run gate, and final-report projection from durable resolved business state.

Required sequence:

`Booking Capture -> Review -> Persist Booking facts -> Booking rule boundary -> Journey continues`

`Delivery Capture -> Review -> Persist Delivery facts -> Compare legitimate sources -> Persist final source -> Post-Delivery rule run -> Final report`

The final state must preserve:

- every non-empty reviewed DI field;
- original DI/document/fact provenance;
- reviewed/effective value;
- Booking vs Delivery stage provenance;
- repeated Payments/Receipts;
- repeated/multiple Invoice documents;
- deterministic final-source selection for scalar attributes where multiple legitimate sources exist;
- audit/rule status and results;
- explainability of the final report.

No populated accepted source may silently disappear, and no later Delivery source may destroy historical Booking evidence.

## 2. Current implementation — verified reuse

### 2.1 Journey and stage model — reuse unchanged

Reuse:

- `journeys` as the lifecycle root;
- `journey_stage_states` for `BOOKING`, `DELIVERY`, `POST_DELIVERY` state;
- Booking 1:1 and Delivery 0..1 linkage already established;
- existing aggregate versioning, stage audit state/status and workflow events.

No new employee-facing Journey/case structure is justified.

### 2.2 Durable reviewed DI fields — reuse unchanged

Reuse `auditcore.journey_document_extracted_fields` as the lossless reviewed-field layer.

Migration `0051_uc03_lossless_review_fields` already supports:

- Booking/Delivery stage;
- DI document identity;
- canonical field + field key + fact version;
- original extracted value;
- reviewed effective value;
- confidence/scale;
- modification and reviewer metadata;
- repeated documents/same-type documents.

Final-source resolution must consume these durable rows, not re-read DI for final values.

### 2.3 Typed business domains — reuse unchanged unless a specific owner gap is later proven

Continue using existing Journey business structures where they already represent the business concept, including:

- Customer / identity review values;
- Booking / Booking Form reviewed values;
- Vehicle;
- Commercial lines;
- Discounts/schemes;
- Payments;
- Finance;
- Insurance;
- Registration;
- Trade-In;
- Delivery;
- Evidence / Findings / Audit evaluations.

Typed projection remains additional operational business state. The generic reviewed-field layer is not a substitute for typed owners, and unknown fields must not force speculative typed columns.

### 2.4 Payments / Receipts — reuse repeated collection unchanged

Payments are already 1:N under a Journey with stage provenance and receipt details/verification events.

Do not create Booking-Payments and Delivery-Payments as separate entity types.

Post-Delivery finalization must not collapse multiple payment rows into a single scalar source.

### 2.5 Document identity and requirement snapshots — reuse current structures

Reuse:

- versioned document requirement profiles/items;
- `journey_document_requirements` as per-Journey effective requirement state;
- `document_capture_v2_documents` for captured DI document identity/stage/classification;
- requirement keys separately from document type keys.

Repeated documents remain distinct by DI document identity.

### 2.6 Rule-run infrastructure — reuse unchanged

Reuse:

- `journey_stage_states.POST_DELIVERY` as Journey-level post-Delivery audit status;
- existing `workflow_tasks` + task events for idempotent execution/retry/dead-letter lifecycle;
- `audit_evaluations` and Findings/Flags for rule results.

No generic `rule_runs` status table is justified by current evidence.

## 3. Verified gaps / root causes

### G1. No durable post-Delivery final-source value snapshot

**Verified gap:** current `GET /audit/source-comparison` computes a transient resolved value from live DI-backed data and persists no final post-Delivery resolution.

**Root cause:** Review comparison was implemented before a durable post-Delivery business-resolution contract existed.

### G2. Existing resolution ledger is reference-only

`journey_attribute_resolutions` already supports `POST_DELIVERY`, selected-source provenance, rule/mapping version and owning business reference, but it has no direct reference to the selected durable reviewed-field row and no resolved value snapshot.

### G3. Current resolver drops source stage

`AttributeCandidate` does not encode Booking/Delivery stage. The resolver orders by document type -> confidence -> deterministic tie break.

That is insufficient as a final business rule where legitimate sources can occur across stages.

### G4. Exact multi-source stage precedence is not fully approved

Current Audit Core evidence identifies legitimate source families for many attributes, but not a blanket final-stage rule.

In particular, post-Delivery precedence remains **UNKNOWN** for overlapping vehicle/commercial attributes including Model, Variant, Color, Ex-Showroom, TCS, Total Price, Discount, Net Amount and Insurance Amount.

No `Delivery wins`, `latest wins`, or `highest confidence wins` rule may be invented.

### G5. Canonical document identity is fragmented across current Audit Core paths

Verified examples:

- published Booking type `booking_docket`; typed Booking Form materializer expects `booking_form`;
- published Insurance document type `insurance_cover`; resolver prioritizes `insurance_cover_note` / `insurance_policy`;
- Booking requirement `minimum_booking_payment_proof` and Delivery `payment_receipt`; typed receipt path expects `dealer_receipt`;
- PAN canonical `pan_card` is aligned, while `pan` is an additional unverified accepted key;
- Invoice canonical `customer_invoice_dms` and `tax_invoice_tally` are aligned, while `tax_invoice_dms` / `tax_invoice` are additional unverified resolver aliases.

V2 capture currently reconciles DI classification to requirement document type by exact equality and does not consult `document_capture_v2_source_truth_rules`.

### G6. Exact final-report contract is unavailable

The Master names a supplied 152-field final-report workbook as the output contract, but the inspected supplied artifacts do not contain that exact 152-field column mapping.

The available 123-field matrix is a provisional capture/source inventory, not the final-report workbook.

Therefore exact output owner mapping and repeated Invoice report requirements remain `UNKNOWN`.

## 4. Smallest safe design

### D1. Reuse and minimally extend `journey_attribute_resolutions`

Proposed additive fields for a later approved migration:

1. `source_reviewed_field_id` — direct reference to the selected `journey_document_extracted_fields.extracted_field_id` within tenant;
2. `resolved_value_snapshot` — durable final value selected at finalization.

Keep existing:

- `stage_code='POST_DELIVERY'`;
- source DI document/canonical field/fact version;
- resolution rule;
- mapping version;
- owning domain/reference;
- actor/time;
- append-only runtime semantics.

No second generic final-value table is justified.

### D2. Final-source candidate set comes only from persisted reviewed fields

Eligible final candidates:

- `stage_code in ('BOOKING','DELIVERY')`;
- accepted reviewed field with non-null/non-empty effective value;
- exact mapped business attribute;
- canonical document identity accepted by the approved source rule.

Rejected/no-effective-value rows remain evidence but are not final candidates.

### D3. Final-source rules must be explicit and stage-aware

An approved source selector must be versioned and address at least:

`(attribute, stage, canonical document family/type, canonical field)`

Rules must distinguish:

- source authority;
- stage;
- repeated vs scalar business concept;
- disagreement behavior.

If two legitimate sources disagree and no approved precedence/disposition rule exists, do not choose by confidence/recency; return/persist an unresolved state and raise the appropriate audit workflow exception once that rule contract is approved.

### D4. Scalar resolution excludes repeated business collections

The generic final-source resolver must not collapse:

- Payments/Receipts;
- multiple Invoice records/documents;
- other configured repeatable documents.

Repeated collections remain repeated. The final report/rules can aggregate/select specific rows only according to explicit report/rule contracts.

### D5. Canonical document normalization boundary

Preferred design:

1. Step 2 validates the authoritative DI/catalogue canonical document keys and aliases;
2. DI classified key, Audit Core requirement `document_type_key`, Review source keys and typed materializer keys are aligned to that canonical contract;
3. requirement purpose remains separate in `requirement_key`;
4. original classified/source identity remains traceable;
5. no fuzzy string matching.

`document_capture_v2_source_truth_rules` may be reused for **attribute final-source policy**, but current evidence does not prove it is sufficient as the sole cross-module alias master.

Do not create another alias table unless Step 2 proves the existing catalogue/contract cannot represent the required normalization.

### D6. Explicit final-source command

Design-only API:

`POST /v2/tenants/{tenant_id}/journeys/{journey_id}/audit/final-source/confirm`

Responsibilities:

1. authorize Journey/project scope;
2. parse If-Match and acquire Journey aggregate lock;
3. require Booking submitted + Review `VERIFIED`;
4. require Delivery submitted + Review `VERIFIED`;
5. reject a duplicate/conflicting finalization under idempotency/version rules;
6. load persisted reviewed Booking + Delivery candidate rows;
7. normalize source identity only through approved canonical mapping;
8. resolve scalar attributes only through explicit stage-aware rules;
9. persist `POST_DELIVERY` resolution rows with selected reviewed-field reference + value snapshot;
10. set/retain `POST_DELIVERY.audit_state='IN_PROGRESS'`;
11. create/reuse one idempotent post-Delivery rule workflow task;
12. return finalization version/count + rule task/status.

Existing `GET /audit/source-comparison` remains read-only. A later read endpoint can expose persisted final resolutions/status.

### D7. Post-Delivery rule/report gate

Proposed lifecycle:

`FINAL_SOURCE_CONFIRMED`
-> `POST_DELIVERY audit IN_PROGRESS`
-> idempotent rule task
-> evaluations/findings
-> task `COMPLETED`
-> `POST_DELIVERY audit COMPLETE`
-> final report eligible.

`RETRY_WAIT`, failure or `DEAD_LETTER` keeps report unavailable with explicit status.

The rule engine internals are out of scope; this is only the integration/status boundary.

## 5. Genuinely new structures

### Proven necessary new tables

**NONE.**

Current evidence supports small extensions/reuse rather than new generic persistence structures.

### Repeated typed Invoice entity

**UNKNOWN — not approved.**

Business decision confirms invoice multiplicity, but current evidence does not prove a typed Invoice table is necessary. Multiple invoice documents are already distinct and can source scalar attributes.

Create a typed repeated Invoice entity only if the exact final-report workbook or approved rules require invoice-level repeated business rows beyond document provenance.

## 6. Database impact — design level

### Proposed additive migration only

Potential scope:

- add `source_reviewed_field_id` to `journey_attribute_resolutions`;
- add `resolved_value_snapshot` to `journey_attribute_resolutions`;
- add tenant-safe FK/index required for selected reviewed-field lookup;
- preserve existing rows/backward compatibility;
- preserve runtime append-only semantics.

No destructive migration, no generic report table, no Invoice table, no Payment redesign.

### Canonical source rules

No database change is currently justified solely for aliases. First validate authoritative DI/catalogue keys in Step 2.

If the existing `document_capture_v2_source_truth_rules` shape can represent the approved final-source rules, seed/use it rather than creating a duplicate configuration table. If not, return to change control with the exact proven deficiency.

## 7. API impact — design level

Additive only:

- one final-source confirm command;
- optionally one persisted final-source/status read endpoint;
- no mutation side effects on existing GET comparison;
- no breaking change to Booking/Delivery Review Confirm;
- no Web contract work until backend contract is frozen.

Required command behaviors:

- authorization;
- If-Match/version conflict;
- idempotency;
- aggregate lock;
- missing Review verification conflict;
- unresolved source-policy conflict/status;
- safe events with references/counts, never raw PII payloads.

## 8. Acceptance-test plan

### A. Persistence / schema

- existing `journey_attribute_resolutions` rows remain valid after additive migration;
- selected reviewed-field FK cannot point across tenant/Journey incorrectly;
- `POST_DELIVERY` rows are append-only to runtime;
- resolved value snapshot exactly equals selected reviewed effective value at finalization time.

### B. Finalization gates

- fail when Booking Review is not VERIFIED;
- fail when Delivery Review is not VERIFIED;
- fail/return deterministic unresolved result when required source precedence is not configured;
- idempotent replay returns same committed finalization;
- stale If-Match fails;
- concurrent finalization cannot create conflicting winner sets.

### C. Source selection

- Booking-only field cannot be overridden by unrelated Delivery evidence;
- high-confidence lower-authority source cannot beat explicitly higher-authority source;
- same source family in Booking and Delivery with disagreement does not silently resolve without an explicit stage rule;
- agreement across sources remains explainable and references selected reviewed row;
- rejected reviewed rows are ineligible final candidates;
- unknown/unmapped reviewed rows remain durable but are not guessed into an attribute.

### D. Repeated entities

- multiple payments survive finalization as multiple rows;
- Booking and Delivery receipts remain separately traceable;
- multiple invoice documents survive finalization;
- multiple same-type invoices remain distinct;
- scalar resolution may reference one invoice fact without merging/deleting sibling invoice documents.

### E. Canonical identity

After Step 2 supplies authoritative keys:

- Booking Docket/Form maps consistently through requirement -> DI classification -> Review -> typed materializer;
- Insurance Cover maps consistently;
- receipt family maps consistently while preserving requirement/stage purpose;
- PAN/Aadhaar aligned paths regress cleanly;
- unapproved alias remains unresolved, not fuzzy-matched.

### F. Rule/report gate

- final-source commit creates/reuses exactly one post-Delivery rule task per finalization version;
- report readiness false while rule task is READY/IN_PROGRESS/RETRY_WAIT/DEAD_LETTER;
- report readiness true only after successful task completion + POST_DELIVERY audit complete;
- evaluations/findings remain separately queryable and explainable.

### G. Final report

Blocked until actual 152-field workbook is available. Then add contract tests asserting every output column has one explicit owner/projection rule and no column is silently fabricated.

## 9. Implementation sequence after approvals

### Phase 1 — Audit Core final-source persistence

Prerequisite: separate `APPROVE WRITE`.

1. additive resolution-ledger migration;
2. stage-aware durable candidate loader;
3. final-source rule adapter using approved source mapping only;
4. final-source confirm command;
5. persisted read/status endpoint if required;
6. focused DB/API tests.

### Phase 2 — canonical contract alignment

Only after Step 2 DI validation identifies exact canonical keys/aliases.

1. align Audit Core catalogue/resolver/materializer constants/seed data to approved canonical keys;
2. reuse source-truth configuration where adequate;
3. add regression tests for known families.

Do not invent aliases in Phase 1.

### Phase 3 — rule-run boundary

1. create/reuse post-Delivery task type/effect key;
2. connect final-source commit to task creation;
3. add completion/readiness transition tests;
4. keep rule evaluator internals out of this stabilization scope unless separately approved.

### Phase 4 — final-report projection

Only after the actual 152-field workbook is mapped.

1. build exact output owner matrix;
2. decide repeated Invoice typed entity only if contract proves necessary;
3. implement projection/snapshot/output;
4. gate on completed post-Delivery rule run;
5. verify representative E2E output against workbook.

## 10. Rollback / containment boundaries

- investigation branch currently changes documentation only;
- later migration must be additive and independently reversible before dependent finalization data exists;
- new endpoint is additive and can be disabled without changing Booking/Delivery capture semantics;
- canonical-key alignment must be isolated from source-value persistence so alias rollback does not delete reviewed facts;
- rule task integration must not make Delivery business progression unrecordable;
- final-report projection remains downstream and must not become the operational persistence model.

## 11. External blockers / UNKNOWN

### Blocker 1 — actual 152-field final-report workbook

**Required to close the report-field/business-owner mapping.**

Current supplied artifacts do not provide the exact 152-column contract named by the Master.

Until supplied/identified:

- exact output columns/order = UNKNOWN;
- exact owner/projection per output = UNKNOWN;
- repeated Payment/Invoice output shape = UNKNOWN;
- typed repeated Invoice necessity = UNKNOWN.

### Blocker 2 — authoritative DI canonical document keys / aliases

Audit Core evidence proves fragmented vocabulary but cannot establish which classified key DI publishes for the disputed families.

Step 2 must validate at minimum:

- Booking Docket/Form;
- PAN alias behavior;
- Customer DMS / Retail Invoice family;
- Tally / Tax Invoice family;
- Insurance Cover family;
- Booking minimum-payment proof / Dealer Receipt / Delivery Payment Receipt family.

### Blocker 3 — exact post-Delivery precedence for overlapping scalar sources

Audit Core can implement the stage-aware mechanism, but current evidence does not authorize final winner values for all overlapping attributes.

Step 2/business source-truth validation must supply explicit versioned precedence/disagreement rules. Until then these remain `UNKNOWN`, not implementation defaults.

## 12. Step-1 design conclusion

### Reuse unchanged

- Journey/stage model;
- reviewed-field persistence;
- typed domains where owners already exist;
- repeated Payments;
- document/evidence identity;
- workflow tasks/events;
- audit evaluations/findings;
- POST_DELIVERY stage status.

### Smallest extension

- `journey_attribute_resolutions`: selected reviewed-field reference + final value snapshot;
- additive final-source confirm/read API;
- stage-aware source-rule consumption;
- post-Delivery task/readiness wiring.

### New structures

- **none proven**;
- repeated typed Invoice entity remains **UNKNOWN**.

### Not safe to implement by assumption

- blanket Delivery precedence;
- confidence/recency as business authority;
- guessed canonical aliases;
- generic 152-field final table;
- one global final Invoice;
- final-report columns without the actual workbook.

This package closes the **Audit Core structural design question**. Full Step 1 cannot be called complete until the exact final-report workbook/business-owner mapping is available, because that is an explicit Master/Plan deliverable and controls the remaining Invoice/report decision.
