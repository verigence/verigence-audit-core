# Audit Core UC02 — Role Mapping Scope Correction

**Status:** OWNER CORRECTION — SUPERSEDES PRIOR UC02 ROLE-SCOPE ASSUMPTIONS  
**Date:** 21-Aug-2026  
**Applies to:** UC02 Project Onboarding / Project Administration Role Mapping

This decision supersedes the Role Mapping scope rules previously stated in `AUDIT_CORE_API_CONTRACT_v1.1.md`, the UC02 solution/design amendments, and the Web Journey-02 frozen baseline wherever those documents say TL is Dealer-scoped or CRM may be Dealer-scoped.

## Correct Phase-1 role scope

| Role | Correct business scope |
|---|---|
| PC | Exactly one primary `ONSITE` Dealer Outlet, with at most one additional `SATELLITE` Dealer Outlet |
| TL | Whole Project |
| PM | Whole Project |
| CRM | Whole Project |
| Executive | Whole Project |

### PC rules

- A PC must be mapped to one `ONSITE` Dealer Outlet.
- The same PC may additionally be mapped to one `SATELLITE` Dealer Outlet.
- Therefore one PC has a maximum of two active Outlet assignments: `1 x ONSITE` plus optional `1 x SATELLITE`.
- Two ONSITE Outlets for the same PC are invalid.
- Two SATELLITE Outlets for the same PC are invalid.
- A SATELLITE-only PC mapping is invalid.
- Outlet ownership must be validated against the current Project/Tenant before Security is changed.
- Dealer is derived from the selected Dealer Outlet; Dealer ID is not a persisted PC scope input.

### TL / CRM rules

- TL is not Dealer-scoped.
- CRM is not Dealer-scoped.
- Both are Project-wide operating roles in Phase 1.
- Dealer/Outlet selections must therefore be absent for TL and CRM Role Mapping writes.

PM and Executive remain Project-wide as previously agreed.

## API compatibility

The current UC02 draft request shape may temporarily retain:

```json
{
  "operatingRole": "PC | TL | PM | CRM | Executive",
  "dealerIds": [],
  "outletIds": []
}
```

For the corrected Phase-1 contract:

- `dealerIds` must always be empty; it is retained only for draft-contract compatibility while the consolidated contract is updated.
- `outletIds` is populated only for PC and must satisfy the PC cardinality/classification rule above.
- TL, PM, CRM and Executive send empty `dealerIds` and `outletIds` and are represented in `business_assignments` by a Project-wide row (`dealer_id = NULL`, `outlet_id = NULL`).

The existing `business_assignments` physical model supports this correction; no new membership table or role-scope table is required.

## Readiness implication

The existing blocking rule remains: every ACTIVE Dealer Outlet must have at least one ACTIVE PC mapping. The corrected PC maximum means staffing may require multiple PCs where a Project has more Outlet/Satellite locations than one PC can cover.
