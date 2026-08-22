# UC03 — Booking & Delivery Audit

UC03 is the primary and largest operational audit use case for Verigence.

## Current canonical design set

1. [`UC03_SOLUTION_DESIGN_v1.1.md`](./UC03_SOLUTION_DESIGN_v1.1.md) — consolidated cross-module Solution Design.
2. [`UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`](./UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md) — authoritative Workflow Manager state/event contract.
3. [`UC03_RULE_FLAG_CATALOG_v1.0.md`](./UC03_RULE_FLAG_CATALOG_v1.0.md) — Phase-1 rule, flag, authority and Audit State completion design.
4. [`UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`](./UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md) — provisional 29-requirement document catalogue and complete 123-field scope matrix.
5. [`UC03_RECONCILIATION_DECISIONS_v1.0.md`](./UC03_RECONCILIATION_DECISIONS_v1.0.md) — decisions allowing mockups to proceed without silently resolving source gaps.

Historical drafts retained for traceability:

- `UC03_SOLUTION_DESIGN_v1.0.md`
- `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.0.md`

They are superseded by v1.1, principally because Delivery no longer has a `DELIVERY_CLOSED` state.

## Cross-module working artifacts

### DI planning branch

`verigence-di / planning/uc-003-booking-delivery-audit`

- `docs/uc-003-booking-delivery-audit/UC03_EXTRACTION_SOURCE_MAPPING_v0.1.md` — working mapping for all 57 source fields marked Extracted, classified SUPPORTED / PROVISIONAL / TBD.

### Web/Android planning branch

`verigence-web / planning/uc-003-booking-delivery-audit`

- `docs/uc-003-booking-delivery-audit/UC03_UX_FLOW_CONTRACT_v0.1.md` — Android-first + tablet + Web interaction contract.
- `docs/uc-003-booking-delivery-audit/UC03_ANDROID_WEB_MOCKUPS_v0.1.html` — static design-review mockup pack covering 8 Android phone states, 1 tablet review state and 2 desktop workspaces.

## Planning baselines

| Module | Baseline | Planning branch |
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
- Document extraction is asynchronous; UX is upload-first, work-while-processing, proposals-not-overwrites.
- Current document requirements retain all 29 numbered source entries provisionally; UAT reconciles the business 26-vs-29 discrepancy.
- Aadhaar is masked in UX; UC03 does not invent a raw-retention policy.
- Audit State completion is published-policy driven and may remain In Progress while required TL/PM/Executive review is outstanding.
- All 123 source fields are accounted for before implementation design.

## Next approval gate

Review the static mockups and UX flow against:

1. business workflow correctness;
2. non-blocking Delivery progression;
3. mobile/tablet usability for PC;
4. extraction-delay handling;
5. machine/human Audit Flag behavior;
6. Booking close/cancel/duplicate behavior;
7. TL/PM/Executive review experience;
8. consistency with the existing Verigence visual framework.

After mockup approval, the next artifacts are:

- UC03 Implementation Design;
- exact API/schema/permission/DI profile deltas;
- UC03 implementation handoff with branches, migrations and test matrix.
