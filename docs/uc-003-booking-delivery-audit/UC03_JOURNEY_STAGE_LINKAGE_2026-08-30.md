# UC03 Journey / Booking / Delivery / Payment Linkage

**Date:** 2026-08-30  
**Status:** Implementation baseline — pending CI/DEV evidence until merged  
**Migrations:** `0041_uc03_journey_stage_linkage.py`, `0042_uc03_customer_relationship.py`, `0043_uc03_booking_stage_link.py`

## 1. Lifecycle decision

`Journey` is the root business transaction. The same Journey contains the Booking and Delivery stages.

```text
Journey
  ├── Customer context
  ├── Booking
  ├── Delivery (when it occurs)
  └── Payments / receipts (1:N)
```

Booking represents what was agreed. Delivery represents what finally happened. Audit and reconciliation operate across both stages under the same Journey.

## 2. Explicit identifiers

| Record | Explicit identifiers |
| --- | --- |
| Customer | `customer_id`, reverse `journey_id`, reverse `booking_id` |
| Journey | `journey_id`, `customer_id`, reverse `booking_id`, reverse `delivery_id` |
| Booking | `booking_id`, `journey_id` |
| Delivery | `delivery_id`, `journey_id`, `booking_id` |
| Payment / Receipt | `payment_id`, `journey_id`, `booking_id`, optional `delivery_id`, `payment_stage` |

Forward relationships remain the integrity source of truth. Customer/Journey reverse IDs are query pointers maintained automatically; they do not copy document data.

## 3. Cardinality

- Journey -> Booking: 1:1 for a Journey participating in UC03 Booking/Delivery/Payment processing.
- Journey -> Delivery: 0..1:1.
- Booking -> Delivery: 0..1:1.
- Journey/Booking -> Payments: 1:N.
- Delivery -> Payments: 0..N only for payments explicitly marked `DELIVERY`.

A generic Journey by itself does **not** receive a Booking row. A Booking is created when the UC03 `BOOKING` stage is created, or lazily when a backwards-compatible Delivery/Payment write proves that the Journey participates in this sale lifecycle. This prevents unrelated Journey use cases from being changed by UC03 linkage.

## 4. Payments and multiple receipts

Payments are not a second top-level Journey. They are a one-to-many sub-process of the Journey.

Each actual receipt/payment has its own `payment_id`. Multiple evidence documents may support the same payment and must not be counted as multiple payments merely because several documents exist.

`payment_stage` is intentionally conservative:

- `UNSPECIFIED` — the source/process has not proven the stage. This is the default and is used for legacy receipts.
- `BOOKING` — explicitly associated with the Booking stage; `delivery_id` must be null.
- `DELIVERY` — explicitly associated with Delivery; `delivery_id` must identify the Delivery for the same Journey.

Audit Core never guesses a legacy payment into Booking or Delivery merely because a Delivery currently exists.

## 5. Customer identity and names

Customer names remain separate facts:

- `customers.display_name` = **PC-entered name**. Document extraction never overwrites it.
- `customers.legal_name` = reviewed/verified identity name. PAN/Aadhaar are the authoritative sources under the existing identity rule.
- Booking Form `customer_name` = genuine document evidence used for comparison; it does not overwrite Entered Name or Legal Name.

Relationship evidence is also separated by source in DI:

- PAN: `pan_father_name`, `pan_relationship_type`, `pan_relationship_name`
- Aadhaar: `aadhaar_relationship_type`, `aadhaar_relationship_name`

Audit Core stores only the reviewed/resolved customer relationship as:

- `customers.relationship_type` = `S/O`, `W/O`, or `D/O`
- `customers.relationship_name`

The relationship marker is never inferred from an unlabeled father/spouse name, gender, address, or surname. Source-specific values and confidence remain in DI; `journey_attribute_resolutions` records accepted provenance.

## 6. Booking Form field completion

The completed DI Booking Form fields are mapped explicitly in Audit Core. No fuzzy field matching is introduced.

Typed business owners are used only where already justified, including customer email, registration type/by, insurance by, exchange applicability/value, and expected-delivery fields.

Commercial fields — Ex-Showroom, TCS, registration charges, road tax, insurance amount, RSA, accessories, additional warranty, total, discount/bonus, net amount, Booking amount, balance and similar values — are supported audit attributes, but their raw machine values remain in DI. Audit Core records source resolution/provenance rather than duplicating every extracted commercial value. Future reconciliation rules can consume those DI facts against invoices, payments and masters.

## 7. Automatic consistency rules

Database functions/triggers enforce:

1. A UC03 `BOOKING` stage creates one Booking row for its Journey.
2. A Booking synchronizes `journeys.booking_id` and Customer reverse `journey_id` / `booking_id`.
3. Delivery derives/validates `booking_id` from the same Journey and synchronizes `journeys.delivery_id`.
4. Every Payment derives/validates the same Journey's `booking_id`.
5. A Delivery-stage Payment must reference that Journey's Delivery.
6. An `UNSPECIFIED` or Booking-stage Payment cannot carry a Delivery ID.
7. Cross-Journey Booking/Delivery references are rejected.

## 8. Rollback

The changes are ordinary Alembic migrations with downgrades:

- 0043 removes the scoped UC03 stage trigger.
- 0042 removes reviewed relationship columns.
- 0041 removes linkage triggers, constraints, indexes and added linkage columns.

DI raw evidence is never deleted or rewritten by these rollbacks.

## 9. Release rule

This document describes the implemented branch state. It must not be described as DEV-deployed until fresh migration/tests, merge to `dev`, Railway deployment, and deployment smoke evidence are green.
