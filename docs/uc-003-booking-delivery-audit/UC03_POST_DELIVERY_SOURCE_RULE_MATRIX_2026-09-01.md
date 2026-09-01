# UC03 Post-Delivery Source Rule Matrix — Investigation

Date: 2026-09-01  
Repository: `verigence-audit-core`  
Branch: `investigation/uc03-post-delivery-final-source`  
Mode: **INVESTIGATION / DESIGN ONLY — NO SCHEMA OR APPLICATION IMPLEMENTATION**

## 1. Evidence question

Can current Audit Core evidence define a safe stage-aware final-source rule matrix for post-Delivery finalization without inventing Booking-vs-Delivery precedence?

## 2. Answer

**PARTIALLY.**

Audit Core evidence is sufficient to define the final-resolution **mechanics and source classes**, but it is not sufficient to freeze a universal ordered stage precedence for every overlapping document-derived attribute.

The current repository explicitly supports deterministic document-source ordering for Review, but:

- `AttributeCandidate` has no `BOOKING` / `DELIVERY` stage;
- the current resolver ranks by document type, then confidence, then deterministic tie-break;
- the reconciliation baseline explicitly says multi-document precedence must be defined and does not permit `last extraction wins`;
- the 123-field matrix still marks exact extracted-field source/profile/precedence mapping as an open reconciliation item.

Therefore **no blanket `DELIVERY wins`, `latest wins`, or `highest confidence wins` final rule is supported by current evidence.**

## 3. Canonical document evidence available in Audit Core

The published UC03 default requirement profile currently uses these relevant canonical requirement/document keys.

### Booking

| Requirement key | Document type key | Stage |
|---|---|---|
| `booking_docket` | `booking_docket` | BOOKING |
| `pan_card` | `pan_card` | BOOKING |
| `aadhaar` | `aadhaar` | BOOKING |
| `minimum_booking_payment_proof` | `minimum_booking_payment_proof` | BOOKING |
| `gst_certificate` | `gst_certificate` | BOOKING |
| `trade_in_vehicle_rc` | `vehicle_rc` | BOOKING |
| `trade_in_transfer_letter` | `transfer_letter` | BOOKING |
| `trade_in_authorization_letter` | `authorization_letter` | BOOKING |

### Delivery

| Requirement key | Document type key | Stage |
|---|---|---|
| `wholesale_invoice` | `wholesale_invoice` | DELIVERY |
| `customer_invoice_dms` | `customer_invoice_dms` | DELIVERY |
| `tax_invoice_tally` | `tax_invoice_tally` | DELIVERY |
| `insurance_cover_note` | `insurance_cover` | DELIVERY |
| `accessory_invoice_dms` | `accessory_invoice_dms` | DELIVERY |
| `accessory_invoice_tally` | `accessory_invoice_tally` | DELIVERY |
| `rto_challan` | `rto_challan` | DELIVERY |
| `customer_ledger` | `customer_ledger` | DELIVERY |
| `cost_sheet` | `cost_sheet` | DELIVERY |
| `gate_pass` | `gate_pass` | DELIVERY |
| `customer_kyc` | `customer_kyc` | DELIVERY |
| `ew_invoice` | `ew_invoice` | DELIVERY |
| `rsa_invoice` | `rsa_invoice` | DELIVERY |
| `value_added_service_document` | `value_added_service_document` | DELIVERY |
| `no_dues_certificate` | `no_dues_certificate` | DELIVERY |
| `payment_receipt` | `payment_receipt` | DELIVERY |

Migration `0049_uc03_delivery_requirement_applicability` later corrects Accessory Invoice DMS/Tally, RTO Challan, EW Invoice and RSA Invoice to conditional requirements based on the appropriate Journey condition. It does not redefine their canonical keys.

## 4. Stage-aware final-source classification

The purpose of this matrix is not to invent a winner. It separates cases that are already deterministic from cases that still require an explicit source policy.

### Class A — Booking-only operational facts

**Classification: VERIFIED — no Booking-vs-Delivery conflict is required.**

Current explicit attribute mappings constrain these concepts to Booking only:

| Attribute | Current legitimate source family | Final-stage direction |
|---|---|---|
| Booking Payment Mode | Booking Form / Booking Docket | preserve Booking reviewed/typed value |
| Booking Payment Reference | Booking Form / Booking Docket | preserve Booking reviewed/typed value |
| Expected Delivery | Booking Form / Booking Docket | preserve Booking reviewed value |
| Expected Delivery Date | Booking Form / Booking Docket | preserve Booking reviewed value |
| Booking Reference | Booking Form / Booking Docket | use approved Booking operational owner / reviewed provenance |
| Actual Booking Date | Booking Form / Booking Docket | use approved Booking operational owner / reviewed provenance |
| Dealer Name / Branch | Booking Form / Booking Docket | Booking-stage value only |
| SC Name | Booking Form / Booking Docket | mapping remains PROVISIONAL, but stage is Booking only |

No post-Delivery selector should substitute a Delivery invoice merely because a similarly named field appears later.

