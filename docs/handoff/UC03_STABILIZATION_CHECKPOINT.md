# UC03 Stabilization — Current Checkpoint

Last updated: 2026-09-01  
Current activity: **Step 1 — approved Audit Core lossless reviewed-DI persistence implementation unit**  
Mode: **WRITE APPROVED only for the exact Audit Core scope in `UC03_IMPLEMENTATION_CONTEXT.md`; NO MERGE / NO DEPLOY**  
Current repository: **`verigence-audit-core` only**  
Implementation branch: `fix/uc03-persist-all-di-fields-booking-delivery-v2`  
Branch starting SHA: `724ddbc4ed11ec82195763d9b10a6f9f339bb729` (current `dev` at re-anchor)  
Previous stabilization branch: `fix/uc03-persist-all-di-fields-booking-delivery` — superseded for application work because it was 9 commits behind current `dev`; its governance files were restored byte-for-byte onto this branch.

## 1. Read-first instruction

Before continuing:

1. Read `UC03_STABILIZATION_MASTER.md`.
2. Read this checkpoint.
3. Read `UC03_STABILIZATION_PLAN.md`.
4. Read `UC03_IMPLEMENTATION_CONTEXT.md`.
5. Continue from `NEXT ACTION` below.
6. Do not broadly rescan repositories already investigated unless a contradiction requires it.

## 2. Locked business decisions

**BUSINESS DECISION**

- One employee-facing Journey; Booking and Delivery are stages/statuses of that Journey.
- Capture/persist Booking document-derived facts at Booking Review; do not wait for Delivery/final report.
- Capture/persist Delivery document-derived facts at Delivery Review.
- **Every non-null/non-empty DI field participating in Review must have durable Audit Core representation after Review. Missing typed mapping must not cause field loss.**
- For an unchanged reviewed field, effective value is the DI extracted value.
- For a changed reviewed field, preserve original DI value/provenance and persist the confirmed effective value.
- Existing typed Audit Core business owners remain the operational projection layer where explicitly supported; unknown/new fields survive generically rather than forcing speculative typed columns.
- Raw DI evidence remains traceable by DI document/fact identity even after Audit Core persistence.
- Reuse existing Journey entities first; avoid parallel Booking/Delivery copies of common business structures.
- Payments/receipts are repeated Journey entities and can be captured during Booking or Delivery.
- Multiple invoices, including multiple invoices of the same valid type, must be supported.
- Canonical document identity must handle naming ambiguity uniformly; exact canonical keys must come from existing contracts/catalogues and must not be invented.
- Alias uncertainty must not cause a non-empty DI field to be discarded.
- A later Delivery source must not erase the historical Booking source fact.
- After Delivery: compare legitimate sources → resolve/persist final source of truth → run post-Delivery rule execution → only then create final report.
- Rule-engine internals are out of current scope.
- Final report uses the supplied spreadsheet format; exact workbook mapping still requires verification.
- Avoid over-engineering; extend existing structures only where genuinely required.

## 3. Current verified Audit Core facts

### 3.1 Current-dev re-anchor

**VERIFIED FACT**

The original stabilization branch was 9 commits behind `dev`. The new implementation branch was created from `dev` SHA `724ddbc4ed11ec82195763d9b10a6f9f339bb729` before any application/schema write.

The intervening `dev` changes did not modify the reviewed-DI persistence/materialization modules relevant to this approved unit. The only UC03 application file among those commits was `src/audit_core/uc03_booking_commands.py`.

### 3.2 Runtime installation

**VERIFIED FACT**

The earlier concern that newer UC03 installers were not active was disproved. `src/audit_core/main.py` installs the current UC03 V2 capture/business-rule extensions.

Do not reopen the installer-wiring defect unless new contradictory evidence appears.

### 3.3 Existing generic persistence is real but currently correction-oriented

**VERIFIED FACT**

Migration `0031_uc03_generic_di_review_fields.py` created `auditcore.journey_document_extracted_fields` with DI document/fact lineage, extracted/modified JSON values, confidence and modification actor/time.

Current `src/audit_core/uc03_pc_generic_review.py` deliberately stores only fields with a human modification. For those rows it writes `extracted_value=NULL` and `confidence_score=NULL`; unchanged DI values are omitted.

Therefore current legacy/direct Review does not satisfy the governing lossless persistence invariant.

