# Audit Core UC02 — Role Mapping Scope Clarification

**Status:** OWNER CLARIFICATION — SUPERSEDES PRIOR UC02 ROLE-SCOPE ASSUMPTIONS AND EARLIER 21-AUG CARDINALITY RESTRICTIONS  
**Date:** 21-Aug-2026  
**Applies to:** UC02 Project Onboarding / Project Administration Role Mapping

This decision supersedes prior UC02 Role Mapping statements wherever they impose artificial cardinality or scope restrictions that are not required by the business. In particular, it supersedes the earlier 21-Aug-2026 correction that limited PC to exactly one ONSITE plus at most one SATELLITE and made CRM Project-wide only.

The design principle is: **model the business scope that may be assigned; do not hard-code staffing assumptions as role cardinality rules.**

## Role scope

| Role | Business scope |
|---|---|
| PC | One or more selected Dealer Outlets |
| TL | Whole Project |
| PM | Whole Project |
| CRM | Whole Project, selected Dealer(s), selected Outlet(s), or a union of selected Dealer(s) and Outlet(s) |
| Executive | Whole Project |

## PC rules

- PC is **Outlet-scoped**.
- A PC must have at least one selected Dealer Outlet.
- A PC may be assigned to an ONSITE Outlet, a SATELLITE Outlet, or any mix of valid Outlets.
- A PC may be **SATELLITE-only**.
- A PC may have **more than one SATELLITE** location.
- No arbitrary maximum number of Outlet assignments is imposed by UC02 Role Mapping.
- No `1 x ONSITE + 1 x SATELLITE` cardinality rule exists.
- Outlet ownership must be validated against the current Project/Tenant before Security is changed.
- Dealer is derived from each selected Dealer Outlet; Dealer ID is not a direct PC scope input.

Outlet classification remains useful business metadata, but it does not determine whether a PC assignment is valid.

## CRM rules

CRM is deliberately flexible because CRM responsibility can differ by Project operating model.

A CRM mapping may be:

- **Project-wide** — no Dealer or Outlet selection;
- **Dealer-scoped** — one or more selected Dealers;
- **Outlet-scoped** — one or more selected Dealer Outlets; or
- **Combined scoped** — selected Dealer(s) plus selected Outlet(s), interpreted as the union of those scopes.

There is no arbitrary count limit on CRM Dealer or Outlet selections. Every selected Dealer/Outlet must belong to the current Project/Tenant.

For a combined CRM mapping, a Dealer selection means the whole selected Dealer scope; an Outlet selection means that specific Outlet scope. The API preserves these as separate direct scopes so the resulting mapping remains explicit and reversible.

## TL / PM / Executive rules

TL, PM and Executive remain Project-wide for the current UC02 design. Dealer and Outlet selections are not accepted for these roles unless a later business decision explicitly changes their scope model.

## API representation

The UC02 request shape remains:

```json
{
  "operatingRole": "PC | TL | PM | CRM | Executive",
  "dealerIds": [],
  "outletIds": []
}
```

Interpretation:

- **PC**: `dealerIds` is empty; `outletIds` contains one or more selected Outlets.
- **CRM Project-wide**: both arrays are empty.
- **CRM Dealer scope**: `dealerIds` contains one or more Dealers.
- **CRM Outlet scope**: `outletIds` contains one or more Outlets.
- **CRM combined scope**: both arrays may contain selections; effective scope is their union.
- **TL / PM / Executive**: both arrays are empty.

The existing `business_assignments` model supports all of these forms:

- Project-wide row: `dealer_id = NULL`, `outlet_id = NULL`;
- Dealer-wide row: `dealer_id = <dealer>`, `outlet_id = NULL`;
- Outlet row: `dealer_id = <derived dealer>`, `outlet_id = <outlet>`.

No additional membership table is required for this flexibility.

## Readiness implication

The existing readiness concern remains coverage-based: ACTIVE Dealer Outlets that require PC coverage must have at least one ACTIVE PC assignment. Readiness must evaluate actual Outlet coverage, not assume a maximum number or a required ONSITE/SATELLITE pattern per PC.
