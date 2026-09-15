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
3. **Auto-raises a new Task for TL**: "review PC's response and close."
   This is the loop-closing step that doesn't exist anywhere today —
   without it, a PC finishing their part has no way to surface back to
   the TL who asked for it.

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
