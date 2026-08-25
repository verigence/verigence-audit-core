# UC03 — Booking & Delivery Audit

**Canonical set refreshed:** 2026-08-25

UC03 is the primary operational Booking/Delivery audit use case for Verigence.

## Current canonical design and execution set

1. [`UC03_SOLUTION_DESIGN_v1.1.md`](./UC03_SOLUTION_DESIGN_v1.1.md) — consolidated cross-module Solution Design.
2. [`UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`](./UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md) — authoritative Workflow Manager state/event contract.
3. [`UC03_RULE_FLAG_CATALOG_v1.0.md`](./UC03_RULE_FLAG_CATALOG_v1.0.md) — Phase-1 rule, flag, authority and Audit State completion design.
4. [`UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`](./UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md) — provisional 29-requirement document catalogue and complete 123-field scope matrix.
5. [`UC03_RECONCILIATION_DECISIONS_v1.0.md`](./UC03_RECONCILIATION_DECISIONS_v1.0.md) — decisions allowing UX/implementation design to proceed without silently resolving source gaps.
6. [`UC03_IMPLEMENTATION_DESIGN_v0.1.md`](./UC03_IMPLEMENTATION_DESIGN_v0.1.md) — cross-module implementation blueprint for Project context, work-list read model, Workflow Manager persistence/APIs, DI integration, Web/Android, permissions, migration and tests.
7. [`UC03_IMPLEMENTATION_HANDOFF_v1.1.md`](./UC03_IMPLEMENTATION_HANDOFF_v1.1.md) — approved implementation execution contract for the single-branch C0/C1/C2/C3 sequence.
8. [`UC03_EXECUTION_BASELINE_ADDENDUM_2026-08-23.md`](./UC03_EXECUTION_BASELINE_ADDENDUM_2026-08-23.md) — active continuity and stabilization addendum.
9. [`UC03_UI_LAYOUT_AMENDMENT_2026-08-25.md`](./UC03_UI_LAYOUT_AMENDMENT_2026-08-25.md) — current UI-only Phase 1 / Phase 2 amendment for the PC landing/work-list interaction and Web/Android reference artifacts.

For execution/status wording introduced after handoff v1.1, dated addenda take precedence only within their stated scope. The 25-Aug UI amendment does **not** change authoritative UC03 workflow, authorization, audit-state or completion-guard invariants.

Historical drafts retained for traceability:

- `UC03_SOLUTION_DESIGN_v1.0.md`
- `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.0.md`
- `UC03_IMPLEMENTATION_HANDOFF_v1.0.md`

The first two are superseded by v1.1 because Delivery no longer has a `DELIVERY_CLOSED` state. `UC03_IMPLEMENTATION_HANDOFF_v1.0.md` is superseded by handoff v1.1 because UC03 will not create a second implementation branch.

## Cross-module working artifacts

### DI UC03 branch

`verigence-di / planning/uc-003-booking-delivery-audit`

- `UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md` — working mapping for all 57 source fields marked Extracted, classified SUPPORTED / PROVISIONAL / TBD.

### Web/Android UC03 artifacts

Canonical planning branch remains:

`verigence-web / planning/uc-003-booking-delivery-audit`

The current Web `dev` integration baseline also contains later UC03 UI work from the recent Booking/mobile/capture PRs. Because `dev` includes unrelated product work, do not bulk-merge the whole branch back into planning merely to reconcile UC03 UI changes.

Reference set:

- `UC03_UX_FLOW_CONTRACT_v0.1.md` — original Android-first + tablet + Web interaction contract;
- `UC03_ANDROID_WEB_MOCKUPS_v0.1.html` — original static design-review mockup pack;
- `UC03_UX_REVIEW_NOTES_v0.2.md` — accepted 22-Aug business review amendment;
- `UC03_UI_LAYOUT_AMENDMENT_2026-08-25.md` — current Phase 1 / Phase 2 Web/Android layout baseline;
- `UC03_PC_UI_MOCKUPS_2026-08-25.html` — updated PC Overview / work-row / Capture New Booking / Booking Workspace / mobile reference board.

## Single-branch execution rule

UC03 canonical work continues on the existing planning branch in each touched repository:

```text
planning/uc-003-booking-delivery-audit
```

No Booking, Delivery, Audit, Foundation, Android, DI or separate `work/uc-003-*` branches are to be created.

