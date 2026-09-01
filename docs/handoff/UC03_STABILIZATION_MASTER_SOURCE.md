# UC03 Booking + Delivery Stabilization — Original Master Prompt

> Immutable source copy captured on 2026-09-01 from the user-provided UC03 stabilization master prompt.  
> Do not shorten or reinterpret this file.  
> `UC03_STABILIZATION_MASTER.md` is the active distilled charter and may be shortened as activities close, but it must not contradict this source or later explicit business decisions.

---

# VERIGENCE UC03 — BOOKING + DELIVERY STABILIZATION MASTER PROMPT

You are working on a real Verigence product codebase.

This is a STABILIZATION exercise, not an architecture experiment, not a redesign of unrelated modules, and not an opportunity to clean up everything you see.

Your objective is to bring UC03 Booking + Delivery to a stable, evidence-backed final state in two areas:

1. DI → Audit Core persistence and review behaviour
2. UC03 V2 UI/UX stabilization

Accuracy, containment, final-state verification and product usability are more important than speculative improvements.

## 1. ABSOLUTE RULE: DO NOT HALLUCINATE

Never state that something:

- exists
- is mapped
- is stored
- is supported
- is deployed
- is tested
- is fixed
- is the root cause

unless repository/code/schema/test/deployment evidence proves it.

Every technical conclusion must be classified as one of:

VERIFIED FACT

INFERENCE

PROPOSED CHANGE

UNKNOWN

Definitions:

VERIFIED FACT = directly supported by inspected code, schema, migration, contract, test, runtime evidence or deployment evidence.

INFERENCE = a conclusion derived from verified facts but not directly proven.

PROPOSED CHANGE = something you recommend changing.

UNKNOWN = evidence has not yet established the answer.

Never silently convert an INFERENCE into a VERIFIED FACT.

If information cannot be found, say UNKNOWN.

Do not invent:

- DI fields
- document types
- API contracts
- database columns
- business rules
- multiplicity assumptions
- mappings
- expected UX behaviour

## 2. WORK FROM FINAL STATE, NOT FROM THE CURRENT SYMPTOM

Before modifying anything, establish the FINAL STATE.

Do not follow this pattern:

bug → patch → deploy → discover contradiction → patch again

Use this pattern:

FINAL BUSINESS STATE → CURRENT IMPLEMENTATION → GAP → ROOT CAUSE → SMALLEST SAFE DESIGN → ACCEPTANCE TEST → IMPLEMENTATION → END-TO-END VERIFICATION

Do not fix individual screenshots, HTTP errors, missing fields or buttons until you understand how they fit into the final UC03 journey.

## 3. SCOPE

Primary stabilization scope:

BACKEND

- verigence-audit-core

DI

- verigence-di, but modify it ONLY if evidence proves the required capability is missing or incorrect there

WEB

- verigence-web
- UC03 Booking + Delivery only

Security:

- treat current Security implementation as source of truth
- do NOT redesign Security
- do NOT modify Security merely to work around a UC03 integration problem
- modify Security only if a concrete missing Security capability is proven and separately approved

Out of scope unless explicitly approved:

- UC01
- UC02
- unrelated dashboards
- unrelated administration
- unrelated journeys
- authentication redesign
- authorization redesign
- global infrastructure redesign
- CI/CD redesign
- repository cleanup
- dependency upgrades
- global navigation redesign
- global theme redesign
- unrelated refactoring

Your responsibility is NOT to improve everything you notice.

Your responsibility is to stabilize the approved UC03 scope.

## 4. CHANGE CONTROL

Start by reading and investigating.

Do not assume that a request to "investigate", "review" or "plan" means permission to write code.

Use these permissions:

APPROVE WRITE: <scope> = permission to modify code only within that exact scope.

APPROVE MERGE: <repository / PR> = permission to merge.

APPROVE DEPLOY: <repository / environment> = permission to deploy.

Write approval does NOT automatically authorize merge.

Merge approval does NOT automatically authorize deployment.

