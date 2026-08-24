# UC02 Project Administration UX / Administration Amendment

**Date:** 2026-08-24  
**Status:** OWNER-APPROVED — supersedes conflicting UC02 assumptions in earlier baselines  
**Scope:** UC02 Project Administration and the UC03 dependencies it configures  
**Repositories:** Audit Core + Web + DI

## Owner decisions

1. This amendment changes UC02 Project Administration only. It does **not** redesign the UC03 mobile/tablet operational UI.
2. The existing Verigence visual identity remains frozen. Logos, brand colours, typography tokens, status colours, button colours and the established visual language are unchanged.
3. UC02 Project Administration is a desktop/Web administration workspace. Persisted lists/tables remain the primary view on data-heavy steps; Add/Edit is a focused secondary action.
4. The browser page scrolls normally. Long data tables may scroll horizontally; focused editors may scroll independently.
5. Tenant Admin may administer the complete Project assigned to its Tenant. SuperAdmin retains cross-Tenant Project administration. Normal operational roles do not gain Project Administration authority.
6. Project Administration keeps **step/context lazy loading**. Do not introduce a large bootstrap response or a Project-wide outlet fan-out merely to reduce request count.

## Step interaction rules

### Step 1 — Project Details

Persisted Project fields remain the primary view/edit form. Existing field-mutability rules remain authoritative.

SuperAdmin also has an explicit **Permanent Project Deletion** operation governed by the hard-delete rule below. It is not implemented as browser-side DELETE calls to individual modules.

### Step 2 — Dealers

Use a full-width Dealer table with Add Dealer as an explicit action. Every Dealer row exposes Edit and Delete.

While a Project is `CONFIGURING`, mistaken Dealer setup may be removed together with empty setup outlets only when linked business/operational dependencies are absent. Active Project Dealer deletion continues to use dependency impact/preflight.

### Step 3 — Dealer Outlets

Dealer Outlets use **Dealer-context lazy loading**:

- load the Dealer list when the step is entered;
- load outlets only for the Dealer currently selected by the administrator;
- cache a Dealer's loaded outlet list for the current page session;
- changing Dealer loads only that Dealer when it has not already been loaded;
- create/update/delete mutates the cached list from the successful API response instead of immediately re-fetching the whole hierarchy.

Do **not** load every Dealer's outlets on step entry and do **not** add a Project-wide aggregate outlet endpoint merely to avoid browser request count.

Every Outlet row exposes Edit/Map/Location and Delete. Add/Edit Outlet uses a focused workspace with a large Google Maps area. Maps/GPS remains optional; manual address remains supported.

### Step 4 — Employees

Employee discovery/list is full-width and scrollable. Employee identity remains Security-owned. UC02 does not hard-delete a global Security USER from Project Administration. Project-specific assignment is controlled in Step 5.

### Step 5 — Role Mapping

Active Mappings is the primary full-width list. Assign Role/Edit Mapping uses a focused editor. Every mapping exposes Edit and Remove.

For PC and CRM outlet scope, outlets are loaded **one Dealer context at a time** and cached for the page session. Previously selected outlet IDs remain selected while another Dealer is loaded. PC mapping supports both `ONSITE` and `SATELLITE` outlets; no artificial onsite/satellite cardinality restriction is introduced.

### Step 6 — Project Masters and Document Intelligence

Audit Core remains authoritative for Project business configuration, including the versioned `document_requirement_profile` used to instantiate Journey evidence requirements.

DI remains generic Document Intelligence. Its Project Administration view exposes **effective configuration**, not only tenant-owned rows:

- globally active Document Types provisioned for the Tenant are shown as effective **Verigence defaults**;
- globally `PUBLISHED` Extraction Profiles for those Document Types are shown as effective **Verigence defaults**;
- inherited defaults may be used as-is;
- customization creates tenant/Project-owned versions using the existing DI lifecycle;
- published global versions remain immutable and are never physically copied into every Project merely for display.

The full DI Test Bench catalogue is not duplicated into each Project. Only effective production configuration is surfaced.

**DI Requirement Profiles are optional advanced DI capability.** UC02 activation and UC03 Journey evidence requirements do not depend on a DI Requirement Profile. Do not create a synthetic default DI Requirement Profile merely to make Readiness green.

Project-owned master reset remains available only while the Project is `CONFIGURING` and must never delete global OEM, Segment, reference masters or inherited Verigence DI defaults.

### Step 7 — Readiness

Render the complete readiness checklist with PASS/FAIL/PENDING status and actionable reason/target step.

Only genuine technical activation prerequisites are `BLOCKING`:

- Project setup/identity validity; and
- Security Tenant lifecycle availability/validity.

The following remain visible as `WARNING` and **do not prevent activation**:

- Dealer/Outlet completeness;
- active Outlet PC coverage;
- Product/Price/Discount masters;
- Project Policy;
- Audit Control;
- Audit Core Document Requirement Profile;
- DI provisioning/customization gaps;
- map/GPS metadata.

When inherited DI defaults are effective, Readiness should communicate that the Project is using Verigence default Document Intelligence configuration and may customize it when different fields/types are required.

### Step 8 — Activate Project

Show the Project activation summary, blocking checks and remaining warnings. The Activate action is disabled only when a `BLOCKING` readiness check is not `PASS` (or the Project is already active). Warnings remain visible after activation and may be completed later.

Activation summary must not force-load all Dealer Outlets merely to display a total; lazy loading remains authoritative.

## Whole-Project hard delete

Owner approval on 2026-08-24 supersedes the earlier baseline prohibition/deferral for this narrowly governed capability:

> **Journey count = 0 → Project may be hard-deleted, irrespective of `CONFIGURING` or `ACTIVE`.**  
> **Journey count > 0 → hard delete is prohibited.**

Deletion is an orchestrated SuperAdmin administrative operation:

1. calculate and display deletion impact/Journey count;
2. require explicit typed Project-name confirmation;
3. lock/re-check the zero-Journey condition;
4. remove Project-owned DI data/configuration and object-storage artifacts while retaining global defaults;
5. delete the existing Security Tenant through the Security-owned Tenant lifecycle API;
6. hard-delete Audit Core Project/setup rows through a narrowly permissioned purge capability;
7. retain the durable `PROJECT_DELETE` administrative receipt, including DI/Security/Audit Core receipts.

The browser must never simulate this transaction with a collection of independent module DELETE calls. The operation is idempotent/recoverable at the orchestration boundary.

Security source is not modified for UC02 hard delete because the existing Security Tenant delete capability is sufficient.

## Delete/edit safety

- Dealer and Outlet PATCH/DELETE APIs use optimistic concurrency and dependency preflight.
- Role Mapping PUT/DELETE remains authoritative for edit/remove.
- Project Master deletion/reset is Project-owned only and does not remove global reference/default data.
- Global Security USER deletion is out of scope for UC02 Project Administration.
- Whole-Project hard delete is the explicit exception above and is permitted only at Journey count zero.
- The Audit Core runtime role does not receive broad DELETE privileges; destructive Project cleanup is encapsulated behind the approved purge function/orchestrator.

## Performance / state rules

- Persisted backend state is authoritative when a context is first loaded.
- Browser memory may cache the selected Project and already-loaded per-step/per-Dealer data during the page session.
- A successful create/update/delete should update that cache directly from the API result whenever possible rather than issuing redundant follow-up list requests.
- Cache is cleared when Project context changes or the Project is deleted.
- Do not reload the Project directory on every step transition when the selected Project has not changed.
- Read-only Project-owned administration reads should use the lightweight authenticated-read path where appropriate and must not require repeated SuperAdmin admin-context calls.
