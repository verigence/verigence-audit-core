# UC03 V2 Booking Capture — Evidence-First Applicability Rules

## Purpose

Booking Step 1 is evidence-first. The PC should only answer an applicability question when the uploaded and classified documents cannot establish the answer.

## Sequence

1. PC uploads Booking documents directly to secure storage.
2. Each upload is classified asynchronously.
3. A successful classification immediately satisfies the matching document requirement and starts extraction in the V2 processing pool.
4. Conditional applicability is inferred from classified evidence whenever possible.
5. Only unresolved conditional requirements are presented to the PC.
6. Step 1 can continue when every blocking required/conditional requirement is resolved. Extraction continues in the background and does not block navigation to Booking Details.
7. Review uses the extracted values, confidence and evidence lineage already persisted by DI.

## Evidence-first UI rule

If a classified document establishes a condition, do not ask the PC the corresponding Yes/No question. The document is the evidence source.

Examples:

- GST Certificate classified → GST applicable is established; no GST question.
- Corporate ID classified → Corporate customer is established; no Corporate question.
- Trade-In supporting document classified → Exchange/Trade-In applicable is established; no Trade-In question.

If no supporting classified document establishes the condition, show the applicability question.

## GST / Corporate mutual-exclusion rule

`gstApplicable` and `corporateCustomer` are mutually exclusive for Booking capture.

- GST established as applicable → Corporate becomes NOT_APPLICABLE without asking the PC.
- Corporate established as applicable → GST becomes NOT_APPLICABLE without asking the PC.
- If contradictory evidence or declarations establish both as applicable, Step 1 is blocked. The system must not silently discard evidence; the incorrect document/declaration must be corrected before capture can proceed.

The rule is V2-only and does not change V1 APIs, adapters or processing behavior.

## PC blocker guidance

A disabled Continue action must always show the exact reason, for example:

- `GST Certificate applicability is still required.`
- `Booking Payment Receipt is still being classified.`
- `GST and Corporate cannot both apply. Remove or correct the contradictory evidence.`

The Web UI should focus/scroll the PC to the unresolved item when possible.

## Timing display

The Step 1 footer shows a live elapsed timer while the Continue action is blocked. It is an elapsed readiness timer, not an artificial countdown: the button enables immediately when classification and applicability gates are actually satisfied.
