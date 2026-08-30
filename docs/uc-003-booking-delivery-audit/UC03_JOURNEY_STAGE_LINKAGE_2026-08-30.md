# UC03 Journey / Booking / Delivery Linkage

**Date:** 2026-08-30  
**Status:** Implementation baseline  
**Migration file:** `0040_uc03_journey_stage_linkage.py`  
**Alembic revision:** `0041_uc03_stage_linkage` (after `0040_uc03_booking_fields`)

## 1. Decision

`Journey` remains the lifecycle root. `Booking`, `Delivery`, `Customer` and `Payment` are linked explicitly so operational queries do not have to rediscover the same relationships through repeated joins.

The implementation remains lightweight: forward foreign keys are the integrity source of truth; reverse identifiers are query pointers maintained by database triggers. This avoids creating cyclic delete dependencies.

## 2. Linkage model

| Record | Explicit identifiers |
| --- | --- |
| Customer | `customer_id`, reverse `journey_id`, reverse `booking_id` |
| Journey | `journey_id`, `customer_id`, reverse `booking_id`, reverse `delivery_id` |
| Booking | `booking_id`, `journey_id` |
| Delivery | `delivery_id`, `journey_id`, `booking_id` |
| Payment | `payment_id`, `journey_id`, `booking_id`, optional `delivery_id`, `payment_stage` |

Cardinality:

- Journey -> Booking: 1:1.
- Journey -> Delivery: 0..1:1.
- Booking -> Delivery: 0..1:1.
- Journey/Booking -> Payments: 1:N.
- Delivery -> Payments: 0..N for `payment_stage = 'DELIVERY'`.

Every new Journey automatically receives a lightweight Booking row. This creates the Booking ID at Journey creation without inventing Booking business values.

## 3. Payment stage semantics

`payments.payment_stage` is constrained to:

- `BOOKING` — `delivery_id` must be null.
- `DELIVERY` — `delivery_id` must identify the Delivery belonging to the same Journey.

`booking_id` is always derived from the Journey and cannot point to a Booking belonging to another Journey.

## 4. Customer name semantics

The linkage change does not collapse customer names.

- `customers.display_name` = **PC-entered name**. It remains immutable after Journey creation.
- `customers.legal_name` = **verified document identity name**. PAN/Aadhaar are the authoritative identity sources.
- Booking Form `customer_name` remains a genuine document-extracted fact and is retained for comparison, but it does not overwrite either `display_name` or `legal_name`.
- PAN `pan_name` and Aadhaar `aadhaar_name` remain source-specific evidence. When validated, they can establish/update Legal Name under the existing identity rules.

Therefore the UI/audit layer can always show both the name typed by the PC and the name(s) genuinely read from documents.

## 5. Automatic synchronization

Database triggers maintain the following invariants:

1. Inserting a Journey ensures one Booking row exists.
2. Inserting/updating the Booking populates `journeys.booking_id` and the Customer reverse `journey_id` / `booking_id` pointers.
3. Inserting/updating Delivery derives `delivery.booking_id` from the Journey and populates `journeys.delivery_id`.
4. Inserting/updating Payment derives and validates `booking_id`; Delivery-stage payments also validate/derive `delivery_id`.
5. Cross-Journey Booking/Delivery references are rejected.

For legacy/shared Customer rows, the reverse Customer pointers identify the latest Journey; all authoritative Journey -> Customer history remains intact.

## 6. Data ownership

No DI extraction payload is duplicated into Audit Core merely to support linkage.

- DI owns raw documents, extracted machine values, confidence and provenance.
- Audit Core owns Journey/business lifecycle, reviewed business values, identity resolution and audit logic.
- Reverse IDs are identifiers only; they do not duplicate document content.

## 7. Rollout status

This document records the intended implemented state on `feature/uc03-journey-stage-linkage`. A change must not be described as deployed until CI, migration execution, merge and DEV deployment evidence are all successful.
