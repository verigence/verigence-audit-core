# UC03 Booking + Delivery Stabilization — Master Charter

Status: ACTIVE GOVERNING CHARTER  
Scope: UC03 Booking + Delivery stabilization  
Primary repositories: `verigence-audit-core`, `verigence-di`, `verigence-web`  
Security: existing Security implementation is source of truth; do not redesign it for UC03.

## 1. Purpose

This is a stabilization exercise. The objective is to reach one stable, evidence-backed UC03 final state for:

1. document-derived data capture and durable Audit Core business persistence;
2. Booking + Delivery review behaviour;
3. final source-of-truth resolution and final reporting;
4. UC03 V2 product usability.

Do not use this work to redesign unrelated modules, clean repositories, upgrade dependencies, or introduce speculative architecture.

## 2. Evidence discipline — mandatory

Every technical conclusion must be classified as one of:

- **VERIFIED FACT** — directly supported by inspected code, schema, migration, contract, test, runtime or deployment evidence.
- **INFERENCE** — derived from verified facts but not directly proven.
- **PROPOSED CHANGE** — recommended work that is not current-state fact.
- **UNKNOWN** — evidence has not established the answer.
- **BUSINESS DECISION** — explicitly agreed product behaviour; implementation evidence may still be pending.

Never silently convert an inference into fact. Never invent document types, DI fields, mappings, API contracts, database columns, business rules, cardinality, UI behaviour or deployment state. If evidence is missing, write `UNKNOWN`.

Correct earlier assumptions when newer evidence disproves them.

## 3. Focus / anti-stuck rules

- Work **one repository at a time**.
- Work **one evidence question at a time**.
- Do not recursively scan entire repositories, histories or unrelated directories without a concrete evidence gap.
- Prefer known UC03 files, migrations, contracts and tests over broad search.
- If one search/path is unproductive after a small number of attempts, pivot to the next direct evidence source instead of repeating it.
- Do not stop merely to report that work is ongoing; make substantive progress first.
- Do not re-investigate a VERIFIED conclusion unless a contradiction appears.
- Do not follow unrelated technical debt.

## 4. Change control

Investigation and planning do not authorize implementation.

- `APPROVE WRITE: <scope>` — permission to modify only that scope.
- `APPROVE MERGE: <repository / PR>` — merge permission only.
- `APPROVE DEPLOY: <repository / environment>` — deploy permission only.

Write approval does not imply merge. Merge does not imply deploy.

When permission is uncertain: **do not change it**.

## 5. Locked UC03 business decisions

### 5.1 One Journey

There is one employee-facing Journey. Booking and Delivery are stages/statuses of that Journey, not separate employee-facing cases.

The underlying data may retain stage provenance (`BOOKING` / `DELIVERY`), but normal users should see one evolving Journey.

### 5.2 Capture by stage; do not wait for final report

Booking facts are captured and persisted when Booking documents are reviewed.

Delivery facts are captured and persisted when Delivery documents are reviewed.

A later Delivery source must not destroy the historical Booking source fact.

### 5.3 DI is the document extraction layer; Audit Core is the operational business source after Review

PC/manual entry is not the focus of this stabilization.

Where information is available from documents, DI extracts it. Raw/extracted DI fields are shown on Review screens where evidence and correction are required.

After Review, normal Journey screens should use Audit Core business data rather than depend on raw DI field APIs.

Original DI provenance must remain traceable.

### 5.4 Review/effective value

For a reviewed document-derived field:

- unchanged: effective business value = DI extracted value;
- changed during Review: retain original DI value/provenance and persist the confirmed effective value.

No accepted/populated document-derived field required by the Journey/final report may silently disappear.

### 5.5 Reuse the existing Journey domain model

Avoid over-engineering.

Reuse existing Audit Core structures first, including Customer, Booking, Vehicle, Commercials, Discounts, Payments, Finance, Insurance, Registration, Trade-In, Delivery, Evidence and Findings where they already represent the requirement correctly.

Add a field/relationship or new structure only when the existing model genuinely cannot represent the requirement cleanly.

Do not create parallel Booking/Delivery copies of the same business entity merely because data arrived at different stages.

