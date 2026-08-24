# UC02 Project Administration UX / Administration Amendment

**Date:** 2026-08-24
**Scope:** UC02 Project Administration only
**Repositories:** Audit Core + Web

## Owner decisions

1. This amendment applies to UC02 only. **Do not change UC03 mobile/tablet UI as part of this work.** UC03 will be reviewed independently after its current UI is available.
2. The existing Verigence visual identity is frozen for this refactor. **Do not change logos, brand colours, typography tokens, status colours, button colours or the established visual language.** The work is layout/usability and administration behaviour only.
3. UC02 Project Administration is a desktop/Web administration workspace. The primary view on data-heavy steps is the persisted data list/table; Add/Edit is a focused secondary action (drawer/modal/focused panel), not a permanent half-screen form.
4. The browser page must scroll normally. Do not clip the Project Administration workspace with fixed-height containers or `overflow:hidden`. Long data tables may scroll horizontally where required; drawers may scroll independently.
5. Tenant Admin may administer the complete Project assigned to its Tenant. SuperAdmin retains cross-Tenant Project administration. Normal operational roles (PC/TL/PM/CRM/Executive) do not gain Project Administration authority from this amendment.

## Step interaction rules

### Step 1 — Project Details
Persisted Project fields remain the primary view/edit form. Existing field-mutability rules remain authoritative.

### Step 2 — Dealers
Use a full-width Dealer table with Add Dealer as an explicit action. Every Dealer row exposes **Edit** and **Delete**.

Delete uses the existing dependency-impact contract. If dependencies exist, deletion is blocked with a useful dependency message; no silent cascade is introduced.

### Step 3 — Dealer Outlets
Use a full-width Outlet table across **all Dealers**, with Dealer filter/search rather than using a single selected Dealer as the only visible context. Every Outlet row exposes **Edit**, **Map/Location**, and **Delete**.

Add/Edit Outlet uses a focused workspace/drawer with a large Google Maps area. Maps remains optional; manual address remains supported.

### Step 4 — Employees
Employee discovery/list is full-width and scrollable. Employee identity remains Security-owned. UC02 does not hard-delete a global Security USER from Project Administration. Project-specific assignment is controlled in Step 5.

### Step 5 — Role Mapping
Active Mappings is the primary full-width list. **Assign Role** / **Edit Mapping** uses a focused editor. Every mapping exposes **Edit** and **Remove**. The screen must support many employees; it must not be constrained to one visible mapping.

### Step 6 — Project Masters
The primary view is a full-width Master catalogue showing Master, Segment/Scope, lifecycle status, WEF, row count/reference and actions.

Each Project Master exposes the applicable actions:
- Upload / Replace
- Review validation
- Confirm / Publish when permitted
- Delete/reset Project-owned master data while the Project is `CONFIGURING`

A file chooser must not consume most of the permanent screen.

**Reset Project Masters** is available to both Tenant Admin (for its Tenant Project) and SuperAdmin. It is tenant/project scoped, allowed only while the Project is `CONFIGURING`, and must never delete global OEM, Segment or reference masters. The reset is atomic and audit-recorded.

### Step 7 — Readiness
Render the complete readiness checklist with PASS/FAIL/PENDING status and actionable reason/target step. Do not show a blank readiness panel for a valid non-ready response.

### Step 8 — Activate Project
Show a Project activation summary and the readiness blockers. Activate only when readiness passes.

## Delete/edit safety

- Dealer and Outlet PATCH/DELETE APIs already use optimistic concurrency and dependency preflight; Web must expose them.
- Role Mapping PUT/DELETE remains authoritative for edit/remove.
- Project Master deletion/reset is Project-owned only and does not remove global reference data.
- Global Security USER deletion is out of scope for UC02 Project Administration.
- Whole-Project destructive delete remains governed by its separately approved/deferral design; this amendment does not silently implement whole-Project deletion.

## Performance / state rules

- Persisted backend state is authoritative when entering a step.
- Browser memory may cache the selected Project and loaded lists during navigation, but must be invalidated after a mutation.
- Do not reload the Project directory on every step transition when the selected Project has not changed.
- Read-only Project-owned administration reads should use the lightweight authenticated-read path where appropriate and must not require repeated SuperAdmin admin-context calls.
