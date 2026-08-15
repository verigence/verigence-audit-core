# Verigence Audit Core — Runtime and Tooling Baseline

**Document ID:** VAC-RT-001  
**Version:** 1.0  
**Status:** APPROVED / BASELINED FOR IMPLEMENTATION  
**Date:** 2026-08-15  
**Implementation task:** P0-02  

## 1. Purpose

This document records the implementation runtime/tooling choice required by `docs/AUDIT_CORE_IMPLEMENTATION_PLAN_v1.0.md` before the Audit Core service is scaffolded. It does not change the approved v2.1 business, API, schema, Security or DI contracts.

## 2. Approved application stack

| Concern | Approved choice |
|---|---|
| Language/runtime | Python 3.12 |
| API framework | FastAPI |
| ORM / database access | SQLAlchemy 2.x |
| Database migrations | Alembic |
| Automated test runner | pytest |
| Primary database | PostgreSQL |

Exact package patch/minor pins beyond the choices above will be recorded in the repository dependency files when A-01 scaffolding is implemented. No additional framework or abstraction is approved by this decision.

## 3. Approved hosting platform

| Concern | Approved choice |
|---|---|
| Application hosting | Railway |
| Managed PostgreSQL hosting | Neon |

Environment topology, service sizing, regions, autoscaling, connection-pooling values and deployment promotion rules are not defined by P0-02 and must not be invented unless required by a later implementation task.

## 4. Retained implementation boundaries

The approved runtime/tooling choice does not alter these existing baseline rules:

- Audit Core remains a modular monolith initially.
- Security remains the identity and effective-permission authority.
- DI remains a separate internal-only module behind Audit Core for user-facing flows.
- Audit Core uses PostgreSQL and must implement the approved Tenant RLS/runtime-role pattern.
- Public Audit Core APIs contain no baseline destructive DELETE operations.
- Published master versions remain immutable.
- Unresolved business formulas, thresholds and vocabularies remain open/configurable rather than guessed.

## 5. P0-02 acceptance

P0-02 is satisfied when this decision is committed and verified in the repository, allowing A-01 and B-01 to proceed without assuming runtime, migration or test tooling.