### 3.4 Current V2 identifiers do not fit the legacy table unchanged

**VERIFIED FACT**

Current V2 `DiFact` exposes `canonical_field_id`, `field_key`, `value`, `confidence_score`, `version_no`, page and evidence region, but not the legacy UUID `source_fact_ref` required by the existing generic table.

Current V2 document review can also have no legacy `evidence_id`, while the legacy generic table currently requires one.

Current V2 confidence is presented on the Review contract on the 0–100 scale, while the legacy generic table's existing confidence constraint was designed around 0–1.

**SMALLEST SAFE DIRECTION:** extend the existing generic table backward-compatibly for current V2 identifiers/stage provenance rather than introduce a second parallel generic field table.

### 3.5 Booking typed reviewed-value persistence exists but is not lossless

**VERIFIED FACT**

Migration `0048_uc03_di_core_field_persistence.py` and `src/audit_core/uc03_v2_review_materialization.py` persist known Booking Form, PAN, Aadhaar and Dealer Receipt reviewed values and project supported values into existing business structures.

That layer is intentionally typed/known-field oriented and must remain the additional business projection layer, not the generic lossless layer.

Current Booking V2 confirm also:

- blocks an accepted unmapped/raw field when it has no typed Core owner;
- can block a supported mapped field when no concrete typed owner is returned;
- records `rawDiValuesCopied: False`.

This conflicts with the governing stabilization invariant that unknown/new populated fields must survive Audit Core Review persistence.

### 3.6 Reference-only resolution can exist without a fake typed owner

**VERIFIED FACT**

`record_attribute_resolution(...)` already accepts null `owning_domain_key` and `owning_record_reference` values.

Therefore a reviewed supported/provenance fact does not need a fabricated typed owner. Generic durable persistence plus reference-only resolution is sufficient where no approved operational owner exists.

### 3.7 Delivery review is currently live/read-only

**VERIFIED FACT**

Current Delivery V2 capture persists Journey/stage/document linkage and Delivery submission state.

`GET /v2/.../audit/source-comparison` reads current Booking + Delivery DI documents and displays mapped/unmapped values, but it is a GET/live comparison and does not commit reviewed Delivery fields to Audit Core.

No equivalent durable Delivery reviewed/effective field commit point has been established in current code.

### 3.8 Payments / Journey linkage

**VERIFIED FACT — earlier assumption corrected**

Migration `0041_uc03_journey_stage_linkage.py` already creates Payment `booking_id`, optional `delivery_id`, and `payment_stage` (`UNSPECIFIED` / `BOOKING` / `DELIVERY`) with constraints/triggers. Payments remain 1:N below the Journey/Booking.

Do not reopen a missing-payment-stage defect unless new contradictory evidence appears.

### 3.9 Repeated entity assessment retained

**VERIFIED FACT**

- Payments/receipts already support repeated rows.
- Document identity is distinct by DI document identity; multiple invoice documents and same-type supporting documents are not inherently collapsed by document type.
- Current evidence did not justify turning Insurance, Trade-In, Finance or Commercial components into arbitrary repeated collections.
- A separate typed repeatable Invoice business entity remains **UNKNOWN** until the final-report/business-owner contract proves it is required; do not invent it in the current persistence unit.

### 3.10 Canonical alias assessment retained

**VERIFIED FACT / VERIFIED GAP**

Audit Core has existing source-truth/document-type policy structures, but current runtime still contains exact/hard-coded document-type families in places and the named canonical source-truth JSON artifact described by an earlier design was not found in current tree.

This is a separate stabilization concern. In this implementation unit, preserve the original classified document identity and do not invent aliases. A missing alias must not prevent generic persistence of a non-empty DI field.

### 3.11 Final source and final report

**VERIFIED FACT / OPEN**

Current post-Delivery comparison is live DI-oriented and no completed persisted final resolved Journey snapshot / agreed final-report generation path has yet been established.

Exact final-report workbook field names/order/ownership remain **UNKNOWN / NOT YET VERIFIED FROM WORKBOOK**.

These open items are not silently closed by the current persistence implementation approval.

## 4. Prior cross-repository evidence retained for later steps

Do not rescan these repositories during the current unit.

### DI

**VERIFIED FACT from prior investigation**

