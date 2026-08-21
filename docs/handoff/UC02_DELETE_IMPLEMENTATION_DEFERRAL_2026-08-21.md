# UC02 — Destructive Delete Implementation Deferral

**Status:** OWNER IMPLEMENTATION DEFERRAL
**Date:** 2026-08-21

The approved UC02 destructive-delete / rollback design remains recorded and is not superseded as architecture. The owner has deferred its **implementation** from the current delivery slice.

Current Audit Core implementation scope excludes whole-Project hard-delete orchestration/status/retry and the associated destructive E2E gate. Do not add a replacement soft-delete or process lifecycle to compensate for this deferral.

Continue implementing all non-delete UC02 capabilities: Project provisioning/create/read/update, Dealer/Outlet administration, Role Mapping, Project Masters and DI facades, Project Readiness, activation, post-activation non-destructive administration, and cross-module non-destructive E2E.

When delete work is resumed, return to the existing approved UC02 delete design and the latest DI owner clarification. For DI, the future Phase-1 delete implementation is hard delete only; PURGING/PURGED lifecycle state, purge-operation receipts/status, and recreation-prevention tombstones remain out of scope unless separately approved.

For the current release slice, destructive delete is an explicitly accepted deferred item and must not block completion/promotion of the remaining non-delete UC02 work.
