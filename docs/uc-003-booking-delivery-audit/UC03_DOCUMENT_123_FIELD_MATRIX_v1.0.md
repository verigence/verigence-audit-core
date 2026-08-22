# Verigence UC03 — Provisional Document Catalogue & 123-Field Matrix

**Document ID:** `VUC03-FM-001`  
**Version:** `1.0`  
**Status:** DRAFT / PROVISIONAL FOR DESIGN & UAT RECONCILIATION  
**Date:** 2026-08-22  
**Parent design:** `VUC03-SD-002 / UC03_SOLUTION_DESIGN_v1.1.md`  
**Rule catalog:** `VUC03-RF-001 / UC03_RULE_FLAG_CATALOG_v1.0.md`

---

## 1. Purpose

This document reconciles the current UC03 capture scope at two levels:

1. a **provisional document catalogue** derived from the supplied PC Evidence Capture Process and its applicability diagram; and
2. the **complete 123-field inventory** from the supplied Web Capture Field List workbook.

It is intentionally marked provisional because the business sources contain known inconsistencies. Those inconsistencies are recorded rather than silently corrected.

---

## 2. Source facts that are currently authoritative for planning

The supplied field inventory contains exactly **123 fields** with this capture split:

| Capture mode | Count | Share | UC03 direction |
|---|---:|---:|---|
| PC types | 36 | 29.27% | human input into owning Audit Core domain |
| Extracted | 57 | 46.34% | DI proposal -> PC accept/correct -> owning Audit Core domain |
| System / computed | 27 | 21.95% | Audit Core/master/calculated read-only value |
| Upload | 3 | 2.44% | Audit Core evidence association + DI processing/storage boundary |
| **Total** | **123** | **100%** | |

The field inventory classifies stage coverage as:

| Source stage tag | Fields |
|---|---:|
| Booking | 89 |
| Delivery | 5 |
| Both | 22 |
| Audit | 7 |
| **Total** | **123** |

`Both` and `Audit` are source tags, not final UC03 persistence ownership. They are reconciled during implementation design.

---

## 3. Ownership principles

UC03 does **not** create one generic 123-field source-of-truth table.

### PC types

Persist validated human input in the appropriate typed Audit Core owner: Booking, Customer, Commercials, Trade-In, Delivery, Payment, Flag/Review, etc.

### Extracted

DI provides extracted facts/proposals with source-document and confidence/provenance. A proposal is explicitly accepted/corrected before becoming the accepted business value in its owning Audit Core domain.

### System / computed

Derived from Audit Core domain values, project masters, hierarchy or deterministic calculation. Never manually editable merely because the old screen displayed the value.

### Upload

Audit Core owns Journey/stage association and evidence semantics; DI owns document intelligence/storage integration under the approved cross-module boundary.

---

## 4. Provisional document catalogue

The PC process prose repeatedly refers to **26 documents**, while the numbered applicability diagram contains **29 entries**. For UC03 planning we retain all 29 numbered entries so no source requirement is lost. The catalogue will be reconciled during testing/UAT before final DI/document-profile configuration.

### 4.1 Always-applicable Booking documents

| # | Document | Stage | Applicability | Answer direction | Extraction direction |
|---:|---|---|---|---|---|
| 1 | Booking Docket | Booking | Always | Required evidence; missing/No creates flag | extraction-supported target; exact profile to confirm |
| 2 | Customer KYC — PAN | Booking | Always | Required evidence | PAN extraction target |
| 3 | Customer KYC — Aadhaar | Booking / Delivery identity | Always in source catalogue | evidence/capture handling to reconcile | identity extraction/capture policy to confirm |
| 4 | Customer KYC — Address Proof | Booking | Always | Required evidence under source catalogue | address extraction target |
| 5 | Minimum Booking Amount proof | Booking | Always unless approved exchange/trade-in exception applies | Required/exception-aware | receipt/date/amount extraction target |

### 4.2 Always-applicable Delivery documents

