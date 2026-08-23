# UC03 C3 — Audit / Review Hardening Report

**Date:** 2026-08-23  
**Branch:** `planning/uc-003-booking-delivery-audit`  
**Runtime baseline reviewed:** `1c61f995e707ed1f944f7357f11c5e146ab6c9c5`

## Purpose

This report closes the C3 migration/backfill, query/index, authorization and operability review required by `UC03_IMPLEMENTATION_HANDOFF_v1.1.md`. It does not create a new business checkpoint and does not modify CI/CD.

## Migration and backfill decision

No C3 schema migration is required for the current Phase-1 functional baseline.

The required C3 finding provenance/event structure already exists from `0011_uc03_booking_capture`:

- `audit_findings.stage_code`;
- `origin_kind`;
- `origin_actor_id` / `origin_role_snapshot`;
- `rule_key` / `rule_version_id`;
- `blocking_completion`;
- append-only `audit_finding_events`.

C2 remains migration head `0012_uc03_delivery_capture`.

No historical finding backfill is performed. Legacy findings whose stage/origin cannot be established reliably remain unchanged and are not silently relabelled as Booking/Delivery UC03 findings. This avoids fabricated stage attribution, actor provenance, rule provenance or historical timestamps.

Therefore the proposed `0013_uc03_audit_flag_events.py` is unnecessary because the event/provenance model already exists, and no `0014` backfill migration is justified by safe deterministic source data at this point.

## Query and index review

C3 case-level reads are bounded by one tenant + one Journey and the timeline endpoint is capped at 200 items. Existing baseline indexes include:

- `ix_findings_open (tenant_id, finding_status, severity, created_at_utc DESC)`;
- `ix_audit_finding_events_finding_time (tenant_id, audit_finding_id, occurred_at_utc, finding_event_id)`;
- primary/foreign-key structures and tenant RLS.

The review identified candidate indexes that may improve larger-volume Journey review later:

- `audit_findings (tenant_id, journey_id, stage_code, finding_status, created_at_utc DESC)`;
- `audit_finding_events (tenant_id, journey_id, stage_code, occurred_at_utc DESC)`;
- `review_decisions (tenant_id, journey_id, decided_at_utc DESC)`;
- `finding_evidence (tenant_id, audit_finding_id)`.

They are **not introduced during the current stabilization pass**. Reason: the Phase-1 review is bounded per Journey, the functional baseline is green, representative production cardinality/query plans are not yet available, and adding speculative indexes would introduce another migration without measured evidence. After UC03 is stable, run representative-volume `EXPLAIN (ANALYZE, BUFFERS)` and add only proven indexes through the normal migration process.

This is a documented post-stabilization performance optimization, not a correctness blocker. If consolidated UAT/load validation shows unacceptable latency, it becomes a pre-promotion defect and must be fixed before Phase-1 promotion.

## Authorization matrix review

Server-side C3 tests cover the Phase-1 default authority model:

- PC: read, raise flag, add remark, complete audit when policy permits; cannot review/resolve/void by default;
- TL: review/acknowledge/resolve/reopen; Void not permitted by conservative default;
- PM: review/acknowledge/resolve/reopen; Void not permitted by conservative default;
- Executive: full Phase-1 finding lifecycle including Void;
- effective Project policy can override the default role sets where the approved policy configuration supports it;
- Security permission check and Audit Core Project/business assignment scope both remain server enforced;
- Web/Android consumes `permittedActions` but is not the authority.

Stale `If-Match` conflicts and idempotency replay are included in the C3 acceptance suite.

## Completion-guard authority hardening

Human flag creation no longer accepts `blockingCompletion`. The request model is `extra=forbid` and the database insert for a human finding explicitly persists `blocking_completion=false`.

A completion blocker must come from configured/published policy/rule semantics. The acceptance test therefore seeds a MACHINE `AUDIT_COMPLETION_GUARD`, verifies audit completion is blocked while it remains active, resolves it under an authorized role, and then verifies completion succeeds.

This prevents the client or a human observer from manufacturing workflow authority.

## Operability / telemetry review

C3 uses the existing Audit Core observability conventions rather than adding a parallel telemetry subsystem:

- correlation ID is propagated into finding/stage events;
- user-safe API errors are used for dependency, authorization, conflict and validation failures;
- timeline output does not expose raw `safe_payload`, actor IDs or internal provider payloads;
- list/timeline reads are bounded;
- workflow/finding events remain append-only;
- existing platform logging/metrics/tracing baseline remains unchanged during UC03 stabilization.

CI/CD and deployment telemetry architecture are explicitly frozen by `UC03_EXECUTION_BASELINE_ADDENDUM_2026-08-23.md` until UC03 is stable.

## Hardening conclusion

C3 does not require a new migration for functional correctness. The current implementation reuses the canonical finding/event model, preserves historical truth, enforces server-side authority, and has a green complete Audit Core regression/contract suite.

The Journey-specific index candidates above remain a measured post-stabilization optimization unless product testing demonstrates they are required before promotion.
