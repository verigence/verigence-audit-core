# UC03 Commercial Facts + Booking SKU Resolution — PROVISIONAL

Date: 2026-08-30  
Status: **PROVISIONAL — discussion/design checkpoint, not a final TL/PM view design**

## 1. Purpose

This note preserves the decisions made while fixing two immediate UC03 gaps:

1. commercial facts extracted from documents must not be discarded before Audit can use them; and
2. Audit Core should resolve a Product SKU directly from Booking Form evidence when the evidence uniquely identifies one master row, while retaining tentative candidates only when the Booking evidence remains ambiguous.

The wider Review/TL/PM experience, Deal Integrity views, notification presentation, and Delivery validation workflow are **not finalized by this note**.

## 2. Ownership boundary

### DI remains the source of truth for document extraction

- DI stores the document and extracted machine facts.
- Audit Core does **not** copy the entire DI extraction payload into its transactional model.
- Audit Core consumes required extracted facts through the DI contract/API when business logic needs them.
- Machine value, extraction confidence and source lineage remain DI facts.
- If an SKU/product code is explicitly printed on a Booking Form, DI may extract `sku_code` exactly as visible; DI must never infer that code.

### Audit Core remains the source of truth for business interpretation

- Product/Price masters remain Audit Core business masters.
- Mapping Booking evidence to a Product SKU is business logic and therefore belongs in Audit Core.
- A derived SKU is an Audit Core interpretation based on evidence + masters, not a DI-extracted fact unless the SKU code itself was explicitly visible.

## 3. Commercial-fact publication rule

Commercial facts are a cross-document exception to narrow UC03 document field allow-lists.

If DI extracts a canonical field whose semantic key represents money, price, cost, tax, discount, payment, balance, finance, invoice or similar commercial meaning, that field remains available to the audit-consumption stream even when the document type is not explicitly enumerated in the UC03 Booking profile.

Examples include `ex_showroom_price`, `insurance_amount`, `road_tax_registration`, `accessories_cost`, `other_charges`, `total_price`, `booking_amount_paid`, `balance_amount`, `mode_of_payment`, `payment_reference_no`, `invoice_value`, `dealer_discount_amount`, and `emi_amount`.

This does **not** make unrelated identity or personal fields authoritative.

## 4. Booking SKU resolution API

### Endpoint

`POST /v2/tenants/{tenant_id}/journeys/{journey_id}/booking/sku-candidates`

### Input

```json
{
  "modelName": "XUV 700",
  "variantName": "AX7L",
  "colourName": "Red Rage",
  "skuCode": "XUV700-AX7L-R",
  "totalCommercialAmount": 2500000,
  "currencyCode": "INR",
  "maxCandidates": 5
}
```

`variantName`, `colourName`, and `skuCode` are optional. Model and Booking commercial total are the primary comparison facts. The intended caller supplies machine-observed Booking Form facts obtained from DI.

## 5. Resolution hierarchy

The service must apply the following hierarchy before fuzzy ranking.

### A. Explicit SKU code from Booking Form

If DI extracted an explicit `sku_code` and it maps to exactly one active/effective Product Master row:

- use that SKU directly;
- store the Journey product as `CONFIRMED` for Booking resolution;
- do **not** add the tentative `*` marker; and
- still validate the booked product later against Delivery Invoice evidence.

Selection method: `BOOKING_DIRECT_SKU_V1`.

### B. Unique model + price mapping

When no explicit SKU code resolves the product, Audit Core compares the Booking Form model and Booking total against effective Product/Price masters.

A row is eligible for direct Booking resolution only when:

- the normalized Booking model exactly matches the master model; and
- the effective master commercial total is within the strict Booking-price tolerance.

Variant and colour, when present in Booking evidence, are deterministic narrowing signals. If these Booking facts leave **exactly one** master row:

- store that SKU as `CONFIRMED` for Booking resolution;
- do **not** show `*`; and
- validate it later against Delivery Invoice evidence.

Selection method: `BOOKING_MODEL_PRICE_UNIQUE_V1`.

### C. Multiple matching master rows

