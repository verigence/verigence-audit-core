# Audit Core UC02 — Product Master Phase-1 Alignment

**Status:** UC02 DESIGN ALIGNMENT — OWNER DECISION CONFIRMED  
**Date:** 2026-08-21  
**Repository:** `verigence/verigence-audit-core`  
**Branch:** `dev`  
**Related baseline:** `docs/AUDIT_CORE_UC02_ADMIN_ALIGNMENT.md`

> This amendment closes the remaining Product Master scope decision for UC02 Phase 1. It supersedes the open Product Master scope statement in `AUDIT_CORE_UC02_ADMIN_ALIGNMENT.md`.

## 1. Phase-1 rule — keep Product Master simple

Phase 1 will **not** introduce a shared/reusable Product Master picker across Projects.

For UC02 Phase 1:

- Product Master is maintained within the Project administration flow;
- each Project receives its own effective-dated Product Master version history through the approved Excel upload workflow;
- repeated uploads are supported;
- every upload requires explicit WEF / Valid From;
- uploaded rows are staged, validated and shown to SuperAdmin before confirmation;
- published historical versions are not overwritten in place;
- historical Journey/Product/SKU meaning must remain reproducible;
- Price Lists and Discount Schemes must resolve against Product/SKU data valid for the same Project/effective context;
- one Project's Product Master changes must not silently change another Project's operational master state.

The exact physical schema is still an Audit Core implementation-design responsibility. This decision defines scope and behaviour; it does not invent table names or migration mechanics.

## 2. Existing platform product references

The current Audit Core v2.1 model contains shared platform reference entities for OEM/Product/Model/Variant/Colour/SKU.

UC02 Phase 1 must not implement Project Product Master by mutating those shared reference rows in a way that changes the historical or operational meaning of another Project.

The next Audit Core data-model revision must introduce the minimal Project-effective/versioned treatment required to satisfy the Phase-1 behaviour above while reusing stable platform reference identity where appropriate.

## 3. Phase-2 direction

Phase 2 may introduce a more reusable master-management experience, including an option for a new Project to **pick/reuse an existing approved Product Master/catalogue** instead of uploading everything again.

That Phase-2 capability may include:

- selecting an existing Product Master/catalogue as a starting point;
- controlled reuse/copy/reference semantics;
- stronger cross-Project master governance;
- controlled master inheritance/supersede rules.

Those semantics are deliberately deferred. Phase 1 must not build them implicitly.

## 4. Implementation consequence

The Product Master work is no longer blocked on scope clarification.

Phase-1 implementation may proceed once the Audit Core solution/API/physical-data-model revision defines the minimal Project-effective version model and the Excel staging/preview/confirm contract.