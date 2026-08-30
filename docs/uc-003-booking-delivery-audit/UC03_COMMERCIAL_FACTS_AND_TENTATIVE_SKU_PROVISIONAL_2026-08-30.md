# UC03 Commercial Facts + Booking SKU Resolution — PROVISIONAL

Date: 2026-08-30  
Status: **PROVISIONAL — discussion/design checkpoint, not a final TL/PM view design**

## 1. Purpose

This note preserves the decisions made while fixing two immediate UC03 gaps:

1. commercial facts extracted from documents must not be discarded before Audit can use them; and
2. Audit Core should resolve a Product SKU directly from Booking Form evidence only when that evidence maps exactly to the effective masters, while retaining tentative candidates only when more than one exact master row remains.

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

There is **no price tolerance and no fuzzy fallback**.

### A. Explicit SKU code from Booking Form

If DI extracted an explicit `sku_code` and it maps to exactly one active/effective Product Master row:

- use that SKU directly;
- store the Journey product as `CONFIRMED` for Booking resolution;
- do **not** add the tentative `*` marker; and
- still validate the booked product later against Delivery Invoice evidence.

Selection method: `BOOKING_DIRECT_SKU_V1`.

If the same explicit SKU code maps to more than one effective master row, treat that as a Product Master configuration conflict rather than choosing arbitrarily.

### B. Exact model + exact commercial total

When no explicit SKU code resolves the product, Audit Core compares the Booking Form against the effective Product/Price masters.

A master row is eligible only when:

- the Booking model is an exact **format-normalized** match to the master model; and
- the summed effective master commercial total is **numerically identical** to the Booking Form total commercial amount.

Format normalization may ignore case, punctuation and spacing only. For example `XUV 700` and `XUV700` are treated as the same label. Semantic/fuzzy substitutions are not allowed.

The commercial amount has **zero tolerance**. A difference of even ₹1 means the row is not an exact match.

Variant and colour, when explicitly available in Booking evidence, may narrow an already exact model/price set. They do not expand the match set.

If these facts leave **exactly one** master row:

- store that SKU as `CONFIRMED` for Booking resolution;
- do **not** show `*`; and
- validate it later against Delivery Invoice evidence.

Selection method: `BOOKING_MODEL_PRICE_EXACT_V1`.

### C. Multiple exact matching master rows

If exact Booking model + exact Booking commercial total still leave more than one SKU row, the result is genuinely ambiguous.

Audit Core:

- returns only those exact matching rows;
- may use exact variant/colour evidence to order or narrow them;
- persists only the most likely exact row in `journey_products`;
- sets `selection_status = TENTATIVE`;
- sets `selection_method = BOOKING_MODEL_PRICE_MULTI_EXACT_V1`; and
- displays the tentative SKU with `*`.

Example:

`XUV700 AX7 L *`

with:

`* Tentative — multiple exact Booking matches; confirmation required`

### D. No exact master match

If there is no explicit SKU match and no exact model + exact commercial-total match:

- do **not** select the nearest price;
- do **not** fuzzy-match model or variant;
- do **not** write a tentative SKU;
- raise the domain validation flag/error **`Model not found in masters`** (`VAC-SKU-001`).

This deliberately exposes master/evidence mismatch instead of hiding it behind an inferred SKU.

## 6. Processing design

### Stage A — SQL master narrowing

Audit Core uses the Booking business date and Project context to read only:

- effective published Project Product Master versions;
- active Product/SKU rows; and
- the effective Price List version, preferring the Booking-selected Price List where present.

Price components for each SKU are summed into a comparable master commercial total.

### Stage B — deterministic exact resolution

Python performs only deterministic comparison:

1. exact explicit Booking SKU code, when present;
2. exact format-normalized Booking model;
3. exact numeric Booking commercial total;
4. optional exact variant narrowing;
5. optional exact colour narrowing.

There is no percentage tolerance, rupee tolerance, fuzzy string ranking, LLM, embeddings or vector search in SKU discovery.

### Stage C — ambiguity ordering only

Python ranking is permitted only after multiple **exact** master rows have already been found. It exists to present a stable shortlist; it must never introduce a row that failed exact model + exact commercial-total matching.

## 7. Delivery Invoice validation

A SKU that is uniquely resolved from Booking Form evidence does not need to remain tentative merely because Delivery has not happened yet.

Delivery provides a separate lifecycle validation. When Delivery Invoice evidence becomes available, Audit should compare the delivered model/SKU facts with the Booking-resolved product and raise a mismatch when they disagree.

Therefore:

- Booking resolution answers **what was booked**;
- Delivery validation answers **whether what was delivered matches what was booked**.

These are separate checks and should not be collapsed into one tentative status.

## 8. Existing confirmed SKU protection

If `journey_products.selection_status = CONFIRMED` already exists, a later Booking resolution call must not replace or downgrade it.

## 9. Important edge cases

1. **Multiple Product Master versions on the same latest effective date for one Segment** — fail with master configuration conflict rather than choose arbitrarily.
2. **No effective Price List** — fail rather than fabricate a comparison.
3. **Currency mismatch** — reject comparison when the Booking currency differs from the effective Price Master currency.
4. **Exact model + exact commercial total yields one row** — resolve directly; no tentative marker.
5. **Exact model + exact commercial total yields multiple rows** — keep only those exact rows as tentative candidates; optional exact variant/colour may narrow them.
6. **Booking total differs from master by ₹1 or more** — no match; raise `Model not found in masters`.
7. **Booking model is only semantically/fuzzily similar** — no match; raise `Model not found in masters`.
8. **Existing confirmed product** — never downgrade or replace it.

## 10. Deliberately deferred

The following remain outside this provisional note:

- TL Review screen design;
- PM Review screen design;
- Deal Integrity/Commercial Integrity view layout;
- final candidate-confirmation UI for genuinely ambiguous bookings;
- automatic triggering of SKU resolution from DI completion;
- persistence of the full candidate-history list; and
- exact Delivery Invoice mismatch presentation.

## 11. Current implementation checkpoint

The immediate implementation covers:

- DI commercial publication;
- explicit Booking `sku_code` extraction/publication when actually visible;
- direct SKU mapping when an explicit SKU code uniquely resolves;
- zero-tolerance exact Booking model + commercial-total resolution;
- tentative shortlist only when multiple exact matches remain;
- `Model not found in masters` when there is no exact match;
- protection of already confirmed Journey products; and
- later Delivery Invoice validation kept as an independent lifecycle check.