- Active Schema V2 extraction persists configured/published profile fields with provenance.
- The trusted Audit Core field API returns current document fields rather than a UC03-specific narrow allow-list.
- Some additional profiles/mappings remain draft/not-processing.

Detailed DI validation remains a later Step-2 activity.

### Web

**VERIFIED FACT from prior investigation**

- Current Delivery Review consumes Audit Core source comparison and can display mapped/unmapped values/evidence.
- Normal Journey views already consume Audit Core business structures for many domains.

Web changes are out of current scope.

## 5. Approved implementation unit — exact scope

**WRITE APPROVED**

Implement the smallest Audit Core change that guarantees lossless durable reviewed-field persistence for non-null/non-empty DI fields in Booking and Delivery while retaining existing typed projections.

Allowed direction:

1. Backward-compatible extension of `journey_document_extracted_fields`; no second generic field table unless direct evidence proves extension unsafe.
2. Shared lossless persistence helper, preferably reusing `uc03_di_core_persistence.py`.
3. Booking V2 confirm persists all populated current DI fields before/with typed projection and no longer requires unknown fields to fabricate typed owners.
4. Legacy/direct Review persists unchanged populated DI fields as well as corrections.
5. Add an explicit non-GET Delivery Review commit path only if no existing write contract can safely serve that purpose. Do not mutate state from the source-comparison GET.
6. Preserve original DI document/canonical-field/fact-version identity and stage provenance.
7. Rejected values retain original DI provenance and are excluded from typed projection; do not invent effective-value semantics beyond the governing Review contract.
8. Focused migration/unit/DB/OpenAPI regression tests.

Explicitly out of scope:

- DI or Web changes;
- Security changes;
- merge/deploy;
- rule-engine internals;
- final-report implementation;
- speculative Invoice/domain redesign;
- invented canonical alias keys;
- unrelated cleanup/refactoring.

## 6. Acceptance tests for this unit

The unit is not `FIXED` until agreed DB/end-to-end acceptance passes.

- Booking Review with N populated DI fields persists all N populated fields after confirm, including unknown/unmapped values.
- Unchanged value persists original DI value as effective reviewed value with DI provenance.
- Changed value preserves original DI value/provenance and stores confirmed effective value.
- Accepted unknown field does not fail solely for lack of a typed owner and does not create an invented typed mutation.
- Legacy/direct Review no longer silently drops unchanged populated fields.
- Delivery Review commit persists all populated Delivery fields with `DELIVERY` provenance.
- Repeated documents and multiple same-type documents remain distinguishable by document/fact identity.
- Existing Booking Form/identity/receipt typed materialization regressions remain green.
- Migration remains compatible with existing legacy generic rows.
- DB-level assertion demonstrates no populated accepted DI field silently disappears.

## 7. Remaining Step-1 questions after this approved unit

These remain open and must not be conflated with the current write scope:

- exact final-report workbook field contract and owner mapping — **UNKNOWN**;
- whether a typed repeated Invoice collection is genuinely required — **UNKNOWN pending report/business mapping**;
- complete canonical alias/family implementation across modules;
- persisted final source-of-truth model after Delivery;
- minimal rule-run status/reference placeholder.

## 8. NEXT ACTION

**Current target: `verigence-audit-core` only. WRITE APPROVED for the lossless reviewed-DI persistence unit; NO MERGE / NO DEPLOY.**

Proceed one coherent implementation unit at a time:

1. inspect the existing generic-review tests and Delivery verification/state contract needed to lock acceptance semantics;
2. implement the smallest backward-compatible migration + shared generic persistence helper;
3. test that helper/schema unit;
4. wire legacy/direct Booking Review to persist all populated fields and test;
5. wire Booking V2 confirm to generic persistence, remove typed-owner-only blockers, preserve typed projection, and test;
6. add the smallest explicit Delivery Review commit path that persists Delivery fields without mutating GET behavior, and test;
7. run focused regression/DB tests and update this checkpoint with actual results;
8. stop before merge/deploy and before moving to DI/Web.

## 9. Anti-stuck / recovery rule

If a path does not answer the current evidence question after a small number of direct attempts, record `UNKNOWN` and pivot. Do not recursively rescan the repository.

If interrupted, resume from Master → Checkpoint → Plan → Implementation Context → `NEXT ACTION` above.