# UC03 Customer Mobile PII Design

**Status:** IMPLEMENTATION APPROVED
**Date:** 2026-08-27
**Scope:** Audit Core + Security; existing Customer APIs and existing UC03 Booking capture only

## Decision

Audit Core shall persist the complete normalized customer mobile number. The database value is not masked or truncated. API masking is performed only at response serialization time according to Security permissions.

No new customer/mobile API is introduced.

## Persistence

`auditcore.customers` gains a nullable `mobile_number` field containing the complete normalized mobile number. The existing `mobile_last4` field remains for backward compatibility, matching, compact display and existing consumers.

When a full customer number is captured, Audit Core shall persist both:

- `mobile_number`: complete normalized number;
- `mobile_last4`: final four digits derived from the normalized number.

For the Indian Phase-1 workflow, normalization removes formatting characters and preserves an explicit leading `+` country code when provided. A ten-digit domestic number is stored as the ten digits supplied; country-code inference is not performed by Audit Core unless separately approved.

## Existing API behavior

The existing Customer APIs remain the contract:

- `POST /v1/tenants/{tenantId}/outlets/{outletId}/customers`
- `GET /v1/tenants/{tenantId}/outlets/{outletId}/customers`
- `GET /v1/tenants/{tenantId}/customers/{customerId}`
- `PATCH /v1/tenants/{tenantId}/customers/{customerId}`

The request model accepts `mobileNumber` while retaining `mobileLast4` for backward compatibility.

The response includes both `mobileNumber` and `mobileLast4`.

### Normal customer-read callers

Callers with ordinary `audit.customer.read` receive a masked `mobileNumber`, exposing only the final four digits, for example:

```json
{
  "mobileNumber": "******3210",
  "mobileLast4": "3210"
}
```

### Full-contact callers

Callers additionally holding `audit.customer.contact.full.read` receive the complete stored value:

```json
{
  "mobileNumber": "+919876543210",
  "mobileLast4": "3210"
}
```

Authorization is permission-based. Audit Core shall not authorize full PII by testing role-name strings.

## Permission policy

Add Audit Core permission:

`audit.customer.contact.full.read`

Default assignment:

- Executive: granted;
- Super Admin: granted where the Security administrative principal is permitted to operate in the target Tenant context;
- PC: not granted;
- TL: not granted;
- PM: not granted;
- CRM: not granted unless separately approved.

`audit.customer.read` remains required for the Customer read itself. The full-contact permission only controls whether the mobile value is revealed or masked.

## UC03 Booking capture

The existing `CUSTOMER_NUMBER` capture path shall stop discarding the full number. It shall normalize the supplied value, persist it to `customers.mobile_number`, and derive/persist `customers.mobile_last4` in the same transaction.

DI extraction proposals remain provenance. They are not the canonical source for customer contact retrieval once an accepted/corrected number has been written into the Customer domain.

## Logging and audit safety

The full mobile number shall not be written to application logs, telemetry, exception messages, workflow safe payloads or generic audit-event payloads.

This change does not create a separate reveal endpoint. Full vs masked output is handled by the normal Customer read serializers.

## Compatibility

Existing consumers using `mobileLast4` continue to work unchanged. Existing rows without a full stored number return `mobileNumber: null` and preserve their current `mobileLast4` value.

## Acceptance criteria

1. Full mobile persists in Audit Core when captured through Customer create/patch or UC03 `CUSTOMER_NUMBER` capture.
2. `mobile_last4` is always derived from a supplied full mobile.
3. Existing Customer GET/list endpoints return masked mobile to ordinary customer-read callers.
4. The same endpoints return full mobile when `audit.customer.contact.full.read` is present.
5. No new API endpoint is added.
6. PC/TL/PM default bundles do not receive the full-contact permission.
7. Tests cover persistence, masking, full reveal and UC03 Booking capture behavior.