| # | Document | Stage | Applicability | Answer direction | Extraction direction |
|---:|---|---|---|---|---|
| 6 | No Dues Certificate (NDC) | Delivery | Always | Yes/No; source states no absence should be hidden | signature/field extraction where supported |
| 7 | Tax Invoice — DMS | Delivery | Always | Yes/No | invoice/chassis/price/tax extraction |
| 8 | Tax Invoice — Tally | Delivery | Always | Yes/No | invoice/price/tax extraction |
| 9 | Insurance Cover Note | Delivery | Always | Yes/No | insurance fields extraction |
| 10 | Gate Pass | Delivery | Always | Yes/No | document presence/fields where supported |
| 11 | Customer ID | Delivery | Always | Yes/No | identity validation/extraction where supported |
| 12 | Customer Ledger | Delivery | Always | Yes/No | ledger/payment facts |
| 13 | Cost Sheet | Delivery | Always | Yes/No | commercial values |
| 14 | Docket audit form | Delivery | Always | Yes/No | process evidence |
| 15 | Car pictures | Delivery | Always | in-app capture preferred | VIN/photo-derived facts where supported |

### 4.3 Conditional documents

| # | Document | Stage | Trigger |
|---:|---|---|---|
| 16 | RC / Transfer Letter / Authorization Letter | Booking + Delivery | Exchange Taken = Yes / ownership support required |
| 17 | Trade-in documents — RC, valuation | Booking + Delivery | Exchange Taken = Yes |
| 18 | GST Certificate | Booking + Delivery | Corporate customer or corporate discount |
| 19 | Corporate ID | Booking + Delivery | Corporate customer or corporate discount |
| 20 | Purchase Order | Booking + Delivery | Corporate customer or corporate discount |
| 21 | Bank approval letter | Delivery | Financed / DO exists / Finance Type = In House |
| 22 | Delivery Order (DO) | Delivery | Financed / DO exists / Finance Type = In House |
| 23 | Registration Invoice | Delivery | Registration done by dealer |
| 24 | RTO Challan | Delivery | Registration done by dealer |
| 25 | Debit note for insurance & registration | Delivery | Registration done by dealer |
| 26 | Accessory Invoice — DMS | Delivery | Accessories taken/billed |
| 27 | Accessory Invoice — Tally | Delivery | Accessories taken/billed |
| 28 | Declaration for 3rd-party payment | Delivery | any receipt not paid by customer |
| 29 | Payment Receipts — Tally | Delivery | any receipt not paid by customer / payment verification path |

### 4.4 Document-answer semantics

- irrelevant requirements are hidden rather than padded with habitual `NA`;
- an attribute change can add new applicable requirements later;
- `NO` is a legitimate audit answer and normally raises a flag for a document that should exist;
- `NA` is available only where the versioned requirement profile permits it;
- where `NA` is permitted, the profile may require a reason;
- original documents are preferred over messaging-app recompressed photos where available;
- in-app car photographs are the intended exception.

---

## 5. Known document-source inconsistencies to preserve for review

1. prose/count says **26 documents**; numbered diagram reaches **29**;
2. the source process uses slightly different wording for KYC/customer ID and some invoice types in different sections;
3. exact DI extraction support for each document type is not yet confirmed;
4. some fields can be sourced from more than one document; final source precedence belongs to DI/Audit Core implementation design;
5. Aadhaar appears both as KYC evidence and as a mandatory Delivery-entered field in the source material; privacy/capture ownership must be reconciled explicitly rather than silently changed.

---

## 6. UC03-specific field remapping decisions

The 123-field source inventory is preserved below, but several legacy UI fields must not survive unchanged.

### Field #90 — Status

Source: PC-controlled `Booking / Delivered` radio.

UC03 action: **REPLACE**.

Authoritative status comes from Workflow Manager:

```text
Booking: BOOKING_STARTED / BOOKING_IN_PROGRESS / BOOKING_CLOSED / BOOKING_CANCELLED / DUPLICATE_BOOKING
Delivery: DELIVERY_STARTED / DELIVERY_IN_PROGRESS / DELIVERY_COMPLETED
```

The PC never directly toggles a generic Booking/Delivered status field.

### Field #91 — Delivery Date

UC03 action: **REMAP**.

Physical Delivery date/time should be associated with the accepted Delivery progression event/source evidence. UI may display/edit only according to the approved event/correction contract; it is not an arbitrary computed label detached from workflow history.

### Fields #117-123 — legacy observations

UC03 action: **REMAP to Flag/Review model**.