The feature work is split into execution checkpoints only:

```text
C0 Foundation / Project Context
        ->
C1 Booking
        ->
C2 Delivery
        ->
C3 Audit / Review / Hardening
        ->
Full C0-C3 product regression + consolidated DEV/UAT
        ->
Phase-1 stable product baseline
```

“C4” in working discussion is only shorthand for the final full-product regression/DEV-UAT baseline. It is not an additional business checkpoint and must not introduce new business functionality.

The UI terms **Phase 1** and **Phase 2** in the 25-Aug amendment are presentation/refinement phases and are not additional workflow checkpoints:

- UI Phase 1 = PC landing/dashboard visual refactor;
- UI Phase 2 = progressive loading, cleaner filters, mobile/Web consistency and compact/expanded work rows.

## Original frozen planning baselines

| Module | Baseline | UC03 branch |
|---|---|---|
| Audit Core | `dev@082cc2ada5cd934bf0707ccae945667feb3f6e37` | `planning/uc-003-booking-delivery-audit` |
| DI | `dev@c97b3f3e5f8577160c88af1080496808189206fb` | `planning/uc-003-booking-delivery-audit` |
| Web/Android | `dev@2c98f753ed1428c0d5f7a0b7144169d528a5bb78` | `planning/uc-003-booking-delivery-audit` |

## Frozen principles

- One immutable internal Journey ID spans Booking and Delivery.
- PC UI uses Booking/Delivery terminology; it does not say Journey.
- Audit conditions never abort/refuse a real dealer progression event.
- Booking can remain In Progress after Delivery starts; a machine flag records the exception.
- Booking business status, Delivery business status, per-stage Audit State, per-stage Audit Status and individual flags are separate concepts.
- Booking business statuses: Started, In Progress, Closed, Cancelled, Duplicate Booking.
- Delivery business statuses: **Started, In Progress, Completed only**.
- Machine and human flags use one canonical register.
- PC/TL/PM/Executive can raise flags; TL/PM normally review/resolve; Executive has all Phase-1 flag privileges.
- VIN reconciliation belongs to Audit Core Rule Engine, not the client.
- Post-Delivery reconciliation is out of Phase-1 scope.
- PC UX is Android phone/tablet first; desktop Web uses the same workflow/components.
- document extraction is asynchronous; UX is upload-first, work-while-processing, proposals-not-overwrites;
- current document requirements retain all 29 numbered source entries provisionally;
- Aadhaar is masked in UX; UC03 does not invent a raw-retention policy;
- Audit State completion is published-policy driven;
- all 123 source fields are accounted for;
- PC/TL/PM may operate in multiple Projects; Project is selected after login when more than one assignment exists;
- one available Project is selected automatically;
- Project role is context-specific;
- operational landing uses **Delivery In Progress**, not Delivery Today;
- landing shows **Latest Bookings & Deliveries** using bounded server pages, with Booking/Delivery + date filtering;
- the client may progressively append those bounded pages as defined by the 25-Aug UI amendment;
- the existing SuperAdmin `/v1/projects` endpoint is not broadened for operational Project selection;
- the approved bundled Verigence logo/lockup is mandatory in runtime UI.

## Stabilization rule — CI/CD frozen for now

Until the UC03 product baseline is stable after full C0-C3 regression and consolidated human DEV/UAT, **do not redesign or materially change CI/CD architecture**.

Provider throttling/outages or validation inconvenience are recorded and retried using the current baseline; they are not triggers to change Railway/GitHub/Cloudflare deployment architecture during UC03 stabilization.

The CI/CD architecture will be reviewed separately only after UC03 is stable. See `UC03_EXECUTION_BASELINE_ADDENDUM_2026-08-23.md` for the binding execution details.

## Current execution gate — 25-Aug-2026

This README does not declare a new C0/C1/C2/C3 closure state. Exact engineering/deployment evidence continues to live in the dedicated `status/` documents and dated execution evidence.

The 25-Aug work records a UI refinement only. Human UAT remains **PENDING** unless a real human pass is explicitly recorded in the appropriate status document; automated CI/deployment success must not be treated as human UAT.

For exact scope, migrations, API groups, tests, status-note requirements and Definition of Done, use `UC03_IMPLEMENTATION_HANDOFF_v1.1.md` together with the active execution addenda.
