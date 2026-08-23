# UC03 C3 — Audit / Review / Hardening — Checkpoint Status

**Checkpoint:** `C3 — Audit / Review / Hardening`  
**Status:** `IMPLEMENTATION IN PROGRESS / AUTOMATED VALIDATION PARTIALLY GREEN / HUMAN UAT DEFERRED`  
**Date:** 2026-08-23  
**Branch:** `planning/uc-003-booking-delivery-audit`  
**CI/CD architecture:** **FROZEN UNTIL UC03 STABLE**

This note is the active execution/status record for C3. It supplements `UC03_IMPLEMENTATION_HANDOFF_v1.1.md` and must be read together with `../UC03_EXECUTION_BASELINE_ADDENDUM_2026-08-23.md`.

No C3 human UAT pass is claimed. Human UAT remains deferred to the consolidated end-of-UC03 DEV/UAT cycle.

---

## 1. C3 implementation currently present

### Audit Core

The C3 implementation builds on the existing canonical `audit_findings` register and append-only `audit_finding_events`; no second anomaly/flag store is introduced.

Implemented/in-progress capabilities include:

- Booking/Delivery stage attribution;
- MACHINE/HUMAN origin and provenance;
- actor/operating-role snapshot;
- rule/version linkage for machine findings;
- append-only finding events;
- remarks/evidence linkage;
- list/create/read summary surfaces;
- lifecycle actions for acknowledge/review/resolve/reopen/void according to role policy;
- optimistic version conflict handling;
- idempotent lifecycle commands;
- Project/business-scope authorization through the existing Security-v2 boundary;
- sticky historical `FLAGS_RAISED` behavior;
- stage Audit State completion surface;
- cross-stage Audit summary;
- unified Booking/Delivery/finding/review timeline;
- server-returned permitted actions so the UI does not become the permission authority;
- conservative Phase-1 default of Executive-only Void unless published Project policy overrides it;
- protection against the legacy generic Finding PATCH silently bypassing the UC03 lifecycle/event path.

No C3 migration has been introduced merely for neatness. Existing C1/C2 structures already contain the required finding-event/provenance fields; additional migration/index work will be introduced only if the final C3 performance/backfill review proves it necessary.

### Web / Android

C3 review experience is implemented/in progress on the unified Web branch, including:

- dedicated `/audit/:journeyId` review workspace;
- Booking and Delivery Audit State / Audit Status presentation;
- open and historical findings;
- machine/human provenance;
- remarks/evidence/history presentation;
- role/capability-driven lifecycle actions;
- Booking/Delivery navigation from the review workspace;
- consolidated timeline;
- `Audit & History` entry point from applicable Project work-list items;
- responsive phone/tablet/Desktop layout using the existing UC03 application shell.

Current Web branch head at status creation: `1d7a1475f6a6dd8e947c1a6aeecee7f49ec4e8b5`.

### DI

No C3 DI runtime change is currently required. C3 must preserve/regress the existing C1/C2 DI publication boundaries. Current DI UC03 branch head at status creation: `29cdef7d1567422bd2ffdbf7f5926f6bc0f23743`.

---

## 2. Current Audit Core validation evidence

C3 runtime/application head immediately before the documentation reconciliation commits: `12aeaca8df9af39e9ca88d687dc941195ad3199d`.

CI run `32622262642` (run 654): **FAIL at unit-test step**.

Steps already green on that exact C3 runtime head:

- package build: **PASS**;
- Ruff: **PASS**;
- fresh PostgreSQL/Alembic migration through `0012_uc03_delivery_capture`: **PASS**;
- pytest completed with **169 passed / 4 failed / 1 warning**.

All four failures share one root cause. PostgreSQL cannot infer the type of a `NULL` `stage_code` bind in the unfiltered UC03 flag-list query:

```sql
AND (:stage_code IS NULL OR stage_code=:stage_code)
```

The failing scenarios are therefore currently blocked by one query implementation defect rather than four independent business-rule failures:

- historical sticky flags after resolution;
- blocking-completion summary path;
- machine provenance/full timeline read path;
- role-capability audit summary path.

Required fix: construct an unfiltered query when `stage_code` is absent, or otherwise provide an explicit safe type without relying on an ambiguous NULL bind. Do not weaken the tests.

---

## 3. C3 contract hardening item — human flags cannot create completion guards

The current C3 create model still contains:

