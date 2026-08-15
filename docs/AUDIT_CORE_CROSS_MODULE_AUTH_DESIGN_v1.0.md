# Verigence Audit Core — Cross-Module Authentication Design

**Document ID:** VAC-SD-AUTH-001  
**Version:** 1.0  
**Status:** APPROVED ARCHITECTURE DECISION  
**Date:** 2026-08-15  
**Applies to:** `VAC-SD-003 v2.1`, Increment G / `G-01`  
**Authority:** Project-owner decision recorded 2026-08-15

## 1. Purpose

This document is an authoritative design amendment to `docs/AUDIT_CORE_SOLUTION_DESIGN_v2.1.md` for authentication and authorization when Audit Core calls another Verigence module, initially Document Intelligence (DI).

It does not change the existing boundaries that Security is the identity/effective-permission authority, DI remains internal behind Audit Core for user-facing Audit Core journeys, and Web/Mobile clients do not call DI directly.

## 2. Governing rule

Audit Core SHALL use Security-mediated authentication for all calls to DI. Audit Core SHALL NOT mint downstream credentials, share a private bypass credential directly with DI, read Security private data, or elevate a user action by substituting a more privileged service identity.

There are two authorization paths, selected by who owns the action.

## 3. System-integration flow

System integration is used for module-owned administrative/system work and for background continuation of an operation that was already authorized and committed under a user context.

```text
Audit Core service
    -> Security service-auth/system-integration flow
    -> short-lived Security-issued SERVICE token
    -> DI
```

Rules:

- Audit Core authenticates to Security as the Audit Core service/workload.
- Security issues the downstream token; Audit Core does not self-sign or manufacture permissions.
- Tenant-scoped DI operations use a Tenant-scoped service token.
- The service token receives only the DI permissions required for the permitted integration operation.
- System integration SHALL NOT be used to bypass a PC/TL/PM/CRM/Executive authorization decision for a new user-driven business action.
- No Web/Mobile client receives the DI service token.
- No shared Audit-Core-to-DI API-key bypass is introduced.

For DI compatibility, the current Security JWT contract remains Security-issued JWT + JWKS verification, with authoritative `permissions[]` and an actor type supporting `SERVICE`.

## 4. User-driven OAuth delegated flow

Booking, delivery, evidence, review and other interactive workflows initiated by a Verigence user SHALL retain that user's authorization context when Audit Core needs a synchronous DI operation.

The approved flow is OAuth 2.0 delegated token exchange / on-behalf-of semantics mediated by Security:

```text
User
  -> Security user access token
  -> Audit Core
       1. verify user token and Tenant/permission/business scope
       2. request downstream delegated token from Security
  -> Security
       3. authorize token exchange for requested DI capability
       4. issue short-lived downstream token
  -> Audit Core
  -> DI
```

The downstream token SHALL:

- remain Tenant-scoped;
- preserve the initiating user as the subject/authoritative business actor;
- identify Audit Core as the authorized calling/delegating client using the canonical Security delegation/authorized-party claim;
- contain only Security-calculated downstream permissions;
- be short-lived and intended for the downstream call, not stored as a long-lived credential.

Audit Core SHALL NOT simply forward the user's original bearer token to DI unless the final Security contract explicitly defines that token as the exchanged/downstream token.

## 5. Permission derivation

For a user-driven downstream operation, effective DI authority SHALL be no broader than:

```text
user effective authority
INTERSECT Audit Core allowed downstream integration authority
INTERSECT requested DI operation authority
```

Security is responsible for issuing the resulting permissions. Audit Core may request a capability but SHALL NOT add permissions to the token.

This preserves the existing rule that PC capture/upload capability does not imply final verification authority. For example, a PC-authorized evidence upload cannot produce a downstream token containing DI verification-write authority unless Security has explicitly granted that authority to the user for the action.

## 6. Asynchronous continuation and retry

A user access/delegated token SHALL NOT be stored or refreshed merely to support background processing.

After a user action has been authorized and the corresponding Audit Core operation has been durably committed, later internal continuation, polling, retry or recovery MAY use the Audit Core system-integration SERVICE token.

Audit Core SHALL preserve the original initiating actor and authorization context in its own durable audit/operation metadata, including the initiating actor identifier and correlation/operation linkage where applicable.

This background service flow is a continuation of an already-authorized action; it is not a mechanism for authorizing a new user business action.

## 7. Fail-closed behavior

For a user-driven synchronous operation:

- if Security denies token exchange, the downstream call SHALL NOT occur;
- if Security is unavailable and a delegated token cannot be obtained, Audit Core SHALL return/record the appropriate Security/dependency error;
- Audit Core SHALL NOT fall back to its SERVICE token to make the user action succeed.

For background continuation, normal persisted retry/recovery rules apply using the approved service identity.

## 8. Token contract alignment

Security, Audit Core and DI SHALL use one canonical JWT/service-auth contract. The current DI implementation already validates Security-issued JWTs through JWKS and consumes `tenant_id`, `actor_type` and authoritative `permissions[]` claims.

The Security module must define and implement:

1. Audit Core workload/service authentication;
2. Tenant-scoped short-lived `SERVICE` token issuance for module integration;
3. OAuth delegated token exchange/on-behalf-of issuance for user-driven downstream calls;
4. downstream permission calculation and denial rules;
5. canonical caller/delegation attribution claim(s);
6. token expiry/rotation/revocation behavior;
7. audit events for service-token issuance and delegated token exchange.

Exact secret storage, credential bootstrap and token TTL values remain Security implementation concerns and SHALL not be invented in Audit Core.

## 9. G-01 decision status

The **G-01 architecture decision is RESOLVED** by this approved design.

This does **not** mark the G-01 implementation task COMPLETE. G-01 implementation remains dependent on the corresponding Security service-token and delegated-token-exchange capability being implemented and then verified through a controlled Audit Core -> Security -> DI integration test.