Never merge or deploy simply because tests passed.

If an existing approved working/stabilization branch already exists, use it.

Do not create additional branches simply for convenience unless needed.

## 5. DI → AUDIT CORE FINAL BUSINESS RULE

This is the required behaviour for BOTH BOOKING journey and DELIVERY journey.

For every document successfully extracted by DI:

EVERY extracted DI field/value must have durable representation in Audit Core.

No populated DI field may silently disappear.

The effective value rule is:

effective_value = PC modified value, when the PC changed the field during review, otherwise DI extracted value.

Therefore:

UNCHANGED FIELD

DI extracted value → stored in Audit Core as effective value

CHANGED FIELD

original DI value remains traceable + PC-confirmed changed value → stored in Audit Core as effective value

Auditability/provenance must be retained.

Where the source provides the information, retain enough information to identify:

- tenant
- journey
- stage: BOOKING / DELIVERY
- source DI document
- source document type
- DI field/canonical field
- DI source fact/reference
- DI source fact version
- original extracted value
- confidence
- effective value
- whether modified by PC
- modified/reviewed actor
- review timestamp
- evidence/document relationship

Do not destroy the original DI provenance when a value is changed.

## 6. LOSSLESS STORAGE AND BUSINESS PROJECTION ARE DIFFERENT

Do not confuse these two responsibilities:

A. LOSSLESS AUDIT CORE PERSISTENCE

Every extracted DI field must have durable representation in Audit Core.

This protects against field loss and future DI schema evolution.

B. TYPED/CANONICAL BUSINESS PROJECTION

Known business fields may additionally be written into proper operational/canonical Audit Core structures.

Examples may include:

- customer information
- commercial amounts
- payments
- vehicle information
- invoice information
- registration information

But only use actual repository/schema evidence.

Important rule:

A failure to map a field into a typed operational table MUST NOT cause the DI extracted field/value itself to disappear.

Typed projection is additional business materialization.

It is NOT a replacement for lossless persistence.

## 7. DOCUMENT MULTIPLICITY / CARDINALITY

Never assume:

one journey = one document type = one document.

The implementation must support repeated documents.

Explicitly investigate and support cases such as:

- multiple dealer/payment receipts
- multiple payment records
- multiple invoices
- multiple invoices of the same type
- retail invoice
- tax invoice
- registration invoice/document
- accessory invoice
- other invoice/document types configured in DI
- multiple supporting documents of another configured DI type

Do not assume the above names are necessarily the exact DI contract names.

Verify actual configured document types from the repositories.

If a document contract is not found:

UNKNOWN — DO NOT INVENT IT.

Database uniqueness must reflect actual cardinality.

Do not use tenant + journey + document_type as a unique identity if multiple documents of the same type are valid.

Document identity must remain distinct.

## 8. FIRST BACKEND DELIVERABLE — EVIDENCE MATRIX

Before changing persistence code, create an evidence-backed matrix.

For every DI document type relevant to Booking and Delivery record:

STAGE

DOCUMENT TYPE

DI CONTRACT / SOURCE FILE

FIELDS EXTRACTED

CARDINALITY

CURRENT AUDIT CORE STORAGE

CURRENT TYPED OWNER

CURRENT REVIEW PATH

CURRENT EFFECTIVE-VALUE BEHAVIOUR

PROVENANCE STORED?

MULTIPLE DOCUMENTS SUPPORTED?

GAP

STATUS

Do not fill missing information from general knowledge.

Use UNKNOWN.

Then summarize only the real gaps.

## 9. BACKEND IMPLEMENTATION PRINCIPLES

The preferred final model should satisfy:

DI FACT → LOSSLESS AUDIT CORE RECORD

and, where known:

DI/PC EFFECTIVE BUSINESS VALUE → TYPED AUDIT CORE BUSINESS OWNER

Do not force every future arbitrary DI field into a new typed DB column.

Do not require an Audit Core migration every time DI adds a field merely to preserve that field.

At the same time:

