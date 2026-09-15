# Unified Work Items — Design v1.1 (Finding lifecycle + Task-only PC)

Builds on `UNIFIED_WORK_ITEMS_DESIGN_v1.0.md` (the `work_items` spine +
`work_item_finding_detail`/`work_item_task_detail`, shipped, Phase 1 only
so far — nothing reads from it yet). v1.0 gave every finding and task a
shared record shape. v1.1 is the actual business lifecycle that runs on
top of that shape, decided over several rounds of direct discussion.

## The rule this whole design serves

**A PC never opens a Finding.** Every finding a PC needs to act on —
whether the rule engine auto-raised it or a TL/PM decided PC needs to do
something — reaches PC only as a Task. Findings are TL/PM's domain
object; Tasks are PC's. This is the one rule everything below exists to
implement correctly.

## Two cross-cutting rules, not just for the PC-upload case

**Booking and Delivery are a process distinction, not a data distinction.**
One journey, one `journey_id`; `stage_code` (`BOOKING`/`DELIVERY`) is an
attribute on a Finding or Task, never a reason to route someone to a
different screen or maintain parallel Booking-only/Delivery-only logic.
This already matches the data model (`journey_stage_states` is
per-stage rows under one journey) and the existing unified capture
screen (one `BookingCaptureV2WorkspacePage`, DI classification decides
which stage a document belongs to, not the PC). Task Queue, Review
Queue, and Audit all operate at the journey level; stage is a filter/tag
on an item, exactly like `severity` or `findingClass` — never a basis
for a second version of a screen or a second workflow.

**Reuse existing screens and components wherever the job is already
done, everywhere in this feature — not only for PC's upload.** The
inline-upload decision below is one instance of this, not a special
case. Before adding any new UI surface for Task Queue, Review Queue, or
the Finding-verdict actions, the first move is checking what
`JourneyDocumentsPage`, `Journey360Page`, `ReviewQueuePage`, and their
existing shared components (`PageHeader`, `SectionCard`, `StatusPill`,
`DocumentCard`, etc.) already cover, and extending or embedding those
rather than building a parallel bespoke screen. A new screen is the
last resort, not the default.

## Finding lifecycle: TL/PM verdicts

A TL (or, identically, a PM once escalated to) reviewing an open Finding
has exactly four verdicts:

