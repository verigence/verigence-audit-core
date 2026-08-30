# UC03 V2 Booking Capture — Evidence-First Applicability Rules

## Purpose

Booking Step 1 is evidence-first and document-first. The upload screen stays focused on collecting the Booking pack. The PC is not asked GST, Corporate or Trade-In applicability questions while documents are being uploaded or classified.

## Booking document groups

The upload screen presents the Booking pack in business groups rather than as one flat technical requirement list.

### Mandatory documents

- Booking Form / Booking Docket as configured for the Project.
- Customer ID: **one out of PAN or Aadhaar is sufficient**.
- Any other Project-configured mandatory Booking document remains in this section.

PAN and Aadhaar remain separate evidence requirements for lineage and extraction, but they form one continuation rule. If either document is successfully classified, the other identity document does not block the Booking.

### Additional documents — if applicable

GST, Corporate and Trade-In / Exchange supporting documents are shown separately as additional documents. They should be uploaded when applicable and available.

## Sequence

1. PC uploads Booking documents directly to secure storage.
2. Each upload is classified asynchronously.
3. A successful classification immediately satisfies the matching document requirement and starts extraction in the V2 processing pool.
4. Conditional applicability is inferred from classified evidence whenever possible.
5. The Step-1 upload screen shows the mandatory and additional document groups only. It does not render GST, Corporate or Trade-In questions inline.
6. Once the mandatory Booking documents are classified, Continue becomes available even when conditional choices remain unresolved.
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

The upload area exposes a compact Help icon. Help summarizes:

- Mandatory: Booking Form / Booking Docket, one customer ID (PAN or Aadhaar), and any other Project-configured mandatory document.
- Additional / if applicable: GST, Corporate, Trade-In and other conditional/optional Booking documents.

The help content is derived from the current requirement set and business grouping rules.

## GST / Corporate mutual-exclusion rule

`gstApplicable` and `corporateCustomer` are mutually exclusive for Booking capture.

- GST established as applicable → Corporate becomes NOT_APPLICABLE without asking the PC.
- Corporate established as applicable → GST becomes NOT_APPLICABLE without asking the PC.
- If contradictory evidence or declarations establish both as applicable, Step 1 is blocked. The system must not silently discard evidence; the incorrect document/declaration must be corrected before capture can proceed.

The rule is V2-only and does not change V1 APIs, adapters or processing behavior.

## PC-facing business messages

Use business status language and avoid implementation terms such as queue, classifier worker, extraction worker, applicability gate or polling.

Preferred lifecycle messages include:

- `Documents uploading`
- `Documents received. Classification has started.`
- `Documents being classified`
- `Documents uploaded and classified`
- `Review values ready`
- `Mandatory documents ready`
- `Upload any one customer ID — PAN or Aadhaar.`

## Timing display

The Step-1 footer and status area show a live elapsed timer. It is an elapsed readiness timer, not an artificial countdown. Continue becomes available immediately when the mandatory document rules are satisfied.

Extraction may continue in the background after the PC moves to Booking Details.