If the Booking evidence leaves more than one matching SKU row, the result is ambiguous.

Audit Core ranks those matching rows and:

- returns the shortlist;
- persists only the most likely row in `journey_products`;
- sets `selection_status = TENTATIVE`;
- sets `selection_method = BOOKING_MODEL_PRICE_MULTI_V1`; and
- displays the tentative SKU with `*`.

Example:

`XUV700 AX7 L *`

with:

`* Tentative — multiple Booking matches; confirmation required`

### D. No strict Booking match

If no row meets the strict model/price rule, Audit Core may use deterministic fuzzy ranking across the bounded effective SKU set. Any result produced only by this fallback remains `TENTATIVE`; it is never silently promoted to confirmed.

If no candidate crosses the reliability floor, no SKU is written.

## 6. Processing design

### Stage A — SQL master narrowing

Audit Core uses the Booking business date and Project context to read only:

- effective published Project Product Master versions;
- active Product/SKU rows; and
- the effective Price List version, preferring the Booking-selected Price List where present.

Price components for each SKU are summed into a comparable master commercial total.

### Stage B — deterministic Booking resolution

Before fuzzy scoring, the script attempts deterministic resolution in this order:

1. exact explicit Booking SKU code;
2. exact normalized Booking model + close Booking price;
3. optional variant narrowing;
4. optional colour narrowing.

Only a single remaining row is treated as directly resolved.

### Stage C — deterministic ranking for ambiguity

When multiple rows remain, Python ranks the bounded candidate set using explainable model, variant and commercial-proximity signals. This is used to order candidates, not to pretend that ambiguity has disappeared.

The implementation intentionally does **not** use an LLM, embeddings or a vector database.

## 7. Delivery Invoice validation

A SKU that is uniquely resolved from Booking Form evidence does not need to remain tentative merely because Delivery has not happened yet.

Delivery provides a separate lifecycle validation. When Delivery Invoice evidence becomes available, Audit should compare the delivered model/SKU facts with the Booking-resolved product and raise a mismatch when they disagree.

Therefore:

- Booking resolution answers **what was booked**;
- Delivery validation answers **whether what was delivered matches what was booked**.

These are separate checks and should not be collapsed into one tentative status.

## 8. Existing confirmed SKU protection

If `journey_products.selection_status = CONFIRMED` already exists, a later Booking inference/resolution call must not replace or downgrade it. Candidate diagnostics may still be returned, but the existing confirmed Journey product remains unchanged.

## 9. Important edge cases

1. **Multiple Product Master versions on the same latest effective date for one Segment** — fail with master configuration conflict rather than choose arbitrarily.
2. **No effective Price List** — fail rather than fabricate a price comparison.
3. **Currency mismatch** — reject comparison when the Booking currency differs from the effective Price Master currency.
4. **Unique exact model + close price** — resolve directly; no tentative marker.
5. **Same model/price across multiple SKU rows** — retain multiple candidates as tentative; variant/colour may narrow only when explicitly present in Booking evidence.
6. **Price outside strict direct-resolution tolerance** — fuzzy ranking may still suggest candidates, but they remain tentative.
7. **Existing confirmed product** — never downgrade or replace it.

## 10. Deliberately deferred

The following remain outside this provisional note:

- TL Review screen design;
- PM Review screen design;
- Deal Integrity/Commercial Integrity view layout;
- final candidate-confirmation UI for genuinely ambiguous bookings;
- automatic triggering of SKU resolution from DI completion;
- persistence of the full candidate-history list;
- final price tolerance after real project data is available; and
- exact Delivery Invoice mismatch presentation.

## 11. Current implementation checkpoint

The immediate implementation now covers:

- DI commercial publication;
- explicit Booking `sku_code` extraction/publication when actually visible;
- direct SKU mapping when an explicit SKU code uniquely resolves;
- unique Booking model + price resolution without a tentative marker;
- tentative shortlist only when multiple/fuzzy matches remain;
- protection of already confirmed Journey products; and
- later Delivery Invoice validation kept as an independent lifecycle check.
