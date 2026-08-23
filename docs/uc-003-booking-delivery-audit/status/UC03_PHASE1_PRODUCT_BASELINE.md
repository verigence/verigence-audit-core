# UC03 Phase-1 Product Baseline — Automated Readiness Record

**Working shorthand:** `C4`  
**Meaning:** final C0-C3 product regression / consolidated DEV-UAT baseline — **not a feature checkpoint**  
**Status:** `AUTOMATED PRODUCT BASELINE GREEN / DEV EVIDENCE PARTIAL-PENDING / HUMAN UAT PENDING / NOT PROMOTION READY`  
**Date:** 2026-08-23  
**Branch:** `planning/uc-003-booking-delivery-audit`

## 1. Baseline decision

The approved implementation checkpoints end at C3. This record captures the complete automated product baseline across C0, C1, C2 and C3 before consolidated human testing.

No new C4 business functionality, migration, status or architecture is introduced.

CI/CD remains frozen until UC03 becomes stable after product testing and consolidated human UAT, per `../UC03_EXECUTION_BASELINE_ADDENDUM_2026-08-23.md`.

## 2. Automated cross-stage baseline

### Audit Core

Application/contract baseline SHA: `1c61f995e707ed1f944f7357f11c5e146ab6c9c5`.

CI run `32623966514` / run 662: **SUCCESS**.

- package build PASS;
- Ruff PASS;
- fresh database migration chain through `0012_uc03_delivery_capture` PASS;
- **176 tests passed**;
- C0 Project context/work-list regression included;
- C1 Booking regression included;
- C2 Delivery regression included;
- C2 Delivery checkpoint OpenAPI/runtime parity included;
- C3 Audit/Review lifecycle and authority regression included;
- C3 OpenAPI/runtime parity included.

### DI

Baseline head `29cdef7d1567422bd2ffdbf7f5926f6bc0f23743`.

CI `32620797583` / run 204: **SUCCESS** — **220 passed, 39 skipped**, lint and typecheck PASS.

C1 Booking and C2 Delivery publication boundaries remain strict. No C3 DI change is required.

### Web

C3 application contract fix SHA: `460f73f88035b930ad1565e280b9f6d5524625cd`.

Current validation/cleanup head: `2c3b7fbb441d8d2dc15cd2438bf5c9f235dc26ec`.

Web CI `32624219327` / run 308: **SUCCESS** — typecheck + production build PASS.

The current Web PR merge view includes current `dev` product changes and the complete UC03 C0-C3 feature set. The only later UC03-branch change was restoration of the existing DEV deployment workflow to its current `dev` baseline after temporary C2 validation instrumentation; no new CI/CD behavior was created.

### Android

Run `32624219324` / run 15: **SUCCESS**.

- native Web typecheck/build PASS;
- C3 Audit Review page emitted into the native bundle;
- Capacitor native configuration PASS;
- Delivery Camera regression contract PASS;
- Gradle lint + debug APK build PASS;
- APK SHA-256 `88912bfb94424a3812ec08dde2342bae47cf4e972c51ff74e5cf86d87a9dcf5d`;
- artifact ID `9489268035`;
- artifact ZIP digest `sha256:347e38b16f45022a6d15cad1ff95e6972413e4f2df687b91d367605cb284dc48`.

Artifact name remains `verigence-uc03-c2-android-debug` only because the existing workflow is frozen; do not infer that the package lacks C3. The build logs explicitly include the C3 Audit Review chunk.

## 3. Business invariants represented in the green baseline

The automated baseline covers the core Phase-1 architecture:

- selected Project controls operational role/business scope;
- Booking and Delivery remain separate business stages on one immutable Journey;
- Delivery is never rejected solely because audit prerequisites are incomplete;
- Booking may remain incomplete while Delivery proceeds, with a machine finding recording the exception;
- Delivery lifecycle remains Started -> In Progress -> Completed only;
- physical Delivery Completed may coexist with Audit State IN_PROGRESS;
- document applicability and Yes/No/NA are server-owned;
- extraction proposals never silently overwrite accepted/PC values;
- DI supplies intelligence/provenance, not audit decisions;
- VIN/chassis decision logic remains Audit Core owned;
- machine and human findings share one auditable register;
- finding lifecycle is versioned, idempotent and append-only in history;
- human findings cannot create completion guards;
- Audit Status FLAGS_RAISED remains historically sticky;
- Project/role permissions remain server enforced;
- user timeline does not expose internal payload/actor identifiers;
- Post-Delivery runtime remains out of Phase-1 scope.

## 4. Migration/backfill/hardening baseline

Fresh migration verification passes through `0012_uc03_delivery_capture`.

No C3 migration is required for functional correctness because C3 reuses the provenance/event structure already introduced in 0011.

No ambiguous historical finding backfill is performed. Candidate Journey-specific query indexes are documented in `../UC03_C3_HARDENING_REPORT_2026-08-23.md` for measured post-stabilization evaluation. If product/load testing shows they are needed for acceptance performance, they become a pre-promotion defect.

## 5. DEV validation status

Existing successful historical DEV evidence remains valid for C0 and C1 as recorded in their checkpoint notes.

C2 Audit Core/DI exact-SHA DEV closure validation was subsequently blocked before application deployment by Railway control-plane throttling. This remains an **external validation blocker**, not an application failure.

No CI/CD redesign is permitted to work around that condition during UC03 stabilization. Retry the missing DEV evidence using the existing process when the provider permits it.

C3 automated validation does not convert missing consolidated DEV evidence into a pass.

## 6. Human UAT status

**PENDING. No human pass has been inferred or fabricated.**

The consolidated human cycle must execute the deferred C0, C1, C2 and C3 scenarios with representative Project/account data and real supported devices/browsers.

Required coverage includes:

- zero/one/multiple Projects and Project-specific roles;
- Project switch isolation;
- landing metrics/latest-10/filter/date/paging;
- Booking start, evidence processing, proposals, dynamic requirements, conclusions and duplicate behavior;
- Delivery non-blocking start, documents, intimation, Camera evidence, VIN/payment findings and physical completion with audit still open;
- PC/TL/PM/Executive finding lifecycle and timeline;
- sticky Audit Status after resolution;
- safe stale-version/idempotency UX;
- Android phone;
- Android tablet;
- desktop Web;
- approved branding and user-safe wording;
- no premature Post-Delivery runtime.

## 7. Remaining items before Phase-1 promotion

At this automated baseline, remaining items are no longer broad feature-development work. They are:

1. retry/complete missing required DEV validation evidence using the unchanged CI/CD baseline when external Railway throttling permits;
2. run consolidated human UAT across C0-C3;
3. log/fix only genuine UAT defects and rerun affected automated/DEV/UAT scenarios;
4. evaluate candidate performance indexes only if representative product/load evidence requires them;
5. reconcile any newer required `dev` product changes deliberately before the final promotion decision;
6. record final exact Audit Core/DI/Web heads and Android artifact after UAT fixes, if any;
7. formally close C0, C1, C2 and C3 human-UAT status notes;
8. only then promote UC03 to `dev` according to the existing process;
9. review CI/CD architecture separately **after UC03 is stable**.

## 8. Readiness statement

**UC03 C0-C3 automated product baseline is GREEN.**

This is **not** a Phase-1 production/promotion approval because consolidated DEV evidence and human UAT are still pending. The repository now distinguishes automated readiness, external validation blockers and human acceptance explicitly.
