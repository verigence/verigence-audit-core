from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core import uc03_confidence_review_policy as confidence_policy
from audit_core.di_client import DiDocument, DiFact


class _FakeSecurityClient:
    def get_service_token(self, *, audience: str) -> str:
        return "fake-token"


class _FakeDiClient:
    def __init__(self) -> None:
        self._documents: dict[str, DiDocument] = {}
        self._facts: dict[str, list[DiFact]] = {}

    def add(self, document: DiDocument, facts: list[DiFact]) -> None:
        self._documents[document.document_id] = document
        self._facts[document.document_id] = facts

    def get_audit_document(self, *, document_id: str, **kwargs) -> DiDocument:
        return self._documents[document_id]

    def get_audit_document_facts(self, *, document_id: str, **kwargs) -> list[DiFact]:
        return self._facts[document_id]


def _fact(field_key: str, value: str, confidence: float = 92.0) -> DiFact:
    return DiFact(
        canonical_field_id=field_key, field_key=field_key, value=value,
        value_source="EXTRACTION", confidence_score=confidence, version_no=1,
    )


def _confirmed(document_id, document_type_key: str) -> DiDocument:
    return DiDocument(
        document_id=str(document_id), upload_status="COMPLETE",
        processing_status="COMPLETED", confirmation_status="CONFIRMED",
        document_type_key=document_type_key, verification_state="NOT_VERIFIED",
    )


def test_sku_resolves_on_the_same_sync_call_that_first_introduces_the_model() -> None:
    """Regression: sync_model_resolution used to run BEFORE materialize_machine_
    booking_values in _sync_booking_document, but it reads journey_products.
    model_name_snapshot -- a column only materialize_machine_booking_values
    (_materialize_product) writes. On the Booking Form's OWN sync call (the one
    that first introduces vehicle_model/vehicle_variant), SKU resolution always
    ran one step too early, saw an empty journey_products row, and silently
    skipped ({"skipped": True}, no finding) -- observed live as a booking with
    fully-extracted, high-confidence documents whose SKU/Deal panel never
    resolved, with no error and no flag explaining why. Reproduced directly:
    this failed on the pre-fix ordering (asserted via a git-stash negative
    control) and passes once materialization runs first.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for this integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-skuord-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"SO-CAT-{suffix[:8]}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"SO-OEM-{suffix[:8]}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date)
                VALUES (:t, :pc, 'SO', :o, :cat, CURRENT_DATE - 60)"""),
            {"t": tenant_id, "pc": f"SO-{suffix[:8]}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"SO-D-{suffix[:8]}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"SO-O-{suffix[:8]}"},
        ).scalar_one()
        customer_id = c.execute(
            text("""INSERT INTO auditcore.customers
                (tenant_id, dealer_id, outlet_id, customer_type_code, display_name)
                VALUES (:t, :d, :o, 'INDIVIDUAL', 'C') RETURNING customer_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id},
        ).scalar_one()
        journey_id = c.execute(
            text("""INSERT INTO auditcore.journeys
                (tenant_id, dealer_id, outlet_id, customer_id, journey_reference)
                VALUES (:t, :d, :o, :cu, :r) RETURNING journey_id"""),
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"SO-J-{suffix[:8]}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
        # journeys insert already creates a bookings row via trigger; no manual insert here.
        booking_form_id = uuid4()
        c.execute(
            text("""INSERT INTO auditcore.evidence
                (tenant_id, journey_id, customer_id, di_subject_id, di_document_id,
                 document_type_key, evidence_purpose)
                VALUES (:t, :j, :cu, :s, :d, 'booking_form', 'BOOKING')"""),
            {"t": tenant_id, "j": journey_id, "cu": customer_id, "s": uuid4(), "d": booking_form_id},
        )

        model_id = c.execute(
            text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                 "VALUES (:o, :mc, 'SCORPIO N') RETURNING model_id"),
            {"o": oem_id, "mc": f"SO-M-{suffix[:8]}"},
        ).scalar_one()
        variant_id = c.execute(
            text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                 "VALUES (:m, :vc, 'Z8L') RETURNING variant_id"),
            {"m": model_id, "vc": f"SO-V-{suffix[:8]}"},
        ).scalar_one()
        sku_id = c.execute(
            text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
                 "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"),
            {"o": oem_id, "m": model_id, "v": variant_id, "sc": f"SO-SKU-{suffix[:10]}"},
        ).scalar_one()
        price_list_id = c.execute(
            text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
                 "VALUES (:t, :c, 'OEM') RETURNING price_list_id"),
            {"t": tenant_id, "c": f"SO-PL-{suffix[:8]}"},
        ).scalar_one()
        price_list_version_id = c.execute(
            text("INSERT INTO auditcore.price_list_versions "
                 "(tenant_id, price_list_id, version_no, lifecycle_status, effective_from) "
                 "VALUES (:t, :pl, 1, 'DRAFT', CURRENT_DATE - 45) RETURNING price_list_version_id"),
            {"t": tenant_id, "pl": price_list_id},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.price_list_items "
                 "(tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount) "
                 "VALUES (:t, :plv, :sku, 'EX_SHOWROOM', 1988996)"),
            {"t": tenant_id, "plv": price_list_version_id, "sku": sku_id},
        )
        c.execute(
            text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
                 "WHERE price_list_version_id=:plv"),
            {"plv": price_list_version_id},
        )

    di_client = _FakeDiClient()
    di_client.add(
        _confirmed(booking_form_id, "booking_form"),
        [
            _fact("vehicle_model", "SCORPIO N"),
            _fact("vehicle_variant", "Z8L"),
            _fact("ex_showroom_price", "1988996"),
        ],
    )

    # ONE single sync call for the Booking Form -- the exact scenario that used
    # to silently skip SKU resolution before this fix.
    with engine.begin() as c:
        confidence_policy._sync_booking_document(
            c,
            tenant_id=tenant_id,
            journey_id=journey_id,
            document_id=booking_form_id,
            service_id="di-service",
            security_client=_FakeSecurityClient(),
            di_client=di_client,
            bump_version=True,
        )

    with engine.begin() as c:
        row = c.execute(
            text("SELECT product_sku_id, selection_status FROM auditcore.journey_products "
                 "WHERE tenant_id=:t AND journey_id=:j"),
            {"t": tenant_id, "j": journey_id},
        ).mappings().one()

    assert row["product_sku_id"] == sku_id
    assert row["selection_status"] == "CONFIRMED"
