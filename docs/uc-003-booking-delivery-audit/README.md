# UC03 — Booking & Delivery Audit

UC03 is the primary and largest operational audit use case for Verigence.

## Current canonical design and execution set

1. [`UC03_SOLUTION_DESIGN_v1.1.md`](./UC03_SOLUTION_DESIGN_v1.1.md) — consolidated cross-module Solution Design.
2. [`UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`](./UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md) — authoritative Workflow Manager state/event contract.
3. [`UC03_RULE_FLAG_CATALOG_v1.0.md`](./UC03_RULE_FLAG_CATALOG_v1.0.md) — Phase-1 rule, flag, authority and Audit State completion design.
4. [`UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`](./UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md) — provisional 29-requirement document catalogue and complete 123-field scope matrix.
5. [`UC03_RECONCILIATION_DECISIONS_v1.0.md`](./UC03_RECONCILIATION_DECISIONS_v1.0.md) — decisions allowing UX/implementation design to proceed without silently resolving source gaps.
6. [`UC03_IMPLEMENTATION_DESIGN_v0.1.md`](./UC03_IMPLEMENTATION_DESIGN_v0.1.md) — cross-module implementation blueprint for Project context, latest-10 work-list read model, Workflow Manager persistence/APIs, DI integration, Web/Android, permissions, migration and tests.
7. [`UC03_IMPLEMENTATION_HANDOFF_v1.1.md`](./UC03_IMPLEMENTATION_HANDOFF_v1.1.md) — **current implementation execution contract**. It freezes single-branch sequential implementation across C0 Foundation, C1 Booking, C2 Delivery and C3 Audit/Review.

Historical drafts retained for traceability:

- `UC03_SOLUTION_DESIGN_v1.0.md`
- `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.0.md`
- `UC03_IMPLEMENTATION_HANDOFF_v1.0.md`

The first two are superseded by v1.1 because Delivery no longer has a `DELIVERY_CLOSED` state. `UC03_IMPLEMENTATION_HANDOFF_v1.0.md` is superseded by handoff v1.1 because UC03 will not create a second implementation branch.

## Cross-module working artifacts

### DI UC03 branch

`verigence-di / planning/uc-003-booking-delivery-audit`

- `UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md` — working mapping for all 57 source fields marked Extracted, classified SUPPORTED / PROVISIONAL / TBD.

### Web/Android UC03 branch

`verigence-web / planning/uc-003-booking-delivery-audit`

- `UC03_UX_FLOW_CONTRACT_v0.1.md` — Android-first + tablet + Web interaction contract;
- `UC03_ANDROID_WEB_MOCKUPS_v0.1.html` — static design-review mockup pack;
- `UC03_UX_REVIEW_NOTES_v0.2.md` — accepted business review amendment and implementation precedence for Project selection, approved logo, landing terminology, latest-10 transactions and date filtering.

## Single-branch execution rule

UC03 continues on the existing branch in each touched repository:

```text
planning/uc-003-booking-delivery-audit
```

No Booking, Delivery, Audit, Foundation, Android, DI or separate `work/uc-003-*` branches are to be created.

The work is split into execution checkpoints only:

```text
C0 Foundation / Project Context
        ->
C1 Booking
        ->
C2 Delivery
        ->
C3 Audit / Review / Hardening
```

Each checkpoint must pass its own acceptance gate before the next starts.

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
- landing shows **Latest Bookings & Deliveries**, maximum 10 per page, with Booking/Delivery + date filtering;
- the existing SuperAdmin `/v1/projects` endpoint is not broadened for operational Project selection;
- the approved bundled Verigence logo/lockup is mandatory in runtime UI.

## Current execution gate

Implementation begins with **C0 — Foundation / Project Context** only.

C1 Booking does not begin until C0 is closed. C2 Delivery does not begin until Booking is closed. C3 Audit/Review does not begin until Delivery is closed as an implementation checkpoint.

For exact scope, migrations, API groups, tests, status-note requirements and Definition of Done, use `UC03_IMPLEMENTATION_HANDOFF_v1.1.md`.
