# Verigence Audit Core — Default Operational Role Bundles

**Document ID:** VAC-SD-RBAC-001  
**Version:** 1.1  
**Status:** APPROVED ARCHITECTURE DECISION  
**Date:** 2026-08-15  
**Updated:** 2026-10-18  
**Applies to:** `VAC-SD-003 v2.1`, `VAC-SD-AUTH-001`, Security onboarding/RBAC  
**Authority:** Project-owner decision recorded 2026-08-15; `REVIEWER` template added by project-owner decision recorded 2026-10-18

## 1. Purpose

This document defines the six default operational role templates used when a Verigence Tenant is onboarded to Audit Core and Document Intelligence (DI):

- `PC` — Process Consultant
- `TL` — Team Lead
- `PM` — Project Manager / PMO operating role
- `CRM` — CRM operator
- `REVIEWER` — document reviewer and validator (added in v1.1)
- `Executive` — Project-wide oversight: reads everything, corrects ordinary business data, neither captures nor verifies (bundle stated in v1.1)

These are **default Security role templates**, not hard-coded authorization rules inside Audit Core or DI. Security resolves the Tenant's current role templates into the authoritative `permissions[]` claim.

`Executive` remains the special Project-wide Audit Core role under `VAC-SD-003`; v1.1 records its bundle here because the role was previously defined everywhere except where its privileges are listed. Security's seeded defaults are authoritative for it, and section 9 reproduces them rather than restating an intent. `SUPER_ADMIN` and `TENANT_ADMIN` are Security administration roles, not Audit Core operating roles.

## 2. Default-template and override rule

Security SHALL maintain a platform default definition for the five operational roles. Tenant onboarding seeds/copies those defaults into the Tenant's role configuration so that normal users can be onboarded without manually assembling permissions.

After onboarding:

- a **Tenant Admin** may change the five role templates for that Tenant only;
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

## 8. Default REVIEWER bundle

The Reviewer sits at one physical location and is mapped to several Dealer Outlets. The Reviewer reviews what a PC has captured: opens every uploaded document, corrects values Document Intelligence read incorrectly, resyncs a page from DI, and records that a Journey's Booking or Delivery documents have been reviewed. The Reviewer does not upload evidence and does not work a task queue.

Reviewer business scope follows the flexible CRM scope shape — Project-wide, Dealer-scoped, Outlet-scoped, or a union of selected Dealers and Outlets — because one Reviewer covers several Outlets at once. The capability difference from CRM is `audit.journey.update`.

### Audit Core

- `audit.project.read`
- `audit.master.read`
- `audit.customer.read`
- `audit.journey.read`
- `audit.journey.update`
- `audit.evidence.read`
- `audit.evidence.refresh`
- `audit.finding.read`
- `audit.analytics.read`

### DI

- `di.subject.read`
- `di.document.read`
- `di.document.content.read`
- `di.document.fields.read`
- `di.document.quality.read`

### Guard

The Reviewer's capability boundary is set by what this bundle withholds, not by a role-name check inside Audit Core:

- no `audit.evidence.upload` and no `di.document.upload` — a Reviewer cannot upload;
- no `audit.work.read`, `audit.work.update` or `audit.work.manage` — a Reviewer has no task queue and cannot complete, cancel or close a Task;
- no `audit.finding.create`, `audit.finding.update`, `audit.finding.resolve` or `audit.review.decide` — a Reviewer neither raises nor adjudicates Findings;
- no `audit.payment.verify`, `audit.delivery.verify`, `audit.trade_in.verify` and no `di.verification.write` — the Booking/Delivery documents-reviewed statuses are Audit Core Journey state written under `audit.journey.update`; they are neither a DI verification nor a TL/PM business verification.

`audit.journey.update` is the permission the Phase 2 Journey and document endpoints authorize against, the page resync endpoint included. `audit.evidence.refresh` is retained for the evidence-level refresh path already granted to PC and TL.

This bundle introduces no new permission names. Every permission listed is already registered for PC, TL, PM or CRM above.

## 9. Default Executive bundle

Executive is Project-wide and read-broad: it sees every part of a deal and may
correct ordinary business data, but it does not capture evidence, verify, or
adjudicate. It is a curated bundle, not "every privilege" -- the distinction
matters, because the permissions it withholds are the ones that would let an
Executive do a PC's or a TL's job rather than oversee it.

