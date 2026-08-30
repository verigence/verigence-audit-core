# UC03 V2 Booking Capture — Evidence-First Applicability Rules

## Purpose

Booking Step 1 is evidence-first and document-first. The upload screen should stay focused on collecting the Booking pack. The PC must not be asked GST, Corporate or Trade-In applicability questions while documents are still being uploaded or classified.

## Sequence

1. PC uploads Booking documents directly to secure storage.
2. Each upload is classified asynchronously.
3. A successful classification immediately satisfies the matching document requirement and starts extraction in the V2 processing pool.
4. Conditional applicability is inferred from classified evidence whenever possible.
5. The Step-1 upload screen shows the required and optional document list only. It does not render GST, Corporate or Trade-In questions inline.
6. Once the required Booking documents are classified, Continue becomes available even when conditional choices remain unresolved.
7. If a supporting optional/conditional document was not uploaded, pressing Continue opens a short customer-confirmation dialog for only those unresolved choices.
8. If the PC confirms a condition is applicable while no supporting document was uploaded, the declaration is recorded with `documentAvailable=false` for audit follow-up.
9. After all unresolved choices are settled, the PC continues to Booking Details. Extraction continues in the background and does not block navigation.
10. Review uses the extracted values, confidence and evidence lineage already persisted by DI.

## Evidence-first rule

If a classified document establishes a condition, do not ask the PC the corresponding Yes/No question. The document is the evidence source.

Examples:

- GST Certificate classified → GST applicable is established; no GST question.
- Corporate ID classified → Corporate customer is established; no Corporate question.
- Trade-In supporting document classified → Exchange/Trade-In applicable is established; no Trade-In question.

If no supporting classified document establishes a condition, defer the question until the PC presses Continue. Do not occupy the upload screen with speculative questions.

## Document help

The upload area exposes a compact Help icon. Help lists:

- Required Booking documents.
- Optional / if-applicable Booking documents.

The help content is derived from the current requirement set; it is not hardcoded into the UI.

## GST / Corporate mutual-exclusion rule

`gstApplicable` and `corporateCustomer` are mutually exclusive for Booking capture.

- GST established as applicable → Corporate becomes NOT_APPLICABLE without asking the PC.
- Corporate established as applicable → GST becomes NOT_APPLICABLE without asking the PC.
- If contradictory evidence or declarations establish both as applicable, Step 1 is blocked. The system must not silently discard evidence; the incorrect document/declaration must be corrected before capture can proceed.

The rule is V2-only and does not change V1 APIs, adapters or processing behavior.

## PC blocker guidance

A disabled Continue action must always use business language and identify the concrete Booking action. Examples:

- `Booking Form is required for this Booking.`
- `Screenshot 123.png is being checked.`
- `GST and Corporate evidence cannot both apply to the same Booking. Please remove the incorrect document.`

Avoid implementation-oriented language such as queue, classifier, extraction worker, applicability gate or polling in PC-facing copy.

## Timing display

The Step-1 footer shows a live elapsed timer while the required Booking documents are being prepared. It is an elapsed readiness timer, not an artificial countdown. Continue becomes available immediately when the required document gate is satisfied.

The timer and status copy use business language, for example:

- `Preparing Booking documents · 00:08`
- `Required Booking documents are ready · 00:12`
- `Review values ready`

Extraction may continue in the background after the PC moves to Booking Details.
