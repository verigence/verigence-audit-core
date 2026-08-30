# UC03 V2 Booking Capture — Evidence-First Applicability Rules

## Governing principle

UC03 is an audit and exception-identification system. **Audit never blocks the Booking or Delivery business process.**

Missing documents, unresolved applicability, low-confidence extraction, contradictory evidence, mismatched values or audit flags remain visible and are recorded for follow-up, but they do not stop a PC from progressing or submitting the business event.

A button may be temporarily disabled only to protect an in-flight technical command such as the same file upload or the same submit request from being duplicated. Audit completeness is never a business-process gate.

## Purpose

Booking Step 1 is evidence-first and document-first. The upload screen stays focused on collecting the Booking pack. The PC is not asked GST, Corporate or Trade-In applicability questions while documents are being uploaded or classified.

## Booking document groups

The upload screen presents the Booking pack in business groups rather than as one flat technical requirement list.

### Mandatory documents

- Booking Form / Booking Docket as configured for the Project.
- Customer ID: **one out of PAN or Aadhaar is sufficient** for the preferred Booking pack.
- Any other Project-configured mandatory Booking document remains in this section.

The term **Mandatory** describes the expected audit pack. It does **not** mean that Booking progression is blocked when the evidence is missing. Missing mandatory evidence becomes an audit exception/flag.

PAN and Aadhaar remain separate evidence requirements for lineage and extraction. If either document is successfully classified, the alternative identity document is not treated as missing for the customer-ID choice.

### Additional documents — if applicable

GST, Corporate and Trade-In / Exchange supporting documents are shown separately as additional documents. They should be uploaded when applicable and available.

## Sequence

1. PC uploads the Booking documents currently available.
2. Upload completion returns control to the PC; classification and extraction continue asynchronously.
3. Each successful classification reconciles the evidence to the configured requirement and starts/continues extraction.
4. Conditional applicability is inferred from classified evidence whenever possible.
5. The Step-1 upload screen shows document groups only; GST, Corporate and Trade-In questions are not shown inline.
6. The PC may continue the Booking process even when evidence is missing, classification/extraction is still running, or an audit exception exists.
7. If classification has settled and a supporting optional/conditional document was not uploaded, Continue may ask a short customer-confirmation question for unresolved choices. If the answer is unavailable or processing is still in progress, this must not block Booking progression.
8. If the PC confirms a condition is applicable while no supporting document is available, record the declaration for audit follow-up.
9. Booking Details and Booking submission remain independent of audit completeness.
10. Review uses the extracted values, confidence and evidence lineage from DI. Review exceptions and flags are handled separately from the Booking business event.

## Evidence-first rule

If a classified document establishes a condition, do not ask the PC the corresponding Yes/No question. The document is the evidence source.

Examples:

- GST Certificate classified → GST applicable is established; no GST question.
- Corporate ID classified → Corporate customer is established; no Corporate question.
- Trade-In supporting document classified → Exchange/Trade-In applicable is established; no Trade-In question.

If no supporting classified document establishes a condition, defer any optional confirmation until Continue. Do not occupy the upload screen with speculative questions, and do not turn an unanswered audit question into a process blocker.

## GST / Corporate mutual-exclusion rule

`gstApplicable` and `corporateCustomer` are mutually exclusive as a business rule.

- GST established as applicable → Corporate is normally treated as NOT_APPLICABLE when there is no contradictory Corporate evidence.
- Corporate established as applicable → GST is normally treated as NOT_APPLICABLE when there is no contradictory GST evidence.
- If documents/declarations establish both, retain both pieces of evidence and raise/identify an audit exception. **Do not block Booking and do not discard either source.**

The rule is V2-only and does not change V1 APIs, adapters or processing behavior.

## Missing evidence and exceptions

Examples of conditions that must remain non-blocking:

- Booking Form missing.
- Neither PAN nor Aadhaar available.
- Required/expected payment evidence missing.
- GST/Corporate/Trade-In document missing.
- GST and Corporate evidence both present.
- Unknown/unclassified document.
- Classification or extraction still processing.
- Low-confidence extracted value.
- Multiple sources disagree on an attribute.

The expected behavior is:

`Business process continues → evidence/status is recorded → exception/flag is raised → audit review follows separately.`

## Document help

The upload area exposes a compact Help icon. Help summarizes:

- Mandatory audit pack: Booking Form / Booking Docket, one customer ID (PAN or Aadhaar), and other Project-configured mandatory documents.
- Additional / if applicable: GST, Corporate, Trade-In and other conditional/optional Booking documents.

The help content is derived from the current requirement set and business grouping rules.

## PC-facing business messages

Use business status language and avoid implementation terms such as queue, worker, gate or polling.

Preferred lifecycle messages include:

- `Documents uploading`
- `Documents received`
- `Documents being classified`
- `Documents uploaded`
- `Review values being prepared`
- `Review values ready`
- `Some documents are missing — this will be flagged for audit`
- `You can continue and complete the Booking`

## Timing display

The screen may show a live elapsed timer to give the PC visibility into document processing. It is informational only. The timer, classification status, extraction status and audit completeness must not create an artificial wait before the PC can continue the Booking process.
