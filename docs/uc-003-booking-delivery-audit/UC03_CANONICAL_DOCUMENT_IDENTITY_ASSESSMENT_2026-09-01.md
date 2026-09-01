# UC03 Canonical Document Identity / Alias Assessment

Date: 2026-09-01  
Repository: `verigence-audit-core`  
Branch: `investigation/uc03-post-delivery-final-source`  
Mode: **INVESTIGATION / DESIGN ONLY — NO SCHEMA OR APPLICATION IMPLEMENTATION**

## 1. Evidence question

Are the canonical document identities used by the published Audit Core requirement catalogue, V2 capture/reconciliation, Review attribute resolver and typed materializers aligned well enough for post-Delivery final-source resolution?

## 2. Current runtime behavior

**VERIFIED FACT:** V2 capture reconciliation maps a DI `classifiedDocumentTypeKey` to a requirement by exact equality with the current requirement `document_type_key`.

Conceptually today:

`DI classifiedDocumentTypeKey == Audit Core requirement.document_type_key`

There is no alias/family normalization step in `_reconcile_documents(...)`.

**VERIFIED FACT:** V2 Review then carries the DI `classifiedDocumentTypeKey` (or the stored classified key) forward as the candidate `documentTypeKey` used by `ATTRIBUTE_SPECS` source ranking.

Therefore inconsistent names between:

- requirement catalogue;
- DI classification;
- attribute resolver;
- typed materializer;

can cause a document to be legitimate business evidence but fail exact requirement reconciliation, source-priority ranking, or typed projection.

## 3. Family-by-family assessment

### 3.1 Booking Form / Booking Docket

Published default catalogue:

- requirement key: `booking_docket`
- document type key: `booking_docket`

Current attribute resolver accepts both:

- `booking_form`
- `booking_docket`

Current V2 typed Booking Form materializer, however, uses only:

- `_BOOKING_FORM_DOCUMENT_TYPE = "booking_form"`

**Classification: VERIFIED GAP / fragmented identity vocabulary.**

`booking_docket` is the published Audit Core catalogue key, but current typed materialization is keyed to `booking_form`. The resolver's dual acceptance masks this only at comparison time; it does not make the capture requirement or typed writer canonical.

Which exact key DI currently emits in production for this source is **UNKNOWN in Audit Core-only Step 1**.

### 3.2 PAN

Published default catalogue:

- requirement/document type: `pan_card`

Current resolver/materializer accepts:

- `pan_card`
- `pan`

**Classification: ALIGNED for canonical `pan_card`; additional `pan` alias is UNVERIFIED.**

There is no current Audit Core catalogue evidence establishing `pan` as a canonical requirement key. It may be an accepted DI alias, but that must be validated rather than assumed.

### 3.3 Aadhaar

Published default catalogue:

- requirement/document type: `aadhaar`

Current resolver/materializer:

- `aadhaar`

**Classification: ALIGNED.**

### 3.4 Customer / Tax Invoice families

Published Delivery catalogue includes:

- `customer_invoice_dms` -> `customer_invoice_dms`
- `tax_invoice_tally` -> `tax_invoice_tally`
- `wholesale_invoice` -> `wholesale_invoice`

Current scalar attribute resolver's invoice family accepts:

- `customer_invoice_dms`
- `tax_invoice_tally`
- `tax_invoice_dms`
- `tax_invoice`

The first two keys align directly with the published catalogue. The extra `tax_invoice_dms` and `tax_invoice` keys are not established as published Audit Core default requirement keys by current evidence.

The Master separately locks the business-family examples:

- DMS Invoice / Retail Invoice are the same business document family;
- Tally Invoice / Tax Invoice are the same business document family;
- exact canonical keys must come from existing catalogue/contracts and must not be invented.

**Classification: PARTIALLY ALIGNED.**

Canonical current Audit Core keys `customer_invoice_dms` and `tax_invoice_tally` are directly supported. Additional resolver aliases remain **UNKNOWN** until the authoritative DI/catalogue contract is validated.

`wholesale_invoice` is a valid Delivery requirement but is not currently part of the generic `_INVOICE` scalar source-priority tuple. Whether that is correct for every final-report/rule attribute is **UNKNOWN** and should be decided per attribute, not by adding it globally.

### 3.5 Cost Sheet

Published Delivery catalogue:

- `cost_sheet` -> `cost_sheet`

Current resolver explicitly uses `cost_sheet` for Ex-Showroom Price comparison.

**Classification: ALIGNED.**

### 3.6 Insurance

Published Delivery catalogue:

- requirement key: `insurance_cover_note`
- document type key: `insurance_cover`

Current attribute resolver for Insurance Amount prioritizes:

- `insurance_cover_note`
- `insurance_policy`
- then Booking Form/Docket.

**Classification: VERIFIED GAP / canonical-key mismatch risk.**

The published `document_type_key` (`insurance_cover`) is not in the current resolver's preferred insurance keys. Because Review ranks the DI classified key, a legitimately reconciled `insurance_cover` document could fail the intended insurance-source priority unless DI emits a different key.

Which exact key DI emits is **UNKNOWN** until Step 2 DI contract validation.

### 3.7 Payment / Dealer Receipt family

Published default catalogue includes:

- Booking requirement `minimum_booking_payment_proof` -> `minimum_booking_payment_proof`;
- Delivery requirement `payment_receipt` -> `payment_receipt`.

Current Booking receipt capture/materialization uses one special document type:

