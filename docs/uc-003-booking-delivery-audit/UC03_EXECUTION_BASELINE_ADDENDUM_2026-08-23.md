# Verigence UC03 — Execution Baseline Addendum

**Document ID:** `VUC03-HO-ADD-001`  
**Status:** ACTIVE EXECUTION OVERRIDE / CONTINUITY RECORD  
**Date:** 2026-08-23  
**Applies to:** `UC03_IMPLEMENTATION_HANDOFF_v1.1.md`  
**Branch:** `planning/uc-003-booking-delivery-audit`

---

## 1. Purpose

This addendum records execution decisions made after approval of `UC03_IMPLEMENTATION_HANDOFF_v1.1.md` so that the repository remains the complete source of truth if implementation resumes in another session.

It does not change UC03 business invariants, data ownership, Security boundaries, DI responsibilities, workflow states, audit semantics, or the single-branch implementation model.

Where this addendum conflicts only with checkpoint sequencing/status wording in handoff v1.1, this addendum takes precedence.

---

## 2. Consolidated human-UAT sequencing

The human owner directed that engineering may continue through C0, C1, C2 and C3 while checkpoint human UAT is consolidated at the end of the UC03 engineering cycle.

Therefore:

- automated validation does not constitute human UAT;
- C0/C1/C2 human UAT remains `DEFERRED / PENDING`;
- C3 human UAT will also be executed in the consolidated end-of-UC03 DEV/UAT cycle;
- formal Phase-1 promotion still requires the consolidated human UAT and final Definition of Done;
- no checkpoint is to be represented as human-UAT passed unless a human tester actually performs and records the scenarios.

---

## 3. CI/CD freeze until UC03 product baseline is stable

**Binding execution decision:** do not redesign, refactor, replace, generalize, or otherwise materially change the current CI/CD architecture while UC03 product engineering, regression and consolidated UAT are still in progress.

For the remainder of the UC03 stabilization cycle:

1. Existing CI/CD workflows are treated as a frozen operational baseline.
2. Do not introduce a new deployment architecture in response to a single provider failure, throttling event, transient outage, or checkpoint-specific inconvenience.
3. Do not change Railway/GitHub/Cloudflare deployment strategy, credential model, target-discovery model, workflow trust model, branch promotion model, or general CI/CD topology merely to unblock UC03 validation.
4. Product/runtime/test defects may be fixed normally.
5. Test and application contract corrections may be made normally.
6. A provider-side problem is recorded as an external validation blocker and retried using the existing approved process when the provider permits it.
7. Temporary branch modifications to deployment architecture are not to be introduced as a workaround.
8. Security is not redesigned as part of this decision.
9. DI architecture is not redesigned as part of this decision.
10. No UC03 planning branch is merged to `dev` merely to bypass validation mechanics.

The current Railway control-plane throttling encountered during C2 validation is therefore classified as an **external environment/validation blocker, not a product defect and not an architecture-change trigger**.

### CI/CD review point

A deliberate CI/CD architecture review may begin **only after UC03 is stable**, meaning:

- C0-C3 engineering and automated regression are green;
- the consolidated UC03 product baseline has been established;
- human DEV/UAT has been executed and defects from that cycle are understood;
- the team can evaluate CI/CD changes using measured end-to-end product experience rather than checkpoint-by-checkpoint reactions.

Any later CI/CD change must be treated as a separate architecture decision and must not rewrite UC03 historical evidence.

---

## 4. Meaning of “C4” in working discussions

The approved handoff defines feature checkpoints only through `C3`.

When the human owner or implementation notes refer informally to **C4**, it means the final UC03 product-baseline gate, not a new business feature checkpoint and not a new migration package:

```text
C0 + C1 + C2 + C3
        ->
full automated cross-stage regression
        ->
Web + Android + DI regression
        ->
contract/migration verification
        ->
consolidated DEV/UAT
        ->
Phase-1 Definition of Done / stable product baseline
```

No C4 business functionality is to be invented.

---

## 5. Current execution state at addendum creation

### C0

- engineering/automated/DEV evidence: complete;
- human UAT: deferred/pending;
- formal human-UAT closure: not complete.

### C1

- engineering/automated/DEV validation: complete;
- human UAT: deferred/pending.

### C2

- Delivery engineering and automated validation are complete;
- C2 Delivery OpenAPI checkpoint contract exists as `api/openapi-uc03-c2.yaml` and is covered by runtime-parity testing;
- Web, DI and Android C2 automated validation are green;
- remaining DEV validation evidence is affected by Railway control-plane throttling encountered before application deployment started;
- this external blocker must not trigger CI/CD redesign;
- human UAT remains deferred/pending.

### C3

C3 Audit / Review / Hardening implementation is in progress on the same branch.

Current Audit Core CI head at the time of this addendum had:

- package build: PASS;
- Ruff: PASS;
- fresh PostgreSQL migration through `0012_uc03_delivery_capture`: PASS;
- pytest: **169 passed / 4 failed / 1 warning**;
- all four failures share one root cause: PostgreSQL cannot infer the type of a `NULL` `stage_code` bind in the unfiltered UC03 flag-list query (`(:stage_code IS NULL OR stage_code=:stage_code)`).

A second C3 contract hardening item is also pending: human-created flags must not be able to self-declare an Audit completion guard. Completion-blocking behavior belongs to configured/published rule or policy semantics, not a PC/TL/PM manual flag input.

---

## 6. Remaining execution order

Without changing CI/CD architecture:

1. correct the C3 unfiltered flag-list SQL;
2. remove human `blockingCompletion` authority and adjust the C3 completion-policy test to use a configured/machine guard;
3. obtain a green complete C3 Audit Core suite;
4. validate/freeze the C3 API contract;
5. obtain final Web C3 typecheck/build;
6. obtain final Android C3 debug build/artifact if the C3 Web changes affect mobile runtime;
7. verify DI regression remains green; no C3 DI change is expected unless a concrete need is proven;
8. create/finalize `status/UC03_C3_AUDIT.md` with exact evidence;
9. execute the full C0-C3 automated product-baseline regression (working shorthand: C4);
10. perform the consolidated human DEV/UAT scenarios for C0-C3;
11. fix genuine UAT defects without inventing new scope;
12. freeze final exact SHAs/artifacts/deferred decisions;
13. only then make the Phase-1 promotion/closure decision;
14. review CI/CD architecture separately after UC03 becomes stable.

---

## 7. Controlled decisions remain controlled

This addendum does not silently resolve the controlled open decisions listed in handoff v1.1, including document-catalogue reconciliation, remaining DI provisional/TBD mappings, final VIN/chassis normalization, high/critical Audit completion policy, permission mapping and historical backfill treatment.

Those items remain explicit design/policy decisions unless later repository evidence records an approved resolution.
