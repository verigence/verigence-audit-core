# Verigence Audit Core — Living Design Decisions

**Document type:** Living / free-running design document  
**Status:** ACTIVE  
**Working branch:** `dev`  
**Baseline reference:** `baseline/dev-2026-08-18`  
**Started:** 2026-08-18

## 1. Purpose and maintenance rule

This document is the running design authority for Audit Core decisions made after the current `dev` baseline.

The versioned baseline design documents remain preserved as the historical baseline. New design decisions SHALL be added here as dated, numbered decisions while implementation continues on `dev`.

Rules for maintaining this document:

1. Append new decisions; do not silently rewrite or delete earlier decisions.
2. If a decision changes, mark the earlier decision `SUPERSEDED` and reference the replacing decision.
3. Keep decisions concise and implementation-oriented.
4. When a future formal baseline is cut, the accepted living decisions may be consolidated into the corresponding versioned architecture/design documents.
5. Where a living decision conflicts with an older baseline statement, the newer explicitly approved living decision governs work on `dev` until the next formal baseline.

---

# Decision Log

## LD-001 — Global employee identity, single Tenant role, functional role bundle, independent Dealer/Outlet assignment

**Date:** 2026-08-18  
**Status:** APPROVED  
**Applies to:** Human users, Security integration, Tenant RBAC, Audit Core business scope and work routing

### 1. Human USER is global and independent of Tenant

A human USER represents an employee of the company and is onboarded once at the Verigence Platform/Security level.

The USER is not created for a Tenant, Project, Dealer, Outlet or operational role.

The same global Security `user_id` is reused everywhere that employee is authorized to work.

```text
EMPLOYEE
   -> one global Security USER
   -> one stable user_id
   -> zero, one or many Tenant authorizations
```

Adding or removing access to a Tenant SHALL NOT create another USER identity.

### 2. One Audit operational role per USER per Tenant

Within one Tenant, a USER may have exactly one Audit operational role from the following set:

- `PC` — Process Consultant
- `TL` — Team Lead
- `PM` — Project Manager / PMO operating role
- `CRM` — CRM operator
- `Executive` — Executive operating role

A USER may have a different Audit operational role in another Tenant.

Example:

```text
USER U123

Tenant A -> PC
Tenant B -> TL
Tenant C -> PM
Tenant D -> Executive
```

The design SHALL NOT combine multiple Audit operational roles for the same USER inside the same Tenant.

### 3. Role means functional capability, not business geography

The Audit operational role determines the USER's functional capabilities in that Tenant.

Each role resolves to a Tenant-specific permission bundle. The bundle contains the functional permissions that the role may exercise, for example capabilities in areas such as:

```text
document.upload
document.read
document.delete
audit.read
audit.review
audit.verify
audit.analytics
work.read
work.manage
```

The examples above describe the capability model; exact registered permission keys and any module-level restrictions remain governed by the canonical Security/module permission catalogues.

A role SHALL NOT contain Dealer or Outlet information.

A role SHALL NOT imply which Dealer or Outlet the USER may work on.

### 4. Role bundles are defined at Tenant level

`PC`, `TL`, `PM`, `CRM` and `Executive` are stable role identities, but their effective functional permission bundles are resolved for the individual Tenant.

Conceptually:

```text
Tenant A
  PC        -> Tenant A PC permission bundle
  TL        -> Tenant A TL permission bundle
  PM        -> Tenant A PM permission bundle
  CRM       -> Tenant A CRM permission bundle
  Executive -> Tenant A Executive permission bundle

Tenant B
  PC        -> Tenant B PC permission bundle
  TL        -> Tenant B TL permission bundle
  ...
```

Therefore the same role name may be configured differently between Tenants where approved.

Security remains authoritative for role assignment, role-bundle resolution and the effective permissions placed into the Tenant-scoped Security token.

Audit Core SHALL authorize functional actions using the effective permissions issued by Security rather than hard-coding behavior from the role-name string.

