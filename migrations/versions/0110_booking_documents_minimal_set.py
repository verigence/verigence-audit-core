"""Booking's document set is KYC + Booking Form + Payment Receipt only;
everything else defaults to Delivery.

Revision ID: 0110_booking_documents_minimal_set
Revises: 0109_customer_kyc_booking_stage
Create Date: 2026-09-24

Direct, explicitly repeated user correction: "Booking has only few
documents -- KYC (Aadhar & PAN), Booking Form, Payment Receipts (upto
minimum booking amount) all other documents are by default in Delivery."
``customer_kyc``/``pan_card``/``aadhaar``/``booking_docket``/
``booking_payment_receipt`` already match this (the first four unchanged;
customer_kyc was migration 0109's own, separate, deliberate correction) --
not touched here. Two other categories didn't match and are fixed below.

1. Real catalog-backed items that only ever had a Booking key --
   ``gst_certificate``, ``trade_in_vehicle_rc``, ``trade_in_transfer_letter``,
   ``trade_in_authorization_letter`` -- moved BOOKING -> DELIVERY, mirroring
   0109's own "move customer_kyc" precedent exactly (immutable, PUBLISHED
   ``document_requirement_items`` can't be UPDATEd, so this fixes every
   already-instantiated journey's own mutable ``journey_document_requirements``
   row, the ``document_capture_v2_requirement_policy`` label row, and
   ``ensure_uc03_default_document_profile`` for brand-new tenants).

   UNLIKE 0109 (which explicitly left "a brand new journey for an existing
   tenant still seeds the old way" as a known, accepted gap), this migration
   closes that gap too: ``initialize_uc03_booking_requirements()`` now
   force-outputs ``process_area='DELIVERY'`` for these four keys even while
   an existing tenant's own catalog row still says 'BOOKING'. Closing it this
   time because, unlike customer_kyc's one-off historical correction, every
   existing tenant creates new journeys continuously -- leaving the gap open
   would mean this exact user complaint recurs on literally the next booking
   tested.

   ``corporate_id`` is a ``document_capture_v2_requirement_policy``-only
   "extension" row (migration 0036) -- never backed by a
   ``document_requirement_items`` catalog row, never materialized into
   ``journey_document_requirements`` at all. A single UPDATE to its policy
   row's ``process_area`` is both necessary and sufficient; it takes effect
   for every journey (old and new) the moment the checklist is next read.

2. The three items that already had BOTH a Booking and a Delivery
   requirement_key (migrations 0073/0078) -- ``credit_note``,
   ``gst_declaration``, ``scrappage_certificate`` -- retire the redundant
   Booking-side one, exactly like migration 0091 retired
   ``booking_bank_statement``: deactivate its policy row, and remove its
   unconditional INSERT from ``initialize_uc03_booking_requirements()``.

   UNLIKE 0091 (which deliberately left existing journeys' stale rows in
   place since nobody was looking at that specific duplicate), this
   migration also deletes each existing journey's stale ``booking_*`` row
   for these three keys where no document is currently linked to it --
   this exact duplicate (both a BOOKING and a DELIVERY card for the same
   document) is what triggered this user complaint on a live journey.
   Never deletes a row that has an active linked document.

Companion code change, same PR (``uc03_booking_confirmation_rules.py``):
the Booking-Form-confirmation discount-evidence check for corporate_id and
trade_in_vehicle_rc's document types used to raise its finding
unconditionally the moment the Booking Form itself confirms -- fine when
those documents were expected at Booking time, but a guaranteed, standing,
HIGH-severity finding on every corporate/exchange booking from day one if
they are not expected until Delivery. Gated to only raise once Delivery has
actually started; resolving an already-open finding the moment evidence
appears is untouched and still fires regardless of stage. Scrappage's own
discount-evidence check is untouched -- its document type was already
uploadable/dispatchable at either stage, unaffected by this migration.
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0110_booking_documents_minimal_set"
down_revision = "0109_customer_kyc_booking_stage"
branch_labels = None
depends_on = None

# Real catalog-backed items, Booking-only until now.
_FLIP_TO_DELIVERY = (
    "gst_certificate",
    "trade_in_vehicle_rc",
    "trade_in_transfer_letter",
    "trade_in_authorization_letter",
)
# Policy-only extension row, never catalog-backed.
_EXTENSION_FLIP_TO_DELIVERY = "corporate_id"
# Dual-key items: retire the Booking-side key, keep the Delivery-side one.
_RETIRE_BOOKING_KEYS = (
    "booking_credit_note",
    "booking_gst_declaration",
    "booking_scrappage_certificate",
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1a. Existing journeys: move the four real catalog-backed items in place.
    bind.execute(
        text(
            """
            UPDATE auditcore.journey_document_requirements
            SET process_area = 'DELIVERY'
            WHERE requirement_key = ANY(:keys)
              AND upper(process_area) = 'BOOKING'
            """
        ),
        {"keys": list(_FLIP_TO_DELIVERY)},
    )

    # 1b. Presentation metadata for those four, plus the policy-only
    # corporate_id extension -- a single-row UPDATE per key, since
    # document_capture_v2_requirement_policy's primary key is requirement_key
    # alone (one row per key, never one-per-stage).
    bind.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET process_area = 'DELIVERY', updated_at_utc = now()
            WHERE requirement_key = ANY(:keys)
            """
        ),
        {"keys": [*_FLIP_TO_DELIVERY, _EXTENSION_FLIP_TO_DELIVERY]},
    )

    # 1c. ensure_uc03_default_document_profile (0022 -> 0109): byte-identical
    # to 0109's body except the four items' process_area flipped to
    # 'DELIVERY', so a brand new tenant gets this right from day one.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.ensure_uc03_default_document_profile(
            p_tenant_id varchar,
            p_effective_from date
        ) RETURNS uuid
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_profile_id uuid;
            v_version_id uuid;
            v_version_no integer;
        BEGIN
            INSERT INTO auditcore.document_requirement_profiles (
                tenant_id, profile_code, profile_name, created_by_actor_id
            ) VALUES (
                p_tenant_id,
                'UC03_DEFAULT_VEHICLE_SALES',
                'Default Booking & Delivery Documents',
                'system.uc03-default-documents'
            )
            ON CONFLICT (tenant_id, profile_code) DO NOTHING;

            SELECT document_requirement_profile_id
            INTO v_profile_id
            FROM auditcore.document_requirement_profiles
            WHERE tenant_id=p_tenant_id
              AND profile_code='UC03_DEFAULT_VEHICLE_SALES';

            SELECT v.document_requirement_profile_version_id
            INTO v_version_id
            FROM auditcore.document_requirement_profile_versions v
            WHERE v.tenant_id=p_tenant_id
              AND v.document_requirement_profile_id=v_profile_id
              AND v.lifecycle_status='PUBLISHED'
              AND EXISTS (
                  SELECT 1 FROM auditcore.document_requirement_items i
                  WHERE i.tenant_id=v.tenant_id
                    AND i.document_requirement_profile_version_id=
                        v.document_requirement_profile_version_id
                    AND upper(i.process_area)='BOOKING'
              )
              AND EXISTS (
                  SELECT 1 FROM auditcore.document_requirement_items i
                  WHERE i.tenant_id=v.tenant_id
                    AND i.document_requirement_profile_version_id=
                        v.document_requirement_profile_version_id
                    AND upper(i.process_area)='DELIVERY'
              )
            ORDER BY v.effective_from DESC, v.version_no DESC
            LIMIT 1;

            IF v_version_id IS NOT NULL THEN
                RETURN v_version_id;
            END IF;

            SELECT COALESCE(max(version_no), 0) + 1
            INTO v_version_no
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id=p_tenant_id
              AND document_requirement_profile_id=v_profile_id;

            INSERT INTO auditcore.document_requirement_profile_versions (
                tenant_id, document_requirement_profile_id, version_no,
                effective_from, lifecycle_status, created_by_actor_id
            ) VALUES (
                p_tenant_id, v_profile_id, v_version_no,
                p_effective_from, 'DRAFT', 'system.uc03-default-documents'
            )
            RETURNING document_requirement_profile_version_id INTO v_version_id;

            INSERT INTO auditcore.document_requirement_items (
                tenant_id, document_requirement_profile_version_id,
                requirement_key, document_type_key, process_area,
                requirement_level, condition_config, sort_order
            )
            SELECT p_tenant_id, v_version_id,
                   x.requirement_key, x.document_type_key, x.process_area,
                   x.requirement_level, x.condition_config::jsonb, x.sort_order
            FROM (VALUES
                ('booking_docket', 'booking_docket', 'BOOKING', 'REQUIRED', '{}', 10),
                ('pan_card', 'pan_card', 'BOOKING', 'OPTIONAL', '{}', 20),
                ('aadhaar', 'aadhaar', 'BOOKING', 'OPTIONAL', '{}', 30),
                ('booking_payment_receipt', 'dealer_receipt', 'BOOKING', 'REQUIRED', '{}', 40),
                ('gst_certificate', 'gst_certificate', 'DELIVERY', 'CONDITIONAL', '{"conditionKey":"corporateCustomer"}', 50),
                ('trade_in_vehicle_rc', 'vehicle_rc', 'DELIVERY', 'CONDITIONAL', '{"conditionKey":"exchangeTaken"}', 60),
                ('trade_in_transfer_letter', 'transfer_letter', 'DELIVERY', 'OPTIONAL', '{}', 70),
                ('trade_in_authorization_letter', 'authorization_letter', 'DELIVERY', 'OPTIONAL', '{}', 80),
                ('customer_kyc', 'customer_kyc', 'BOOKING', 'REQUIRED', '{}', 90),
                ('wholesale_invoice', 'wholesale_invoice', 'DELIVERY', 'REQUIRED', '{}', 110),
                ('customer_invoice_dms', 'customer_invoice_dms', 'DELIVERY', 'REQUIRED', '{}', 120),
                ('tax_invoice_tally', 'tax_invoice_tally', 'DELIVERY', 'REQUIRED', '{}', 130),
                ('insurance_cover_note', 'insurance_cover', 'DELIVERY', 'REQUIRED', '{}', 140),
                ('accessory_invoice_dms', 'accessory_invoice_dms', 'DELIVERY', 'REQUIRED', '{}', 150),
                ('accessory_invoice_tally', 'accessory_invoice_tally', 'DELIVERY', 'REQUIRED', '{}', 160),
                ('rto_challan', 'rto_challan', 'DELIVERY', 'REQUIRED', '{}', 170),
                ('customer_ledger', 'customer_ledger', 'DELIVERY', 'REQUIRED', '{}', 180),
                ('cost_sheet', 'cost_sheet', 'DELIVERY', 'REQUIRED', '{}', 190),
                ('gate_pass', 'gate_pass', 'DELIVERY', 'REQUIRED', '{}', 200),
                ('ew_invoice', 'ew_invoice', 'DELIVERY', 'REQUIRED', '{}', 220),
                ('rsa_invoice', 'rsa_invoice', 'DELIVERY', 'REQUIRED', '{}', 230),
                ('value_added_service_document', 'value_added_service_document', 'DELIVERY', 'OPTIONAL', '{}', 240),
                ('no_dues_certificate', 'no_dues_certificate', 'DELIVERY', 'REQUIRED', '{}', 250),
                ('payment_receipt', 'payment_receipt', 'DELIVERY', 'REQUIRED', '{}', 260)
            ) AS x(requirement_key, document_type_key, process_area,
                   requirement_level, condition_config, sort_order);

            UPDATE auditcore.document_requirement_profile_versions
            SET lifecycle_status='PUBLISHED',
                published_by_actor_id='system.uc03-default-documents',
                published_at_utc=now(),
                updated_at_utc=now()
            WHERE tenant_id=p_tenant_id
              AND document_requirement_profile_version_id=v_version_id
              AND lifecycle_status='DRAFT';

            RETURN v_version_id;
        END;
        $$
        """
    )

    # 2a. Deactivate the three redundant Booking-side policy rows.
    bind.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET is_active=false, updated_at_utc=now()
            WHERE requirement_key = ANY(:keys)
            """
        ),
        {"keys": list(_RETIRE_BOOKING_KEYS)},
    )

    # 2b. initialize_uc03_booking_requirements(): starting from 0091's full
    # body (the current one), with the three retired-key INSERT blocks
    # removed, and a CASE added so the four flipped keys always land as
    # DELIVERY even for an existing tenant whose own (immutable, PUBLISHED)
    # catalog row still says BOOKING. That CASE is a no-op for a brand new
    # tenant provisioned after this migration: their catalog row already
    # says DELIVERY (step 1c above), so this function's own WHERE clause
    # (upper(dri.process_area)='BOOKING') never selects it here at all --
    # initialize_uc03_delivery_requirements() picks it up instead, unchanged.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.initialize_uc03_booking_requirements()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.stage_code <> 'BOOKING' THEN
                RETURN NEW;
            END IF;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            )
            SELECT
                j.tenant_id,
                j.journey_id,
                dri.document_requirement_item_id,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'booking_payment_receipt'
                    ELSE dri.requirement_key
                END,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'dealer_receipt'
                    ELSE dri.document_type_key
                END,
                CASE
                    WHEN dri.requirement_key IN (
                        'gst_certificate', 'trade_in_vehicle_rc',
                        'trade_in_transfer_letter', 'trade_in_authorization_letter'
                    ) THEN 'DELIVERY'
                    ELSE dri.process_area
                END,
                CASE
                    WHEN dri.requirement_key IN ('pan_card','aadhaar')
                        THEN 'OPTIONAL'
                    ELSE dri.requirement_level
                END,
                'PENDING',
                dri.condition_config
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=j.tenant_id
             AND dri.document_requirement_profile_version_id=
                    j.document_requirement_profile_version_id
            WHERE j.tenant_id=NEW.tenant_id
              AND j.journey_id=NEW.journey_id
              AND upper(dri.process_area)='BOOKING'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            RETURN NEW;
        END;
        $$
        """
    )

    # 2c. Delete each existing journey's stale Booking-side row for the
    # three retired keys, but only where no document is currently linked to
    # it (safe: linkage is a plain string match on requirement_key, not a
    # foreign key, so this is exactly the same signal _linked_documents
    # itself uses to decide a requirement has real evidence).
    bind.execute(
        text(
            """
            DELETE FROM auditcore.journey_document_requirements jdr
            WHERE jdr.requirement_key = ANY(:keys)
              AND NOT EXISTS (
                  SELECT 1 FROM auditcore.document_capture_v2_documents d
                  WHERE d.tenant_id=jdr.tenant_id
                    AND d.journey_id=jdr.journey_id
                    AND d.requirement_key=jdr.requirement_key
                    AND d.capture_status <> 'SUPERSEDED'
              )
            """
        ),
        {"keys": list(_RETIRE_BOOKING_KEYS)},
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse 1a/1b. The four flipped catalog-backed items + corporate_id.
    bind.execute(
        text(
            """
            UPDATE auditcore.journey_document_requirements
            SET process_area = 'BOOKING'
            WHERE requirement_key = ANY(:keys)
              AND upper(process_area) = 'DELIVERY'
            """
        ),
        {"keys": list(_FLIP_TO_DELIVERY)},
    )
    bind.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET process_area = 'BOOKING', updated_at_utc = now()
            WHERE requirement_key = ANY(:keys)
            """
        ),
        {"keys": [*_FLIP_TO_DELIVERY, _EXTENSION_FLIP_TO_DELIVERY]},
    )

    # Reverse 2a.
    bind.execute(
        text(
            """
            UPDATE auditcore.document_capture_v2_requirement_policy
            SET is_active=true, updated_at_utc=now()
            WHERE requirement_key = ANY(:keys)
            """
        ),
        {"keys": list(_RETIRE_BOOKING_KEYS)},
    )

    # Reverse 1c: restore 0109's exact prior function body.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION auditcore.ensure_uc03_default_document_profile(
            p_tenant_id varchar,
            p_effective_from date
        ) RETURNS uuid
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_profile_id uuid;
            v_version_id uuid;
            v_version_no integer;
        BEGIN
            INSERT INTO auditcore.document_requirement_profiles (
                tenant_id, profile_code, profile_name, created_by_actor_id
            ) VALUES (
                p_tenant_id,
                'UC03_DEFAULT_VEHICLE_SALES',
                'Default Booking & Delivery Documents',
                'system.uc03-default-documents'
            )
            ON CONFLICT (tenant_id, profile_code) DO NOTHING;

            SELECT document_requirement_profile_id
            INTO v_profile_id
            FROM auditcore.document_requirement_profiles
            WHERE tenant_id=p_tenant_id
              AND profile_code='UC03_DEFAULT_VEHICLE_SALES';

            SELECT v.document_requirement_profile_version_id
            INTO v_version_id
            FROM auditcore.document_requirement_profile_versions v
            WHERE v.tenant_id=p_tenant_id
              AND v.document_requirement_profile_id=v_profile_id
              AND v.lifecycle_status='PUBLISHED'
              AND EXISTS (
                  SELECT 1 FROM auditcore.document_requirement_items i
                  WHERE i.tenant_id=v.tenant_id
                    AND i.document_requirement_profile_version_id=
                        v.document_requirement_profile_version_id
                    AND upper(i.process_area)='BOOKING'
              )
              AND EXISTS (
                  SELECT 1 FROM auditcore.document_requirement_items i
                  WHERE i.tenant_id=v.tenant_id
                    AND i.document_requirement_profile_version_id=
                        v.document_requirement_profile_version_id
                    AND upper(i.process_area)='DELIVERY'
              )
            ORDER BY v.effective_from DESC, v.version_no DESC
            LIMIT 1;

            IF v_version_id IS NOT NULL THEN
                RETURN v_version_id;
            END IF;

            SELECT COALESCE(max(version_no), 0) + 1
            INTO v_version_no
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id=p_tenant_id
              AND document_requirement_profile_id=v_profile_id;

            INSERT INTO auditcore.document_requirement_profile_versions (
                tenant_id, document_requirement_profile_id, version_no,
                effective_from, lifecycle_status, created_by_actor_id
            ) VALUES (
                p_tenant_id, v_profile_id, v_version_no,
                p_effective_from, 'DRAFT', 'system.uc03-default-documents'
            )
            RETURNING document_requirement_profile_version_id INTO v_version_id;

            INSERT INTO auditcore.document_requirement_items (
                tenant_id, document_requirement_profile_version_id,
                requirement_key, document_type_key, process_area,
                requirement_level, condition_config, sort_order
            )
            SELECT p_tenant_id, v_version_id,
                   x.requirement_key, x.document_type_key, x.process_area,
                   x.requirement_level, x.condition_config::jsonb, x.sort_order
            FROM (VALUES
                ('booking_docket', 'booking_docket', 'BOOKING', 'REQUIRED', '{}', 10),
                ('pan_card', 'pan_card', 'BOOKING', 'OPTIONAL', '{}', 20),
                ('aadhaar', 'aadhaar', 'BOOKING', 'OPTIONAL', '{}', 30),
                ('booking_payment_receipt', 'dealer_receipt', 'BOOKING', 'REQUIRED', '{}', 40),
                ('gst_certificate', 'gst_certificate', 'BOOKING', 'CONDITIONAL', '{"conditionKey":"corporateCustomer"}', 50),
                ('trade_in_vehicle_rc', 'vehicle_rc', 'BOOKING', 'CONDITIONAL', '{"conditionKey":"exchangeTaken"}', 60),
                ('trade_in_transfer_letter', 'transfer_letter', 'BOOKING', 'OPTIONAL', '{}', 70),
                ('trade_in_authorization_letter', 'authorization_letter', 'BOOKING', 'OPTIONAL', '{}', 80),
                ('customer_kyc', 'customer_kyc', 'BOOKING', 'REQUIRED', '{}', 90),
                ('wholesale_invoice', 'wholesale_invoice', 'DELIVERY', 'REQUIRED', '{}', 110),
                ('customer_invoice_dms', 'customer_invoice_dms', 'DELIVERY', 'REQUIRED', '{}', 120),
                ('tax_invoice_tally', 'tax_invoice_tally', 'DELIVERY', 'REQUIRED', '{}', 130),
                ('insurance_cover_note', 'insurance_cover', 'DELIVERY', 'REQUIRED', '{}', 140),
                ('accessory_invoice_dms', 'accessory_invoice_dms', 'DELIVERY', 'REQUIRED', '{}', 150),
                ('accessory_invoice_tally', 'accessory_invoice_tally', 'DELIVERY', 'REQUIRED', '{}', 160),
                ('rto_challan', 'rto_challan', 'DELIVERY', 'REQUIRED', '{}', 170),
                ('customer_ledger', 'customer_ledger', 'DELIVERY', 'REQUIRED', '{}', 180),
                ('cost_sheet', 'cost_sheet', 'DELIVERY', 'REQUIRED', '{}', 190),
                ('gate_pass', 'gate_pass', 'DELIVERY', 'REQUIRED', '{}', 200),
                ('ew_invoice', 'ew_invoice', 'DELIVERY', 'REQUIRED', '{}', 220),
                ('rsa_invoice', 'rsa_invoice', 'DELIVERY', 'REQUIRED', '{}', 230),
                ('value_added_service_document', 'value_added_service_document', 'DELIVERY', 'OPTIONAL', '{}', 240),
                ('no_dues_certificate', 'no_dues_certificate', 'DELIVERY', 'REQUIRED', '{}', 250),
                ('payment_receipt', 'payment_receipt', 'DELIVERY', 'REQUIRED', '{}', 260)
            ) AS x(requirement_key, document_type_key, process_area,
                   requirement_level, condition_config, sort_order);

            UPDATE auditcore.document_requirement_profile_versions
            SET lifecycle_status='PUBLISHED',
                published_by_actor_id='system.uc03-default-documents',
                published_at_utc=now(),
                updated_at_utc=now()
            WHERE tenant_id=p_tenant_id
              AND document_requirement_profile_version_id=v_version_id
              AND lifecycle_status='DRAFT';

            RETURN v_version_id;
        END;
        $$
        """
    )

    # Reverse 2b: restore 0091's exact prior function body (this migration's
    # own upgrade() started from it).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auditcore.initialize_uc03_booking_requirements()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.stage_code <> 'BOOKING' THEN
                RETURN NEW;
            END IF;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            )
            SELECT
                j.tenant_id,
                j.journey_id,
                dri.document_requirement_item_id,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'booking_payment_receipt'
                    ELSE dri.requirement_key
                END,
                CASE
                    WHEN dri.requirement_key='minimum_booking_payment_proof'
                        THEN 'dealer_receipt'
                    ELSE dri.document_type_key
                END,
                dri.process_area,
                CASE
                    WHEN dri.requirement_key IN ('pan_card','aadhaar')
                        THEN 'OPTIONAL'
                    ELSE dri.requirement_level
                END,
                'PENDING',
                dri.condition_config
            FROM auditcore.journeys j
            JOIN auditcore.document_requirement_items dri
              ON dri.tenant_id=j.tenant_id
             AND dri.document_requirement_profile_version_id=
                    j.document_requirement_profile_version_id
            WHERE j.tenant_id=NEW.tenant_id
              AND j.journey_id=NEW.journey_id
              AND upper(dri.process_area)='BOOKING'
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'booking_credit_note', 'credit_note', 'BOOKING',
                 'OPTIONAL', 'PENDING', '{}'::jsonb),
                (NEW.tenant_id, NEW.journey_id, NULL,
                 'booking_gst_declaration', 'gst_declaration', 'BOOKING',
                 'OPTIONAL', 'PENDING', '{}'::jsonb)
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            INSERT INTO auditcore.journey_document_requirements (
                tenant_id, journey_id, document_requirement_item_id,
                requirement_key, document_type_key, process_area,
                requirement_level, requirement_status, condition_snapshot
            ) VALUES (
                NEW.tenant_id, NEW.journey_id, NULL,
                'booking_scrappage_certificate', 'scrappage_certificate_of_deposit', 'BOOKING',
                'OPTIONAL', 'PENDING', '{}'::jsonb
            )
            ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING;

            RETURN NEW;
        END;
        $$
        """
    )
    # 2c's DELETE is not reversible -- matching 0025/0070/0091 precedent,
    # mutable per-journey operational rows removed during upgrade() are not
    # resurrected on downgrade.
