# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — Audit Core data-model and persistence stabilization design**  
Mode: **READ-ONLY for application/schema code**  
Current repository: **`verigence-audit-core` only**  
Stabilization branch: `fix/uc03-persist-all-di-fields-booking-delivery`  
Technical baseline SHA before handoff-document commits: `92fa9b686132a847fd9a5832402852f7c8e36a8a`

Documentation commits after that SHA are governance/handoff only and are not application changes.

## 1. Read-first instruction

Before continuing:

1. Read `UC03_STABILIZATION_MASTER.md`.
2. Read `UC03_STABILIZATION_PLAN.md`.
3. Continue from `NEXT ACTION` below.
4. Do not broadly rescan repositories already investigated unless a contradiction requires it.

## 2. Locked business decisions

**BUSINESS DECISION**

- One employee-facing Journey; Booking and Delivery are stages/statuses of that Journey.
- Capture/persist Booking document-derived facts at Booking Review; do not wait for Delivery/final report.
- Capture/persist Delivery document-derived facts at Delivery Review.
- Raw DI extracted fields/evidence belong on Review screens. After Review, normal Journey views should use Audit Core business data.
- Preserve original DI provenance even when an effective/reviewed value is materialized.
- Reuse existing Journey entities first; avoid parallel Booking/Delivery copies of common business structures.
- Payments/receipts are repeated Journey entities and can be captured during Booking or Delivery.
- Multiple invoices, including multiple invoices of the same valid type, must be supported.
- Canonical document identity must handle naming ambiguity uniformly; examples agreed: DMS Invoice/Retail Invoice and Tally Invoice/Tax Invoice represent common business document families. Exact canonical keys must come from existing contracts/catalogues.
- A later Delivery source must not erase the Booking source fact.
- After Delivery: compare legitimate sources → resolve/persist final source of truth → run post-Delivery rule execution → only then create final report.
- Booking has a synchronous rule-run boundary after Booking facts are persisted. Rule-engine internals are out of current scope; keep only a minimal placeholder/status/reference contract.
- Final report uses the supplied spreadsheet format. Business target has been described as 152 fields; exact workbook mapping still requires verification.
- Data structures should make later anomaly/discount/payment/mismatch rule evaluation simple and fast without requiring rules to understand DI raw field names.
- Avoid over-engineering; extend existing structures only where genuinely required.

## 3. Current verified Audit Core facts

### 3.1 Runtime installation

**VERIFIED FACT**

The earlier concern that newer UC03 installers were not active was disproved.

`src/audit_core/main.py` calls `install_uc03_v2_capture_business_rules()`. That installer activates the UC03 Booking commercial/review/persistence extensions before route use.

**Do not reopen the “installer not wired” defect unless new contradictory evidence appears.**

### 3.2 Booking reviewed-value persistence exists

**VERIFIED FACT**

Current Audit Core contains explicit Booking reviewed/effective-value persistence for known Booking Form/identity/receipt fields, including later Booking commercial extensions.

Relevant evidence includes current UC03 Booking review/materialization modules and migrations `0048` / `0050`.

The current Booking Review confirm path includes explicit ownership/materialization logic rather than silently accepting a known mapped field with no Core owner.

### 3.3 Delivery capture does not currently mirror Booking field materialization

**VERIFIED FACT**

Current Delivery V2 capture persists Journey/stage/document linkage, classification/capture state and Delivery submission state.

The inspected Delivery submission path does not currently materialize the extracted Delivery document fields into an equivalent durable reviewed/effective business-field persistence path.

### 3.4 Current post-Delivery comparison is live-source oriented

**VERIFIED FACT**

The current post-Delivery source-comparison flow reads Booking/Delivery document facts from DI for comparison rather than relying on a persisted final resolved Journey snapshot.

This is useful for Review/audit evidence, but it is not yet the agreed final-report source-of-truth model.

### 3.5 Existing generic/legacy persistence structures require careful reuse assessment

**VERIFIED FACT**

Audit Core already contains structures such as `journey_document_extracted_fields` and `journey_attribute_resolutions` from earlier UC03 iterations.

Prior migrations changed the semantics of the extracted-field structure toward correction/provenance usage rather than blindly copying unchanged DI values. Attribute-resolution structures retain source references but current evidence has not yet established a complete persisted post-Delivery final-value snapshot implementation.

**PROPOSED DIRECTION, NOT YET APPROVED:** reuse or minimally extend existing structures if they satisfy the newly agreed business requirement; do not introduce a second parallel persistence model without first proving it is necessary.

### 3.6 Final report persistence/export

**VERIFIED FACT from current repository inspection**