| Verdict | Meaning | Remark | Result |
|---|---|---|---|
| **Accept** | Real, and needs no PC work | none required | Finding resolved (≈ today's Confirm Breach) |
| **Reject** | False positive / not applicable | **mandatory, ≤50 words** | Finding resolved as false positive |
| **Take Action** | Real, but PC needs to add info/evidence | **mandatory comment + mandatory severity** | Spawns a Task assigned to PC (see below); Finding stays open, owned by TL, awaiting PC's response |
| **Escalate to PM** | Needs PM's authority/judgment | mandatory | Handed to PM, **always tagged high-priority on the escalation itself** — the finding's own `severity` (the rule's original assessment) is never overwritten |

PM, once an item is escalated, has the identical four verdicts — same
lifecycle one tier up, not a different one.

**Manual observations** (TL/PM raising something the rule engine didn't
catch, case-specific, never promoted to a reusable rule) go through this
exact same four-verdict lifecycle once raised, with one fixed rule: **a
TL-raised observation is always verdicted by PM, never by the TL who
raised it** — segregation of duties, decided as a universal rule rather
than severity-gated (see "Open questions: decided" below). A PM raising
one verdicts it themselves, same as PM already does for anything
escalated to them.

Word-count validation (the 50-word cap) is new — no existing remark field
enforces anything but a character limit. Needs a real word-counter, not a
truncation.

## Task lifecycle: what PC actually does

Two ways a Task reaches PC, same shape either way:

1. **Auto-spawned** — a self-serve (`DATA_GAP`/`DOCUMENT_GAP`) finding
   fires; a Task is created for PC immediately, no TL step.
2. **TL/PM Take Action** — a `VIOLATION` finding TL/PM decided needs PC's
   input; the Task carries TL's comment and TL-set severity.

A PC Task's completion action is **upload a document, or add a comment,
or both** — reusing the exact existing upload component (the same code
`BookingCaptureV2WorkspacePage`/`DeliveryCaptureV2Page` already use for
classification/DI-sync), embedded inline in the Task's own screen. This
is deliberate and non-negotiable: uploading through the real capture path
is what makes the auto document-sync pipeline (DI classification, rule
re-evaluation, etc.) fire exactly as it does today. Nothing about that
pipeline changes — only *where* the upload control is mounted changes.

**Completing a PC Task**:
1. Updates the underlying Finding (links the new evidence/comment to it).
2. Marks the PC Task resolved.
3. **Auto-raises a new review-and-close Task for whoever issued the Take
   Action that spawned this PC Task** — TL in the normal case, but **PM**
   if this round was PM's own Take Action after an escalation. Not
   hardcoded to TL: caught as a real bug in an earlier draft of this doc
   (if PM escalated and then Took Action, "always route back to TL"
   would silently drop PM out of a loop they escalated into). Track the
   issuing actor/role on the Task itself and return to it.

## When TL isn't satisfied with PC's response

No new verb needed — reuse the two that already exist at this point:

- **Send back to PC again** = another **Take Action**, same mechanism,
  mandatory new comment explaining what's still missing, TL may adjust
  severity. PC gets a fresh Task.
- **Escalate to PM** = the same TL→PM path used for a first-pass
  VIOLATION. "PC's response didn't satisfy me" is exactly as valid a
  reason to escalate as anything else.

Two guardrails, both agreed as worth adding:

- **A visible round counter** on the Finding — how many times it's
  bounced PC→TL→PC. Just a count of linked PC Tasks against the same
  Finding; makes "is this stuck?" visible without anyone having to
  notice by hand.
- **Every send-back requires a remark**, same 50-word-cap convention as
  Reject — "not good enough, try again" with no explanation doesn't help
  PC fix anything.

No hard cap on rounds (e.g., auto-escalate after N) unless asked for —
left as a human judgment call for now.

## UI decision: stop linking out to the full workspace

Task Queue, Review Queue, and Audit currently would (if built naively)
send PC to the full `BookingCaptureV2WorkspacePage`/
`DeliveryCaptureV2Page` to act on something. Decided instead: **the
upload + comment control is embedded inline inside the Task itself** —
same underlying component, no page-level navigation, no new screen. The
workspace pages themselves are untouched and keep serving their existing
job (a PC opening a Booking/Delivery normally, outside of any Task
context); Task/Review/Audit simply stop being an on-ramp to them.

## What this means for the v1.0 spine, concretely

- `item_kind='EXECUTION_TASK'` rows gain a real `related_finding_id`
  (today's one existing satellite-task mechanism,
  `PC_DOCUMENT_REUPLOAD`, has no such link — a genuine gap, now closed by
  design: every Task must trace back to the Finding that produced it).
- `work_item_finding_detail.disposition` needs `REJECTED` alongside the
  existing `FIXED`/`CONFIRMED_BREACH`/`FALSE_POSITIVE` (Accept and Reject
  are new, named verdicts, not disposition-free states).
- A `bounce_count` (or equivalent, derived from counting linked Tasks) on
  the Finding side for the round-counter guardrail.
- `origin_kind='HUMAN'` already covers manual observations; no schema
  change needed there, only wiring into the four-verdict lifecycle.

## Race conditions: decided, filtered through "don't build for what's rare"

Explicit instruction applied here: fix what's cheap and/or common; for
anything genuinely rare where the fix would mean touching a different
subsystem, record it as an accepted shortcoming instead of building
speculative machinery for it. Four of the five below are either the
common/primary path or a near-zero-cost reuse of an existing pattern —
worth doing regardless of how often they'd actually fire. The fifth is
neither, and is deliberately deferred.

1. **Decided — duplicate task creation on retry.** Reuse the exact
   `effect_key` pattern `PC_DOCUMENT_REUPLOAD` already proves:
   `effect_key = f"task:{finding_id}:open"`. Cheap (the DB unique
   constraint already exists), and retries/redeliveries are a routine
   occurrence in this codebase, not an edge case — worth doing regardless.
2. **Decided — one transaction, not two.** Route a PC completion through
   the same per-journey advisory lock `_sync_booking_document` already
   uses. Zero new locking primitive; every multi-write completion (update
   Finding, resolve Task, raise the next Task) happens inside it.
3. **Decided, simplified from the original proposal — don't block
   escalation, just make closure clean up after itself.** Rather than add
   a specific "refuse to escalate while a task is open" check (a new
   validation path, a new error case the UI has to handle), the simpler,
   more general rule: **whenever a Finding transitions to a terminal
   state (Accept, Reject, or PM's own verdict), auto-cancel any of its
   still-open Tasks.** One rule, applies everywhere a Finding closes, not
   a special case for escalation specifically — and it fully resolves the
   original concern (an orphaned task with nobody sure who's the next
   step) without adding a new blocking action anywhere.
4. **Decided — automated re-evaluation never touches a Finding mid-human-
   loop.** This is not the rare case — it's the *expected* path, since
   completing a Take-Action task normally means uploading a document,
   which runs the full document-sync pipeline immediately. One guard
   before `_machine_flag`-style logic touches a finding: if it has an
   open Task, log that the rule re-fired (the execution log already has a
   place for this) and stop there. Only a human verdict closes a Finding
   once a loop has started.
5. **Deferred — accepted as a known shortcoming, not built now.**
   Journey/booking cancellation does **not** cascade to open Findings/
   Tasks. If a booking is voided mid-loop, its open items stay open until
   a human notices. Real fix means reaching into a different subsystem
   (whatever owns booking cancellation) for a genuinely uncommon
   intersection — not worth the surface area against how rarely a booking
   is cancelled specifically while a Finding is mid-review. Documented
   here as the deliberate gap it is, revisit if it turns out to matter in
   practice.

## Open questions: decided

- **Segregation of duties — simplified to one universal rule.** Every
  TL-raised manual observation is reviewed by **PM**, always — not
  severity-gated. Simpler than a conditional threshold, and correctly
  folds Daily Ops in for free: every Daily Ops finding is already
  manually raised (no rule engine involved there), so "manual
  observation → PM reviews" already covers Daily Ops without a separate
  rule.
- **Notifications — badge + banner only, decided for launch.** No push/
  email. Reuses the existing badge-count pattern (Review Queue already
  has one); a "you have N new tasks" banner on dashboard load is the
  extent of it for now.
- **Task-level SLA — decided, and effectively free.** Reuse
  `uc03_finding_routing.py`'s existing `sla_due_at`/`_DEFAULT_SLA_HOURS`
  table directly for a Task's own `due_at_utc`, keyed off the linked
  Finding's class and the Task's own severity (TL-set at Take-Action
  time). No new SLA table, no new policy surface — the exact same
  function call, pointed at a Task instead of only a Finding.
- **Multiple open PC Tasks on one journey — decided, UI-level only.**
  Each Finding keeps its own Task record and its own independent review
  loop underneath (clean data model, nothing merged). Task Queue
  presents multiple open tasks on the same journey as **one visit** to
  `JourneyDocumentsPage` — which already shows every document for a
  journey at once, so this is close to "already works" rather than new
  UI.
- **PC reassignment — decided.** An explicit reassign action, available
  to TL/PM, not automatic turnover detection. A one-click fix for a human
  problem, not a system to build.
- **Daily Ops findings — decided.** Same four-verdict lifecycle,
  uniformly. The universal "manual observation → PM reviews" rule above
  already gives Daily Ops its own answer without a separate policy.
- **Reject/send-back reason — decided.** A required category dropdown
  (Not applicable / Data already correct / System misclassified /
  Duplicate / Other) alongside the existing 50-word free-text remark.
  Cheap now; retrofitting categorization onto historical rejections later
  is real, avoidable work.

## Explicitly not decided yet (next conversation, not assumed)

- Exact wording/labels for the four verdict buttons in the UI.
- Whether "Escalate to PM" is available to TL only from the initial
  review, or also mid-bounce (this design assumes yes, always available,
  but hasn't been asked outright).
- Permission-catalog additions (new `audit.finding.accept`,
  `audit.finding.reject`, `audit.finding.take_action`,
  `audit.finding.escalate` keys, or reuse of `audit.review.decide` for
  all four) — a real Security-service provisioning decision, not made
  here.
