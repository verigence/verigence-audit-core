# Verigence Audit Core — Default Operational Role Bundles

**Document ID:** VAC-SD-RBAC-001  
**Version:** 1.0  
**Status:** APPROVED ARCHITECTURE DECISION  
**Date:** 2026-08-15  
**Applies to:** `VAC-SD-003 v2.1`, `VAC-SD-AUTH-001`, Security onboarding/RBAC  
**Authority:** Project-owner decision recorded 2026-08-15

## 1. Purpose

This document defines the four default operational role templates used when a Verigence Tenant is onboarded to Audit Core and Document Intelligence (DI):

- `PC` — Process Consultant
- `TL` — Team Lead
- `PM` — Project Manager / PMO operating role
- `CRM` — CRM operator

These are **default Security role templates**, not hard-coded authorization rules inside Audit Core or DI. Security resolves the Tenant's current role templates into the authoritative `permissions[]` claim.

The existing Audit Core `Executive` role remains a separate special tenant-wide Audit Core role under `VAC-SD-003`; it is not one of these four onboarding operational templates. `SUPER_ADMIN` and `TENANT_ADMIN` are Security administration roles, not Audit Core operating roles.

## 2. Default-template and override rule

Security SHALL maintain a platform default definition for the four operational roles. Tenant onboarding seeds/copies those defaults into the Tenant's role configuration so that normal users can be onboarded without manually assembling permissions.

After onboarding:

- a **Tenant Admin** may change the four role templates for that Tenant only;
- a **Super Admin** may change a Tenant's role templates and may change the platform defaults used for future Tenant onboarding;
- changing a platform default SHALL NOT silently rewrite an already-onboarded Tenant's customized role configuration;
- role-template changes are audited and apply to subsequently issued/refreshed Security tokens;
- `roles[]` remains informational; the resolved `permissions[]` claim remains authoritative.

Operational role templates may contain registered `audit.*` and Tenant-scoped `di.*` permissions. They SHALL NOT be used to grant Security administration privileges or DI platform-administration privileges.

## 3. Authorization layering

A normal Security user token may contain permissions from both Audit Core and DI.

Audit Core checks the relevant `audit.*` permission for the incoming business action. If that action requires DI, Audit Core requests only the required `di.*` permission through Security token exchange. The downstream token remains narrowed by the approved intersection rule in `VAC-SD-AUTH-001`.

Therefore a role may legitimately contain both:

```text
audit.evidence.upload
+
di.document.upload
```

The first authorizes the Audit Core business action; the second allows Security to authorize the corresponding delegated DI operation. Neither module authorizes by role-name string.

## 4. Default PC bundle

### Audit Core

- `audit.project.read`
- `audit.master.read`
- `audit.customer.read`
- `audit.customer.write`
- `audit.journey.create`
- `audit.journey.read`
- `audit.journey.update`
- `audit.journey.submit`
- `audit.evidence.read`
- `audit.evidence.upload`
- `audit.evidence.refresh`
- `audit.payment.read`
- `audit.payment.write`
- `audit.delivery.read`
- `audit.delivery.write`
- `audit.trade_in.read`
- `audit.trade_in.write`
- `audit.finding.read`
- `audit.finding.create`
- `audit.work.read`
- `audit.work.update`
- `audit.daily_ops.read`
- `audit.daily_ops.execute`

### DI

- `di.subject.create`
- `di.subject.read`
- `di.document.upload`
- `di.document.read`
- `di.document.content.read`
- `di.document.fields.read`
- `di.document.quality.read`
- `di.entity_link.read`
- `di.entity_link.write`

### Guard

PC receives no `audit.*.verify` capability and no `di.verification.write`. Capture/upload does not imply formal verification.

## 5. Default TL bundle

### Audit Core

