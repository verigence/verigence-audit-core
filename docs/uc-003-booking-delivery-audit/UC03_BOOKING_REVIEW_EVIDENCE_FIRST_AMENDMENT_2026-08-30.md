# UC03 Booking Review — Evidence-First Amendment

**Date:** 2026-08-30  
**Status:** IMPLEMENTATION BASELINE  
**Scope:** Booking V2 Review only

## Product decision

Booking Review is an **attribute audit**, not a document browser and not an Excel-entry screen.

The PC must be able to see every extracted value that DI has produced, including values that are not yet mapped to a formal UC03 business attribute. Audit Core must not copy those raw DI values into its database merely to make the Review screen work.

## Review behaviour

1. Booking completion and document extraction remain decoupled. Booking can be completed while DI is still processing.
2. Review opens immediately after Booking completion.
3. While Review is open and documents remain pending, Web refreshes after two minutes.
4. High-confidence, non-conflicting values do not require repetitive human clicks.
5. A populated mapped attribute requires an explicit **Accept / Reject** decision when its resolved value is below the Review confidence threshold or when competing source values mismatch.
6. An unmapped/raw DI field remains visible. It requires **Accept / Reject** when its best available source is low-confidence or multiple documents disagree on the value.
7. Evidence is first-class: the reviewer can open the original DI document, page and bounding box from the value being reviewed.
8. Booking Review cannot be finally confirmed while a required exception decision is unresolved.
9. Reject means **do not project this value into an Audit Core business owner during Booking Review**. It does not delete or mutate the DI machine fact.
10. Accept means the reviewer accepts the current DI fact/reference set for Review. Existing typed-domain governance still decides whether Audit Core may project that attribute.

## Persistence boundary

DI remains source of truth for:

- original document content;
- extracted value;
- confidence;
- page number;
- evidence/bounding region;
- machine fact history.

Audit Core stores only the human decision and DI references needed to prove what was decided:

- Journey/stage;
- review key and kind;
- Accept/Reject;
- DI document ID;
- canonical field ID;
- field key;
- source fact version;
- deterministic source-reference set;
- actor and timestamp.

No extracted value, confidence, page number or bounding box is copied into the decision ledger.

## Stale-decision rule

A decision applies only to the exact DI source-reference set that was present when the reviewer made it. If a source document/fact/version changes, the old decision is treated as stale and the exception must be reviewed again.

## Final confirmation

On **Confirm reviewed values**, Audit Core re-reads DI facts server-side and re-runs resolution. It then:

- blocks if DI is still pending or has failed documents;
- blocks if any current exception lacks a current Accept/Reject decision;
- skips rejected mapped attributes;
- applies accepted/high-confidence mapped attributes only through existing approved typed-domain rules;
- records reference-only attribute resolution provenance;
- marks Booking PC Verification as VERIFIED;
- writes safe workflow metadata only, never PII/raw values.

## Non-goals

This amendment does not change V1 behaviour, does not turn Audit Core into another extracted-field store, does not allow free-text Product/SKU bypass, and does not silently correct DI facts.