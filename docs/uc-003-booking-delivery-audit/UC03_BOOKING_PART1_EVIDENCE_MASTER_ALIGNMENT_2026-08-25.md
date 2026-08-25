# UC03 Booking Part 1 — Evidence, Review and Product Master Alignment

**Date:** 2026-08-25  
**Status:** OWNER DECISION — IMPLEMENTATION BASELINE  
**Scope:** C1 Booking Part 1 only

## 1. Purpose

This amendment freezes the first Booking capture segment before any later PC-entered business inputs are designed or implemented.

Part 1 is evidence-led. The Process Consultant (PC) does not re-key facts that are available from the approved Booking evidence.

## 2. Mandatory Part-1 evidence

A Booking Part-1 case requires:

1. **Booking Form / Booking Docket** — at least one;
2. **Customer KYC** — at least one of:
   - PAN; or
   - Aadhaar.
   Both are preferred and both may be uploaded, but the minimum requirement is one identity document;
3. **Booking Payment Receipt** — at least one receipt for payments made as part of Booking.

Address Proof is **not** a separate mandatory Part-1 requirement.

## 3. Multiple Booking payments

A customer may make more than one payment during Booking.

Therefore the Booking Payment Receipt requirement is repeatable:

```text
Booking Payment Receipt
  -> Receipt 1
  -> Receipt 2
  -> ...
```

Every uploaded receipt remains an independent evidence record with its own DI processing result, provenance and PC review decisions. Uploading another receipt must not replace or hide an earlier active receipt.

The accepted payment domain keeps receipt number distinct from transaction/payment reference (for example UTR, cheque/DD reference or another payment reference). These values must not be collapsed into one field.

## 4. Extraction and PC decision

For Part 1 the flow is:

```text
Upload evidence
 -> DI classify/extract
 -> Audit Core receives provenance-bearing proposals
 -> PC compares extracted value with source evidence
 -> PC accepts or corrects the value
 -> accepted value is persisted in the approved Audit Core owner where that owner is already defined
```

The original DI machine value and provenance remain immutable.

For KYC source-specific values, PAN and Aadhaar remain independent evidence proposals. Part 1 does not invent a precedence rule when both are supplied and disagree.

## 5. Model / Variant master rule

Vehicle Model and Variant are not PC free-text inputs.

The Booking Docket supplies the extracted Model and Variant strings. Audit Core resolves those strings against the **effective Project Product Master for the Actual Booking Date**.

Resolution is deterministic only. No fuzzy substitution and no arbitrary same-WEF tie-break are introduced.

A successful resolution returns the canonical Product Master Model/Variant and records the master version used. If Model/Variant identifies one canonical pair but several sellable SKUs exist below that pair, Part 1 records the canonical Model/Variant without arbitrarily selecting a SKU.

The PC approves the resolved master value; the PC does not type a replacement Model or Variant.

## 6. Product Master mismatch

If the extracted Model/Variant cannot be uniquely resolved to the effective Project Product Master, the UI must not expose a free-text fallback.

The PC records the mismatch as a human Booking Flag observation. The source evidence, extracted Model/Variant and resolution outcome are retained with that observation.

The mismatch flag is an audit observation; it does not rewrite Product Master data.

## 7. Part-1 completion semantics

Part 1 is ready to move to the later PC-input segment when:

- Booking Docket evidence exists and processing/review is complete;
- at least one of PAN/Aadhaar exists and its published Part-1 proposals are reviewed;
- at least one Booking Payment Receipt exists;
- every uploaded Booking Payment Receipt has completed processing and its published receipt proposals are reviewed;
- Model/Variant has either:
  - been resolved to Product Master and approved by the PC; or
  - been explicitly recorded by the PC as a Product Master mismatch Flag observation;
- no other published Part-1 proposal remains awaiting the PC's decision.

This Part-1 gate is not the final Booking closure gate. Part 2 and later Booking audit work remain separate.

## 8. Out of scope for this increment

This amendment does not define or implement the later PC-entered Booking inputs, Delivery capture, final Booking closure, rule catalogue expansion, or a new Product Master administration workflow.