- `audit.project.read`
- `audit.master.read`
- `audit.customer.read`
- `audit.journey.read`
- `audit.evidence.read`
- `audit.evidence.refresh`
- `audit.payment.read`
- `audit.payment.verify`
- `audit.delivery.read`
- `audit.delivery.verify`
- `audit.trade_in.read`
- `audit.trade_in.verify`
- `audit.finding.read`
- `audit.finding.create`
- `audit.finding.update`
- `audit.review.read`
- `audit.review.decide`
- `audit.work.read`
- `audit.work.update`
- `audit.work.manage`
- `audit.daily_ops.read`
- `audit.daily_ops.review`
- `audit.escalation.read`
- `audit.analytics.read`

### DI

- `di.subject.read`
- `di.document.read`
- `di.document.content.read`
- `di.document.fields.read`
- `di.document.quality.read`
- `di.verification.read`
- `di.verification.write`
- `di.operations.read`

## 6. Default PM bundle

### Audit Core

- `audit.project.read`
- `audit.project.update`
- `audit.project.assignment.manage`
- `audit.master.read`
- `audit.customer.read`
- `audit.journey.read`
- `audit.evidence.read`
- `audit.evidence.refresh`
- `audit.payment.read`
- `audit.payment.verify`
- `audit.delivery.read`
- `audit.delivery.verify`
- `audit.trade_in.read`
- `audit.trade_in.verify`
- `audit.finding.read`
- `audit.finding.create`
- `audit.finding.update`
- `audit.finding.resolve`
- `audit.review.read`
- `audit.review.decide`
- `audit.work.read`
- `audit.work.update`
- `audit.work.manage`
- `audit.daily_ops.read`
- `audit.daily_ops.review`
- `audit.crm.read`
- `audit.crm.manage`
- `audit.escalation.read`
- `audit.escalation.manage`
- `audit.analytics.read`
- `audit.audit_trail.read`

### DI

- `di.subject.read`
- `di.document.read`
- `di.document.content.read`
- `di.document.fields.read`
- `di.document.quality.read`
- `di.verification.read`
- `di.verification.write`
- `di.operations.read`

### Guard

Possession of a PM verification permission is a capability ceiling only. Audit Core still enforces the configured business policy governing when PM review/verification is applicable.

## 7. Default CRM bundle

### Audit Core

- `audit.project.read`
- `audit.customer.read`
- `audit.journey.read`
- `audit.evidence.read`
- `audit.finding.read`
- `audit.work.read`
- `audit.work.update`
- `audit.crm.read`
- `audit.crm.execute`
- `audit.escalation.read`

### DI

- `di.subject.read`
- `di.document.read`
- `di.document.content.read`
- `di.document.fields.read`
- `di.document.quality.read`

CRM is read-only in DI by default and receives no upload, configuration or verification-write capability.

## 8. Hard safety constraints on editable operational templates

Even when a Tenant Admin or Super Admin customizes these four role templates:

1. only permissions registered in the Security platform permission catalogue may be assigned;
2. Tenant Admin changes are limited to the Tenant in the authenticated token;
3. `security.*` administrative permissions may not be placed into PC/TL/PM/CRM templates;
4. `di.platform.whatsapp.admin` may not be placed into Tenant operational templates;
5. `di.document.delete` is excluded from these four default/tenant operational templates because Audit Core evidence is audit-relevant and destructive removal is outside the approved Audit Core operating model;
6. every role-template change records actor, Tenant, previous permission set, new permission set, time and correlation/request identifier where available.

## 9. Canonical Audit Core permission catalogue

The Audit Core-owned permission names used by these defaults are registered in `design/AUDIT_CORE_SECURITY_CATALOG_v2.1.json`. The former v1.0 catalogue remains historical/on hold and SHALL NOT be registered where it conflicts with this decision.

## 10. Implementation dependency

Security owns default-template seeding, Tenant overrides, Super Admin/Tenant Admin authorization, effective permission resolution and change audit. Those capabilities are implemented/tracked in the Security module.
