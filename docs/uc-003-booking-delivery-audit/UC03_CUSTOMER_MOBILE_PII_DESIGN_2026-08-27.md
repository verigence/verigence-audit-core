# UC03 Customer Mobile PII Design

**Status:** IMPLEMENTATION APPROVED  
**Date:** 2026-08-27  
**Scope:** Audit Core + Security; existing Customer APIs and existing UC03 Booking capture only

## 1. Decision

Audit Core shall persist the complete normalized customer mobile number. The database value is not masked or truncated. Masking is a response-layer authorization concern only.

No new customer/mobile API is introduced.

## 2. Persistence

`auditcore.customers` gains nullable `mobile_number`, containing the complete normalized mobile number. Existing `mobile_last4` remains for backward compatibility, matching, compact display and existing consumers.

When a complete customer number is captured, Audit Core persists both:

- `mobile_number`: complete normalized value;
- `mobile_last4`: final four digits derived from that value.

Normalization removes formatting characters and preserves an explicit leading `+`. Audit Core does not infer a country code that the source did not provide.

Existing records that contain only `mobile_last4` are not backfilled with an invented full number.

## 3. Existing API contract

The existing Customer APIs remain the contract:

- `POST /v1/tenants/{tenantId}/outlets/{outletId}/customers`
- `GET /v1/tenants/{tenantId}/outlets/{outletId}/customers`
- `GET /v1/tenants/{tenantId}/customers/{customerId}`
- `PATCH /v1/tenants/{tenantId}/customers/{customerId}`

No reveal endpoint is added.

Customer create/patch accepts additive `mobileNumber` while retaining `mobileLast4` for backward compatibility. Customer responses contain both fields.

### Ordinary customer-read callers

When the caller does not hold the dedicated full-contact permission, `mobileNumber` is masked and only the final four digits are exposed:

```json
{
  "mobileNumber": "******3210",
  "mobileLast4": "3210"
}
```

### Full-contact callers

When the caller also holds `audit.customer.contact.full.read`, the same API returns the complete value:

```json
{
  "mobileNumber": "+919876543210",
  "mobileLast4": "3210"
}
```

Authorization is permission-based. Audit Core must not authorize PII disclosure using a role-name comparison.

## 4. Permission policy

Add Audit Core permission:

`audit.customer.contact.full.read`

Default assignment:

- Executive: granted;
- Super Admin: full authority over active registered permissions continues to apply;
- PC: not granted;
- TL: not granted;
- PM: not granted;
- CRM: not granted by default.

The permission controls disclosure only. It does not independently grant Customer access or expand Tenant/Dealer/Outlet business scope.

## 5. UC03 Booking capture

The existing `CUSTOMER_NUMBER` capture path must stop discarding the complete number. It shall normalize the supplied value, persist it to `customers.mobile_number`, and derive/persist `customers.mobile_last4` in the same transaction.

The same rule applies when a DI `customer_phone` extraction proposal is accepted or corrected into the typed Customer domain, because that flow uses the same UC03 capture mapping.

DI extraction/proposal data remains provenance; it is not the canonical customer contact source after an accepted/corrected number has been written into the Customer domain.

## 6. Logging and audit safety

The complete customer mobile number shall not be written to application logs, telemetry, exception messages, workflow safe payloads or generic audit-event payloads.

The UC03 capture command may return the value to the authenticated PC that just entered/corrected it as part of the existing command response, but persistent logs/audit-safe payloads must not contain it.

## 7. Compatibility

Existing consumers using `mobileLast4` continue to work unchanged.

For historical rows with no `mobile_number`, `mobileNumber` is `null` and the existing `mobileLast4` remains available.

## 8. Acceptance criteria

1. Complete mobile persists in Audit Core when supplied through Customer create/patch.
2. UC03 `CUSTOMER_NUMBER` capture persists the complete number and derives `mobile_last4`.
3. Accepted/corrected DI customer-phone proposals follow the same persistence rule.
4. Existing Customer GET/list endpoints return a masked `mobileNumber` to callers without the full-contact permission.
5. The same endpoints return the complete value to callers with `audit.customer.contact.full.read`.
6. No new API endpoint is introduced.
7. PC/TL/PM/CRM default bundles do not receive the full-contact permission.
8. Executive receives the permission; Super Admin retains full-authority resolution for the active permission.
9. Tests cover persistence, masking, full reveal and UC03 Booking capture behavior.