No current UC03 implementation has yet been identified that persists/generates the agreed final 152-field report snapshot from a completed post-Delivery rule run.

If later evidence finds such an implementation, correct this checkpoint immediately.

## 4. Prior cross-repository evidence retained for later steps

These findings were already established during earlier read-only investigation. They are recorded here to avoid rescanning now; Step 1 remains Audit Core only.

### DI

**VERIFIED FACT (prior investigation)**

- The active Schema V2 extraction worker persists configured/published profile fields into DI fact/current-value structures with provenance.
- The trusted Audit Core field API returns current document field values rather than a UC03-specific narrow allow-list.
- Booking Form extraction is comparatively mature and includes expanded commercial fields.
- Some additional document profiles/mappings exist only in DRAFT/not-processing state.

Detailed DI revalidation belongs to Step 2 after the Audit Core design is fixed.

### Web

**VERIFIED FACT (prior investigation)**

- Current Delivery Review consumes the Audit Core source-comparison API and displays mapped and unmapped extracted values/evidence.
- Current normal Journey 360 views already consume Audit Core business structures for many domains.
- Current post-Delivery source comparison is presentation of the live comparison contract, not the agreed durable final-report snapshot.

Web implementation belongs to the later Web stabilization step, not Step 1.

## 5. Step-1 open questions — do not guess

### A. Final-report field contract

**UNKNOWN / NOT YET VERIFIED FROM WORKBOOK**

- exact field names/order in the supplied final-report workbook;
- whether workbook contains exactly 152 output fields in the active format;
- which fields are direct business facts, repeated-entity summaries, computed values, rule outputs, or final-source-resolved values.

### B. Existing Invoice model/cardinality

**OPEN INVESTIGATION**

Determine whether the existing Audit Core model can represent multiple invoices cleanly, including multiple same-type invoices and invoice-specific provenance, or whether a minimal Journey invoice collection is genuinely required.

### C. Payment/Receipt reuse

**OPEN INVESTIGATION**

Confirm existing Payment structures, current uniqueness/cardinality and provenance are sufficient for multiple dealer receipts captured across Booking/Delivery. Prefer reuse over new tables.

### D. Canonical document alias mechanism

**OPEN INVESTIGATION**

Determine whether the existing document catalogue / requirement policy / type mapping already has an appropriate alias/family mechanism. If yes, reuse it. If no, propose the smallest extension. Do not invent canonical keys.

### E. Delivery reviewed/effective persistence

**OPEN INVESTIGATION**

Define the smallest change that makes reviewed Delivery document facts durable in Audit Core and available to normal Journey views without creating unnecessary parallel models.

### F. Final source-of-truth persistence

**OPEN INVESTIGATION**

Determine whether existing resolution structures can be minimally extended to persist:

- canonical/report field identity;
- selected effective value;
- selected source document/fact/version;
- resolution status/reason/version;
- competing source provenance where required.

Do not design a large generic platform if a small extension can satisfy this.

### G. Rule-run placeholder

**OPEN INVESTIGATION**

Check whether an existing execution/audit/status structure can represent Booking and post-Delivery rule run `PENDING/RUNNING/COMPLETED/FAILED` + execution reference/version. Reuse it if suitable; do not design the rule engine.

## 6. NEXT ACTION

Stay in `verigence-audit-core` and complete Step 1 in this order:

1. inspect existing Invoice, Payment/Receipt, document catalogue/type, source-resolution and execution-status structures only;
2. verify the supplied final-report workbook field contract and map fields to existing Audit Core business owners without inventing mappings;
3. classify each requirement as `REUSE EXISTING`, `SMALL EXTENSION`, `NEW STRUCTURE REQUIRED`, or `UNKNOWN`;
4. define the smallest Delivery persistence and final-source-resolution design;
5. produce the evidence-backed Step-1 matrix + implementation sequence;
6. stop before application/schema code changes.

## 7. Anti-stuck instruction

If a file/search path does not answer the current evidence question, do not keep drilling recursively. Record the result, pivot to the next direct schema/migration/module/test evidence source, and continue.

If context is lost, do not read all repositories. Restart from this checkpoint and the Master Charter.

## 8. Step-1 stop condition

Do not move to DI/Web or implementation until Step 1 has an evidence-backed answer for:

- reused Journey structures;
- repeated payments/receipts;
- multiple invoices;
- canonical document alias handling;
- Delivery reviewed/effective persistence;
- final-source persistence;
- rule-run placeholder;
- final-report field ownership/mapping;
- exact smallest proposed data-model/API/migration impact;
- acceptance tests.
