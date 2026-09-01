# UC03 Final Report — Field / Final-Source / Audit Core Owner Matrix

Date: 2026-09-01  
Repository: `verigence-audit-core`  
Branch: `investigation/uc03-post-delivery-final-source`  
Mode: **INVESTIGATION / DESIGN ONLY — NO SCHEMA OR APPLICATION CODE**

## Authority

This matrix is built from the **authoritative final report field list and Final Source of Truth list supplied by the user on 2026-09-01**.

The supplied contract contains **122 physical rows**, including structural blank/separator rows, and **113 labelled report rows** (excluding two `-` separators). Repeated labels are intentional section outputs and are not deduplicated.

This supersedes the earlier unverified assumption that the final workbook contract necessarily contains exactly 152 output rows.

`NA` in the supplied Final Source of Truth list is preserved as **not document-derived**. It does not mean the report value is absent.

## Owner classes

- `TYPED DOMAIN` / `TYPED SOURCE_SYSTEM`: existing Audit Core business state is the report owner.
- `POST_DELIVERY RESOLUTION`: scalar document/evidence-derived output must freeze the approved final source into post-Delivery resolution state.
- `POST_DELIVERY RESOLUTION → TYPED`: final source is frozen first, then may project into an existing commercial/domain owner.
- `REPEATED COLLECTION / AGGREGATE`: repeated records are retained and the report computes an aggregate; they must not be collapsed into one scalar evidence winner.
- `COMPUTED/AUDIT` / `AUDIT/REVIEW`: report derives from rule/audit/workflow state rather than a source document.

## Exact row mapping

