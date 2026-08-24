# UC02 — Mahindra Native Workbook Import

## Decision

For Mahindra Projects, Verigence accepts the OEM-supplied price and policy workbooks in their native layout. SuperAdmin is not required to transpose the OEM file into the Verigence-generated template.

The generated Verigence templates remain supported as a fallback/manual format.

## Supported Mahindra source layouts

### Vehicle & Price Master

The native Passenger Vehicle, Commercial Vehicle and Battery Electric workbooks may contain multiple worksheets. The importer detects the worksheet layout and normalizes each priced vehicle row into the canonical Product/Configuration/SKU structure and a dynamic set of Price Master component rows.

No price sub-line item is hard-coded. Monetary columns are discovered from the workbook headers and normalized to component keys at import time.

The source worksheet name, source row and source WEF detected from the workbook are retained in staged import data for traceability.

### Discount & Policy Master

The Mahindra monthly discount grid is normalized into effective-dated rule parameters. The current August 2026 source includes model-scoped Booking Protection, Agreed Buffer and Insurance OD %, plus project-level narrative controls. Explicit numeric controls such as aged-stock days, bulk-deal quantity, MR percentage, Trade-in holding/profit limits and policy-deviation penalty are extracted as rule parameters while the original policy text is also retained.

Minimum Booking Amount is part of the Booking Policy model but is populated only when supplied by an OEM source; the August workbook does not invent a value.

## Trade-in

Trade-in remains a separate Journey linked to the originating Booking Journey. The Trade-in rule engine reads effective Trade-in policy parameters from the published policy version and can continue evaluating the Trade-in Journey after Booking/Delivery has completed.

## Auditability

Every upload still follows stage/validate -> confirm -> publish. Published master versions remain immutable and rule evaluations reference the effective master version used at evaluation time.
