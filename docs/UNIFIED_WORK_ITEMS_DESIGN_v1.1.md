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
exact same four-verdict lifecycle once raised — raising one is just a new
entry point into the same Finding record, everything after that is
identical.

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

## Race conditions and gaps this design must resolve before implementation

Found by deliberately stress-testing the model above, not raised during
the original discussion — these need an answer, not just a footnote.

1. **Duplicate task creation on retry.** The existing
   `PC_DOCUMENT_REUPLOAD` mechanism already solves this correctly
   (`effect_key` + a DB unique constraint, checked before creating). Both
   auto-spawn (self-serve) and Take-Action task creation must use the
   same discipline — an `effect_key` scoped to (finding_id, current open
   round), or a retried request / redelivered webhook creates two tasks
   for one finding.
2. **A completion write touches both the Task and the Finding — needs one
   transaction, not two.** If TL is mid-verdict on a Task's current state
   while PC is simultaneously completing it, TL's write can land against
   data that's already stale. The existing If-Match/`version_no`
   optimistic-concurrency pattern must cover both records together (an
   aggregate lock spanning both, matching the existing per-journey
   advisory-lock pattern already used for document sync), not just the
   Finding alone.
3. **Escalating to PM while a PC Task is still open is undefined.**
   Sequence: TL sends to PC (Task open) → before PC responds, TL (or
   someone else with access) escalates to PM. Nothing supersedes the now-
   orphaned PC Task. Decide one of: escalation is blocked while a PC Task
   is outstanding, or escalating auto-cancels the outstanding PC Task.
   Leaving both open at once means two people each think they're the
   next step.
4. **Automated rule re-evaluation must never auto-close a Finding a human
   is actively tracking.** PC completing a Take-Action task usually means
   uploading a document, which runs through the *entire* existing
   document-sync pipeline — the same rule that raised the original
   VIOLATION can re-fire right then. Today's `_machine_flag` pattern
   (raise-if-absent, else just touch stage status) was built for a world
   with no human verdict loop layered on top of it. Explicit rule needed:
   automated re-evaluation only ever detects *new* issues on a Finding
   that's mid-human-loop; it never resolves or silently overwrites one a
   TL/PM is actively deciding on. Only a human verdict (Accept/Reject)
   closes a Finding once a loop has started.
5. **Journey/booking cancellation doesn't cascade to open Tasks.** If a
   booking is voided while a PC Task is open against it, nothing closes
   that Task — it sits live in PC's queue for a case that no longer
   exists. Needs a cancellation path triggered off booking/journey
   cancellation, not left to go stale.

## Open questions, not guessed at

- **Segregation of duties for manual observations.** A TL raising their
  own observation and then also Accepting/Rejecting it themselves is a
  self-review — standard GRC practice would want a second reviewer (PM)
  for anything above a low severity. Worth a deliberate policy call
  rather than defaulting to "TL can close their own raise."
- **No notification path exists.** Every step here relies on someone
  opening a queue to discover new work — no push/email/in-app alert on
  assignment, unlike Jira/ServiceNow/PagerDuty. May be an accepted
  limitation for launch; worth saying so explicitly rather than leaving
  it implicit.
- **No SLA/timeout on the Task itself.** The Finding has SLA/escalation
  machinery already; a Task a PC simply never opens has no equivalent —
  it can sit indefinitely with no reminder or auto-escalation.
- **Multiple open PC Tasks on one journey.** If three VIOLATIONs on the
  same booking are all Take-Actioned close together, does PC get three
  separate tasks (three separate visits, three separate TL/PM review
  tasks generated back), or do they consolidate into one visit covering
  all three? Not decided either way.
- **PC reassignment/turnover.** Tasks are assigned to a specific actor;
  no stated path for reassigning one if that person is unavailable or
  leaves.
- **Daily Ops findings under this same lifecycle is unconfirmed.** Every
  Daily Ops finding today is already TL/PM-raised manually (no rule
  engine involved there at all) — does a TL verdict their own manual
  raise under the same four-verdict flow, or does Daily Ops need a
  different shape? Not addressed either way.
- **Reject/send-back reason is free text only.** Good for forcing a real
  answer (the 50-word floor), but harder to aggregate later (Compliance
  Report can't answer "why do most rejections happen" without reading
  every one). A lightweight reason *category* alongside the free text is
  the more standard shape for this kind of GRC reporting — not a
  blocker, worth a note for later.

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