| Row | Report field | Final Source of Truth | Report owner class | Existing / proposed Audit Core owner | Status |
|---:|---|---|---|---|---|
| 1 | Booking Number | NA | TYPED DOMAIN | bookings.booking_reference | REUSE |
| 2 | Booking Date | Minimum Booking Amount payment proof | POST_DELIVERY RESOLUTION | bookings.booking_date exists, but final source requires payment-proof resolution | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 3 | Booking Intimation Date | NA | TYPED DOMAIN | bookings.booking_intimated_at_utc | REUSE |
| 4 | DMS Invoice Date | Tax Invoice — DMS | POST_DELIVERY RESOLUTION | journey_document_extracted_fields + POST_DELIVERY resolution | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required; no typed Invoice entity required by report |
| 5 | DMS Invoice Number | Tax Invoice — DMS | POST_DELIVERY RESOLUTION | journey_document_extracted_fields + POST_DELIVERY resolution | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required; no typed Invoice entity required by report |
| 6 | Delivery Date | Gate Pass | POST_DELIVERY RESOLUTION | deliveries.actual_delivered_at or resolution snapshot from Gate Pass | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 7 | Dealership | NA | TYPED DOMAIN | dealers.dealer_name | REUSE |
| 8 | Location | NA | TYPED DOMAIN | dealer_outlets.outlet_name | REUSE |
| 9 | Customer Name | Customer KYC (PAN, Aadhaar, address proof) | POST_DELIVERY RESOLUTION | customer identity reviewed fields / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 10 | Type of customer | Booking & Retail Dump | TYPED SOURCE_SYSTEM | customers.customer_type_code | REUSE; stable source-system mapping |
| 11 | Pincode | Customer KYC (PAN, Aadhaar, address proof) | POST_DELIVERY RESOLUTION | reviewed KYC field + resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 12 | KYC District | Customer KYC (PAN, Aadhaar, address proof) | POST_DELIVERY RESOLUTION | reviewed KYC field + resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 13 | KYC State | Customer KYC (PAN, Aadhaar, address proof) | POST_DELIVERY RESOLUTION | reviewed KYC field + resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 14 | Contact No | Booking Docket (Sales Contract) | POST_DELIVERY RESOLUTION | booking_form_review_values.customer_phone / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 15 | Aadhar No | Customer KYC (PAN, Aadhaar, address proof) | POST_DELIVERY RESOLUTION | customer_identity_review_values.aadhaar_number / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 16 | Pan No | Customer KYC (PAN, Aadhaar, address proof) | POST_DELIVERY RESOLUTION | customer_identity_review_values.pan_number / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 17 | GST | GST Certificate | POST_DELIVERY RESOLUTION | reviewed GST field + resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 18 | Model | Booking & Retail Dump | TYPED SOURCE_SYSTEM | journey_products model/variant snapshots | REUSE |
| 19 | Model Variant | Booking & Retail Dump | TYPED SOURCE_SYSTEM | journey_products model/variant snapshots | REUSE |
| 20 | Deal Type | Booking Docket (Sales Contract) | POST_DELIVERY RESOLUTION | bookings.deal_type_code / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 21 | Out of scope reasons | Booking Docket (Sales Contract) | POST_DELIVERY RESOLUTION | reviewed Booking Docket field + resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 22 | Registration Number | RTO Paper | POST_DELIVERY RESOLUTION | registration_records.registration_number / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 23 | Registration State | RTO Paper | POST_DELIVERY RESOLUTION | registration_records.registration_state / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 24 | Territory Categorization | RTO Paper | POST_DELIVERY RESOLUTION | registration_records.registration_territory / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 25 | Registration District | RTO Paper | POST_DELIVERY RESOLUTION | registration_records.registration_district / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 26 | State | RTO Paper | POST_DELIVERY RESOLUTION | registration_records.registration_state / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 27 | New Car Chasiss No. | Tax Invoice — DMS | POST_DELIVERY RESOLUTION | vehicle_records.chassis_number / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 28 | Finance Type | Bank DO | POST_DELIVERY RESOLUTION | finance_records.finance_type_code / resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 29 | Bank Name | Bank Statement | POST_DELIVERY RESOLUTION | finance_records.provider_name/details or resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 30 | First receipt date | Money Receipt | REPEATED COLLECTION / DERIVED | payments/dealer_receipt_review_values (earliest receipt_date) | REUSE; aggregate query/report rule |
| 31 | Exchange (Y/N) | Booking Docket (Sales Contract) | POST_DELIVERY RESOLUTION | booking_form_review_values.exchange_applicable / trade_in_case + resolution | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 32 | DSA Commsission | Booking Docket (Sales Contract) | POST_DELIVERY RESOLUTION | reviewed Booking Docket field + resolution snapshot | SMALLEST EXTENSION: final resolution snapshot; canonical source key mapping required |
| 33 | File updated (Date) | NA | AUDIT/WORKFLOW | journey_workflow_events / reviewed_by metadata | REUSE; projection rule needed |
| 34 | File updated by (PC name) | NA | AUDIT/WORKFLOW | journey_workflow_events / reviewed_by metadata | REUSE; projection rule needed |
| 35 | Ex Showroom | NA | TYPED STANDARD | price_list_items / commercial_lines.standard_amount | REUSE; component_key mapping needed |
| 36 | Registration Type | NA | TYPED STANDARD | price_list_items / commercial_lines.standard_amount | REUSE; component_key mapping needed |
| 37 | Insurance | NA | TYPED STANDARD | insurance_records.standard_premium_amount | REUSE |
| 38 | Accessories | NA | TYPED STANDARD | journey_addons.standard_amount | REUSE; addon_type key mapping needed |
| 39 | RSA | NA | TYPED STANDARD | journey_addons.standard_amount | REUSE; addon_type key mapping needed |
| 40 | EW | NA | TYPED STANDARD | journey_addons.standard_amount | REUSE; addon_type key mapping needed |
| 41 | Fast tag | NA | TYPED STANDARD | journey_addons.standard_amount | REUSE; addon_type key mapping needed |
| 42 | TCS | NA | TYPED STANDARD | price_list_items / commercial_lines.standard_amount | REUSE; component_key mapping needed |
| 43 | Other Charges- Standard Amount | NA | TYPED STANDARD | price_list_items / commercial_lines.standard_amount | REUSE; component_key mapping needed |
| 44 | HP Charges | NA | TYPED STANDARD | price_list_items / commercial_lines.standard_amount | REUSE; component_key mapping needed |
| 45 | Green tax- Standard Amount | NA | TYPED STANDARD | price_list_items / commercial_lines.standard_amount | REUSE; component_key mapping needed |
| 46 | Standard Deal | NA | COMPUTED/STANDARD | price_list_items + commercial_lines.standard_amount + discount_applications.standard_eligible_amount | REUSE; formula/config mapping needed |
| 47 | Sales Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 48 | Buffer Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 49 | Inhouse Insurance Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 50 | MR Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 51 | Other Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 52 | Self Insurance Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 53 | Corporate Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 54 | Exchange Discount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 55 | Upword | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 56 | Scrap | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 57 | FOC Accessories | NA | TYPED STANDARD | journey_addons.standard_amount | REUSE; addon_type key mapping needed |
| 58 | EW Discount- Standard Amount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 59 | Price protection | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 60 | Loyalty discount- Standard Amount | NA | TYPED STANDARD / CONFIG | discount schemes + discount_applications.standard_eligible_amount | REUSE generic discount structure; key mapping needed |
| 61 | Standard Total Discount | NA | COMPUTED/STANDARD | price_list_items + commercial_lines.standard_amount + discount_applications.standard_eligible_amount | REUSE; formula/config mapping needed |
| 62 | Net Standard Deal | NA | COMPUTED/STANDARD | price_list_items + commercial_lines.standard_amount + discount_applications.standard_eligible_amount | REUSE; formula/config mapping needed |
| 63 | Ex Showroom | RTO Paper | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → commercial_lines.actual_amount | SMALLEST EXTENSION + projection |
| 64 | Registration Type | RTO Paper | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → commercial_lines.actual_amount | SMALLEST EXTENSION + projection |
| 65 | Insurance | Insurance Cover Note | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → insurance_records.actual_premium_amount | SMALLEST EXTENSION + projection |
| 66 | Accessories | Accessory Invoice — Tally / bookkeeping software | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → journey_addons.actual_amount | SMALLEST EXTENSION + projection |
| 67 | RSA | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → journey_addons.actual_amount | SMALLEST EXTENSION + projection |
| 68 | EW | EW Tally Invoice | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → journey_addons.actual_amount | SMALLEST EXTENSION + projection |
| 69 | Fast tag | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → journey_addons.actual_amount | SMALLEST EXTENSION + projection |
| 70 | TCS | Tax Invoice — DMS | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → commercial_lines.actual_amount | SMALLEST EXTENSION + projection |
| 71 | Other Charges | NA | TYPED/COMPUTED ACTUAL | commercial_lines.actual_amount | REUSE; source/formula mapping needed |
| 72 | HP Charges | RTO Paper | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → commercial_lines.actual_amount | SMALLEST EXTENSION + projection |
| 73 | Green tax- actual Amount | NA | TYPED/COMPUTED ACTUAL | commercial_lines.actual_amount | REUSE; source/formula mapping needed |
| 74 | Actual Deal | NA | COMPUTED/ACTUAL | commercial_lines.actual_amount + discount_applications.actual_discount_amount | REUSE; formula needed |
| 75 | Sales Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 76 | Buffer Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 77 | Inhouse Insurance Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 78 | MR Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 79 | Other Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 80 | Self Insurance Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 81 | Corporate Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 82 | Exchange Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 83 | Upword | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 84 | Scrap | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 85 | FOC Accessories | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → journey_addons.actual_amount | SMALLEST EXTENSION + projection |
| 86 | EW Discount- actual Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 87 | Price protection | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 88 | Loyalty discount- actual Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 89 | Total- actual Discount | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → discount_applications.actual_discount_amount | SMALLEST EXTENSION + projection |
| 90 | Standard Deal | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → commercial_lines.actual_amount | SMALLEST EXTENSION + projection |
| 91 | Actual Deal | Customer Ledger | POST_DELIVERY RESOLUTION → TYPED | resolution snapshot → commercial_lines.actual_amount | SMALLEST EXTENSION + projection |
| 92 | Variance | NA | COMPUTED | standard vs actual commercial outputs | REUSE inputs; calculation/report logic needed |
| 95 | Error Summary | (blank) | COMPUTED/AUDIT | audit_evaluations + audit_findings | REUSE; projection logic needed |
| 96 | Bank Transfer | Customer Ledger | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 97 | Cash/DD | Customer Ledger | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 98 | Cheque | Cheque photo | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 99 | DO | Bank DO | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 100 | PO | Purchase Order (PO) | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 101 | Trade-in | RC / Transfer Letter / Authorization Letter (exchange) | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 102 | Refund | Customer Ledger | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 103 | Total | NA | COMPUTED/REPEATED | aggregate of payment/reconciliation block | REUSE inputs; aggregation rule needed |
| 106 | Bank Transfer | Bank Statement | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 107 | Cash/DD | Cash Ledger | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 108 | Cheque | Bank Statement | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 109 | DO | Bank Statement | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 110 | PO | Bank Statement | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 111 | Trade-in | Bank Statement | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 112 | Refund | Bank Statement | REPEATED COLLECTION / AGGREGATE | payments + reviewed source documents/finance/trade-in evidence | REUSE; no scalar resolution row |
| 113 | Total | (blank) | COMPUTED/REPEATED | aggregate of payment/reconciliation block | REUSE inputs; aggregation rule needed |
| 116 | Error Summary | NA | COMPUTED/AUDIT | audit_evaluations + audit_findings | REUSE; projection logic needed |
| 117 | PC Remarks | NA | AUDIT/REVIEW | finding_remarks / review_decisions by role | REUSE; report selection/aggregation rule needed |
| 118 | TL Remarks | NA | AUDIT/REVIEW | finding_remarks / review_decisions by role | REUSE; report selection/aggregation rule needed |
| 119 | PMO Remarks | NA | AUDIT/REVIEW | finding_remarks / review_decisions by role | REUSE; report selection/aggregation rule needed |

