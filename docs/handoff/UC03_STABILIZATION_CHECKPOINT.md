# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — Audit Core investigation/design COMPLETE; implementation approval gate**  
Repository: **`verigence-audit-core` only**  
Investigation branch: `investigation/uc03-post-delivery-final-source`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Mode: **INVESTIGATION / DESIGN ONLY — no schema/application writes authorized yet**

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → continue from `NEXT ACTION`.

Do not reconstruct completed work from chat history or broadly rescan completed repositories.

## Locked business decisions

- One Journey; Booking and Delivery are stages of that Journey.
- Booking document-derived facts persist at Booking Review; Delivery document-derived facts persist at Delivery Review.
- Every non-null/non-empty reviewed DI field has durable Audit Core representation, even without a typed owner.
- Unchanged effective value = DI value; changed value preserves DI original/provenance plus reviewed effective value.
- Repeated Payments/Receipts and repeated/multiple Invoice documents remain distinct under the same Journey.
- Exact canonical technical aliases/keys must come from authoritative contracts; never invent them.
- Final report is a projection of resolved Journey state and is blocked until post-Delivery final-source resolution + successful post-Delivery rule run.
- The user-supplied **Final Source of Truth list is authoritative at business-source-label level** for the final report.
- `NA` in that list means the output is not document-derived; it does not mean no value.

## Completed implementation baseline

PR #135 is merged to `dev`.

- merge commit: `ab0cf4c6a3e97cf70e482d0afdb6ae4c0ada6dd1`;
- live-baseline child commit: `10701bf9968968d0efe4920b9230c2ed2664bd5f`;
- fresh migration including `0051`: PASS;
- Ruff: PASS;
- pytest: 366 passed;
- Railway DEV deployment/fresh-deploy verification/smoke: PASS.

Completed persistence baseline:

1. shared lossless reviewed-field persistence using `journey_document_extracted_fields`;
2. legacy/direct Booking Review persists all populated reviewed fields;
3. Booking V2 Review Confirm persists all populated fields and retains unknown fields without fabricated typed owners;
4. Delivery V2 Review Confirm persists all populated Delivery fields before Delivery verification becomes `VERIFIED`.

Do not reopen these without contradictory evidence.

## Completed Step-1 investigation/design units

### A. Post-Delivery persistence shape

Document:
`docs/uc-003-booking-delivery-audit/UC03_POST_DELIVERY_FINAL_SOURCE_DESIGN_2026-09-01.md`

Verified direction:

- final resolution consumes persisted reviewed Audit Core rows, not live DI;
- reuse `journey_attribute_resolutions` for `POST_DELIVERY`;
- smallest extension is a resolved-value snapshot plus an optional selected reviewed-field reference;
- reuse `journey_stage_states.POST_DELIVERY`, workflow tasks/events and audit evaluations/findings;
- no generic final-state table or generic rule-run table;
- finalization gate requires Booking + Delivery Review both `VERIFIED`;
- existing source-comparison GET remains read-only.

### B. Stage-aware source-rule investigation

Document:
`docs/uc-003-booking-delivery-audit/UC03_POST_DELIVERY_SOURCE_RULE_MATRIX_2026-09-01.md`

Originally verified that the current resolver loses Booking/Delivery stage identity and that document-type → confidence → tie-break is not a safe final business rule.

**Current correction after receiving the authoritative final-source list:**

- final report outputs now have an approved **business source label** per row;
- therefore no blanket `Delivery wins`, `latest wins`, or `highest confidence wins` rule is needed for those outputs;
- the remaining unresolved part is only the technical mapping from approved business source labels to DI/source-system canonical keys and field keys.

### C. Canonical document identity assessment

Document:
`docs/uc-003-booking-delivery-audit/UC03_CANONICAL_DOCUMENT_IDENTITY_ASSESSMENT_2026-09-01.md`

Verified current vocabulary gaps include:

