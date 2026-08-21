# Verigence Audit Core — Cross-Module Authentication Design Revision

**Document ID:** VAC-SD-AUTH-002  
**Version:** 1.1  
**Status:** DRAFT FOR IMPLEMENTATION REVIEW  
**Date:** 2026-08-21  
**Supersedes for Phase 1:** `VAC-SD-AUTH-001 v1.0` where this revision conflicts  
**Applies to:** `VAC-SD-004 v2.2`, Security v2.0/v2.1, UC02 Project Onboarding

> This revision reconciles Audit Core with the current Security Phase-1 actor/token model. It does not change code, keys, token TTLs or machine-readable API files.

---

## 1. Governing identities

Phase 1 has two distinct Security-issued actor models:

```text
HUMAN USER
  -> Security-issued human access JWT

SERVICE_INTEGRATION
  -> Security-issued machine JWT
  -> audience-bound to target module
```

Audit Core must never blur these actor types.

No Audit Core code/module integrates with Clerk.

---

## 2. Human request to Audit Core

For a protected human request:

```text
Browser/User
  -> Audit Core with Security human JWT
```

Audit Core:

1. validates Security signature/issuer/expiry using Security trusted signing/JWKS;
2. extracts the authenticated global Verigence USER identity;
3. calls Security `/authorization/check` using Audit Core's `ServiceIntegration` JWT with audience `security` when a live functional authorization decision is required;
4. supplies the USER identity derived from the validated human token, Tenant context and required registered Audit Core permission;
5. fails closed when Security cannot return a trustworthy authorization decision;
6. additionally enforces Audit Core Dealer/Outlet business scope where applicable.

The human JWT is authentication/session evidence; Audit Core does not treat embedded human permission claims as the live authority when Security v2 requires synchronous authorization.

---

## 3. Normal Audit Core -> DI business integration

For normal document-intelligence integration after Audit Core has authorized the outer human/business operation:

```text
Audit Core
  -> Security /service/token using Audit Core service credential
  -> Security-issued ServiceIntegration JWT, aud=di
  -> DI normal integration endpoint
```

Rules:

- machine token authenticates Audit Core to DI;
- token is audience-bound to DI;
- no human bearer token is required merely to authenticate Audit Core's normal DI integration call;
- Audit Core retains the original initiating USER ID/correlation/provenance in its own authoritative operation/audit data and may send safe provenance context to DI only where the DI contract permits;
- DI trusts provenance only because it came from authenticated Audit Core, not as human authentication;
- Audit Core does not self-sign downstream credentials;
- Audit Core does not forward the browser's human token to a normal machine integration endpoint merely to avoid using ServiceIntegration.

Background retry/continuation uses the same ServiceIntegration model.

---

## 4. UC02 downstream administrative operations

UC02 requires a different rule for **administrative ownership**.

When the SuperAdmin performs a Project administration action through Audit Core and the operation includes a Security-owned or DI-owned administrative write, Audit Core SHALL pass the **same Security human JWT** to that downstream admin endpoint.

Examples:

```text
Create Project
  Audit Core -> Security create Tenant        same human JWT
  Audit Core -> DI administrative provision  same human JWT if DI exposes it as admin operation

Role Mapping
  Audit Core -> Security operating-role PUT  same human JWT

Activate Project
  Audit Core -> Security Tenant activate      same human JWT

Delete Project
  Audit Core -> DI administrative purge       same human JWT
  Audit Core -> Security Tenant DELETE        same human JWT, last
```

The downstream owning module independently validates and authorizes the human SuperAdmin.

Audit Core SHALL NOT:

- replace the human JWT with `ServiceIntegration` for these admin calls;
- mint an impersonated human token;
- pass a caller-supplied `userId` as proof of admin identity;
- fall back to a ServiceIntegration token when the downstream human admin call is denied/unavailable.

This rule is exactly the separation between administrative human authority and machine integration authority.