## Structural conclusions

### 1. The final-report contract does **not** justify a new generic report-state table

Existing structures remain sufficient:

- typed Journey/Booking/Delivery/Customer/Product/Registration/Finance/Insurance/Addon/Trade-In domains;
- `commercial_lines.standard_amount / actual_amount`;
- `discount_applications.standard_eligible_amount / actual_discount_amount`;
- repeatable `payments`;
- durable reviewed DI fields;
- audit evaluations/findings and workflow history.

The report is a projection of these structures plus sparse `POST_DELIVERY` final-source resolutions.

### 2. Typed repeated Invoice entity is **not required by this report contract**

The report asks only for scalar `DMS Invoice Date` and `DMS Invoice Number`, both with final source `Tax Invoice — DMS`.

Multiple invoice documents must remain distinct by document/fact identity, but the report only needs an approved scalar winner for those outputs. The existing durable reviewed-field layer plus a `POST_DELIVERY` resolution row can select the exact DMS invoice field/document.

Therefore:

**Repeated typed Invoice entity: NOT REQUIRED FOR FINAL REPORT V2 by current evidence.**

Do not add an Invoice table for this stabilization unit. A future rule that genuinely needs invoice-level repeated business rows would be a separate evidence-backed requirement.

### 3. Final-source policy is now frozen at business-source-label level