```text
blockingCompletion: bool = False
```

This must be removed/rejected from the human flag creation contract before C3 is considered complete.

Business rule:

- a PC/TL/PM/Executive human-created observation may raise a flag;
- a human flag must not self-declare itself as an `AUDIT_COMPLETION_GUARD` merely through client input;
- completion-blocking semantics belong to configured/published Project policy/rule semantics and machine/configured findings;
- the C3 test that proves a blocking guard prevents Audit State completion must seed/use a configured or machine guard, not a manually supplied `blockingCompletion=true` value;
- Web/Android must not expose a completion-blocking toggle.

This is a contract/authority correction, not a new business feature.

---

## 4. Remaining C3 engineering gates

1. Fix the unfiltered flag-list PostgreSQL query.
2. Remove human `blockingCompletion` authority and update the completion-policy acceptance fixture appropriately.
3. Obtain a fully green Audit Core C3 suite without weakening C0/C1/C2 regression coverage.
4. Freeze/validate the C3 Audit/Review API contract in the checkpoint.
5. Confirm no migration is needed; if a migration/index/backfill is genuinely required, follow the migration discipline in handoff v1.1 and record explicit counts/safety analysis.
6. Run final Web C3 TypeScript typecheck + production build.
7. Run final Android C3 validation/build and capture APK evidence because the shared Web/mobile review flow changed.
8. Confirm DI C1/C2 regression remains green; do not change DI for C3 unless a concrete runtime need is proven.
9. Update the Audit Core, Web and DI draft validation PR descriptions to the current C2/C3 state; keep them draft / DO NOT MERGE.
10. Update this note with exact final C3 application SHAs, CI run IDs, contract evidence and Android artifact.

DEV validation, where still required, uses the current CI/CD baseline. **No CI/CD architecture redesign is permitted during UC03 stabilization.** Provider throttling/outage is recorded as an external blocker and retried later using the existing process.

---

## 5. C3 acceptance scenarios still to prove green

Automated and later human validation must cover:

- machine finding provenance;
- PC manual finding;
- TL review/resolve;
- PM review/resolve;
- Executive full permitted path including Void under the effective policy;
- `Audit State = COMPLETE` with `Audit Status = FLAGS_RAISED` where the configured policy permits completion;
- resolved flags remain historically visible;
- stale version conflict never silently overwrites review state;
- idempotency replay never duplicates a finding event;
- Project/role isolation remains correct;
- sensitive/internal payload data is not exposed in ordinary UI/history;
- human-created flags cannot manufacture completion guards;
- complete cross-stage Booking/Delivery/finding timeline remains ordered and user-safe.

---

## 6. Human UAT — DEFERRED / PENDING

No human C3 UAT result is recorded yet.

The consolidated end-of-UC03 cycle must include representative PC, TL, PM and Executive accounts and exercise the review/lifecycle/history flows on Android phone, Android tablet and desktop Web as applicable.

No automated result may be converted into a human-UAT pass.

---

## 7. Final product baseline after C3 — working shorthand “C4”

There is no approved C4 business feature checkpoint. Once C3 is fully green, the next gate is the full UC03 product baseline:

```text
C0 + C1 + C2 + C3
        ->
full automated cross-stage regression
        ->
Audit Core contract + migration verification
        ->
DI regression
        ->
Web production build
        ->
Android native validation/APK
        ->
consolidated human DEV/UAT
        ->
defect fixes + revalidation
        ->
Phase-1 stable baseline / promotion decision
```

This final gate must include all deferred C0/C1/C2/C3 human-UAT scenarios and the Phase-1 Definition of Done in handoff v1.1.

---

## 8. CI/CD freeze

Per `UC03_EXECUTION_BASELINE_ADDENDUM_2026-08-23.md`, do not redesign or materially change CI/CD until UC03 is stable after the final product regression and consolidated human DEV/UAT.

This applies while resolving every remaining C3 and final-baseline item in this note.

---

## 9. Current checkpoint decision

C3 implementation is **not yet closed**.

The immediate engineering sequence is:

```text
fix flag-list NULL bind
        ->
remove human completion-guard authority
        ->
full Audit Core green
        ->
C3 contract/Web/Android/DI regression
        ->
freeze C3 evidence
        ->
full C0-C3 product baseline
        ->
consolidated human UAT
```

Do not claim C3 completion until the remaining gates above are actually green and recorded.
