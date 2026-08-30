# UC03 V2 — Fast Booking Sequence and Performance Contract

**Date:** 2026-08-30  
**Status:** approved implementation amendment  
**Applies to:** UC03 V2 Booking capture. V1 APIs/adapters are explicitly out of scope.

## Business objective

The Process Consultant is KPI-driven. The journey must prevent avoidable document rework and use the time spent on Booking Details to finish document extraction.

Engineering objectives:

- four normal Booking documents uploaded and accepted/classified: **target <= 10 seconds** under supported network/file-size conditions;
- App open -> Capture New Booking -> Documents -> Booking Details -> populated Review: **P95 target < 2 minutes**;
- measure P50/P95 in DEV/UAT rather than represent these as guarantees for arbitrary networks or files.

## Hard rule

**Classification is the Step-1 exit gate. Extraction is not the Step-1 exit gate.**

The PC cannot leave Documents until every currently-required document is durably uploaded and classified so Audit Core can reconcile the active Booking requirement set. Extraction starts immediately after each accepted classification and overlaps the PC's Booking Details work.

Once evidence is safely stored, a transient downstream classification/extraction/provider failure is a system retry problem. The PC must not be asked to upload the same evidence again. Replacement is required only when the evidence itself cannot be accepted/identified (for example genuine UNKNOWN/ambiguous/unusable evidence).

## End-to-end sequence

```text
App / workspace ready
        |
        v
Capture New Booking
        |
        v
Booking conditions
(GST / Corporate / Exchange)
        |
        v
Active document requirement set
        |
        v
PC selects/captures documents
        |
        +--> direct upload -> classify -> accepted -> extraction starts -> DI facts
        +--> direct upload -> classify -> accepted -> extraction starts -> DI facts
        +--> direct upload -> classify -> accepted -> extraction starts -> DI facts
        +--> direct upload -> classify -> accepted -> extraction starts -> DI facts
                              |
                              +--> Audit Core requirement reconciliation
        |
        v
ALL REQUIRED DOCUMENTS IDENTIFIED
        |
        v
Step 1 COMPLETE
        |
        v
Booking Details
        |
        |   DI extraction continues concurrently
        v
Submit Booking
        |
        v
Booking Attribute Review
        |
        +--> Attribute
        +--> resolved/extracted value
        +--> confidence
        +--> source document type
        +--> evidence link
        +--> review state
        |
        v
Evidence click -> original document + source page + bounding box
```

## Ownership boundaries

### Web / Android

- Uploads evidence using the V2 direct-upload contract exposed through Audit Core.
- Does not ask the PC to select document type.
- Presents business states (`Uploading`, `Identifying`, `Identified`) instead of internal worker vocabulary.
- Enables Step 1 -> Step 2 only when Audit Core says all blocking requirements are satisfied.
- Does not make extraction completion a Step-1 blocker.

### Audit Core

- Owns Booking conditions, active requirement reconciliation and journey progression.
- Treats classification as the document-completeness gate.
- Does not duplicate DI raw extracted values/confidence/page/box data.
- Booking Review uses the common UC03 attribute mapping/resolution contract defined in `UC03_ATTRIBUTE_AUDIT_REVIEW_DESIGN_2026-08-30.md`.

### Document Intelligence

- Owns durable evidence, classification, extraction, confidence and source localization.
- V2 byte-based classification occurs before Step 1 completes.
- Accepted V2 documents are handed to a bounded V2 extraction pool immediately.
- The existing processing pipeline remains the authority for normalization, deterministic validation, extracted facts, current machine values, confidence, verification, retry and backout.
- The accepted V2 classification is reused locally when the processing pipeline asks for classification; there is no second provider classification request.
- Concrete V1/provider adapter implementations and `/v1` API contracts remain unchanged.

## Performance overlap

The intended elapsed-time model is pipelined, not sequential:

```text
0s        App/workspace
          |
          v
~5-15s    upload + classification gate completes
          |
          +-----------------------+
          |                       |
          v                       v
      Booking Details        DI extraction
          |                       |
          +-----------+-----------+
                      v
                 Submit Booking
                      |
                      v
                   Review
```

Extraction should normally be complete by the time the PC finishes Booking Details. If a legitimate extraction is still running, Review may show the available values plus a bounded processing state; it must not force a repeat upload.

## Required telemetry

Per document:

- upload intent time;
- object upload/reconciliation completed time;
- classification start/end;
- extraction start/end;
- Review-value-available time.

Per journey:

- App open -> Capture ready;
- first file selected -> all blocking requirements classified;
- Booking Details submit -> Review rendered;
- App open -> populated Review rendered.

## Non-regression constraints

This amendment must not:

- change any working V1 API contract;
- change `DocumentAIAdapter` interface;
- modify existing concrete V1/Gemini adapter behaviour;
- duplicate DI raw extraction data into Audit Core;
- make extraction completion a prerequisite for leaving Documents;
- treat processing order as source precedence;
- auto-accept a classification below the configured V2 threshold.