The supplied Final Source of Truth list resolves the earlier ambiguity for report outputs. Examples:

- DMS Invoice Date/Number -> `Tax Invoice — DMS`;
- Delivery Date -> `Gate Pass`;
- identity/KYC outputs -> `Customer KYC`;
- registration outputs -> `RTO Paper`;
- chassis -> `Tax Invoice — DMS`;
- Finance Type -> `Bank DO`;
- Bank Name -> `Bank Statement`;
- actual Insurance -> `Insurance Cover Note`;
- actual Accessories -> `Accessory Invoice — Tally / bookkeeping software`;
- actual EW -> `EW Tally Invoice`;
- many actual discounts -> `Customer Ledger`.

This removes the need for a blanket `Delivery wins`, `latest wins`, or `highest confidence wins` policy for those report fields.

The remaining technical work is to map each approved business source label to the authoritative DI/source-system canonical key(s). That mapping remains Step-2 contract validation where Audit Core evidence cannot prove the emitted DI key.

### 4. `Booking & Retail Dump` requires a non-DI final-source path

`Type of customer`, `Model`, and `Model Variant` use `Booking & Retail Dump` as the approved final source.

These should consume stable typed/source-system snapshots (`customers.customer_type_code`, `journey_products` model/variant snapshots), not force a fake DI reviewed-field reference.

