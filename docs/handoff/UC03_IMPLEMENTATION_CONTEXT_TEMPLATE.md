# UC03 Implementation Context — Template

Copy this file to `docs/handoff/UC03_IMPLEMENTATION_CONTEXT.md` on each approved implementation branch and fill only the activity-specific values.

## Mandatory read-first prompt

> Resume UC03 stabilization from repository handoff.  
> Read `docs/handoff/UC03_STABILIZATION_MASTER.md` first, then `docs/handoff/UC03_STABILIZATION_CHECKPOINT.md`, then this implementation context.  
> Do not reconstruct decisions from chat history.  
> Work only within the approved activity scope below.  
> Verify before claiming. Use `UNKNOWN` when evidence is missing.  
> Do not broaden into unrelated repositories/files, do not recursively rescan completed work, and do not make drive-by changes.  
> If a path is unproductive, pivot to the next direct evidence source.  
> Do not merge or deploy unless separately approved.

## Activity

- Stabilization step: `<STEP>`
- Repository: `<REPOSITORY>`
- Implementation branch: `<BRANCH>`
- Branch starting SHA: `<SHA>`
- Approved write scope: `<EXACT APPROVED SCOPE>`
- Approval reference/date: `<REFERENCE>`

## Final invariant to implement

`<ONE SHORT PARAGRAPH DESCRIBING THE FINAL BUSINESS/TECHNICAL INVARIANT>`

## Verified current state

List only facts already proven by the Step plan/checkpoint and direct repository evidence.

- VERIFIED FACT: `<FACT + FILE/MIGRATION/TEST>`
- VERIFIED FACT: `<FACT + FILE/MIGRATION/TEST>`

Do not copy long investigation history here.

## Exact gap / root cause

- GAP: `<VERIFIED GAP>`
- ROOT CAUSE: `<VERIFIED ROOT CAUSE OR UNKNOWN>`

## Files / structures allowed to change

- `<PATH / TABLE / CONTRACT>`
- `<PATH / TABLE / CONTRACT>`

## Files / areas explicitly not to touch

- Security unless separately approved
- unrelated UC01/UC02 modules
- unrelated global infrastructure / CI/CD
- unrelated global Web styling/navigation
- dependencies unless separately approved
- `<ACTIVITY-SPECIFIC EXCLUSIONS>`

## Data-model rule

Reuse existing Journey/business structures first. Add a field/relationship before adding a new entity when that correctly represents the requirement. Add a genuinely new structure only when the approved Step design proves the existing model cannot represent the requirement.

For repeated documents/business entities, preserve actual cardinality. Do not impose `journey + document_type` uniqueness when multiple same-type documents are valid.

## Document identity rule

Use the approved canonical document identity/alias mechanism. Preserve original document identity and DI provenance. Do not add one-off alias `if/else` logic when a common mechanism exists.

## Rule-engine boundary

Rule-engine internals are out of scope unless this activity explicitly says otherwise. Implement/use only the approved execution/status integration contract.

## Acceptance tests

The activity is not FIXED until all approved final-state tests pass.

- `<TEST 1>`
- `<TEST 2>`
- `<DB / API ASSERTION>`
- `<CARDINALITY SCENARIO>`
- `<PROVENANCE ASSERTION>`

## Implementation discipline

For each coherent implementation unit report only:

- `CHANGED:`
- `WHY:`
- `VERIFIED:`
- `REMAINING:`

Use `IMPLEMENTED` until final acceptance passes. Do not repeatedly claim FIXED.

## Stop / escalation conditions

STOP and request a decision when:

- implementation requires a business behaviour not present in the approved Step design;
- evidence contradicts the checkpoint/master decision;
- a new repository must be modified outside the approved scope;
- Security change appears necessary;
- migration/API compatibility impact is materially larger than approved;
- the final invariant cannot be preserved safely.

Do not decide these issues silently.

## Recovery checkpoint

If work is interrupted, update the active checkpoint with only:

- current implementation branch/SHA;
- completed coherent units;
- tests actually run/results;
- blocker/UNKNOWN;
- exact next action.

Then resume from that next action instead of rescanning the project.
