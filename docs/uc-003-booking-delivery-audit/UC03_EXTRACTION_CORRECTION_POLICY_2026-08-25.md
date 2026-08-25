# UC03 Extraction Correction Confidence Policy

**Date:** 2026-08-25  
**Status:** Product-owner confirmed implementation baseline  
**Scope:** UC03 Booking — PC review of DI-extracted values

## 1. Principle

DI-extracted values remain machine proposals until the Process Consultant (PC) reviews them through the Get Details flow. The PC may approve an extracted value or correct it. Every correction must preserve the original machine value, the corrected value, DI confidence, source evidence, actor and timestamp.

## 2. Confidence threshold

The threshold is **90%**.

- **Confidence < 90%**
  - PC correction is allowed.
  - Corrected value becomes authoritative in the main typed transaction immediately.
  - Original DI value remains immutable in the proposal/audit history.
  - Audit Core automatically raises an **INFO** Booking Flag.
  - The Flag records the field, value changed from, value changed to, DI confidence, source evidence, actor and timestamp.
  - The Flag is non-blocking by default.

- **Confidence >= 90%**
  - PC correction is allowed.
  - Corrected value still becomes authoritative in the main typed transaction immediately.
  - Original DI value remains immutable in the proposal/audit history.
  - Audit Core automatically raises a **HIGH** Booking Flag.
  - The Flag records the field, value changed from, value changed to, DI confidence, source evidence, actor and timestamp.
  - The Flag must be reviewed by TL through the existing UC03 Flag review lifecycle.
  - The correction is not rolled back while TL review is pending.

## 3. Approval path

If the PC approves the DI-extracted value without changing it:

- the proposal becomes ACCEPTED;
- the accepted value is written to the approved typed-domain destination;
- no correction Flag is created.

## 4. Atomicity

Correction persistence and automatic Flag creation are one Audit Core transaction. A corrected value must never be committed without its corresponding correction Flag.

## 5. Audit provenance

For every correction, retain:

- proposal ID;
- canonical field key;
- source evidence ID;
- original DI machine value;
- corrected/accepted value;
- DI confidence score;
- correcting actor and operating role;
- correction timestamp;
- owning typed-domain record reference;
- automatically created Flag ID and severity.

The existing proposal row keeps the machine value immutable. The correction updates accepted_value and typed-domain persistence; it does not overwrite proposed_value.

## 6. Flag semantics

Automatic correction Flag category: `DOCUMENT_EXCEPTION` unless a more specific approved category is defined later for a field.

Titles:

- `<90%`: `DI value corrected by PC`
- `>=90%`: `High-confidence DI value corrected — TL review required`

The Flag records the field and the changed-from / changed-to values. Sensitive values are kept out of workflow event `safe_payload`; the Flag record itself is the controlled audit record for the correction.

## 7. TL review

The existing UC03 audit Flag lifecycle is reused. TL can review/acknowledge/resolve the HIGH correction Flag through the existing audit review authority. No separate correction-approval workflow is introduced for this rule.