do not hide established business data permanently inside opaque JSON if the existing Audit Core domain model already has an appropriate canonical owner.

Use repository evidence to determine which fields should receive typed projection.

## 10. REVIEW CONFIRM BEHAVIOUR

Review Confirm must produce a deterministic persistence outcome.

For every field:

If PC DID NOT modify it:

- effective value = DI extracted value

If PC DID modify it:

- original DI extracted value remains available for provenance
- effective value = PC-confirmed value

When Review Confirm succeeds:

there must be no accepted field that silently vanishes.

The transaction should fail clearly if a required invariant cannot be preserved.

Do not report a successful confirmation when persistence partially failed silently.

## 11. BACKEND ACCEPTANCE TESTS

Do not call this issue FIXED because:

- unit tests passed
- CI passed
- API returned HTTP 200
- a migration ran
- one known document type worked

The final backend verification must test:

DI extracts N populated fields → Review loads all N → PC optionally modifies M fields → PC confirms → Audit Core database is queried → all N fields have durable Audit Core representation → N-M unchanged fields contain DI effective values → M changed fields contain PC effective values → original DI provenance remains traceable → no silent field loss.

Repeat for BOOKING and DELIVERY.

And include representative cardinality scenarios:

1. one document
2. multiple documents
3. multiple documents of same type
4. multiple payment/dealer receipts
5. multiple invoices
6. multiple invoice/document types
7. overlapping business fields from different documents
8. PC modification
9. no PC modification
10. unknown/new DI field

The unknown/new DI field must still be losslessly represented even if no typed business projection exists.

## 12. UC03 V2 UI — FINAL OBJECTIVE

The current UI should be stabilized as a real product.

Do NOT interpret stabilization as: "make everything fit without scrolling."

The real objective is:

USE THE AVAILABLE SCREEN SPACE EFFECTIVELY + KEEP THE WORKFLOW CLEAR + MINIMIZE UNNECESSARY SCROLLING + ALLOW INTENTIONAL SCROLLING WHEN CONTENT REQUIRES IT.

Scrolling is permitted and expected for variable-volume content such as:

- multiple line items
- multiple invoices
- multiple receipts
- many extracted fields
- multiple documents
- long document/evidence views
- large comparisons

Never:

- hide required data to eliminate scrolling
- truncate legitimate line items
- freeze content that exceeds the viewport
- shrink text excessively
- make controls unusably small
- force 40 rows into one viewport
- interpret "optimize screen space" as "zero scrolling"

## 13. VERIGENCE BRANDING IS STRICT

Every UC03 screen must preserve the approved Verigence identity.

Strictly maintain:

- approved Verigence logo / lockup
- approved Verigence colour scheme
- approved branding treatment
- approved typography/tokens where defined
- consistent Booking/Delivery appearance

Do not:

- recolour the Verigence logo
- substitute another logo
- invent a new brand palette
- introduce a visually unrelated theme
- redesign global Verigence branding

Before UI implementation:

inspect existing approved Verigence brand assets/tokens and use them as source of truth.

If multiple historical brand implementations conflict:

report the conflict and use the clearly approved/current source.

Do not invent a new branding rule.

## 14. NEW UC03 UI / CSS IS ALLOWED

You ARE allowed to create:

- new UC03-specific React components
- new UC03-specific layout components
- new UC03-specific CSS
- new clean V2 UC03 presentation structures
- consolidated UC03 styling

if this is safer and cleaner than continuing to patch the current implementation.

The restriction is NOT: "never create new CSS."

The restriction is: "do not create another speculative override layer."

A new stylesheet/component is acceptable when it becomes the clear owner of the UI.

If current UC03 CSS has become structurally unstable:

you may consolidate, replace or bypass conflicting UC03-specific styling.

Do not leave several competing implementations active if they can safely be consolidated.

New UI must still preserve:

- Verigence branding
- Verigence colour palette
- existing business workflow
- API semantics
- backend rules

## 15. UI SCOPE TO INSPECT

Start with the actual current V2 screens:

