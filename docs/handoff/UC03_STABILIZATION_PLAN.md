# UC03 Stabilization — Activity Plan

Status: ACTIVE  
Governing document: `docs/handoff/UC03_STABILIZATION_MASTER.md`

## Operating rule

Every activity begins by reading the Master Charter and latest Checkpoint. Work one repository at a time. Do not broaden scope without a verified dependency.

## Step 0 — Stabilization governance baseline

**Status:** COMPLETE

Deliverables:

- Master Charter;
- activity plan;
- current checkpoint;
- implementation-context template.

No application/schema changes are part of Step 0.

## Step 1 — Audit Core data-model and persistence stabilization design

**Status:** IN PROGRESS  
**Mode:** READ-ONLY until separate write approval  
**Repository:** `verigence-audit-core` only

### Objective

Determine the smallest Audit Core changes required to support the agreed Journey lifecycle and final report without over-engineering.

### Questions to close

1. Which existing Journey/business structures already satisfy the requirement?
2. How are Booking reviewed/effective facts persisted today?
3. What is missing for equivalent Delivery reviewed/effective persistence?
4. How should repeated Payments/Receipts be reused and stage provenance retained?
5. Does the existing model support multiple Invoices; if not, what is the smallest missing structure?
6. Can existing document/catalogue structures support canonical document aliases uniformly; if not, what minimal extension is required?
7. What source/provenance information must remain durable after Review?
8. What minimal final-source resolution structure is needed after Delivery?
9. What minimal Booking/post-Delivery rule-run status/reference placeholder is needed without designing the rule engine?
10. How will the supplied final-report workbook fields obtain values from the resolved Audit Core state?

### Required Step-1 deliverables

- evidence-backed current-state summary;
- report-field/business-owner mapping using the supplied final-report format;
- repeated Payments/Receipts assessment;
- repeated/multiple Invoices assessment;
- canonical document identity/alias assessment;
- VERIFIED GAPS only;
- smallest proposed data-model changes;
- database/API impact;
- implementation sequence;
- acceptance-test plan;
- UNKNOWN/business decisions called out explicitly.

### Step-1 stop condition

Step 1 ends when we can state exactly:

- what existing structures will be reused;
- what must be extended;
- what genuinely new structure, if any, is required;
- how Booking and Delivery facts become durable Audit Core business state;
- how final source of truth is persisted;
- how the final-report fields consume that state.

Then STOP before code changes and request/confirm implementation approval.

## Step 2 — DI contract / extraction coverage validation

**Status:** NOT STARTED FOR FINAL PLAN

Enter this step only after Step 1 is closed.

Repository: `verigence-di` only.

Objective:

- validate exact published Booking/Delivery document contracts and field keys needed by the Step-1 design;
- validate document-type canonical identities/aliases against actual DI catalogue/profile evidence;
- classify required sources as PUBLISHED / DRAFT / MISSING / UNKNOWN;
- modify DI only if evidence proves a capability required by the approved Step-1 design is missing or incorrect.

Do not redesign DI persistence if its existing fact model already satisfies the requirement.

## Step 3 — Cross-repository implementation plan and approval gate

**Status:** NOT STARTED

Consolidate only verified gaps from Step 1 + Step 2.

Before writes, define per repository:

- implementation branch;
- exact files/tables/contracts in scope;
- files explicitly not to touch;
- migration/API/backward-compatibility impact;
- acceptance tests;
- rollback plan;
- write approval required.

## Step 4 — Backend implementation

**Status:** NOT STARTED

Implement approved Audit Core work first. Implement DI changes only if Step 2 proved them necessary.

Each implementation branch must contain/read an `UC03_IMPLEMENTATION_CONTEXT.md` based on the template in this handoff folder.

No merge/deploy without separate approval.

## Step 5 — Backend end-to-end verification

**Status:** NOT STARTED

Verify representative scenarios end-to-end, including:

- Booking capture/review/effective persistence;
- Delivery capture/review/effective persistence;
- unchanged and changed values;
- multiple documents;
- multiple same-type documents where valid;
- multiple receipts/payments;
- multiple invoices;
- overlapping business fields from multiple legitimate sources;
- canonical document aliases;
- provenance traceability;
- final-source resolution;
- rule-run placeholders/status transitions;
- final-report readiness gate.

Do not claim FIXED before DB-level/end-to-end evidence passes.

## Step 6 — UC03 Web stabilization

**Status:** NOT STARTED

Repository: `verigence-web` only after backend contracts are stable.

Objectives:

- one employee-facing Journey experience;
- raw DI fields/evidence confined to Review experiences;
- normal Journey/business views consume Audit Core;
- Payments/Receipts shown in one Journey section;
- multiple invoices supported cleanly;
- current stage/status communicates Booking vs Delivery progression;
- final report shown only when ready;
- preserve approved Verigence branding;
- stabilize V2 layout/responsive/scroll behaviour without unrelated global redesign.

## Step 7 — Final UC03 verification

**Status:** NOT STARTED

Overall stabilization is complete only when backend and UI agreed definitions of done are both satisfied, including final report generation from the supplied format after post-Delivery rule completion.

## No-diversion rule

Adjacent technical debt may be recorded as a separate note, but it is not part of stabilization unless explicitly approved.