| Legacy field | UC03 direction |
|---|---|
| Breach Status | per-stage Audit Status + flag counts/history |
| Observation ID | internal flag/finding reference |
| Observation Category | configurable flag category / rule key |
| Observation Description | flag title/description/expected/observed summaries |
| Breach flag | represented by existence/result of the flag; no parallel boolean required by default |
| Auditor Remarks | PC flag remark event |
| Team Lead Remarks | TL remark/review event; PM/Executive also supported |

---

## 7. Complete 123-field source inventory

The table below reproduces the field labels, source stage classification, capture mode and mandatory marker from the supplied `Web_Capture_Field_List.xlsx`. It does not silently alter source labels.

| # | Stage | Field | Capture | Mandatory |
|---:|---|---|---|---|
| 1 | Booking | Price List | PC types | Yes |
| 2 | Booking | Customer Name | Extracted | Yes |
| 3 | Booking | Customer Number | Extracted | Yes |
| 4 | Booking | Type of Customer | PC types | Yes |
| 5 | Booking | Alternate No | Extracted | No |
| 6 | Booking | Mail ID | Extracted | Yes |
| 7 | Booking | Pan | Extracted | Yes |
| 8 | Booking | GST No | Extracted | No |
| 9 | Booking | SC Name | Extracted | Yes |
| 10 | Booking | SC Number | Extracted | Yes |
| 11 | Booking | Pincode | Extracted | No |
| 12 | Booking | Dealer Name | System / computed | No |
| 13 | Booking | Location | System / computed | No |
| 14 | Booking | Sales Contract Copy | Upload | No |
| 15 | Booking | Booking Date | System / computed | No |
| 16 | Booking | Booking Intimation Date and Time | System / computed | No |
| 17 | Booking | Type of Deal | PC types | Yes |
| 18 | Booking | Deal Source | PC types | Yes |
| 19 | Booking | Lead Generated Through | PC types | Yes |
| 20 | Booking | Model | Extracted | Yes |
| 21 | Booking | Fuel Type | Extracted | Yes |
| 22 | Booking | Variant | Extracted | Yes |
| 23 | Booking | Color | Extracted | Yes |
| 24 | Booking | Registration State | PC types | Yes |
| 25 | Booking | Territory Categorization | PC types | Yes |
| 26 | Booking | District Name | PC types | Yes |
| 27 | Booking | Vin Number/Chasis Number | Extracted | Yes |
| 28 | Booking | DMS Customer Name | Extracted | Yes |
| 29 | Booking | DMS Invoice Number | Extracted | Yes |
| 30 | Booking | DMS Invoice Date | System / computed | No |
| 31 | Booking | Registration Type | PC types | Yes |
| 32 | Booking | Registration Category | PC types | Yes |
| 33 | Booking | Outright Purchase | PC types | Yes |
| 34 | Booking | Booking Intimation Copy | Upload | No |
| 35 | Booking | Ex Showroom | Extracted | No |
| 36 | Booking | Registration Type (amount) | Extracted | No |
| 37 | Booking | Accessories Taken | PC types | No |
| 38 | Booking | Essential Kit | Extracted | No |
| 39 | Booking | Ceramic Coating | Extracted | No |
| 40 | Booking | Maintenance Package | Extracted | No |
| 41 | Booking | Genuine Accessory | Extracted | No |
| 42 | Booking | Non Genuine with OEM | Extracted | No |
| 43 | Booking | Non Genuine | Extracted | No |
| 44 | Booking | Insurance | Extracted | No |
| 45 | Booking | Fasttag Taken | PC types | No |
| 46 | Booking | RSA | Extracted | No |
| 47 | Booking | TCS | Extracted | No |
| 48 | Booking | EW (Extended Warranty) | Extracted | No |
| 49 | Booking | Green Tax | PC types | No |
| 50 | Booking | Service Package | Extracted | No |
| 51 | Booking | Other Charges | PC types | No |
| 52 | Booking | HP Charges | PC types | No |
| 53 | Booking | Deal | System / computed | - |
| 54 | Booking | Discount | System / computed | - |
| 55 | Booking | Net Deal | System / computed | - |
| 56 | Booking | Sales Discount | Extracted | No |
| 57 | Booking | Buffer Discount | Extracted | No |
| 58 | Booking | Exchange Discount Taken | PC types | No |
| 59 | Booking | Corporate Discount Taken | PC types | No |
| 60 | Booking | Inhouse Insurance Discount | Extracted | No |
| 61 | Booking | MR Discount | Extracted | No |
| 62 | Booking | OEM Referral | Extracted | No |
| 63 | Booking | Other Discount | Extracted | No |
| 64 | Booking | Scrap Exchange | Extracted | No |
| 65 | Booking | Sambandh Scheme | Extracted | No |
| 66 | Booking | Upward Sales | Extracted | No |
| 67 | Booking | Pro Pack Trims | Extracted | No |
| 68 | Booking | Non Pro Pack Trims | Extracted | No |
| 69 | Booking | Self Insurance Discount | Extracted | No |
| 70 | Booking | Navratri Booking Bonus | Extracted | No |
| 71 | Booking | 2 to 4 Consumer offer | Extracted | No |
| 72 | Booking | Discount (total) | System / computed | - |
| 73 | Booking | Trade Date | System / computed | No |
| 74 | Booking | Amount of Old Vehicle | Extracted | No |
| 75 | Booking | Trade-in Vehicle Registration No. | Extracted | No |
| 76 | Booking | Trade-in Car Model | Extracted | No |
| 77 | Booking | Whether RC Available | PC types | No |
| 78 | Booking | Trade-in Car in the Name of New Car Owner | PC types | No |
| 79 | Booking | Type of Exchange Discount | PC types | No |
| 80 | Booking | Trade-in Vehicle Model Year | Extracted | No |
| 81 | Booking | Standard Discount | System / computed | No |
| 82 | Booking | Actual Discount | Extracted | No |
| 83 | Booking | Variance | System / computed | - |
| 84 | Booking | Type of Corporate Discount | PC types | No |
| 85 | Booking | Corporate ID available? | PC types | No |
| 86 | Booking | Name of Firm | Extracted | No |
| 87 | Booking | Standard | System / computed | No |
| 88 | Booking | Actual | Extracted | No |
| 89 | Booking | Variance | System / computed | - |
| 90 | Both | Status | PC types | Yes |
| 91 | Delivery | Delivery Date | System / computed | No |
| 92 | Delivery | Aadhar | PC types | Yes |
| 93 | Delivery | Was delivery Intimated to you? | PC types | No |
| 94 | Delivery | Reason for non intimation of delivery? | PC types | No |
| 95 | Delivery | Petrol and Diesel slip | PC types | No |
| 96 | Both | Receipt Type | System / computed | - |
| 97 | Both | Receipt Number | System / computed | - |
| 98 | Both | Amount | Extracted | - |
| 99 | Both | Verification Status | System / computed | - |
| 100 | Both | Verification Docs count | System / computed | - |
| 101 | Both | Receipt Date | Extracted | - |
| 102 | Both | Genuine flag | System / computed | - |
| 103 | Both | Made By Customer? | PC types | No |
| 104 | Both | Receipt Date | Extracted | No |
| 105 | Both | Amount | Extracted | No |
| 106 | Both | UTR No | Extracted | No |
| 107 | Both | Receipt from system | Upload | No |
| 108 | Both | Payment Verification? | PC types | No |
| 109 | Both | Realized Amount | PC types | No |
| 110 | Both | Amount matches with receipt? | System / computed | - |
| 111 | Both | Cash deposited directly in bank by customer? | PC types | No |
| 112 | Both | Booking Date | System / computed | No |
| 113 | Both | Amount | Extracted | No |
| 114 | Both | Bank Name | Extracted | No |
| 115 | Both | Finance Type | PC types | No |
| 116 | Both | Payment Verification? | PC types | No |
| 117 | Audit | Breach Status | System / computed | - |
| 118 | Audit | Observation ID | System / computed | - |
| 119 | Audit | Observation Category | System / computed | - |
| 120 | Audit | Observation Description | System / computed | - |
| 121 | Audit | Breach flag | System / computed | - |
| 122 | Audit | Auditor Remarks | PC types | No |
| 123 | Audit | Team Lead Remarks | PC types | No |