### 5.6 Repeated business entities

A Journey may have multiple:

- payments / dealer receipts;
- invoices;
- invoices of the same business type;
- supporting documents of other configured types.

Document identity must remain distinct. Do not assume `journey + document_type` is unique when business cardinality allows repetition.

Payments/receipts should remain one repeated Journey collection and may be captured during Booking or Delivery.

Multiple invoices should be represented under the same Journey rather than flattened into one arbitrary invoice value.

### 5.7 Canonical document identity / aliases

Source-of-truth logic must use canonical document identity, not presentation names.

Known business examples include:

- DMS Invoice / Retail Invoice — same business document family;
- Tally Invoice / Tax Invoice — same business document family.

Exact canonical keys must be taken from the existing document catalogue/contracts; do not invent them.

Alias handling must be uniform across DI → Audit Core → Web → reporting. Preserve the original classified/source identity for audit evidence.

### 5.8 Rule engine boundary

The rule engine is a separate workstream. Do not design its internals during data-model stabilization.

Reserve only the minimal integration/status boundary needed for:

- Booking rule execution after Booking facts are persisted;
- post-Delivery rule execution after final-source resolution.

Business sequence:

`Booking Capture → Normalize Document Identity → Persist Booking Facts → Booking Rule Run (synchronous boundary) → Journey continues`

`Delivery Capture → Normalize Document Identity → Persist Delivery Facts → Compare Legitimate Sources → Resolve/Persist Final Source of Truth → Post-Delivery Rule Run (synchronous boundary) → Final Report`

The final report must not be created until the post-Delivery rule run completes successfully according to the later approved rule-engine contract.

### 5.9 Rule-friendly Audit Core model

The Audit Core business model should make later anomaly checks simple and fast.

Rules should query business structures such as payments, invoices, discounts, commercial lines, vehicle, finance, insurance and resolved values. Rules should not have to parse documents or understand raw DI field names.

Do not persist speculative derived values unless they materially simplify an approved rule/report requirement.

### 5.10 Final report

The supplied final-report spreadsheet is the output contract. The business target has been described as a 152-field extract; the exact workbook field mapping must be verified from the supplied workbook before implementation.

The report is a projection/snapshot of the resolved Journey business state, not the persistence model itself.

For fields with multiple legitimate sources, Audit Core must support deterministic final-source resolution and retain enough provenance to explain which source/value was selected.

Final report creation occurs only after Booking + Delivery capture, final-source resolution and completion of the post-Delivery rule run.

## 6. Final-state design test

For every proposed data-model change ask:

1. Does an existing Journey entity already represent this?
2. Can a small extension represent it correctly?
3. Is a genuinely new structure required?
4. Does it make capture, rule evaluation or reporting materially easier?

If the answer to #4 is no, do not add it.

## 7. Required workflow for every stabilization activity

Use this order:

`FINAL BUSINESS STATE → CURRENT IMPLEMENTATION → VERIFIED GAP → ROOT CAUSE → SMALLEST SAFE DESIGN → ACCEPTANCE TEST → IMPLEMENTATION → END-TO-END VERIFICATION`

Do not patch a symptom before the final state is understood.

## 8. Definition of “fixed”

Do not say `FIXED` because unit tests, CI, HTTP 200, a migration, or one document worked.

A scope is FIXED only when its agreed final-state acceptance test passes end-to-end.

Until then use accurate states such as `INVESTIGATED`, `ROOT CAUSE CONFIRMED`, `IMPLEMENTED`, `TESTED`, `CI PASSED`, `DEPLOYED`, `VISUALLY VERIFIED`, `E2E VERIFIED`.

## 9. Recovery rule

If context is lost or work appears stuck:

1. Read this Master Charter.
2. Read `UC03_STABILIZATION_CHECKPOINT.md`.
3. Read `UC03_STABILIZATION_PLAN.md`.
4. If on an implementation branch, read its `UC03_IMPLEMENTATION_CONTEXT.md` derived from the template.
5. Continue from `NEXT ACTION`; do not reconstruct the project from chat history and do not broadly rescan completed repositories.