### Class B — identity-authority facts

**Classification: SOURCE FAMILY VERIFIED; SAME-FAMILY CROSS-STAGE PRECEDENCE UNKNOWN.**

Current resolver explicitly expresses identity/document authority for:

| Attribute family | Current source priority |
|---|---|
| Customer Name | PAN / PAN Card -> Aadhaar -> Booking Form/Docket |
| Date of Birth | PAN / PAN Card / Aadhaar |
| Aadhaar Number | Aadhaar |
| Gender | Aadhaar |
| Customer Address | Aadhaar -> Booking Form/Docket |
| PAN | PAN Card / PAN |
| PAN Father Name | PAN Card / PAN |
| Relationship Type/Name | PAN / PAN Card / Aadhaar |

This proves that source-document semantics, not stage recency, determine authority.

However, if the same authoritative source family is present in both Booking and Delivery and disagrees (for example two Aadhaar/PAN/KYC documents), current Audit Core evidence does not define whether Booking-stage or Delivery-stage evidence wins. Confidence is extraction quality, not an approved business precedence rule.

**Final policy for same-family cross-stage disagreement: `UNKNOWN` pending authoritative mapping / business contract.**

### Class C — Booking-origin facts exposed across both stages

**Classification: CURRENT SOURCE FAMILY IS BOOKING; NO DELIVERY OVERRIDE IS PROVEN.**

Current `ATTRIBUTE_SPECS` allow these attributes to appear in Booking/Delivery Review but source-priority contains only Booking Form/Docket:

- Customer Number;
- Mail ID;
- SKU Code;
- Registration By / Registration Type;
- Insurance By;
- Exchange Applicable / Exchange Value;
- Registration Charges;
- Road Tax / Road Tax+Registration source value;
- RSA Amount;
- Accessories Cost;
- Additional Warranty Amount;
- Other Charges;
- Bonus Amount;
- Booking Amount Paid;
- Balance Amount.

The current mapping therefore does **not** authorize a later invoice/cost-sheet value to replace these. If a future approved source map adds a Delivery source, that must be explicit and versioned.

### Class D — true overlapping vehicle/commercial scalar candidates

**Classification: LEGITIMATE SOURCE FAMILIES VERIFIED; POST-DELIVERY ORDER UNKNOWN.**

These are the important attributes where current Review mapping allows both Booking evidence and later Delivery evidence:

| Attribute | Current Review source order | Post-Delivery final precedence |
|---|---|---|
| Model | Booking Form/Docket -> DMS/Tally/Tax Invoice family | `UNKNOWN` |
| Variant | Booking Form/Docket -> DMS/Tally/Tax Invoice family | `UNKNOWN` |
| Color | Booking Form/Docket -> DMS/Tally/Tax Invoice family | `UNKNOWN` |
| Ex-Showroom Price | Booking Form/Docket -> Cost Sheet -> Invoice family | `UNKNOWN` |
| TCS | Booking Form/Docket -> Invoice family | `UNKNOWN` |
| Total Price | Booking Form/Docket -> Invoice family | `UNKNOWN` |
| Discount | Booking Form/Docket -> Invoice family | `UNKNOWN` |
| Net Amount | Booking Form/Docket -> Invoice family | `UNKNOWN` |
| Insurance Amount | insurance source family -> Booking Form/Docket | `UNKNOWN`, plus canonical-key gap below |

The current source order is suitable as evidence of **which source families are currently considered legitimate for Review**. It is **not evidence that Booking should remain final after Delivery**. The governing design explicitly requires final source resolution after Delivery and separately says exact multi-source precedence remains to be frozen.

These attributes require a versioned final-source rule before implementation. A conflict must remain unresolved/flagged rather than be decided by recency or confidence when no rule exists.

### Class E — repeated Payments / Receipts

**Classification: VERIFIED — do not collapse to one scalar final source.**

The 123-field source inventory marks receipt/payment concepts across both stages, including Receipt Type, Receipt Number, Amount, Verification Status, Receipt Date, UTR, Realized Amount, Bank Name, Finance Type and payment-verification answers.

Payments are already a repeated Journey collection with stage provenance. Finalization must preserve the collection. Report logic may aggregate or expand it according to the final report contract; a generic `one attribute -> one winner` resolver must not select one arbitrary payment row.

### Class F — repeated Invoices

**Classification: VERIFIED document multiplicity; typed repeated Invoice owner remains UNKNOWN.**

The Delivery catalogue has distinct invoice requirements including Wholesale Invoice, Customer Invoice DMS, Tax Invoice Tally, Accessory Invoice DMS/Tally, EW Invoice and RSA Invoice. The Master also locks the business decision that multiple invoices, including invoices of the same business type, can exist under one Journey.

Therefore:

- there is no globally unique `final invoice`;
- document identities remain distinct;
- a scalar business attribute may resolve to one selected invoice fact under an explicit attribute-specific rule;
- any report/rule that needs invoice-level repeated rows must consume a repeated invoice representation or document-derived collection rather than flattening all invoices.