---

## 5. Supersession of delegated OAuth token exchange

`VAC-SD-AUTH-001 v1.0` required OAuth delegated token exchange/on-behalf-of for user-driven Audit Core -> DI calls.

That requirement is superseded for current Phase 1 by the later Security v2 architecture:

```text
outer human action
  -> Security human JWT establishes USER identity
  -> Audit Core obtains live Security authorization
  -> normal Audit Core -> DI integration uses ServiceIntegration
```

No delegated user-token exchange is required in current Phase 1.

This does **not** permit Audit Core to use ServiceIntegration to bypass the outer human authorization. The outer human action is still authorized under the current Security USER/Tenant permission state and Audit Core business scope before normal DI integration begins.

---

## 6. Authorization-check flow

Audit Core calls:

```text
POST /security/v1/authorization/check
Authorization: Bearer <ServiceIntegration JWT, aud=security>
```

with USER identity derived from the validated Security human JWT.

A browser cannot establish identity by supplying an arbitrary USER ID to this internal flow.

Audit Core strips/ignores client attempts to supply internal actor context.

For UC02 SuperAdmin-only control-plane operations such as Project create/hard-delete, the current approved Audit Core permission catalogue does not contain a destructive Project-delete permission. Audit Core therefore also needs live Security administrative-classification attestation sufficient to establish the one active SuperAdmin. The exact Security response field/contract must be frozen before code; this document does not invent a role claim in the human JWT or a new permission key.

---

## 7. DI storage-context call

For Audit Core-originated evidence, Audit Core supplies trusted business context to DI under authenticated machine integration:

```text
Tenant/Project ID
Dealer ID
Dealer Outlet ID
Customer ID
Audit Core external context/Journey reference
safe display names where required for readable storage path
```

The browser never supplies an object-storage path.

DI owns the resulting storage context/key construction.

---

## 8. Failure rules

### Human -> Audit Core authorization failure

No downstream DI call occurs.

### Security `/authorization/check` unavailable

Protected human business/admin request fails closed or remains in the durable UC02 administrative-operation recovery state where the outer operation already started.

### Normal ServiceIntegration token unavailable

Normal Audit Core -> DI integration does not occur and follows existing dependency/retry rules.

### Downstream human-admin call denied/unavailable

Audit Core does not retry it with a machine token. The UC02 administrative operation remains failed/recovery-required and retains its safe operation receipt.

### Partial cross-module write

Audit Core reports/reconciles the actual module states and never fabricates combined success.

---

## 9. Token handling

Audit Core SHALL NOT persist:

- human bearer JWTs;
- ServiceIntegration JWTs;
- Security service credentials in business/admin operation records.

Audit Core may persist:

- global USER ID;
- correlation ID;
- downstream operation/receipt ID;
- safe authorization/audit decision identifiers where the Security contract provides them.

Token TTL/rotation remains Security-owned and is not redefined here.

---

## 10. Verification matrix

| Scenario | Downstream actor to use |
|---|---|
| Audit Core -> Security `/authorization/check` | ServiceIntegration, `aud=security` |
| Audit Core -> DI normal evidence/document integration | ServiceIntegration, `aud=di` |
| Audit Core -> Security Tenant create/update/activate/delete | original Security human admin JWT |
| Audit Core -> Security operating-role administration | original Security human admin JWT |
| Audit Core -> DI UC02 admin provision/purge/config operation | original Security human admin JWT |
| background DI polling/retry after authorized business action | ServiceIntegration, `aud=di` |

Any implementation using the wrong actor type for these paths fails the UC02 security design review.

---

## 11. No-code boundary

This revision makes no change to:

- application code;
- Security or DI code;
- OpenAPI/YAML;
- service credentials;
- JWT TTL/configuration;
- permission catalogue files.

It only replaces the outdated Phase-1 delegated-token-exchange assumption and records the approved human-admin pass-through rule.