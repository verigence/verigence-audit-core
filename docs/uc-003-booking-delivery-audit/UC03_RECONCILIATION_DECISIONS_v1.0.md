# Verigence UC03 — Reconciliation Decisions

**Document ID:** `VUC03-DR-001`  
**Version:** `1.0`  
**Status:** DESIGN DECISIONS FOR MOCKUP / IMPLEMENTATION DESIGN  
**Date:** 2026-08-22  
**Parent design:** `VUC03-SD-002 / UC03_SOLUTION_DESIGN_v1.1.md`  
**Workflow:** `VUC03-WF-002 / UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`  
**Rule catalog:** `VUC03-RF-001 / UC03_RULE_FLAG_CATALOG_v1.0.md`  
**Field matrix:** `VUC03-FM-001 / UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`

---

## 1. Purpose

This document closes the five highest-impact UC03 design gaps sufficiently to proceed with Android-first, tablet and desktop Web mockups without pretending that unresolved business or privacy rules are final.

Decisions are classified as:

- **FROZEN** — mockups and implementation design may rely on the decision;
- **PROVISIONAL** — safe design direction for Phase-1 planning, subject to UAT/configuration review;
- **DEFERRED** — intentionally not decided in UC03 UI; implementation must keep the boundary configurable.

No production code or database migration is authorized by this document.

---

## 2. DR-01 — document catalogue reconciliation

**Status:** PROVISIONAL / SAFE FOR MOCKUPS

### Source condition

The supplied PC process repeatedly describes **26 documents**, while the numbered applicability diagram enumerates **29 requirement entries**.

### Decision

UC03 SHALL retain **29 provisional document requirement keys** in planning and mockups so that no numbered source requirement is silently lost.

This does **not** assert that the final business catalogue contains 29 distinct physical document types. During UAT, requirements may be:

- grouped into one physical document package;
- renamed;
- identified as aliases of another requirement;
- split by stage/applicability;
- retired if confirmed redundant.

No requirement may be removed solely to force the count back to 26 without explicit business reconciliation.

### UX consequence

The PC UI does not show a fixed statement such as “26 documents required”. It shows only the **currently applicable requirements**, for example:

```text
Documents
13 of 17 applicable items addressed
```

The UI also supports:

```text
9 requirements not applicable to this booking
[See why]
```

### Implementation consequence

Document requirements require stable `requirement_key` values and versioned applicability profiles. Physical document type and business requirement are not assumed to be the same concept.

---

## 3. DR-02 — Aadhaar capture, display and retention boundary

**Status:** FROZEN FOR UX; DEFERRED FOR RAW RETENTION POLICY

### Source condition

The source material is internally mixed:

- Aadhaar appears in Booking KYC requirements;
- Delivery capture identifies Aadhaar as a mandatory human-supplied/verified field;
- the supplied extraction mockup shows a masked Aadhaar value.

The source does not provide a complete approved raw-Aadhaar storage/privacy policy.

### Decision

1. **User-facing Aadhaar is masked by default.**
2. UC03 mockups SHALL NOT display a complete Aadhaar number after capture/verification.
3. UC03 SHALL NOT invent a requirement to persist raw Aadhaar in Audit Core merely because the legacy form contains an Aadhaar field.
4. Booking-stage Aadhaar/KYC evidence and Delivery-stage Aadhaar verification can both exist as audit activities without requiring the client to decide raw-retention policy.
5. Exact raw-value storage, tokenisation, encryption, deletion and evidence-retention policy requires explicit Security/privacy implementation review before production freeze.

### UX direction

Example accepted display:

```text
Aadhaar
XXXXXXXX5033
Verified at Delivery
```

The UI may show source/provenance and verification time, but never exposes hidden digits merely to make the audit screen look complete.

### Rule-engine direction

Rules consume only the approved canonical representation/fact contract. Web/Android never implements Aadhaar matching or duplicate rules directly.

---

## 4. DR-03 — Audit State completion policy

**Status:** FROZEN ARCHITECTURE; DEFAULT POLICY PROVISIONAL

### Decision

Audit State remains independent per stage:

```text
NOT_STARTED
IN_PROGRESS
COMPLETE
```

A stage is `COMPLETE` when its **published audit completion policy** is satisfied. Completion is not equivalent to “no flags”.

### Default Phase-1 direction

The completion policy may require all of the following classes of work:

1. applicable capture/requirement questions are addressed;
2. required evidence processing has either completed or received an explicit approved unavailable/manual disposition;
3. required rule evaluations have executed;
4. mandatory PC remarks/answers are present;
5. any flag whose published review policy requires TL/PM/Executive disposition before stage completion has received that disposition.