---

## 8. Field-family ownership direction

The final physical owner for every field is confirmed during implementation design by reviewing existing typed Audit Core models before adding schema.

Initial families:

| Source grouping | UC03 owner direction |
|---|---|
| Customer and Dealer details | Customer / hierarchy / Booking projection as appropriate |
| Deal Details | Booking + Vehicle domains |
| Booking Summary | Commercials / pricing / service-value domains |
| Discount Details | Commercials / discount evaluation |
| Exchange Discount / Trade-in | Trade-In domain |
| Corporate Discount | Commercials/corporate context |
| Delivery Information | Delivery domain |
| Receipts / Bank Transfer / DO | Payment domain |
| Observations | UC03 Flag/Review domain |
| Generic Status | UC03 Workflow Manager |

No new table is created solely because an old screen grouped fields together.

---

## 9. Extracted-field source mapping status

The 57 extracted fields are accounted for, but this document does **not** claim a final one-to-one source document for every field because the supplied source material does not support that precision for all 57.

Known source directions include:

- Booking docket / Booking documents -> customer, vehicle, booking, pricing and discount facts;
- PAN -> PAN identity;
- Aadhaar / Customer ID -> Aadhaar/identity where approved by privacy policy;
- Address Proof -> address/pincode facts;
- Booking/payment proof -> receipt date/amount/payment facts;
- Tax Invoices -> invoice, chassis/VIN, pricing and tax facts;
- Insurance Cover Note -> insurance facts;
- trade-in RC/valuation -> used vehicle identity/value facts;
- Bank Approval / DO -> finance/bank/amount facts;
- payment receipts -> amount/date/UTR/payment evidence.