Whether Audit Core requires a new typed repeated Invoice business entity is still **UNKNOWN** until the actual final-report workbook/rule owner requirements are verified.

### Class G — workflow/event/audit outputs, not source-selection attributes

**Classification: VERIFIED — not part of document final-source precedence.**

The 123-field matrix already remaps:

- legacy `Status` to Workflow Manager stage/status;
- `Delivery Date` to the Delivery event/timestamp model;
- legacy Breach/Observation fields to the Flag/Review/Audit model.

These should come from typed workflow/audit state and evaluations, not from a document-source winner table.

## 5. Canonical alias/family finding

### Verified inconsistency: Insurance

The published Audit Core default requirement catalogue uses:

- requirement key: `insurance_cover_note`
- document type key: `insurance_cover`

The current attribute resolver for `booking_insurance_amount` prioritizes:

- `insurance_cover_note`
- `insurance_policy`
- then Booking Form/Docket.

The V2 Review path uses DI `classifiedDocumentTypeKey` (falling back to the stored classified document type) as the candidate `documentTypeKey` used by the resolver.

Therefore the current repository contains a **canonical-key mismatch risk**: `insurance_cover` from the requirement catalogue is not one of the resolver's preferred insurance keys.

Whether DI currently emits `insurance_cover`, `insurance_cover_note` or `insurance_policy` for the relevant document is **UNKNOWN in Audit Core-only Step 1**.

This is not justification to invent an alias. It is evidence that final-source normalization must use an authoritative canonical family mapping before Insurance finalization can be frozen.

### Existing reuse structure

Migration `0036_uc03_document_capture_v2` already created `auditcore.document_capture_v2_source_truth_rules` with:

- source labels;
- final source label;
- final document type key;
- due stage.

That migration creates the structure but does not seed mapping rows. A targeted current-repository search did not establish an authoritative populated mapping artifact in Audit Core.

**Design direction:** reuse the existing source-truth mechanism / authoritative catalogue; do not create a second alias table.

## 6. What the current matrix permits us to implement later

Once write approval is given **and only after the unresolved precedence values are supplied/validated**, the final-source resolver can safely use this shape:

1. candidate set = persisted reviewed `effective_value` rows only;
2. each candidate retains `stage_code`, document type, DI document, canonical field and fact version;
3. attribute rule contains an explicit ordered list of legitimate `(stage, canonical document family/type, field)` selectors;
4. if exactly one eligible source exists, select it;
5. if multiple eligible sources agree, select according to the explicit rule while retaining all provenance;
6. if multiple sources disagree and no approved precedence/disposition rule exists, **do not guess** — finalization for that attribute remains unresolved and the workflow/rule layer surfaces the exception;
7. repeated Payments/Invoices bypass scalar winner logic and stay repeated collections;
8. persist the selected reviewed-field reference + resolved value snapshot in the reused `POST_DELIVERY` resolution structure.

## 7. Acceptance-test implications for a later implementation unit

The final-source implementation must eventually prove at least:

- Booking-only fields cannot be overwritten by unrelated Delivery evidence;
- same field from Booking and Delivery cannot be resolved by stage recency unless an explicit versioned rule says so;
- same source family in two stages with conflicting values fails/flags deterministically when precedence is not configured;
- source confidence cannot override a higher-authority source family by itself;
- multiple payment rows survive finalization;
- multiple invoice documents survive finalization;
- one invoice-derived scalar may resolve to a selected invoice fact without deleting/merging the other invoice documents;
- Insurance normalization handles only approved canonical aliases and never guessed aliases;
- final resolution references the exact persisted reviewed field and value snapshot used;
- no final-source resolver re-reads DI to obtain the final business value.

## 8. Final-report dependency

The currently supplied spreadsheets establish process/capture/report **families**, but the actual 152-field final-report workbook identified by the Master is still not present among the inspected artifacts.

Therefore current evidence can classify ownership families and repeated-vs-scalar behavior, but cannot truthfully freeze:

- exact 152 output fields/order;
- exact output owner for every column;
- exact repeated Invoice representation required by the output;
- exact aggregation/expansion rules for repeated Payments/Invoices.

Those remain `UNKNOWN` until the final-report workbook is identified.

## 9. Conclusion of this evidence unit

**VERIFIED:** Audit Core has enough structure to implement stage-aware final-source persistence without a new generic final-state table.

**VERIFIED:** the current resolver is not safe to reuse unchanged as the final post-Delivery resolver because it drops stage identity and its source order is a Review-time document priority, not a frozen post-Delivery business precedence contract.

**VERIFIED:** Booking-only facts and repeated Payments/Invoices can already be classified safely.

**VERIFIED GAP:** canonical document normalization is incomplete for final-source use; Insurance exposes a concrete key mismatch risk.

**UNKNOWN:** post-Delivery precedence for overlapping identity/vehicle/commercial sources where the current business/DI mapping does not explicitly decide stage ordering.

**UNKNOWN:** exact 152-field final-report contract and whether it requires a typed repeated Invoice domain.

No application/schema change is authorized or performed by this document.
