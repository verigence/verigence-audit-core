# Phase 2 Design Closure Matrix

Source of truth:
- Verigence Phase 2 Design Blueprint v2.2
- v2.1 UI mockups are information/data intent only where carried forward by v2.2; v2.2 visual guardrails supersede the literal mockup layout.
- Existing UC03 pages/routes remain unchanged until P2 acceptance/cutover.

Status legend:
- COMPLETE: implemented and acceptance evidence exists.
- PARTIAL: implementation exists but is incomplete against design.
- GAP: required design capability not implemented.
- NOT PROVEN: code may exist, but required DEV/acceptance evidence does not.
- BLOCKED: cannot be completed safely without a clarified dependency/contract.

| Area | Design requirement | Current state | Closure requirement / evidence |
|---|---|---|---|
| P2 isolation | p2-prefixed API/tables/modules and /p2 Web routes; no destructive legacy change | PARTIAL | Re-run diff guard against pre-P2 baselines; prove no legacy route/table/page removed for P2. |
| Authorization | Security-backed P2 authorization is authoritative; local DB context must not become a second independent authorization authority | PARTIAL | Review uc03_p2_access + every P2 endpoint; remove/reshape duplicate authorization decisions without broadening data scope; add tests for allowed/denied scope. |
| Upload acceptance | Direct object-storage upload; finalize only after durable object confirmation + DB work record | PARTIAL | Contract/integration tests proving no DI/rule call before durable ACK. |
| Multi-page PDF | Original retained; deterministic page queue; bounded parallel processing; retries/dead-letter/reconciliation visible | PARTIAL | 20-page PDF E2E + restart/no-page-loss test + retry/dead-letter evidence. |
| Scoped reducers / parallelism | Document-local processing stays parallel; coalescing/leases scoped to (tenant, journey, reducer_type), never whole-Journey | GAP | Replace whole-Journey P2 reconciliation dependency with explicit IDENTITY/VEHICLE/DEAL/PAYMENT/FINANCE/INSURANCE/REGISTRATION reducer scheduling and version-aware coalescing; concurrency tests. |
| Document processing | Upload -> classify -> extract -> Audit Core sync -> processor/resolver -> controls -> tasks | PARTIAL | E2E trace for representative Booking + Delivery docs showing each stage and lineage. |
| Unified document facade | P2 document-driven GET/detail/correct/replace/logical-void API; no stage-specific Web contract | GAP | Add design-contract replace/re-upload + logical supersede routes; align correction route; preserve immutable evidence/lineage. |
| Complete document blueprint | Executable mapping for verified 29-document universe: persistence, processors/resolvers, controls, Journey 360, task effects | GAP | Build/validate machine-readable P2 registry against live DI schema/rule/task catalogs before enabling executable entries. |
| Stage engine | Six stages: BOOKING_DOCUMENTS -> BOOKING_VERIFICATION -> BOOKING_COMPLETE -> DELIVERY_DOCUMENTS -> DELIVERY_VERIFICATION -> DELIVERY_COMPLETE | PARTIAL | Align runtime/status naming with approved six-stage product model and validate event-driven transitions. |
| Booking gates | Booking Form + PAN + Aadhaar + minimum Booking payment + zero manual verification | PARTIAL | Unit + integration + correction/reopen + payment aggregation tests. |
| Delivery gates | Same framework; business rules remain placeholder until approved | COMPLETE by design | Must not invent completion criteria. Clearly show configuration pending where applicable. |
| Payment allocation | Chronological eligible non-duplicate receipts; threshold satisfies Booking gate; remainder available to Delivery | NOT PROVEN | Integration tests for multi-receipt, duplicate change and correction/reallocation. |
| Control registry/ledger | One authoritative P2 control state across native + external Rule Engine | PARTIAL | Validate every enabled executable control maps to one P2 identity/state and dependency/fact version; retry/waiting/error semantics tested. |
| Async control execution | HTTP actions enqueue; no synchronous full rule run; only changed dependencies execute | PARTIAL | Trace tests proving asynchronous path, dedupe and no duplicate active control work. |
| Machine task completion | Action -> VERIFYING -> exact origin rerun -> PASS closes / FAIL returns / technical retry | PARTIAL | Native + Rule Engine E2E task verification tests. |
| Human task completion | Assignee action -> requester review -> ACCEPT closes / REJECT+comment returns same root chain | PARTIAL | Full UI/API round-trip test across two actors/roles. |
| Task dedupe | One logical active task per origin/focus/dedupe key | PARTIAL | Retry/flapping test proving no duplicate active task. |
| Task Queue API | Self-contained Type/Category/Origin/Description/Severity/Priority/SLA/Owner/Reference/Status/Action | PARTIAL | Add Journey/Customer context and readable immutable reference fields; remove raw-JSON dependence in UI. |
| Documents UI | One upload surface + readiness + compact file/page table; type, processing, review state, blocker, one action | PARTIAL | Redesign current P2 Documents page to v2.2 professional/minimal pattern; responsive UAT. |
| Document Review/Edit | Source preview/boxed evidence + extracted/effective values + correction history + task/rule references | PARTIAL | Validate all required fields/history/lineage; correction round-trip; desktop/mobile. |
| Journey 360 preservation | Keep current enhanced Journey 360 information architecture and business panels | PARTIAL | No separate simplified Journey 360. Preserve all existing customer/deal/payment/vehicle/finance/insurance/etc. behavior. |
| Journey 360 P2 stage/readiness | Six-stage progression + Booking/Delivery readiness + exception counts | GAP/PARTIAL | Add compact additive layer to current Journey 360 without dashboard/card wall. |
| Booking statistics | required/received docs, pages, receipts, payment vs min, manual verification, controls, tasks | GAP/PARTIAL | Complete local read model + compact UI. |
| Delivery statistics | docs, invoices, receipts, Finance/Insurance/Vehicle/Registration, controls, tasks | GAP | Complete local read model + compact UI. |
| Journey statistics | uploads, reuploads, superseded, extraction failures, retries, corrected fields, findings, tasks, SLA breaches | GAP | Complete local read model + compact UI. |
| Source-aware Deal/Commercial | Standard/Master -> Booking Offer -> Billed -> Paid/Actual -> Variance/Control; preserve source lineage | PARTIAL | Reconcile existing Journey 360 data with P2 projection; parity test representative journey. |
| Lazy business detail | Documents; Deal; Vehicle/Finance/Insurance/etc.; Tasks/Findings/Activity loaded on demand | GAP/PARTIAL | Section endpoints/read models; avoid giant initial payload and live DI/rule calls. |
| Journey 360 performance | Local Audit Core read model, no synchronous DI/Rule Engine/control execution on first paint; p95 target | NOT PROVEN | Instrumented DEV p95 + parity reconciliation. |
| Activity/history | Paged operational detail with task/control/document lineage | PARTIAL | Expose readable Journey activity/history drill-down. |
| P2 top-level navigation | Professional, minimal, existing Verigence shell | PARTIAL | Documents + Tasks as clear Phase 2 entry points; Journey 360 entered in Journey context; no duplicate/abstract work-queue UX. |
| Responsive behavior | Desktop tables/split panes; mobile compact rows/accordions | NOT PROVEN | Desktop + mobile UAT/screenshots. |
| Error handling | Actionable product messages; no raw infrastructure/system errors exposed | PARTIAL | Verify all P2 pages for 4xx/5xx/timeout/retry states. |
| Worker deployment | Dedicated P2 worker preferred, isolated from API runtime | NOT PROVEN | Confirm DEV service/process, config, health/observability and deployed SHA. |
| P2 API deployment | Route contract + migrations + runtime deployment evidence | PARTIAL | Add P2 route smoke tests to deploy workflow; verify actual P2 endpoints after deploy. |
| Canary | Selected tenant/project/new journeys, parallel observation/reconciliation | GAP | P2.6 report and rollback test. |
| Existing journey bootstrap | Explicit optional read-only bootstrap; lineage + unmatched reconciliation | GAP | P2.7 implementation/evidence before using existing journeys as P2 parity proof. |
| Cutover | P2 default only after stability + zero-loss + user signoff | GAP | P2.8 acceptance pack. |
| Legacy retirement | Separate approval only after cutover | NOT STARTED by design | No destructive action in current closure scope. |

## Merge gate

No Phase 2 feature PR may be called complete unless:
1. design row is COMPLETE (or explicitly COMPLETE by design),
2. automated tests pass,
3. DEV deployment SHA is confirmed,
4. relevant E2E smoke/UAT evidence exists,
5. no legacy regression is introduced.

CI green alone is never sufficient evidence of Phase 2 completion.
