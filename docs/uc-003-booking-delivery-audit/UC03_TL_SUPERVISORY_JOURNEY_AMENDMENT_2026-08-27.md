# UC03 Team Lead Supervisory Journey Amendment

**Date:** 2026-08-27  
**Status:** Approved product decision / implementation input  
**Applies to:** UC03 Booking & Delivery Audit  
**Supersedes:** any UC03 wording that makes Team Lead (TL) review a mandatory gate for normal PC Booking/Delivery progression.

## 1. Decision summary

The Team Lead journey is a supervisory operational journey. It is not the PC capture journey and it is not read-only.

A TL has authority to inspect and intervene in submitted/progressed Booking and Delivery cases within the TL's assigned business scope, but TL review is **optional in the current phase**. Normal PC Booking/Delivery progression must not wait for TL approval.

## 2. Business scope

TL scope is derived from the TL's active Dealer-level assignment.

For every Dealer assigned to the TL, the TL must be able to see Booking and Delivery activity for **all Outlets under that Dealer**.

The TL dashboard must provide:

- overall Booking/Delivery counts for the TL's complete scope;
- Outlet-wise counts;
- individual PC-wise counts;
- a list of submitted/progressed Booking/Delivery cases in scope.

PC ownership/counting must be based on the PC responsible for the operational case/submission, not merely the actor on the most recent workflow event.

## 3. Case visibility and displayed status

The TL sees cases that have reached the submitted/progressed operational boundary. Unsubmitted PC drafts are not part of the TL supervisory list for this phase.

The primary status shown to TL is the latest **business stage**, not a mandatory TL approval state.

Status precedence for the current phase is:

1. Delivery completed/submitted -> `Delivery Completed` (or final approved Delivery wording used by UC03 UI);
2. Delivery started and not completed -> `Delivery In Progress`;
3. Booking submitted and Delivery not started -> `Booking Submitted`.

If optional TL activity exists, it may be shown as secondary review metadata, but it must not replace the business-stage status or block progression.

## 4. TL authority

For an in-scope submitted/progressed case, TL may:

- open Booking/Delivery details;
- open submitted documents;
- inspect the extracted field set stored for the document/journey;
- review extracted values;
- verify values;
- overwrite/correct extracted values where the applicable Audit Core field/correction policy permits it;
- add the required audit/provenance record for a TL correction;
- request the responsible PC to upload/re-upload a document when the document itself is missing, incorrect, unreadable, or needs replacement.

TL corrections must preserve the existing extracted value and record the modified value, actor and timestamp using the current generic extracted-field/provenance model wherever possible.

## 5. Explicit TL restrictions

TL must **not** directly upload, capture, replace or re-upload Booking/Delivery documents.

Document capture remains a PC operational responsibility.

The TL UI/API must therefore not expose PC upload/camera controls to TL.

## 6. Re-upload request behavior

A TL request for re-upload is an explicit action back to the responsible PC.

The request must:

- identify the affected case and document requirement/document;
- be visible to the responsible PC as an action requiring attention;
- remain traceable until the PC resolves it by the permitted upload/re-upload flow;
- retain requester, request time and resolution provenance;
- return the updated document/case to normal supervisory visibility after the PC acts.

The re-upload request does **not** convert all cases into mandatory TL approval flows. It creates a concrete PC action only for the affected document/case.

## 7. Non-blocking rule

TL review is a **right, not a mandatory gate**, in this phase.

Therefore:

- PC Booking submission does not wait for TL review;
- PC Delivery start/progression does not wait for TL review;
- PC Delivery completion/submission does not wait for TL review unless an existing independent UC03 business rule already requires otherwise;
- absence of TL review must not manufacture `PENDING_TL_APPROVAL` as the primary business status;
- TL review/correction activity is auditable supervisory activity alongside the normal Booking/Delivery lifecycle.

## 8. Data/read direction

After PC submission/review, the generic extracted-field set is already persisted in Audit Core. TL supervisory review should use the persisted submitted field set and provenance as the durable review source, rather than introducing a second TL-specific extraction source of truth.

Any field corrected by TL must continue to retain both extracted and modified values according to the existing generic-field model.

## 9. Implementation direction

Audit Core implementation should add only the missing capabilities required by this decision:

- TL dealer-scoped submitted/progressed case reads;
- overall, Outlet-wise and PC-wise aggregation;
- TL case/document/extracted-field read support;
- optional TL verification/correction using existing provenance structures where possible;
- PC re-upload request creation/read/resolution lifecycle;
- audit events/tests proving TL review is optional and does not block normal PC progression.

Do not redesign Security v2 for this amendment. Reuse current role/permission and business-scope controls unless implementation exposes a concrete missing backend permission/capability.

## 10. Acceptance rules

Implementation is acceptable only if tests demonstrate all of the following:

1. TL sees all Outlets under assigned Dealer(s), and no unauthorized Dealer/Outlet data.
2. Counts reconcile between overall, Outlet-wise, PC-wise and case-list views.
3. Booking submitted + no Delivery start displays Booking Submitted.
4. Delivery started displays Delivery In Progress even if TL has never reviewed the case.
5. Delivery completion remains visible as the latest business stage without TL approval.
6. TL can review/verify/correct permitted extracted values with full provenance.
7. TL cannot directly upload/re-upload documents.
8. TL can request PC re-upload; the PC sees and can resolve that request.
9. A case with no TL review can still proceed through the normal PC Booking/Delivery lifecycle.
10. TL activity never silently rewrites historical extracted/original values or workflow history.