- Booking Docket/Form: catalogue `booking_docket` vs typed materializer `booking_form`;
- Insurance: catalogue `insurance_cover` vs resolver `insurance_cover_note` / `insurance_policy`;
- Receipt family: Booking `minimum_booking_payment_proof`, Delivery `payment_receipt`, typed receipt path `dealer_receipt`.

V2 capture currently reconciles DI classification to requirement type by exact equality and does not use `document_capture_v2_source_truth_rules` as an alias normalizer.

No new alias table is approved. Step 2 must validate authoritative DI canonical keys and then align/reuse existing structures.

### D. Consolidated Audit Core structural package

Document:
`docs/uc-003-booking-delivery-audit/UC03_STEP1_AUDIT_CORE_STABILIZATION_PACKAGE_2026-09-01.md`

This package records reuse, verified gaps, smallest final-resolution extension, final-source API boundary, rule-run gate, acceptance tests, implementation sequence and rollback boundaries.

### E. Authoritative final-report field/source/owner matrix

Document:
`docs/uc-003-booking-delivery-audit/UC03_FINAL_REPORT_FIELD_SOURCE_OWNER_MATRIX_2026-09-01.md`

**Status: COMPLETE for the user-supplied final report contract.**

The user supplied the final report field list and the corresponding Final Source of Truth list directly after workbook mounting failed.

The aligned contract contains:

- **122 physical rows** including structural blank/separator rows;
- **113 labelled output rows** excluding two `-` separators;
- **81 unique non-separator labels** because Standard/Actual and payment sections deliberately repeat labels.

This **disproves and supersedes the earlier assumption that the current final report contract must contain exactly 152 output rows**. Do not continue calling 152 an unresolved blocker unless a later authoritative workbook/version proves a different contract.

Examples of approved final sources:

- DMS Invoice Date/Number → `Tax Invoice — DMS`;
- Delivery Date → `Gate Pass`;
- Customer/KYC outputs → `Customer KYC (PAN, Aadhaar, address proof)`;
- Type of customer / Model / Model Variant → `Booking & Retail Dump`;
- registration outputs → `RTO Paper`;
- chassis → `Tax Invoice — DMS`;
- Finance Type → `Bank DO`;
- Bank Name → `Bank Statement`;
- First receipt date → `Money Receipt`;
- actual Insurance → `Insurance Cover Note`;
- actual Accessories → `Accessory Invoice — Tally / bookkeeping software`;
- actual EW → `EW Tally Invoice`;
- many actual discount values → `Customer Ledger`.

## Final report owner conclusions

### Reuse existing typed/master structures

Repository evidence confirms existing owners for the report shape:

- `bookings.booking_reference`, `booking_date`, `booking_intimated_at_utc`;
- Dealer/Outlet and Customer domain;
- `journey_products` model/variant snapshots;
- Registration and Vehicle domain;
- Finance / Insurance / Addons / Trade-In;
- `price_list_items.standard_amount`;
- `commercial_lines.standard_amount` + `actual_amount`;
- `discount_applications.standard_eligible_amount` + `actual_discount_amount`;
- repeated `payments`;
- `audit_evaluations`, `audit_findings`, `finding_remarks`, `review_decisions`;
- workflow/event metadata for report update/audit columns.

### Scalar document-derived outputs

Use sparse `POST_DELIVERY` resolution snapshots. Do not create one report table per field and do not let live DI determine final report values.

### Booking & Retail Dump outputs

`Type of customer`, `Model`, `Model Variant` are source-system/typed outputs, not DI-reviewed-field-only outputs.

Therefore the proposed `source_reviewed_field_id` on `journey_attribute_resolutions` must be **nullable**. Existing owning-domain/reference plus resolved-value snapshot can represent a typed/source-system final source.

### Payments/reconciliation blocks

The two Bank Transfer/Cash/DD/Cheque/DO/PO/Trade-in/Refund/Total sections are **repeated/aggregate report outputs**, not scalar final-source rows.

Reuse repeated Payments and source evidence; define explicit aggregation/reconciliation rules. Do not collapse them into one payment or one evidence winner.

