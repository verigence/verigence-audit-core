# UC03 Field Evidence Localization Contract

Date: 2026-08-30

## Governing rule

Whenever Verigence presents a Document Intelligence extracted field for human review, the source-evidence action must identify the exact place from which the value was extracted.

For a localized extracted field, Review must:

1. retain the DI source document reference;
2. use the DI page number when the source is a multi-page document;
3. use the DI `BOX_2D` / `NORMALIZED_1000` evidence region;
4. open/render the correct source page; and
5. visibly box/highlight the extracted field location.

Verigence must never infer, approximate or fabricate a bounding box.

## Scope

This is a project-wide UC03 Review contract and applies to all active and future extracted-field review surfaces, including:

- Booking V2 Review;
- Delivery V2 Review;
- Booking/Delivery Audit source comparison;
- Team Lead / supervisory document review; and
- future PC, TL, PM or Executive screens that display DI-extracted values with source evidence.

A feature-specific review screen must not implement a weaker evidence behavior than this common contract.

## Missing localization

DI field values, page numbers and evidence regions remain source-owned by Document Intelligence.

If DI returns an extracted value without a valid source location:

- continue to display the extracted value and its source document identity;
- show `Source location unavailable` / an evidence-localization exception;
- do not label the action as `boxed evidence`;
- do not open an unboxed document as though it were extracted-field evidence; and
- do not fabricate a page or box.

The absence of localization is an audit/evidence exception only. It must never block Booking, Delivery or another business event.

## Document-only viewing

This contract distinguishes field review from ordinary document browsing.

When a user opens an uploaded document without reviewing a specific extracted field, Verigence may display the document normally without boxes. No artificial highlight is required in document-only context.

## Data ownership

- DI owns the original document, extracted fact, confidence, page number and evidence region.
- Audit Core may transport/reference localization required for review but must not create synthetic localization.
- Web renders the DI localization and must not silently fall back to an unboxed document while presenting an extracted-field evidence action.

## Acceptance matrix

| Context | DI value | Reliable localization | Expected behavior |
| --- | --- | --- | --- |
| Extracted-field Review | Yes | Yes | Open correct page/image and visibly box the exact DI region |
| Extracted-field Review | Yes | No | Show value + `Source location unavailable`; do not substitute an unboxed source document |
| Extracted-field Review | No | N/A | No extracted-field evidence action |
| Document-only browse | N/A | N/A | Normal document viewer is allowed; box is not required |

## Non-blocking audit principle

Evidence-localization exceptions participate in the same UC03 governing policy as other audit exceptions:

**Business process continues → evidence/status is recorded → exception/flag is raised or surfaced → review/follow-up occurs separately.**

Missing or invalid localization must not become a Booking or Delivery completion gate.