The authoritative definition is Security's own, seeded by
`migrations/0013_v2_operating_role_defaults.sql`; the lists below are that
bundle, not a restatement of intent.

### Audit Core (25)

- `audit.project.read`
- `audit.master.read`
- `audit.customer.read`
- `audit.journey.read`
- `audit.evidence.read`
- `audit.payment.read`
- `audit.delivery.read`
- `audit.trade_in.read`
- `audit.finding.read`
- `audit.review.read`
- `audit.work.read`
- `audit.daily_ops.read`
- `audit.crm.read`
- `audit.escalation.read`
- `audit.analytics.read`
- `audit.audit_trail.read`
- `audit.project.update`
- `audit.master.write`
- `audit.customer.write`
- `audit.journey.update`
- `audit.payment.write`
- `audit.delivery.write`
- `audit.trade_in.write`
- `audit.finding.update`
- `audit.work.update`

### DI (13)

- `di.subject.read`
- `di.document.read`
- `di.document.content.read`
- `di.document.fields.read`
- `di.document.quality.read`
- `di.verification.read`
- `di.entity_link.read`
- `di.operations.read`
- `di.unassigned_document.read`
- `di.requirement_profile.read`
- `di.extraction_config.read`
- `di.quality_config.read`
- `di.tenant_config.read`

### Deliberately withheld (18)

Recorded because a bundle is defined as much by what it leaves out, and
because an Executive asking why they cannot do one of these should find the
answer here rather than in a migration:

- `audit.crm.execute`
- `audit.crm.manage`
- `audit.daily_ops.execute`
- `audit.daily_ops.review`
- `audit.delivery.verify`
- `audit.escalation.manage`
- `audit.evidence.refresh`
- `audit.evidence.upload`
- `audit.finding.create`
- `audit.finding.resolve`
- `audit.journey.create`
- `audit.journey.submit`
- `audit.master.publish`
- `audit.payment.verify`
- `audit.project.assignment.manage`
- `audit.review.decide`
- `audit.trade_in.verify`
- `audit.work.manage`

No `audit.*.verify`, so formal verification stays with TL and PM. No
`audit.evidence.upload` or `audit.evidence.refresh`, so capture stays with the
PC. No `audit.review.decide`, `audit.finding.create` or `audit.finding.resolve`,
so adjudication stays where the finding routing puts it. No
`audit.work.manage`, `audit.crm.manage`, `audit.escalation.manage`,
`audit.master.publish` or `audit.project.assignment.manage`.

### Scope

Project-wide, and only Project-wide: an Executive mapping carries no Dealer or
Outlet rows. Audit Core enforces that in
`role_mapping_policy._PROJECT_WIDE_ONLY_ROLES` alongside TL and PM.

### Guard

Holding a write permission is a capability ceiling, not an instruction: Audit
Core still applies its own business policy over when an operation is
available, exactly as it does for a PM's verification permissions. Executive
remains an *Audit Core* role -- it carries no `security.*` administration
permission and no DI platform-administration permission, and `SUPER_ADMIN` and
`TENANT_ADMIN` remain Security administration roles rather than operating
roles.

## 10. Hard safety constraints on editable operational templates

Even when a Tenant Admin or Super Admin customizes these six role templates:

1. only permissions registered in the Security platform permission catalogue may be assigned;
2. Tenant Admin changes are limited to the Tenant in the authenticated token;
3. `security.*` administrative permissions may not be placed into PC/TL/PM/CRM/REVIEWER/Executive templates;
4. `di.platform.whatsapp.admin` may not be placed into Tenant operational templates;
5. `di.document.delete` is excluded from these six default/tenant operational templates because Audit Core evidence is audit-relevant and destructive removal is outside the approved Audit Core operating model;
6. every role-template change records actor, Tenant, previous permission set, new permission set, time and correlation/request identifier where available.

## 11. Canonical Audit Core permission catalogue

The Audit Core-owned permission names used by these defaults are registered in `design/AUDIT_CORE_SECURITY_CATALOG_v2.1.json`. The former v1.0 catalogue remains historical/on hold and SHALL NOT be registered where it conflicts with this decision.

## 12. Implementation dependency

Security owns default-template seeding, Tenant overrides, Super Admin/Tenant Admin authorization, effective permission resolution and change audit. Those capabilities are implemented/tracked in the Security module.