The definitive mapping requires DI extraction-profile review and is a required pre-implementation deliverable. A field with multiple candidate source documents must have an explicit precedence/resolution rule in Audit Core/DI; Web/Android must not choose one on its own.

---

## 10. Mandatory-marker reconciliation note

The source field inventory and the narrative PC process use different counts/wording for some mandatory fields. This matrix preserves the workbook's mandatory markers exactly. UC03 stage-completion policy will use the **versioned requirement/rule configuration**, not a hard-coded interpretation of the spreadsheet `Mandatory` column.

This is especially important for:

- extracted customer/vehicle fields that may be prefilled/confirmed rather than manually typed;
- Aadhaar handling;
- conditional fields driven by Exchange/Corporate/Finance/Registration/Accessories/payment attributes;
- payment rows that repeat for multiple receipts;
- legacy Audit/Observation fields that are being remapped.

---

## 11. Mockup implications

The matrix supports the agreed Android-first UX sequence:

1. upload applicable documents immediately;
2. expose PC-entered fields that do not depend on extraction;
3. show per-document processing state rather than one global spinner;
4. progressively surface DI values as proposals;
5. bulk-accept clean proposals and isolate low-confidence/variance items;
6. recalculate document applicability as attributes change;
7. show Audit Flags independently of business status;
8. allow Delivery Start/Completion even when earlier/current audit prerequisites remain incomplete;
9. keep the stage Audit State visible so the PC knows what work remains after physical Delivery Completed.

---

## 12. Reconciliation checklist before implementation freeze

- [ ] Review 29 provisional documents and resolve the 26-vs-29 source discrepancy.
- [ ] Confirm exact document display names/keys.
- [ ] Confirm Yes/No/NA policy for every document requirement.
- [ ] Confirm final applicability expression per conditional document.
- [ ] Confirm DI extraction support per document.
- [ ] Map all 57 extracted fields to exact source document/profile and precedence.
- [ ] Map all 36 PC-entered fields to owning Audit Core domain and validation.
- [ ] Map all 27 computed fields to deterministic owner/calculation/master.
- [ ] Map all 3 uploads to UC03 evidence/document requirement keys.
- [ ] Replace field #90 with Workflow Manager status projections.
- [ ] Reconcile field #91 with Delivery event timestamp model.
- [ ] Remap fields #117-123 to Flag/Review model.
- [ ] Resolve Aadhaar capture/privacy/source inconsistency.
- [ ] Resolve VIN 8/17-character representation in Rule Engine design.
- [ ] Confirm repeatable/multi-row semantics for Receipt/Payment fields.

Until this checklist is closed, this matrix is authoritative for **scope accounting**, not permission to invent unresolved field/document behavior in code.