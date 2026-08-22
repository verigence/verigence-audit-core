# UC03 — Booking & Delivery Audit

UC03 is the primary and largest operational audit use case for Verigence.

## Current canonical design documents

1. [`UC03_SOLUTION_DESIGN_v1.1.md`](./UC03_SOLUTION_DESIGN_v1.1.md) — current consolidated cross-module business/solution design.
2. [`UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md`](./UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.1.md) — current authoritative Workflow Manager state/event contract.
3. [`UC03_RULE_FLAG_CATALOG_v1.0.md`](./UC03_RULE_FLAG_CATALOG_v1.0.md) — Phase-1 rule, flag, authority and audit-completion design.
4. [`UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md`](./UC03_DOCUMENT_123_FIELD_MATRIX_v1.0.md) — provisional 29-document catalogue and complete 123-field scope matrix.

The earlier `UC03_SOLUTION_DESIGN_v1.0.md` and `UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.0.md` are retained as historical drafts. They are superseded by the v1.1 documents above, principally because Delivery no longer has a `DELIVERY_CLOSED` state.

Mockups remain the next major deliverable after business review/reconciliation of this four-document design set.

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
- Delivery business statuses: **Started, In Progress, Completed only**. There is no Delivery Closed/Success/Failure in Phase 1.
- Machine and human flags use one canonical register.
- PC/TL/PM/Executive can raise flags; TL/PM normally review/resolve; Executive has all Phase-1 flag privileges.
- VIN reconciliation belongs to Audit Core Rule Engine, not the client.
- Post-Delivery reconciliation is out of Phase-1 scope.
- PC UX is Android phone/tablet first; desktop Web uses the same workflow/components.
- Document extraction is asynchronous; UX is upload-first, work-while-processing, proposals-not-overwrites.
- The current document list is provisional because source prose says 26 while the numbered applicability diagram contains 29 entries.
- All 123 source fields are accounted for before mockup design.

## Next design work

### Immediate

- Business review/reconciliation of Rule/Flag Catalog.
- Business review/reconciliation of provisional Document + 123-Field Matrix.
- Close the highest-impact open items: document list, VIN rule, exact extraction-source mapping, Audit State completion policy.

### Then

- Complete **Android-first UC03 mockup pack**.
- Adapt the same interaction model for Android tablet and desktop Web.
- Create UC03 Implementation Design.
- Create UC03 Implementation Handoff with exact branches/modules/APIs/migrations/tests.