- BookingCaptureV2Page
- BookingDetailsV2Page
- BookingReviewV2Page
- DeliveryCaptureV2Page
- DeliveryDetailsV2Page
- DeliveryReviewV2Page

Also inspect their directly used:

- UC03 components
- service clients
- UC03 styles
- document/evidence viewers
- review components
- navigation components

Inspect AppShell/global styling where needed for diagnosis.

Do NOT modify global shell/global CSS merely because a local workaround is easier.

Only modify a global owner if:

1. evidence proves the root cause is global
2. local correction is inappropriate
3. the exact global change is approved

## 16. UI LAYOUT PRINCIPLES

DESKTOP

Use the available desktop width intelligently.

Avoid:

- narrow phone-like cards in a large desktop viewport
- huge empty horizontal regions
- avoidable vertical growth caused by arbitrary max-widths

Where appropriate, use:

- split panes
- responsive columns
- well-sized document/evidence pane
- review/details pane
- controlled variable-content areas

VARIABLE CONTENT

A strong target pattern is:

HEADER / JOURNEY CONTEXT

-------------------------------------

MAIN AVAILABLE VIEWPORT AREA

DOCUMENT / EVIDENCE | REVIEW / FIELDS / LINE ITEMS

-------------------------------------

PRIMARY ACTION / WORKFLOW

This is a principle, not a mandatory exact layout.

Choose the structure according to the actual workflow.

SCROLL OWNERSHIP

Every scrollbar must have a clear owner.

Avoid accidental combinations of browser scroll + shell scroll + screen scroll + panel scroll + table scroll unless each one is genuinely necessary.

Prefer one primary variable-content scroll owner.

PRIMARY ACTIONS

Next / Continue / Confirm / Submit must always be obvious and reachable.

They must never disappear because of overflow, clipping, z-index, content height, nested scrolling, mobile viewport or responsive breakpoints.

Sticky actions are allowed where useful. They are not mandatory.

## 17. RESPONSIVE BEHAVIOUR

Do not merely shrink desktop UI.

Desktop, tablet and mobile/native may arrange the same information differently.

Desktop:

- exploit width
- minimize unnecessary vertical stacking

Tablet:

- adapt columns intelligently

Mobile/native:

- stacking is acceptable
- vertical scrolling is expected
- controls must remain comfortable
- no accidental horizontal overflow
- important actions must remain reachable

Tables/large structured content may use deliberate horizontal scrolling only when that is the most usable representation.

Do not remove business information just to produce a clean screenshot.

## 18. UI ACCEPTANCE VIEWPORTS

Verify relevant V2 screens at minimum at:

- 1920 x 1080
- 1440 x 900
- 1366 x 768
- representative tablet
- representative supported mobile/native viewport

For every relevant screen test:

BRANDING

- correct Verigence logo
- correct Verigence colour scheme
- no visual identity drift

LAYOUT

- available screen space used effectively
- no unnecessary large empty areas
- logical alignment
- no clipping
- no overlap
- no accidental horizontal overflow

SCROLL

- intentional
- predictable
- suitable for content volume
- multiple line items accessible
- multiple documents accessible
- nested scrolling minimized

WORKFLOW

- Next visible/reachable
- Confirm visible/reachable
- navigation correct
- evidence usable
- editing usable
- review understandable

Do not call UI FIXED because one screenshot or one viewport looks good.

## 19. DO NOT MAKE DRIVE-BY CHANGES

Do not:

- rename unrelated files
- reformat unrelated modules
- upgrade dependencies
- rewrite APIs for convenience
- alter DI schemas merely to simplify Audit Core
- alter Security to bypass integration problems
- change unrelated global CSS
- remove legacy behaviour without confirming its current use
- clean repository history
- redesign unrelated modules

If you discover adjacent technical debt:

REPORT IT SEPARATELY.

Do not fix it unless approved.

## 20. REQUIRED ANALYSIS STYLE — SPECIFIC AND CRISP

Do not give vague conclusions.

BAD:

"CSS may be conflicting."

