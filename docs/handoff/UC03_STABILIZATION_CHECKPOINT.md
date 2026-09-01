# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — post-Delivery final-source investigation/design, READ-ONLY application/schema**  
Repository: **`verigence-audit-core` only**  
Investigation branch: `investigation/uc03-post-delivery-final-source`  
Branch starting `dev` SHA: `10701bf9968968d0efe4920b9230c2ed2664bd5f`  
Mode: **DESIGN/DOCUMENTATION ONLY — no schema/application writes authorized in this unit**

## Recovery order

Read `UC03_STABILIZATION_MASTER.md` → this checkpoint → `UC03_STABILIZATION_PLAN.md` → continue from `NEXT ACTION`.

Do not reconstruct completed work from chat history or broadly rescan repositories.

## Locked business decisions

- One Journey; Booking and Delivery are stages of that Journey.
- Persist Booking document-derived facts at Booking Review and Delivery document-derived facts at Delivery Review.
- Every non-null/non-empty DI field participating in Review must have durable Audit Core representation after Review, even without a typed owner.
- Unchanged reviewed value: effective value = DI extracted value.
- Changed reviewed value: preserve original DI value/provenance and persist confirmed effective value.
- Existing typed Audit Core business owners remain the operational projection layer where explicitly supported.
- Unknown/new fields survive generically; do not create speculative typed columns.
- Repeated documents and multiple same-type documents remain distinct by DI document/fact identity.
- A Journey may have multiple Payments/Receipts and multiple Invoices, including multiple invoices of the same business type.
- Exact canonical aliases must come from authoritative contracts/catalogues; never invent aliases.
- Final report is a projection of resolved Journey state and is gated behind post-Delivery final-source resolution + successful post-Delivery rule run.

## Completed persistence implementation baseline

PR **#135 — UC03: persist all reviewed DI fields for Booking and Delivery** is merged to `dev`.

Merge commit: `ab0cf4c6a3e97cf70e482d0afdb6ae4c0ada6dd1`.

The repository deployment workflow recorded live baseline commit `10701bf9968968d0efe4920b9230c2ed2664bd5f`, whose parent is the merge commit.

Post-merge `dev` verification completed successfully:

- package build: PASS;
- Ruff: PASS;
- fresh PostgreSQL `alembic upgrade head`: PASS, including `0051_uc03_lossless_review_fields`;
- full pytest suite: PASS (`366 passed` in the verified PR/dev run);
- Railway DEV deployment: PASS;
- fresh deployment verification: PASS;
- deployed-service smoke test: PASS.

Completed persistence units remain:

1. shared lossless reviewed-field persistence foundation using existing `journey_document_extracted_fields`;
2. legacy/direct Booking Review persists all populated reviewed fields;
3. Booking V2 Review Confirm persists all populated fields and no longer blocks accepted unknown fields solely for lacking a typed owner;
4. Delivery V2 explicit Review Confirm persists all populated Delivery fields before advancing Delivery PC verification to `VERIFIED`.

Do not reopen these unless contradictory evidence appears.

## Current Step-1 investigation — completed meaningful units

### Unit A — smallest post-Delivery persistence shape

**Status: INVESTIGATED / DESIGN PROPOSED; no implementation approval**

Recorded in:

`docs/uc-003-booking-delivery-audit/UC03_POST_DELIVERY_FINAL_SOURCE_DESIGN_2026-09-01.md`

Verified design direction:

- final source must consume durable reviewed Audit Core rows, not fresh/live DI values;
- existing `journey_attribute_resolutions` is the strongest reuse candidate for `POST_DELIVERY` final resolutions;
- smallest later extension is a direct reference to the selected reviewed field plus a resolved value snapshot;
- no generic 152-field final-state table is justified;
- `journey_stage_states.POST_DELIVERY` + existing workflow tasks/evaluations can provide the rule-run status/gate; no generic rule-run status table is justified;
- finalization should require both Booking and Delivery Review `VERIFIED`;
- existing `GET /audit/source-comparison` remains evidence/read-only and is not a final commit path.

### Unit B — stage-aware final-source source-rule matrix

**Status: INVESTIGATED; evidence boundary confirmed**

Recorded in:

`docs/uc-003-booking-delivery-audit/UC03_POST_DELIVERY_SOURCE_RULE_MATRIX_2026-09-01.md`

Verified findings:

- current `AttributeCandidate` does not carry stage identity;
- current resolver ranks by document type, then confidence, then deterministic tie-break, so it cannot safely serve as the final post-Delivery resolver unchanged;
- no repository evidence supports a blanket `DELIVERY wins`, `latest wins`, or `highest confidence wins` rule;
- Booking-only operational fields are deterministic and do not require Booking-vs-Delivery resolution;
- identity source families are explicitly prioritized by document authority, but same-family Booking-vs-Delivery disagreement precedence remains **UNKNOWN**;
- overlapping vehicle/commercial fields (Model, Variant, Color, Ex-Showroom, TCS, Total Price, Discount, Net Amount, Insurance Amount) have legitimate source families but post-Delivery stage precedence remains **UNKNOWN**;
- Payments/Receipts are repeated Journey entities and must not be collapsed to one scalar winner;
- Invoices are repeatable documents and must not be collapsed to one global `final invoice`; whether a typed repeated Invoice owner is needed remains **UNKNOWN** pending report/rule ownership;
- legacy Status/Delivery Date/Audit fields should come from workflow/event/audit owners, not source-resolution rows.