### Error Summary / Remarks

- Error Summary → audit evaluations/findings projection.
- PC/TL/PMO Remarks → existing finding/review remark structures using actor role.

No generic remarks table is justified. Exact latest/concatenate/finding-scoped report selection remains a report-projection rule.

## Repeated Invoice decision — CLOSED for this report

**Typed repeated Invoice entity: NOT REQUIRED FOR FINAL REPORT V2 by current evidence.**

The final report requires scalar `DMS Invoice Date` and `DMS Invoice Number`, both sourced from `Tax Invoice — DMS`.

Multiple invoice documents remain distinct in durable reviewed-field storage. A `POST_DELIVERY` resolution can reference the exact selected invoice document/field without merging or deleting sibling invoices.

Do not add an Invoice table in this stabilization unit. Reopen only if a separately approved rule later proves invoice-level repeated business rows are required.

## Structural Step-1 conclusion

### Reuse unchanged

- Journey/stage model;
- durable reviewed-field persistence;
- typed business domains;
- price-list/commercial/discount/addon structures;
- repeated Payments;
- requirement/document/evidence identity;
- workflow tasks/events;
- audit evaluations/findings/review remarks;
- POST_DELIVERY stage state.

### Smallest extension after approval

1. extend `journey_attribute_resolutions` with:
   - `resolved_value_snapshot`;
   - nullable `source_reviewed_field_id` to selected reviewed field;
2. add explicit post-Delivery final-source confirm/read contract;
3. use the approved business-source matrix and reject unresolved technical mappings rather than guessing;
4. project selected commercial/domain values into existing typed owners where already supported;
5. create/reuse the post-Delivery rule workflow task and report-readiness gate;
6. implement final-report projection/aggregation in the supplied exact row order.

### New structures

**None proven necessary.**

No generic final table, no generic rule-run table, no new Payment model, no Invoice table.

## Remaining UNKNOWN / Step-2 contract items

These are no longer Audit Core structural-design blockers, but must remain explicit:

1. exact DI canonical technical key(s) for approved business labels such as `RTO Paper`, `Customer KYC`, `Money Receipt`, `Customer Ledger`, `Bank Statement`, etc.;
2. exact field-key mapping for any approved source where Audit Core cannot prove the DI emitted key;
3. exact arithmetic/aggregation formulas for the two payment/reconciliation report blocks;
4. exact report selection semantics when multiple PC/TL/PMO remarks exist.

Do not invent any of these.

## Corrected / withdrawn assumptions

- no second generic raw/final fact table;
- no blanket Delivery override;
- no confidence/recency business-authority rule;
- no one-final-Invoice assumption;
- no typed Invoice entity required by current final report;
- 123-field capture matrix is not the final report contract;
- the earlier `152-field final-report blocker` is superseded by the authoritative user-supplied 122-row / 113-labelled-output contract;
- `document_capture_v2_source_truth_rules` is not an already-working alias normalizer.

## NEXT ACTION

**Step 1 investigation/design is complete. Stop at change control.**

Ask the user for separate approval before any schema/application write for the following narrow Audit Core implementation scope:

1. additive `journey_attribute_resolutions` extension (`resolved_value_snapshot` + nullable selected reviewed-field reference);
2. persisted stage-aware/business-source-aware final-source resolver using the supplied final-report source matrix;
3. additive final-source confirm/read API with authorization, If-Match, idempotency and aggregate locking;
4. reuse existing typed commercial/domain projections—no new generic or Invoice table;
5. post-Delivery workflow task/readiness gate;
6. focused migration/API/resolution/repeated-payment/report-contract tests.

Technical DI canonical-key values that cannot be proven from Audit Core remain unresolved and must fail closed / stay `UNKNOWN` until Step 2 contract validation. Do not invent aliases during Audit Core implementation.

After Audit Core implementation is CI/DB verified and merged/deployed, move to **Step 2 — DI contract validation only**, then Web integration.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
