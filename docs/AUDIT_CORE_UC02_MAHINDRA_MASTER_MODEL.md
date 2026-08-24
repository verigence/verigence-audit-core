# Audit Core UC02 — Mahindra OEM Segment and Master Model

**Status:** IMPLEMENTATION BASELINE — MAHINDRA OEM EXTENSION  
**Date:** 2026-08-24  
**Repository:** `verigence/verigence-audit-core`  
**Branch:** `feature/uc02-mahindra-segment-masters`

## 1. Scope

This amendment records the UC02 design for the **Mahindra OEM** based on the supplied August 2026 price and discount/policy masters. It preserves the existing generic Audit Core baseline for other OEMs, while removing Product Category from the new Project-onboarding UX/API contract.

The Mahindra-specific segment set is:

- `PASSENGER_VEHICLE` — Passenger Vehicle
- `COMMERCIAL` — Commercial
- `BATTERY_ELECTRIC` — Battery Electric

A Project selects one OEM and one or more OEM Segments. Product Category is a legacy database field only and is no longer a required business input for new Project creation.

## 2. Product hierarchy

The target vehicle hierarchy is:

```text
OEM
  -> OEM Segment
      -> Model
          -> Trim
              -> Vehicle Configuration
                  -> Colour / SKU
```

For Mahindra, Vehicle Configuration carries the discriminator set that can change price within the same trim, including fuel/powertrain, transmission, drive type, seating capacity and extensible OEM attributes.

The existing `product_variants` identity remains available for backward compatibility and represents the trim layer for the new Mahindra path. A separate vehicle-configuration identity is introduced rather than overloading the trim with drivetrain/powertrain semantics.

## 3. Project onboarding

New UC02 Project creation uses:

- Project Name
- OEM
- one or more Segments configured for that OEM
- Effective Start Date
- optional Effective End Date
- Timezone
- optional Region

For Mahindra the Project Administration screen renders the three approved segment values as checkboxes. Segment selections are persisted against the Project and are returned by the Project API.

A Project may select one, two or all three Mahindra segments. Segment selection is Project scope; it does not imply that every model in the OEM catalogue is automatically operational for that Project.

## 4. Mahindra master administration UX

The UC02 Masters step is intentionally simple.

For **each Segment selected on the Project**, the Web UI exposes exactly one:

1. **Vehicle & Price Master upload** — one effective-dated workbook that contains the Segment's vehicle identity/configuration and dynamic commercial price lines. On confirmation Audit Core normalizes the same upload into the segment-scoped Product Master version and its Price List version; the two sources of truth remain separate internally even though the user uploads once.

In addition, the Project exposes one Project-level:

2. **Discount & Policy Master upload** — effective-dated parameters used by the audit/rule engine, including Booking Protection, Minimum Booking Amount, agreed buffer, Insurance OD %, commercial controls and Trade-in policy values.

The UI must not create Vehicle & Price upload cards for Segments that were not selected on the Project.

Both upload paths retain the controlled lifecycle:

```text
Download template
 -> upload workbook + WEF
 -> stage/parse
 -> validate
 -> preview errors
 -> explicit confirm
 -> create DRAFT master version(s)
 -> explicit publish
```

## 5. Price Master — dynamic line items

Price sub-lines are **not predefined in application code**.

The persisted price-line model remains generic:

```text
Price List Version
  -> Vehicle SKU / Configuration
      -> component_key
      -> standard_amount
      -> metadata
```

The Mahindra Segment workbook uses a long-form dynamic price contract: vehicle/configuration identity is repeated for each price component while `component_key`, optional `component_label` and `standard_amount` carry the OEM-defined line. This avoids adding columns or code whenever an OEM introduces a new commercial component.

Mahindra examples such as Ex-showroom, TCS, Insurance 30%, Insurance 50%, Extended Warranty, Accessories Kit, RSA, Fastag, Registration and On-road totals are data supplied by the effective master, not mandatory schema columns.

New OEM price components must therefore be loadable without a Web/API/database schema release. Validation normalizes the supplied component key for stable comparison but does not maintain a hard-coded allow-list of commercial components.