### Unit C — canonical document identity / alias assessment

**Status: INVESTIGATED; VERIFIED GAPS identified; exact DI aliases remain UNKNOWN**

Recorded in:

`docs/uc-003-booking-delivery-audit/UC03_CANONICAL_DOCUMENT_IDENTITY_ASSESSMENT_2026-09-01.md`

Verified runtime behavior:

- V2 capture reconciles DI `classifiedDocumentTypeKey` to requirement `document_type_key` by exact equality;
- the current runtime does not consult `document_capture_v2_source_truth_rules` during capture reconciliation;
- Review uses the classified key as the candidate source key;
- typed materializers also contain their own document-type constants/sets.

Family assessment:

- Booking Docket/Form: **VERIFIED GAP** — published `booking_docket`; typed materializer expects `booking_form`; resolver accepts both;
- PAN: **ALIGNED canonical / alias UNKNOWN** — `pan_card` aligned; `pan` is extra unverified alias;
- Aadhaar: **ALIGNED**;
- Customer DMS Invoice: **ALIGNED canonical**;
- Tally Tax Invoice: **ALIGNED canonical / extra aliases UNKNOWN**;
- Wholesale Invoice: **ALIGNED catalogue; per-attribute final-source use UNKNOWN**;
- Cost Sheet: **ALIGNED**;
- Insurance: **VERIFIED GAP** — published document type `insurance_cover`; resolver uses `insurance_cover_note` / `insurance_policy`;
- Payment/Dealer Receipt: **VERIFIED GAP** — Booking `minimum_booking_payment_proof`, Delivery `payment_receipt`, but typed receipt path expects `dealer_receipt`.

Corrected design conclusion:

- `document_capture_v2_source_truth_rules` can be a reuse candidate for **attribute-level final-source policy**;
- it is not currently a runtime alias normalizer and current evidence does not prove it is sufficient as the sole cross-module document alias master;
- prefer one authoritative canonical DI/catalogue document key contract and align Audit Core catalogue/resolver/materializers to it;
- do not create another alias table until Step 2 proves existing cross-module catalogue/source-truth structures cannot represent the contract.

### Unit D — final-report artifact verification

**Status: BLOCKED / exact output contract UNKNOWN**

Inspected supplied project artifacts include:

- `SPR Details - Copy (2).xlsx` — Booking/process UI/form flow;
- `SPR_Tool_Process_SubProcess_Activity_Details (1).xlsx` — process/rule/activity catalogue and report families;
- dealership audit scope presentation;
- supplied Booking/Delivery source PDFs/images.

These provide useful process/report-family evidence but do **not** establish the exact 152-field final-report workbook/column contract named by the Master.

Therefore still `UNKNOWN`:

- exact 152 output fields and ordering;
- exact typed/resolved/computed owner for every output column;
- exact repeated Payment/Invoice report expansion/aggregation;
- whether the output requires a typed repeated Invoice business entity.

## Corrected / withdrawn assumptions

- Do not add another generic raw/final fact table: existing reviewed-field persistence + sparse final resolution is sufficient by current evidence.
- Do not assume Delivery needs to overwrite Booking values merely because it is later.
- Do not assume confidence is business authority; it is extraction-quality evidence.
- Do not assume one final Invoice or one Invoice per Journey.
- Do not treat the 123-field matrix as the final 152-field report workbook; it is a provisional capture/source inventory.
- Do not treat `document_capture_v2_source_truth_rules` as an already-working canonical alias mechanism; current capture reconciliation does not consume it.

## Remaining Step-1 items

1. produce the consolidated Step-1 gap/design package: reuse vs extension vs genuinely new structures, database/API impact, implementation sequence and acceptance tests;
2. identify/obtain the actual 152-field final-report workbook and map every output to typed/resolved/repeated/computed ownership;
3. retain typed repeated Invoice structure as **UNKNOWN** until #2 or an approved rule proves it necessary;
4. freeze exact overlapping-source precedence only where authoritative evidence exists; leave the rest `UNKNOWN` for the later DI/business contract-validation step rather than guessing.

## NEXT ACTION

**One investigation/design unit only:** consolidate the completed Audit Core Step-1 evidence into a single implementation-ready gap/design package without writing schema/application code.

The package must state:

1. structures to reuse unchanged;
2. structures requiring the smallest extension;
3. genuinely new structures, if any;
4. exact verified canonical-identity gaps and what remains `UNKNOWN`;
5. final-source confirm API/database impact at design level;
6. rule-run/report readiness gate at design level;
7. acceptance-test plan;
8. implementation sequence and rollback boundaries;
9. explicit external blockers — especially the actual 152-field final-report workbook and exact DI canonical/source mapping values.

Then update this checkpoint and stop before implementation approval. Do **not** enter DI or Web and do not write migration/application code in this unit.

## Anti-stuck rule

If a direct path does not answer the current evidence question after a small number of attempts, mark it `UNKNOWN` and pivot. Do not recursively rescan completed repositories.