### 5. Dealer/Outlet is a separate business assignment

Dealer and Outlet coverage is independent of role and permissions.

Audit Core owns the business-scope assignment that determines where the USER may exercise the functional capabilities already granted by Security.

Conceptually:

```text
USER U123
Tenant A
Role = PC                    <- functional capability from Security

Business assignments:       <- business scope from Audit Core
  Dealer D1 / Outlet O1
  Dealer D1 / Outlet O2
```

A USER may have one or many Dealer/Outlet assignments inside the Tenant according to the operating model.

Changing a Dealer/Outlet assignment SHALL NOT change the USER's role.

Changing a role SHALL NOT automatically change Dealer/Outlet assignments.

### 6. Runtime authorization is intentionally two-dimensional

For an Audit Core operation, the two concerns are checked separately:

```text
1. FUNCTIONAL AUTHORITY
   Does the Tenant-scoped Security token contain the required permission?

2. BUSINESS SCOPE
   Is the USER assigned to the relevant Dealer/Outlet for this operation?
```

Where the operation is Dealer/Outlet scoped, both checks must pass.

```text
ALLOW = required functional permission
        AND
        required Dealer/Outlet business assignment
```

Role is therefore never used as a substitute for Dealer/Outlet scope, and Dealer/Outlet scope is never used as a substitute for functional permission.

### 7. Executive

`Executive` remains the Tenant-wide Audit operating role as defined by the Audit Core design. It is still permission-driven and remains subject to the approved no-destructive-delete/purge constraints of the Audit solution.

Dealer/Outlet assignment is not embedded in the Executive role definition.

### 8. Token / Tenant context

When a global USER works in a Tenant, Security issues/evaluates authorization in that Tenant context.

The Tenant-scoped context resolves:

```text
same global user_id
+
selected tenant_id
+
exactly one Audit operational role for that Tenant
+
Tenant-specific effective functional permissions
```

Audit Core then applies its independent Dealer/Outlet business-scope check.

### 9. Audit Core data-model implication

The business-scope model SHALL represent USER-to-Dealer/Outlet assignment independently from Audit operational role.

The current Audit Core `business_assignments.business_role_code` field mixes two separate concepts and is therefore not part of the intended target model under this decision.

Target conceptual assignment:

```text
business_assignment
  tenant_id
  security_actor_id
  dealer_id
  outlet_id
  effective_from
  effective_to
  assignment_status
  created_by_actor_id
  created_at_utc
  updated_at_utc
```

Role remains Security-owned Tenant authorization and SHALL NOT be duplicated into the Audit Core Dealer/Outlet assignment merely to authorize business functionality.

Implementation/schema/API alignment to this decision should be handled as a separate development change.

### 10. Administrative UX separation

The administration model should present three separate concepts, even if a future UI makes them convenient to manage from one workspace:

```text
A. Employee / USER
   Global identity and lifecycle

B. Tenant Role
   Exactly one of PC / TL / PM / CRM / Executive
   Resolves Tenant-specific functional permissions

C. Business Assignment
   Dealer / Outlet coverage
```

This separation is a design invariant.

### 11. Canonical example

```text
Global USER U123 — Employee A

Tenant T1
  Role: PC
  Functional permissions: resolved from T1 PC bundle
  Business scope:
    Dealer D1 / Outlet O1
    Dealer D1 / Outlet O2

Tenant T2
  Role: TL
  Functional permissions: resolved from T2 TL bundle
  Business scope:
    Dealer D8 / Outlet O15
    Dealer D8 / Outlet O16

Tenant T3
  Role: Executive
  Functional permissions: resolved from T3 Executive bundle
  Business scope: Tenant-wide according to Executive operating rules
```

There is still only one employee and one global Security `user_id`.

---

## Future decisions

Add subsequent approved design decisions below as `LD-002`, `LD-003`, and so on. Keep this document cumulative on `dev` until the next formal design baseline is created.