## 6. Discount & Policy Master

The Mahindra August master is not treated as a flat discount amount table. It contains effective-dated parameters consumed by rules.

The Project-level Discount & Policy Master supports scoped key/value parameters such as:

### Booking controls

- `BOOKING_PROTECTION_DAYS`
- `MINIMUM_BOOKING_AMOUNT`

### Commercial / discount controls

- `AGREED_BUFFER`
- `INSURANCE_OD_PERCENT`
- `AGED_STOCK_MAX_DAYS`
- `BULK_DEAL_MIN_QUANTITY`
- `MR_MAX_PERCENT_PREVIOUS_MONTH_RETAIL`
- `GENUINE_ACCESSORY_DISCOUNT_ALLOWED`
- `POLICY_DEVIATION_PENALTY`
- additional OEM-defined parameters

### Trade-in controls

- `TRADE_IN_MAX_HOLDING_DAYS`
- `TRADE_IN_MIN_PROFIT`

Parameters may apply at Project, Segment, Model, Trim or Configuration scope. Values are data; evaluation logic belongs to the rule engine.

For the supplied Mahindra August master, examples include Booking Protection of 30/60 days by product scope, Agreed Buffer of INR 5,000/Nil, Insurance OD percentages, aged-stock threshold of 90 days, bulk-deal threshold of 5 vehicles, MR limit of 5%, trade-in resale within 90 days, minimum trade-in profit of INR 10,000 for Passenger Vehicle and INR 5,000 for Commercial, no discount on genuine accessories and INR 30,000 policy-deviation penalty.

The exact semantic date anchor for Booking Protection remains a rule-definition concern; the master stores the approved number of days and effective version, not hard-coded evaluation logic.

## 7. Trade-in Journey boundary

Trade-in is a **separate Journey** linked to the originating Booking Journey. It must not be embedded as a lifecycle stage of Booking because the traded vehicle can remain open and be resold after the new-vehicle Booking/Delivery Journey has completed.

Conceptually:

```text
Booking Journey
  -> optional linked Trade-in Journey
       -> acquisition/acceptance
       -> valuation and evidence
       -> inventory holding
       -> resale
       -> profit calculation
       -> Trade-in Policy evaluation
```

Closing Booking must not automatically close the linked Trade-in Journey.

## 8. Rule/master traceability

Rule evaluations must retain the exact effective master-version reference used for a decision. Internal version IDs are system-generated traceability references; they are not user-entered Project fields.

Conceptually:

```text
rule_evaluation
  journey_id
  rule_code
  master_type
  master_version_id
  parameter_key
  expected_value
  actual_value
  result
  evaluated_at
```

For example, a Minimum Booking Amount rule records the effective Booking/Discount Policy version used for the booking date. A Trade-in Profit rule records the effective policy version used for the Trade-in evaluation. Historical audit results must remain reproducible after later master uploads.

## 9. Compatibility rule

The existing Product Category table/column is retained temporarily for backward compatibility with older rows and tests, but the field becomes nullable and is removed from the new UC02 creation UX/API requirement. New logic starts from OEM and selected OEM Segments.

Existing non-Mahindra OEMs remain valid. Segment metadata can be added for them without a code change; Mahindra is the first OEM for which a concrete segment catalogue is baselined.

## 10. Validation gate

This extension must pass the repository's normal lint, fresh-database migration and automated test gates before merge to `dev`; DEV deployment must use the tested merged SHA and Alembic head `0014_uc02_mahindra_seg`.
## Universal Segment model

Segment is a platform-wide reference master and is **not owned by an OEM**. All OEM Projects select from the same Phase-1 Segment set:

- `PASSENGER_VEHICLE` — Passenger Vehicle
- `COMMERCIAL` — Commercial
- `BATTERY_ELECTRIC` — Battery Electric

OEM and Segment are independent Project dimensions. Selecting or changing OEM does not change the available Segment choices. Mahindra-specific workbook ingestion remains an OEM adapter; it consumes the Project's universal Segment selections rather than defining them.
