# UC03 UI Layout Amendment — 25-Aug-2026

**Status:** UI IMPLEMENTATION AMENDMENT / HUMAN UAT PENDING  
**Date:** 2026-08-25  
**Repository authority:** Audit Core remains authoritative for workflow, authorization, audit state, completion guards and event semantics. This document records a Web/Android presentation refinement only.

## Purpose

Record the agreed UC03 Process Coordinator UI work completed in two UI phases and point the canonical UC03 set to the corresponding Web reference artifacts.

This amendment does **not** reopen the frozen C0-C3 business model and does not authorize a Security, schema, migration or rule-engine change.

## Phase 1 — PC landing/dashboard visual refactor

Phase 1 is presentation-only and preserves existing API/workflow contracts.

Required layout direction:

- Project and operating role remain visible as the work context;
- PC Dealer / Outlet context is visible near the primary heading;
- **Capture New Booking** is the primary PC action;
- operational KPI cards show Bookings In Progress, Delivery In Progress, Needs Attention and Audit Flags;
- Bookings/Deliveries KPIs may filter the current work list;
- **Latest Bookings & Deliveries** is the principal operational list;
- Booking is the primary transaction action;
- Delivery and Audit Review are secondary actions;
- Booking Workspace header/presentation uses the same visual baseline;
- approved Verigence branding, blue/teal palette and enterprise typography remain unchanged.

No backend/API redesign is required by Phase 1.

## Phase 2 — interaction refinement

Phase 2 refines how the existing cursor-paged work-list API is consumed and rendered.

- server page size remains bounded at 10 transactions;
- the Web/Android client progressively appends subsequent cursor pages;
- approaching the list end may trigger the next fetch automatically;
- an explicit **Load more** fallback remains available;
- filters remain server-backed: All / Bookings / Deliveries plus From/To date;
- work items default to a compact scan state;
- **View details** expands the same item in place;
- expanded state may show Booking/Delivery audit state, flag summary, processing document count and extraction proposal-ready count;
- desktop may expose secondary actions inline while phone uses a compact More action;
- expanded/collapsed state is UI-only and must not mutate the Journey.

No authoritative workflow decision may be moved to the client as part of this phase.

## Evidence-first guardrails preserved

The UI refinement must continue to enforce these product boundaries:

- evidence/document upload remains the preferred source of transaction facts;
- extracted values remain proposals until accepted/corrected under the existing server contract;
- accepted/PC-entered values are never silently overwritten;
- Aadhaar remains masked in ordinary UI;
- server-returned `permittedActions` remains the action authority;
- raw tenant/journey identifiers are not business labels;
- Phase-1 PAN/protected-identity boundaries remain unchanged;
- Post-Delivery reconciliation remains outside Phase-1 scope.

## Web reference artifacts

Current Web reference set on `verigence-web/dev`:

- `docs/uc-003-booking-delivery-audit/UC03_UI_LAYOUT_AMENDMENT_2026-08-25.md`
- `docs/uc-003-booking-delivery-audit/UC03_PC_UI_MOCKUPS_2026-08-25.html`
- `src/pages/DashboardPage.tsx`
- `src/styles/uc03-phase2-worklist.css`
- `src/pages/CreateBookingPage.tsx`
- `src/pages/BookingWorkspacePage.tsx`

The HTML mockup is reference-only. Runtime React/CSS, accessibility behavior and Audit Core server responses remain authoritative.

## Branch note

The canonical UC03 planning branch remains `planning/uc-003-booking-delivery-audit`, but the current Web `dev` branch already contains later UC03 UI integration together with unrelated product changes. Do not bulk-merge all of `dev` into the planning branch merely to reconcile this UI amendment; any back-port must be UC03-scoped.

## Status as of 25-Aug-2026

- Phase 1 UI direction: implemented in current Web `dev` baseline;
- Phase 2 progressive list/filter/compact-expanded refinement: implemented in current Web `dev` baseline;
- updated static UI reference board: added;
- Audit Core business/workflow contract changes required: none;
- human UAT: **PENDING**;
- formal product closure must not be inferred from this document.