- `_RECEIPT_DOCUMENT_TYPE = "dealer_receipt"`.

Booking Review also special-cases `dealer_receipt` to preserve receipt-by-receipt raw review keys.

**Classification: VERIFIED GAP / fragmented identity vocabulary.**

`dealer_receipt`, `minimum_booking_payment_proof`, and `payment_receipt` are currently distinct keys with no runtime family normalization shown in Audit Core. They may represent different requirement semantics around the same business receipt family, but exact equivalence must not be guessed.

This matters because Payments are a repeated Journey entity spanning Booking and Delivery. Canonical normalization must preserve:

- stage provenance;
- distinct receipt/document identity;
- requirement purpose;
- repeated payment rows;

while preventing naming differences from blocking matching/materialization.

## 4. Existing `document_capture_v2_source_truth_rules` assessment

Migration `0036_uc03_document_capture_v2` already creates:

`auditcore.document_capture_v2_source_truth_rules`

with fields including:

- source spreadsheet row;
- section / attribute label;
- source labels array;
- final source label;
- final document type key;
- due stage.

### What it can potentially support

This shape is useful for **attribute-level source-truth policy**: for a source row/attribute, it can store an approved final document type and stage.

It can therefore be a reuse candidate for the post-Delivery source-rule configuration rather than creating a second attribute-source table.

### What it does not currently prove

**VERIFIED FACT:** current V2 capture reconciliation does not read this table; it matches DI classification to requirement document type by exact equality.

**VERIFIED FACT:** the table-creation migration itself does not seed the source-truth rows.

**UNKNOWN:** no authoritative populated mapping was established by the targeted current Audit Core search.

The table also stores `source_labels`, not an explicit versioned list of document-type alias keys/families. Therefore current evidence does **not** prove it is sufficient as the sole document-alias normalization structure across DI -> Audit Core -> Web -> reporting.

## 5. Smallest safe design direction

### D1. Canonical classified document key should be a contract, not a local guess

The preferred normalization boundary is:

1. an authoritative canonical document type key is defined in the cross-module DI/catalogue contract;
2. DI returns that canonical classified key (while retaining original classifier/source identity for audit evidence where required);
3. Audit Core requirement `document_type_key`, source resolver and typed materializer use the same canonical key;
4. presentation labels/legacy aliases map to the canonical key through an explicit versioned contract, never fuzzy matching.

### D2. Preserve requirement purpose separately from document family

`requirement_key` and canonical `document_type_key` serve different purposes and must not be collapsed.

Example: Booking minimum-payment proof and Delivery payment receipt can remain distinct requirement purposes even if Step 2 proves that their accepted physical-document classifications belong to one canonical receipt family.

### D3. Do not create a second alias table in Audit Core yet

Current evidence proves inconsistent vocabulary, but it does not prove that Audit Core needs a new alias table.

First validate in Step 2 whether DI already owns/publishes the canonical classification contract. If it does, the smallest Audit Core change is to align existing catalogue/resolver/materializer keys and consume canonical DI keys.

Only if Step 2 proves the canonical alias contract cannot be represented cleanly by existing catalogue/source-truth structures should a new structure be proposed.

### D4. `document_capture_v2_source_truth_rules` remains a candidate for final-source policy, not automatically the alias master

Do not overload it silently. Its best evidence-backed role today is attribute/source resolution policy (`which approved source for this attribute/stage`), while document-family alias normalization should follow the authoritative document catalogue/DI contract.

## 6. Acceptance-test implications for later implementation

A later approved normalization implementation must prove:

- a canonical Booking Form/Docket classification both satisfies the Booking requirement and reaches typed Booking materialization;
- canonical PAN/Aadhaar classifications remain aligned;
- Customer DMS and Tally/Tax invoice families resolve only through approved aliases;
- Insurance Cover Note classification resolves to the intended insurance source priority without requiring a guessed string;
- Booking/Delivery receipt classifications preserve distinct requirement purposes while both can materialize repeated Payment rows when applicable;
- unknown classifier keys remain visible/unresolved rather than being fuzzy-matched;
- original DI classification/provenance remains traceable after normalization;
- repeated same-type invoice/receipt documents remain distinct after normalization.

## 7. Conclusion

| Family | Assessment |
|---|---|
| Booking Docket / Form | **VERIFIED GAP** — `booking_docket` catalogue vs `booking_form` typed materializer |
| PAN | **ALIGNED canonical / alias UNKNOWN** — `pan_card` aligned, `pan` unverified alias |
| Aadhaar | **ALIGNED** |
| Customer DMS Invoice | **ALIGNED canonical** |
| Tally Tax Invoice | **ALIGNED canonical / extra aliases UNKNOWN** |
| Wholesale Invoice | **ALIGNED catalogue; per-attribute final-source use UNKNOWN** |
| Cost Sheet | **ALIGNED** |
| Insurance Cover | **VERIFIED GAP** — `insurance_cover` catalogue vs resolver `insurance_cover_note`/`insurance_policy` |
| Payment / Dealer Receipt | **VERIFIED GAP** — `minimum_booking_payment_proof` / `payment_receipt` vs typed `dealer_receipt` vocabulary |

**Current Audit Core conclusion:** canonical document identity is not uniformly consumed across capture, Review resolution and typed materialization. This is a real stabilization gap, but exact alias values remain a Step 2 DI/catalogue validation dependency. No alias value or new table is invented in this assessment.