GOOD:

VERIFIED FACT:
BookingReviewV2Page uses class X.

VERIFIED FACT:
Selector Y in file Z constrains the content width to N px.

ROOT CAUSE:
At 1440px viewport, the review workspace uses only N px while content wraps vertically, producing avoidable scrolling.

PROPOSED CHANGE:
Make selector Y the responsive layout owner and remove conflicting UC03 selector Q.

IMPACT:
Booking Review V2 only.

Do not write long defensive narratives.

Do not repeat findings.

Be technical, specific and concise.

## 21. REQUIRED DEFECT FORMAT

For every identified problem report:

AREA:

SCREEN / SERVICE:

ISSUE:

VERIFIED EVIDENCE:

ROOT CAUSE:

PROPOSED CHANGE:

FILES TO CHANGE:

FILES NOT TO TOUCH:

DATABASE IMPACT:

API IMPACT:

EXPECTED RESULT:

ACCEPTANCE TEST:

STATUS:

Only use:

NOT INVESTIGATED

INVESTIGATED

ROOT CAUSE CONFIRMED

IMPLEMENTED

TESTED

CI PASSED

DEPLOYED

VISUALLY VERIFIED

E2E VERIFIED

FIXED

FIXED requires the agreed final-state acceptance test to pass.

## 22. IMPLEMENTATION PLAN BEFORE WRITING CODE

Before implementation provide:

FINAL STATE

CURRENT STATE

VERIFIED GAPS

ROOT CAUSES

DI → AUDIT CORE MATRIX

DATABASE DESIGN IMPACT

CARDINALITY DESIGN

REVIEW EFFECTIVE-VALUE DESIGN

BOOKING IMPACT

DELIVERY IMPACT

UI CURRENT-STATE FINDINGS

UI TARGET STATE

BRANDING SOURCE OF TRUTH

FILES TO CHANGE

NEW FILES TO CREATE

FILES EXPLICITLY NOT TO TOUCH

API IMPACT

MIGRATION IMPACT

BACKWARD-COMPATIBILITY IMPACT

TEST PLAN

VISUAL TEST PLAN

ROLLBACK PLAN

RISKS

UNKNOWNS

Then STOP and request write approval if none has been given.

## 23. IMPLEMENTATION DISCIPLINE

Once implementation is approved:

Do not create a stream of tiny speculative patches.

Prefer coherent changes that implement an agreed invariant.

After each meaningful implementation unit state briefly:

CHANGED:

WHY:

VERIFIED:

REMAINING:

Do not repeatedly say "fixed" during implementation.

Use IMPLEMENTED until final validation proves otherwise.

## 24. FINAL DEFINITION OF DONE

UC03 backend is FIXED only when:

- Booking DI fields persist losslessly
- Delivery DI fields persist losslessly
- PC changes become effective stored values
- unchanged DI values become effective stored values
- provenance remains available
- multiple receipts work
- multiple invoices work
- multiple same-type documents work
- unknown/new extracted fields are not silently lost
- typed projections remain correct
- end-to-end DB assertions pass

UC03 UI is FIXED only when:

- Verigence branding is preserved
- colours are preserved
- desktop space is used well
- responsive behaviour is usable
- scrolling is intentional rather than accidental
- long content remains accessible
- multiple line items remain usable
- primary actions remain reachable
- no clipping/overlap/accidental overflow exists
- Booking V2 workflow works
- Delivery V2 workflow works
- required viewports have been visually verified

The overall stabilization is FIXED only when BOTH backend and UI definitions of done are satisfied.

## 25. MOST IMPORTANT RULE

When unsure whether you have permission to change something:

DO NOT CHANGE IT.

When unsure about the intended business behaviour:

DO NOT DECIDE IT YOURSELF.

When evidence is missing:

WRITE UNKNOWN.

When tests have not demonstrated the final state:

DO NOT SAY FIXED.

The goal is not to produce many changes.

The goal is to make UC03 Booking + Delivery reach one stable, verified final state.
