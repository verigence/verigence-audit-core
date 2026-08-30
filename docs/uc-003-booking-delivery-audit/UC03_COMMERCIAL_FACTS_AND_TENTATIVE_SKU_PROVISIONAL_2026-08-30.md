# UC03 Commercial Facts + Tentative SKU Resolution — PROVISIONAL

Date: 2026-08-30  
Status: **PROVISIONAL — discussion/design checkpoint, not a final TL/PM view design**

## 1. Purpose

This note preserves the decisions made while fixing two immediate UC03 gaps:

1. commercial facts extracted from documents must not be discarded before Audit can use them; and
2. Booking Form model + variant text may not uniquely identify the exact Product SKU, so Audit Core needs an advisory SKU-candidate service using Product/Price masters.

The wider Review/TL/PM experience, Deal Integrity views, notification presentation, and final confirmation workflow are **not finalized by this note**.

## 2. Ownership boundary

### DI remains the source of truth for document extraction

- DI stores the document and extracted machine facts.
- Audit Core does **not** copy the entire DI extraction payload into its own transactional model.
- Audit Core consumes required extracted facts through the DI contract/API when business logic needs them.
- Machine value, extraction confidence and source lineage remain DI facts.

### Audit Core remains the source of truth for business interpretation

- Product/Price masters remain Audit Core business masters.
- SKU deduction is business logic and therefore belongs in Audit Core.
- A deduced SKU is never treated as a document-extracted fact.

## 3. Commercial-fact publication rule

Commercial facts are a cross-document exception to narrow UC03 document field allow-lists.

If DI extracts a canonical field whose semantic key represents money, price, cost, tax, discount, payment, balance, finance, invoice or similar commercial meaning, that field remains available to the audit-consumption stream even when the document type is not explicitly enumerated in the UC03 Booking profile.

Examples include:

- `ex_showroom_price`
- `insurance_amount`
- `road_tax_registration`
- `accessories_cost`
- `other_charges`
- `total_price`
- `booking_amount_paid`
- `balance_amount`
- `mode_of_payment`
- `payment_reference_no`
- `invoice_value`
- `dealer_discount_amount`
- `emi_amount`

This does **not** make unrelated identity or personal fields authoritative. For example, a Booking Form customer name remains non-authoritative even though its commercial facts are retained.

## 4. Tentative SKU candidate API

### Endpoint

`POST /v2/tenants/{tenant_id}/journeys/{journey_id}/booking/sku-candidates`

### Input

```json
{
  "modelName": "XUV 700",
  "variantName": "AX7L",
  "totalCommercialAmount": 2500000,
  "currencyCode": "INR",
  "maxCandidates": 5
}
```

The intended caller supplies machine-observed Booking Form facts obtained from DI. Audit Core does not persist a second copy merely to perform this comparison.

### Output rule

The API returns a ranked shortlist. Every returned item is always:

- `candidateStatus = TENTATIVE`
- `confirmationRequired = true`

Even an exact score must **not** auto-confirm the SKU.

## 5. Processing design

### Chosen approach: Master SQL + deterministic Python ranking

The initial implementation intentionally does **not** use an LLM, embeddings or a vector database.

#### Stage A — SQL master narrowing

Audit Core uses the Booking business date and Project context to read only:

- effective published Project Product Master versions;
- active Product/SKU rows; and
- the effective Price List version, preferring the Booking-selected Price List where present.

Price components for each SKU are summed into a comparable master commercial total.

This keeps the candidate population bounded to the Project's actual sellable configurations rather than comparing against an unconstrained catalogue.

#### Stage B — Python deterministic scoring

Python scores the bounded candidate set using three explainable signals:

| Signal | Weight |
|---|---:|
| Model-name similarity | 40% |
| Variant-name similarity | 35% |
| Total-commercial proximity | 25% |

Text normalization ignores punctuation, case and spacing so values such as `XUV 700` vs `XUV700`, or `AX7-L` vs `AX7 L`, can still match strongly.

Commercial proximity is based on percentage difference between the Booking Form total and the summed effective Price Master components. Commercial distance is intentionally a supporting signal, not the dominant signal, because different documents may present different total conventions.

Candidates below a minimum composite threshold are omitted rather than returning a misleading random SKU.

## 6. Why this processing approach is preferred now

For the current startup-scale master sizes, SQL + deterministic Python gives:

- low runtime cost;
- no additional AI/token cost;
- no new infrastructure;
- predictable latency;
- reproducible results;
- easy audit explanation; and
- straightforward unit testing.

An ML/embedding layer should only be considered later if real confirmation history demonstrates that deterministic matching cannot handle actual dealer naming variability.

## 7. Confirmation semantics

SKU candidate inference is advisory only.

The service does not:

- write `product_sku_id` into the Booking;
- change Product Master data;
- change Price Master data;
- mark a candidate as confirmed; or
- bypass human/business confirmation.

The final confirmation actor, screen, and persistence workflow remain to be designed together with the broader Review experience.

## 8. Important edge cases

1. **Multiple Product Master versions on the same latest effective date for one Segment** — fail with master configuration conflict rather than choose arbitrarily.
2. **No effective Price List** — fail rather than fabricate a price comparison.
3. **Currency mismatch** — reject comparison when the caller supplies a currency different from the effective Price Master currency.
4. **Price master contains only partial commercial components** — text signals can still rank candidates, but the result stays tentative. Component-level comparison can be added later.
5. **Same model/variant/price across multiple colour SKUs** — multiple SKU candidates may legitimately remain. Confirmation is required.

## 9. Deliberately deferred

The following are **not finalized** and must not be inferred from this provisional note:

- TL Review screen design;
- PM Review screen design;
- Deal Integrity/Commercial Integrity view layout;
- final candidate-confirmation UI;
- whether confirmation becomes an explicit audit observation or Booking amendment;
- automatic triggering of SKU inference from DI completion;
- persistence of candidate history;
- use of confirmed matches as future learning data;
- final scoring weights/tolerance after real project data is available; and
- notification/timer behavior in the Review UI.

## 10. Current implementation checkpoint

The immediate implementation is intentionally narrow:

- DI commercial publication fix;
- Audit Core tentative SKU candidate API;
- deterministic ranking tests; and
- this provisional design checkpoint.

After these are validated, the broader Review views can resume without reopening the basic commercial/SKU boundary decision.