Therefore the proposed `source_reviewed_field_id` extension on `journey_attribute_resolutions` must be **nullable**. Document-derived resolutions reference the reviewed field; source-system/typed resolution can use the existing owning-domain/reference fields and a resolved-value snapshot.

### 5. Existing commercial structures are sufficient

The base schema already supports:

- `price_list_items.standard_amount`;
- `commercial_lines.standard_amount` and `actual_amount`;
- `discount_applications.standard_eligible_amount` and `actual_discount_amount`;
- `insurance_records.standard_premium_amount` / `actual_premium_amount`;
- `journey_addons.standard_amount` / `actual_amount`.

Therefore the Standard and Actual commercial sections do not justify new per-column tables. Exact report labels map to component/discount/addon keys through configuration.

### 6. Payment sections remain repeated/aggregate outputs

The two payment/reconciliation blocks are not scalar evidence fields.

Existing `payments` is already repeatable. Report rows such as Bank Transfer, Cash/DD, Cheque, DO, PO, Trade-in, Refund and Total must be produced by explicit aggregation/reconciliation rules over repeated payment/source evidence.

The supplied source labels define where each category is checked; they do **not** define the arithmetic/aggregation formula. Those formulas must be configured/tested rather than guessed.

### 7. Remarks and Error Summary can reuse audit/review state

- Error Summary -> `audit_evaluations` / `audit_findings`.
- PC/TL/PMO Remarks -> existing `finding_remarks` and/or `review_decisions` with actor-role identity.

No generic remarks table is justified. Exact report selection semantics (latest vs concatenated vs finding-scoped) must be frozen in the report projection contract.

## Remaining verified implementation impact

After separate approval, the smallest Audit Core implementation remains:

1. minimally extend `journey_attribute_resolutions` for a resolved value snapshot and nullable selected reviewed-field reference;
2. implement explicit post-Delivery final-source confirm/read APIs;
3. consume the business-approved source matrix above, with technical canonical-key mapping supplied by authoritative DI/source contracts;
4. project final selected values into existing typed commercial/domain owners only where already approved;
5. create/reuse the post-Delivery rule-run workflow task and gate report readiness on completion;
6. implement report projection/aggregation using the exact repeated row order above.

## Remaining UNKNOWN / external contract items

- exact DI canonical key(s) corresponding to business labels such as `RTO Paper`, `Customer KYC`, `Money Receipt`, `Customer Ledger`, `Bank Statement`, etc.;
- exact aggregation formulas for the two payment/reconciliation blocks;
- exact role/selection rule for multiple PC/TL/PMO remarks;
- any rule-specific requirement for invoice-level repeated business rows beyond this final report (none is established by this report contract).

No schema/application code is authorized by this document.
