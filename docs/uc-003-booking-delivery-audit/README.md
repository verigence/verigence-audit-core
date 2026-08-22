# UC03 — Booking & Delivery Audit

UC03 is the primary operational audit use case for Verigence.

## Canonical design documents

1. [`UC03_SOLUTION_DESIGN_v1.0.md`](./UC03_SOLUTION_DESIGN_v1.0.md) — cross-module business/solution design.
2. [`UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.0.md`](./UC03_WORKFLOW_STATE_EVENT_CATALOG_v1.0.md) — explicit Workflow Manager state/event contract.

These two documents are the current UC03 source of truth for business/architecture review. Mockups and implementation design are intentionally deferred until this baseline is approved.

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
- Machine and human flags use one canonical register.
- PC/TL/PM/Executive can raise flags; TL/PM review/resolve; Executive has all Phase-1 flag privileges.
- VIN reconciliation belongs to Audit Core Rule Engine, not the client.
- Post-Delivery reconciliation is out of Phase-1 scope.
- PC UX is Android phone/tablet first; desktop Web is secondary.
- Document extraction is asynchronous; UX is upload-first, work-while-processing, proposals-not-overwrites.

## Next design work after review

- UC03 Rule/Flag Catalog
- UC03 Document & 123-Field Matrix
- UC03 Android-first + tablet + desktop mockups
- UC03 Implementation Design
- UC03 Implementation Handoff