### Important distinction

An ordinary open flag does **not** automatically keep Audit State `IN_PROGRESS`.

A published rule can carry a policy such as:

```text
requires_review_before_stage_complete = true
review_roles = [TL, PM, EXECUTIVE]
```

Only then does required review become part of Audit State completion.

### UX consequence

PC work and overall Audit State are displayed separately.

Example:

```text
Your capture: Complete
Audit State: In Progress
Reason: 1 critical flag awaiting Team Lead review
```

This avoids making the PC think their work is unfinished when the remaining action belongs to TL/PM.

### Business progression consequence

Audit State never blocks recording `DELIVERY_STARTED` or `DELIVERY_COMPLETED`.

---

## 5. DR-04 — extraction source mapping for 57 extracted fields

**Status:** FROZEN PROCESS; MAPPING VALUES PROVISIONAL UNTIL DI REVIEW

### Decision

The 57 fields marked `Extracted` in the source field inventory SHALL each have an explicit DI source mapping before an extraction profile can be published.

Every mapping is classified:

```text
SUPPORTED    source relationship is directly supported by supplied process/document material
PROVISIONAL  plausible source relationship, requires DI/business confirmation
TBD          source cannot be responsibly inferred from current material
```

A `TBD` mapping SHALL NOT be silently converted into a production extraction profile.

### Precedence rule

Where multiple documents may supply the same field, implementation design must define source precedence and disagreement behavior. UC03 does not assume “last extraction wins”.

Expected pattern:

```text
DI fact proposal
  -> source document + confidence + raw provenance
  -> Audit Core proposal/read model
  -> PC accept/correct where required
  -> authoritative typed domain value
```

A later document result cannot silently overwrite a previously accepted value.

### Canonical mapping artifact

The DI planning branch maintains:

`docs/uc-003-booking-delivery-audit/UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md`

That document is the working reconciliation table for all 57 extracted fields.

---

## 6. DR-05 — VIN / chassis reconciliation

**Status:** FROZEN BOUNDARY; ALGORITHM DEFERRED

### Source condition

The source explicitly raises the unresolved question of an 8-character app chassis value versus a 17-character invoice/policy VIN/chassis representation.

### Decision

1. Web/Android SHALL capture and display **observed vehicle identifier evidence** and **document-derived vehicle identifier evidence** as separate provenance-bearing facts.
2. DI may extract identifiers from documents/photos where supported.
3. Audit Core Rule Engine owns canonicalisation and comparison.
4. No client code may implement suffix matching, truncation, substring matching or a guessed 8-vs-17 algorithm.
5. Until the rule algorithm is approved, the published VIN reconciliation evaluator remains unavailable/provisional rather than faking a deterministic match.

### Result contract direction

The Rule Engine should ultimately return a safe result such as:

```text
MATCH
MISMATCH
INSUFFICIENT_DATA
RULE_NOT_CONFIGURED
```

with rule version and provenance.

### Workflow effect

`MISMATCH` may create a `CRITICAL` flag and immediate escalation according to policy, but it does not reject a real Delivery progression event.

### UX consequence

The client can show:

```text
Vehicle identifier check
Needs review
Observed: ••••3774
Invoice:  ••••3774
Rule: awaiting configured reconciliation
```

or, once configured:

```text
Vehicle identifier check
Mismatch — critical audit flag raised
```

It never states a match based on client-side string comparison.

---

## 7. Mockup design guardrails approved by these decisions

Mockups may now rely on the following:

- applicable-document counts, not a hard-coded 26/29 total;
- masked Aadhaar presentation;
- PC capture completion shown separately from stage Audit State;
- extraction proposals with explicit document/provenance labels;
- unknown/TBD extraction mappings shown as manual/awaiting configuration rather than fake automation;
- VIN result rendered from a backend rule result only;
- Delivery progression remains recordable irrespective of rule/audit outcome;
- Android phone is primary; tablet and desktop use the same workflow and data semantics.

---

## 8. Items intentionally left for UAT / implementation design

The following do not block mockups:

1. final 26-vs-29 physical document reconciliation;
2. exact Aadhaar raw-retention/encryption/tokenisation policy;
3. final severity for every provisional rule;
4. final list of rules requiring review before Audit State becomes `COMPLETE`;
5. exact DI source precedence where two documents carry the same field;
6. exact VIN/chassis 8-vs-17 canonicalisation algorithm;
7. final configuration keys and database column names.

These must remain visible in the UC03 implementation handoff and must not be silently resolved during coding.
