# UC03 Phase 2 — Design Validation & Closure Matrix

Baseline: Verigence Phase 2 Design Blueprint v2.2 (27 Sep 2026)
Validation date: 27 Sep 2026
Status rule: COMPLETE means design requirement + code + automated validation + DEV evidence where applicable.

| Area | Design requirement | Current state | Status | Closure evidence required |
|---|---|---|---|---|
| Isolation | p2_* tables/modules/APIs and /p2 Web routes; legacy unchanged | Isolated runtime and routes exist | PARTIAL | Diff audit proving no legacy behavior regression |
| Durable upload | Presigned direct object upload; finalize only after object verified/durable metadata committed | Implemented in P2 API/Web | PARTIAL | DEV upload smoke + object CORS + restart/no-loss test |
| Multi-page PDF | Split into independently retryable pages; bounded parallel worker | Worker code implemented | PARTIAL | Separate P2 worker deployed; 20-page DEV test; retry/dead-letter test |
| Worker runtime | Dedicated P2 worker process preferred/required by runbook | Worker module/config exists; current Audit Core DEV workflow deploys API only | BLOCKED | Railway P2 worker service deployed from same tested SHA and health/log evidence |
| Stage engine | Six stages; approved Booking gates; Delivery placeholder | Booking evaluator exists; six-stage Web representation exists | PARTIAL | Integration test through upload/correction/payment/task events |
| Booking gates | Booking Form, PAN, Aadhaar, minimum payment, no manual verification | Encoded | PARTIAL | Unit/integration/DEV evidence |
| Control ledger | One authoritative P2 state across native + external executors | p2_control_state + rule-execution bridge exist | PARTIAL | Stage attribution design gap; retry/restart + executor coverage tests |
| Stage-level control stats | Booking/Delivery controls pass/fail/waiting | p2_control_state has no authoritative stage dimension | NOT DONE | Persist authoritative stage/scope for control execution or revise design explicitly |
| Tasks | Dedup, severity/priority/SLA/owner/reference/actions, machine verify, human requester round-trip | Backend lifecycle implemented; Web improved | PARTIAL | E2E machine PASS/FAIL/retry + human Accept/Reject round-trip |
| Task Queue UX | Action-first professional worklist; clear context/reason/reference | Latest Web has priority, severity, reason, Journey/customer, owner/SLA, status, primary action | PARTIAL | DEV visual/UAT + ordering verification |
| Task ordering | Priority + SLA + status | API currently primarily due-date ordered | NOT DONE | Deterministic priority/SLA/status ordering contract + test |
| Documents UX | Readiness + one upload control + compact processing/action table | Implemented substantially | PARTIAL | DEV realtime processing/retry/reupload smoke |
| Document Review | Source preview/boxed fields; extracted/effective; correction history; task/rule refs | Boxed preview + extracted/effective + correction action exist | PARTIAL | Correction history and task/rule references still required |
| Reupload/replace | Unified replace/reupload with lineage, non-destructive | Backend design exists only partially in current surface | NOT DONE | P2 replace endpoint + Web action + superseded lineage E2E |
| Journey 360 stage/statistics | Six-stage progress + Booking/Delivery/Journey stats | Latest Web implements view; deployed API did not provide statistics | PARTIAL | PR #390 API contract + CI + DEV smoke |
| Journey 360 source-aware business view | Preserve rich product pattern; Deal source/master/booking/billed/paid/variance; Vehicle/Payments/Finance/Insurance/Registration | P2 page currently links to legacy Detailed Journey 360; does not implement P2 business sections | NOT DONE | P2 business projections/lazy sections or approved reuse design without breaking isolation |
| Journey 360 tabs | Documents, Deal & Commercial, Vehicle, Payments, Finance, Insurance, Registration, Tasks, Findings, Activity | P2 tabs only Journey 360 / Documents / Tasks | NOT DONE | Required lazy sections + routing/API contracts |
| Findings/Activity | Paged operational detail, lazy-loaded | Counts exist; full P2 detail surfaces absent | NOT DONE | P2 findings/activity endpoints/views or approved reuse |
| Overview read performance | Local Audit Core read model; no DI/Rule Engine calls; lazy detail | Overview is local DB but incomplete; no p95 evidence | PARTIAL | p95 benchmark + parity reconciliation |
| Authorization | One Security-backed P2 authorization authority; local DB context not second authority | Security decision plus local business-assignment denial | DESIGN GAP | Security scope contract or explicit design amendment; do not weaken scope blindly |
| Responsive UI | Compact tables/split panes; mobile concise rows/accordions | Scoped responsive CSS exists | PARTIAL | Desktop/mobile UAT |
| Existing journey bootstrap | Optional explicit reconciliation; no silent migration | Not implemented/proven | NOT DONE | Reconciliation job/report if existing journeys are included in canary |
| Canary | Selected tenant/project/new journeys + reconciliation | Not done | NOT DONE | Canary evidence |
| Cutover | Only after stability/zero-loss/user signoff | Not applicable yet | NOT READY | P2.0-P2.7 closure first |

## Current PRs
- Audit Core #390 — P2 overview statistics contract (draft; do not merge until CI/design validation complete)
- Web #358 — Phase 2 UI correction (draft; automated Web/Android checks green on latest head)

## Release gate
Do not mark Phase 2 ready to merge/deploy as a complete solution until all items required for the intended DEV test scope are COMPLETE or explicitly accepted as deferred by design. CI green alone is not completion evidence.
