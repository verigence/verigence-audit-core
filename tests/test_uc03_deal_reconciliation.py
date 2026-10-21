from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from sqlalchemy import create_engine, text

import audit_core.uc03_deal_reconciliation as dr
import audit_core.uc03_invoice_materialization as im


# ── unit: reviewed booking discount fields -> OEM benefit keys ────────────────
def test_actual_discounts_collapse_onto_benefit_keys() -> None:
    """2026-10-20 product owner rules: the booking form's declared totals
    (discount_amount, bonus_amount) already contain the components and are
    never added to them (they used to be: 50,000 total + 30,000 sales read as
    an 80,000 cash discount). MR is its own key, not "Other scheme"."""
    out = dr._actual_discounts_by_benefit(
        {
            "discount_amount": "50000",        # declared total: not a component
            "sales_discount_amount": "30000",
            "consumer_offer_amount": "5000",   # also CASH_DISCOUNT — sums
            "bonus_amount": "25000",           # declared total bonus: not a component
            "exchange_discount_amount": "25000",
            "corporate_discount_amount": "20000",
            "mr_discount_amount": "10000",
            "buffer_discount_amount": "20000",
        }
    )
    assert out == {
        "CASH_DISCOUNT": Decimal(35000),
        "EXCHANGE_BONUS": Decimal(25000),
        "CORPORATE_PRIVILEGE": Decimal(20000),
        "MANAGEMENT_REFERRAL": Decimal(10000),
        "ADDITIONAL_DISCOUNT": Decimal(20000),
    }


def test_actual_discounts_skip_zero_blank_and_missing() -> None:
    out = dr._actual_discounts_by_benefit(
        {"discount_amount": "0", "bonus_amount": "", "loyalty_discount_amount": None}
    )
    assert out == {}


def test_to_decimal_handles_none_and_garbage() -> None:
    assert dr._to_decimal(None) is None
    assert dr._to_decimal("") is None
    assert dr._to_decimal("not-a-number") is None
    assert dr._to_decimal("1234.50") == Decimal("1234.50")


# ── integration ──────────────────────────────────────────────────────────────
@pytest.fixture
def journey():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for deal-reconciliation integration tests")
    engine = create_engine(database_url)
    suffix = uuid4().hex[:10]
    tenant_id = f"tenant-dr-{suffix}"
    with engine.begin() as c:
        category_id = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) "
                 "VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"DR-CAT-{suffix}"},
        ).scalar_one()
        oem_id = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"),
            {"c": f"DR-OEM-{suffix}"},
        ).scalar_one()
        c.execute(
            text("""INSERT INTO auditcore.projects
                (tenant_id, project_code, project_name, oem_id, product_category_id,
                 effective_start_date, timezone_name, project_status)
                VALUES (:t, :pc, 'DR', :o, :cat, CURRENT_DATE - 60, 'Asia/Kolkata', 'ACTIVE')"""),
            {"t": tenant_id, "pc": f"DR-{suffix}", "o": oem_id, "cat": category_id},
        )
        dealer_id = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                 "VALUES (:t, :c, 'D') RETURNING dealer_id"),
            {"t": tenant_id, "c": f"DR-D-{suffix}"},
        ).scalar_one()
        outlet_id = c.execute(
            text("INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name) "
                 "VALUES (:t, :d, :c, 'O') RETURNING outlet_id"),
            {"t": tenant_id, "d": dealer_id, "c": f"DR-O-{suffix}"},
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
            {"t": tenant_id, "d": dealer_id, "o": outlet_id, "cu": customer_id, "r": f"DR-J-{suffix}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) "
                 "VALUES (:t, :j, CURRENT_DATE - 10)"),
            {"t": tenant_id, "j": journey_id},
        )
        c.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_IN_PROGRESS', 'IN_PROGRESS', 'NOT_EVALUATED',
                        now(), now(), 1)"""),
            {"t": tenant_id, "j": journey_id},
        )
    engine.dispose()
    engine = create_engine(database_url)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.tenant_id = tenant_id  # type: ignore[attr-defined]
            c.journey_id = journey_id  # type: ignore[attr-defined]
            c.oem_id = oem_id  # type: ignore[attr-defined]
            c.customer_id = customer_id  # type: ignore[attr-defined]
            yield c
    finally:
        delete_tenant_data(engine, tenant_id)
        engine.dispose()


def _seed_pinned_sku(c, *, model: str, variant: str, components: dict[str, str], pin: bool = True,
                     effective_from=None):
    """One published price-list version + one SKU, optionally pinned on the journey."""
    tenant_id, oem_id = c.tenant_id, c.oem_id
    pl_id = c.execute(
        text("INSERT INTO auditcore.price_lists (tenant_id, price_list_code, price_list_name) "
             "VALUES (:t, :c, 'OEM') RETURNING price_list_id"),
        {"t": tenant_id, "c": f"PL{uuid4().hex[:8]}"},
    ).scalar_one()
    plv_id = c.execute(
        text("INSERT INTO auditcore.price_list_versions "
             "(tenant_id, price_list_id, version_no, lifecycle_status, effective_from) "
             "VALUES (:t, :pl, 1, 'DRAFT', COALESCE(CAST(:eff AS date), CURRENT_DATE - 45)) "
             "RETURNING price_list_version_id"),
        {"t": tenant_id, "pl": pl_id, "eff": effective_from},
    ).scalar_one()
    model_id = c.execute(
        text("INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
             "VALUES (:o, :mc, :mn) RETURNING model_id"),
        {"o": oem_id, "mc": f"M{uuid4().hex[:8]}", "mn": model},
    ).scalar_one()
    variant_id = c.execute(
        text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
             "VALUES (:m, :vc, :vn) RETURNING variant_id"),
        {"m": model_id, "vc": f"V{uuid4().hex[:8]}", "vn": variant},
    ).scalar_one()
    sku_id = c.execute(
        text("INSERT INTO auditcore.product_skus (oem_id, model_id, variant_id, sku_code) "
             "VALUES (:o, :m, :v, :sc) RETURNING product_sku_id"),
        {"o": oem_id, "m": model_id, "v": variant_id, "sc": f"SKU{uuid4().hex[:10]}"},
    ).scalar_one()
    for key, amount in components.items():
        c.execute(
            text("INSERT INTO auditcore.price_list_items "
                 "(tenant_id, price_list_version_id, product_sku_id, component_key, standard_amount) "
                 "VALUES (:t, :plv, :sku, :k, :a)"),
            {"t": tenant_id, "plv": plv_id, "sku": sku_id, "k": key, "a": amount},
        )
    c.execute(
        text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
             "WHERE tenant_id=:t AND price_list_version_id=:plv"),
        {"t": tenant_id, "plv": plv_id},
    )
    c.execute(
        text("INSERT INTO auditcore.journey_products "
             "(tenant_id, journey_id, product_sku_id, model_name_snapshot, variant_name_snapshot, "
             " selection_status, selection_method, selection_source) "
             "VALUES (:t, :j, :sku, :m, :v, :st, 'TEST', 'EVIDENCE')"),
        {"t": tenant_id, "j": c.journey_id, "sku": sku_id if pin else None,
         "m": model, "v": variant, "st": "CONFIRMED" if pin else None},
    )
    return {"model_id": model_id, "variant_id": variant_id, "sku_id": sku_id, "plv_id": plv_id}


def _seed_scheme(c, *, category: str, benefit_key: str, amount: str, model_id, variant_id=None,
                 customer_type_code=None, benefit_type: str = "AMOUNT"):
    tenant_id = c.tenant_id
    scheme_id = c.execute(
        text("INSERT INTO auditcore.discount_schemes "
             "(tenant_id, scheme_code, scheme_name, scheme_category) "
             "VALUES (:t, :c, 'S', :cat) RETURNING discount_scheme_id"),
        {"t": tenant_id, "c": f"SCH{uuid4().hex[:8]}", "cat": category},
    ).scalar_one()
    dsv_id = c.execute(
        text("INSERT INTO auditcore.discount_scheme_versions "
             "(tenant_id, discount_scheme_id, version_no, lifecycle_status, effective_from) "
             "VALUES (:t, :s, 1, 'DRAFT', CURRENT_DATE - 30) RETURNING discount_scheme_version_id"),
        {"t": tenant_id, "s": scheme_id},
    ).scalar_one()
    c.execute(
        text("INSERT INTO auditcore.discount_scheme_benefits "
             "(tenant_id, discount_scheme_version_id, benefit_key, benefit_type, amount_value) "
             "VALUES (:t, :v, :k, :bt, :a)"),
        {"t": tenant_id, "v": dsv_id, "k": benefit_key, "bt": benefit_type, "a": amount},
    )
    c.execute(
        text("INSERT INTO auditcore.discount_scheme_eligibility "
             "(tenant_id, discount_scheme_version_id, model_id, variant_id, customer_type_code) "
             "VALUES (:t, :v, :m, :vr, :ct)"),
        {"t": tenant_id, "v": dsv_id, "m": model_id, "vr": variant_id, "ct": customer_type_code},
    )
    c.execute(
        text("UPDATE auditcore.discount_scheme_versions SET lifecycle_status='PUBLISHED' "
             "WHERE tenant_id=:t AND discount_scheme_version_id=:v"),
        {"t": tenant_id, "v": dsv_id},
    )
    return dsv_id


def _seed_reviewed_booking(c, **fields):
    cols = ["tenant_id", "journey_id", "source_di_document_id", "reviewed_by_actor_id", *fields.keys()]
    vals = [":tenant_id", ":journey_id", ":doc", ":actor", *(f":{k}" for k in fields)]
    params = {"tenant_id": c.tenant_id, "journey_id": c.journey_id,
              "doc": uuid4(), "actor": "tester", **fields}
    c.execute(
        text(f"INSERT INTO auditcore.booking_form_review_values ({', '.join(cols)}) "
             f"VALUES ({', '.join(vals)})"),
        params,
    )


def _details(c, key) -> dict:
    return c.execute(
        text("SELECT details FROM auditcore.discount_applications "
             "WHERE tenant_id=:t AND journey_id=:j AND discount_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": key},
    ).scalar_one()


def _commercial(c, key):
    return c.execute(
        text("SELECT standard_amount FROM auditcore.commercial_lines "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": key},
    ).scalar_one_or_none()


def _discount(c, key):
    return c.execute(
        text("SELECT standard_eligible_amount, actual_discount_amount, eligibility_result, "
             "       actual_source_kind "
             "FROM auditcore.discount_applications "
             "WHERE tenant_id=:t AND journey_id=:j AND discount_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": key},
    ).mappings().one_or_none()


def test_price_standards_materialized_with_registration_basis(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="SCORPIO N", variant="Z8L", components={
        "EX_SHOWROOM": "1600000",
        "INSURANCE": "60000",
        "EXT_WARRANTY_4TH_YR": "12000",
        "EXT_WARRANTY_4TH_5TH_YR": "18000",
        "REGISTRATION_INDIVIDUAL": "170000",
        "REGISTRATION_CORPORATE": "220000",
    })

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("priceLines", 0) >= 4

    assert _commercial(c, "ex_showroom_price") == Decimal(1600000)
    assert _commercial(c, "insurance_amount") == Decimal(60000)
    # Direct user correction (2026-09-23): a customer takes at most one
    # warranty tier, never both -- the two tiers must never be summed
    # (30000 would be the old, wrong, inflated behavior). Both map onto
    # the same commercial line; the later one wins deterministically by
    # component_key order (EXT_WARRANTY_4TH_YR sorts after
    # EXT_WARRANTY_4TH_5TH_YR), not the sum of both.
    assert _commercial(c, "additional_warranty_amount") == Decimal(12000)
    # individual buyer -> individual registration rate, corporate variant not double counted
    assert _commercial(c, "registration_charges") == Decimal(170000)


def test_price_standards_match_warranty_tier_to_the_known_actual_amount(journey) -> None:
    """Direct user instruction (2026-09-23): once an actual warranty amount
    is already known for this journey (invoice already wins over booking
    form there -- see _upsert_commercial_line's own source_priority), the
    standard must be whichever tier's master price matches it, not a
    deterministic guess. Reuses the real invoice-materialization upsert
    (im._upsert_commercial_line) rather than a raw INSERT, so this proves
    the same code path a real invoice sync uses actually feeds this."""
    c = journey
    _seed_pinned_sku(c, model="SCORPIO N", variant="Z8L", components={
        "EX_SHOWROOM": "1600000",
        "EXT_WARRANTY_4TH_YR": "12000",
        "EXT_WARRANTY_4TH_5TH_YR": "18000",
    })
    im._upsert_commercial_line(
        c,
        tenant_id=c.tenant_id,
        journey_id=c.journey_id,
        component_key="additional_warranty_amount",
        amount=Decimal(18000),
        document_type="retail_invoice",
        document_id=uuid4(),
        evidence_id=None,
    )

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("priceLines", 0) >= 2

    assert _commercial(c, "additional_warranty_amount") == Decimal(18000)


def test_price_standards_pick_the_nearest_warranty_tier_when_not_an_exact_match(journey) -> None:
    """Direct user correction (2026-09-23): the actual amount an invoice/
    booking form reports will not always land exactly on the master's own
    paisa-precise price (OCR rounding, a whole-rupee printed total vs a
    fractional master price) -- an exact-match-or-arbitrary-fallback rule
    is too strict and silently picks the wrong tier the moment the actual
    is even ₹1 off. Must always select whichever tier's price is nearest
    the actual, not fall back to something unrelated."""
    c = journey
    _seed_pinned_sku(c, model="SCORPIO N", variant="Z8L", components={
        "EX_SHOWROOM": "1600000",
        "EXT_WARRANTY_4TH_YR": "12000",
        "EXT_WARRANTY_4TH_5TH_YR": "18000.62",
    })
    im._upsert_commercial_line(
        c,
        tenant_id=c.tenant_id,
        journey_id=c.journey_id,
        component_key="additional_warranty_amount",
        amount=Decimal(18001),  # 38 paise off the 4TH_5TH_YR master price, nowhere near 4TH_YR's 12000
        document_type="retail_invoice",
        document_id=uuid4(),
        evidence_id=None,
    )

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("priceLines", 0) >= 2

    assert _commercial(c, "additional_warranty_amount") == Decimal("18000.62")


def test_registration_basis_corporate_picks_corporate_rate(journey) -> None:
    c = journey
    c.execute(
        text("UPDATE auditcore.customers SET customer_type_code='CORPORATE' WHERE customer_id=:id"),
        {"id": c.customer_id},
    )
    _seed_pinned_sku(c, model="XUV700", variant="AX7", components={
        "EX_SHOWROOM": "2000000",
        "REGISTRATION_INDIVIDUAL": "200000",
        "REGISTRATION_CORPORATE": "260000",
    })

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _commercial(c, "registration_charges") == Decimal(260000)


def test_discount_standard_and_actual_on_one_row(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="THAR", variant="LX", components={"EX_SHOWROOM": "1500000"})
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="20000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, sales_discount_amount="15000")

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("eligible") == 1

    row = _discount(c, "CASH_DISCOUNT")
    assert row is not None
    assert row["standard_eligible_amount"] == Decimal(20000)
    assert row["actual_discount_amount"] == Decimal(15000)
    assert row["eligibility_result"] == "ELIGIBLE"
    assert row["actual_source_kind"] == "CALCULATED"


def test_unclaimed_entitlement_recorded(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="BOLERO", variant="B4", components={"EX_SHOWROOM": "1000000"})
    _seed_scheme(c, category="EXCHANGE", benefit_key="EXCHANGE_BONUS", amount="25000",
                 model_id=seeded["model_id"])
    # no reviewed booking discount values at all

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    # 2026-10-20 product owner rules: an exchange nobody claimed or proved is
    # shown (with what it would be) but is not owed -- no standard.
    assert result.get("informational") == 1

    row = _discount(c, "EXCHANGE_BONUS")
    assert row is not None
    assert row["standard_eligible_amount"] is None
    assert row["actual_discount_amount"] is None
    assert row["eligibility_result"] == "ELIGIBLE_UNCLAIMED"
    assert _details(c, "EXCHANGE_BONUS")["potentialAmount"] == "25000.00"


def test_over_grant_flagged_not_eligible(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV400", variant="EL", components={"EX_SHOWROOM": "1700000"})
    # a corporate discount was given but no corporate scheme makes the customer eligible
    _seed_reviewed_booking(c, corporate_discount_amount="30000")

    # No corporate policy in force on the date at all: not known, never an
    # over-grant (review of PR 472, S1/B1).
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CORPORATE_PRIVILEGE")
    assert row["eligibility_result"] == "SCHEME_NOT_IN_FORCE" and row["standard_eligible_amount"] is None

    # A corporate policy in force that offers this deal nothing (its one
    # scheme withdrawn): an over-grant.
    assert seeded
    _scheme(c, category="CORPORATE", effective_from=date(2000, 1, 1), benefits={}, eligibility=[])
    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("overGranted") == 1

    row = _discount(c, "CORPORATE_PRIVILEGE")
    assert row is not None
    assert row["standard_eligible_amount"] is None
    assert row["actual_discount_amount"] == Decimal(30000)
    assert row["eligibility_result"] == "NOT_ELIGIBLE"


def test_idempotent(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="MARAZZO", variant="M2", components={
        "EX_SHOWROOM": "1400000", "INSURANCE": "50000",
    })
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="10000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, sales_discount_amount="8000")

    for _ in range(3):
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    lines = c.execute(
        text("SELECT count(*) FROM auditcore.commercial_lines WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    apps = c.execute(
        text("SELECT count(*) FROM auditcore.discount_applications WHERE tenant_id=:t AND journey_id=:j"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one()
    assert lines == 2
    assert apps == 1


def _invoice_doc(document_type: str, fields: dict[str, object]):
    return SimpleNamespace(
        documentId=uuid4(),
        evidenceId=None,
        documentTypeKey=document_type,
        extractionState="READY",
        fields=[SimpleNamespace(fieldKey=k, value=v) for k, v in fields.items()],
    )


def _link_evidence(c, document_type_key: str) -> None:
    c.execute(
        text(
            """
            INSERT INTO auditcore.evidence (
                tenant_id, journey_id, customer_id,
                di_subject_id, di_document_id,
                document_type_key, evidence_purpose
            ) VALUES (:t, :j, :cu, :subject, :doc, :doc_type, 'BOOKING')
            """
        ),
        {
            "t": c.tenant_id, "j": c.journey_id, "cu": c.customer_id,
            "subject": uuid4(), "doc": uuid4(), "doc_type": document_type_key,
        },
    )


def _evidence_status(c, discount_key: str) -> str | None:
    return c.execute(
        text("SELECT evidence_status FROM auditcore.discount_applications "
             "WHERE tenant_id=:t AND journey_id=:j AND discount_key=:k"),
        {"t": c.tenant_id, "j": c.journey_id, "k": discount_key},
    ).scalar_one_or_none()


def _open_finding(c, rule_key: str):
    return c.execute(
        text("SELECT audit_finding_id, finding_class, severity, finding_status "
             "FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key=:k "
             "AND finding_status IN ('OPEN', 'ACKNOWLEDGED')"),
        {"t": c.tenant_id, "j": c.journey_id, "k": rule_key},
    ).mappings().one_or_none()


def test_raise_findings_false_skips_evidence_and_variance_checks(journey) -> None:
    """Regression: sync_deal_reconciliation runs unconditionally on every
    Journey Overview page view (via sync_model_resolution's self-heal on
    read) for any journey with a resolved SKU -- the vast majority of
    traffic. Running the evidence-status and total-variance checks (each a
    handful of sequential DB round-trips per discount row) on every single
    page view, not just on the document-sync events that actually change
    these facts, was a measured latency regression. raise_findings=False
    must still materialize standard/actual values -- just skip the two
    finding-raising checks entirely."""
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV700", variant="AX7L", components={"EX_SHOWROOM": "2000000"})
    _seed_scheme(c, category="CORPORATE", benefit_key="CORPORATE_PRIVILEGE", amount="40000",
                 model_id=seeded["model_id"], customer_type_code="CORPORATE")
    c.execute(
        text("UPDATE auditcore.customers SET customer_type_code='CORPORATE' WHERE customer_id=:id"),
        {"id": c.customer_id},
    )
    _seed_reviewed_booking(c, corporate_discount_amount="40000")

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="", raise_findings=False,
    )
    assert "evidenceRowsChecked" not in result
    assert "totals" not in result
    # Standard/actual materialization itself is unaffected.
    row = _discount(c, "CORPORATE_PRIVILEGE")
    assert row is not None
    assert row["actual_discount_amount"] == Decimal(40000)
    # But no evidence_status was computed, and no finding was raised --
    # exactly the expensive part this flag exists to skip.
    assert _evidence_status(c, "CORPORATE_PRIVILEGE") is None
    assert _open_finding(c, "BK_DISCOUNT_EVIDENCE_MISSING:corporate_discount") is None


def test_conditional_discount_without_evidence_is_missing_and_flagged(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV700", variant="AX7L", components={"EX_SHOWROOM": "2000000"})
    _seed_scheme(c, category="CORPORATE", benefit_key="CORPORATE_PRIVILEGE", amount="40000",
                 model_id=seeded["model_id"], customer_type_code="CORPORATE")
    c.execute(
        text("UPDATE auditcore.customers SET customer_type_code='CORPORATE' WHERE customer_id=:id"),
        {"id": c.customer_id},
    )
    _seed_reviewed_booking(c, corporate_discount_amount="40000")

    result = dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result.get("evidenceRowsChecked", 0) >= 1

    assert _evidence_status(c, "CORPORATE_PRIVILEGE") == "MISSING"
    finding = _open_finding(c, "BK_DISCOUNT_EVIDENCE_MISSING:corporate_discount")
    assert finding is not None
    assert finding["finding_class"] == "DOCUMENT_GAP"
    assert finding["severity"] == "HIGH"

    task = c.execute(
        text("SELECT task_type, assigned_role_code FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND journey_id=:j AND related_finding_id=:f"),
        {"t": c.tenant_id, "j": c.journey_id, "f": finding["audit_finding_id"]},
    ).mappings().one()
    assert task["assigned_role_code"] == "PC"


def test_conditional_discount_with_evidence_is_verified(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="THAR", variant="LX", components={"EX_SHOWROOM": "1500000"})
    _seed_scheme(c, category="EXCHANGE", benefit_key="EXCHANGE_BONUS", amount="25000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, exchange_discount_amount="25000")
    _link_evidence(c, "vehicle_rc")

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert _evidence_status(c, "EXCHANGE_BONUS") == "VERIFIED"
    assert _open_finding(c, "BK_DISCOUNT_EVIDENCE_MISSING:exchange_bonus") is None


def test_evidence_finding_resolves_once_document_appears(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="BOLERO", variant="B4", components={"EX_SHOWROOM": "1000000"})
    _seed_scheme(c, category="EXCHANGE", benefit_key="EXCHANGE_BONUS", amount="25000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, exchange_discount_amount="25000")

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _evidence_status(c, "EXCHANGE_BONUS") == "MISSING"
    assert _open_finding(c, "BK_DISCOUNT_EVIDENCE_MISSING:exchange_bonus") is not None

    _link_evidence(c, "vehicle_rc")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert _evidence_status(c, "EXCHANGE_BONUS") == "VERIFIED"
    assert _open_finding(c, "BK_DISCOUNT_EVIDENCE_MISSING:exchange_bonus") is None


def test_unclaimed_conditional_discount_is_pending_not_missing(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="BOLERO NEO", variant="N10", components={"EX_SHOWROOM": "900000"})
    _seed_scheme(c, category="EXCHANGE", benefit_key="EXCHANGE_BONUS", amount="25000",
                 model_id=seeded["model_id"])
    # no reviewed booking discount value at all -- masters-eligible, unclaimed

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert _evidence_status(c, "EXCHANGE_BONUS") == "PENDING"
    assert _open_finding(c, "BK_DISCOUNT_EVIDENCE_MISSING:exchange_bonus") is None


def test_cash_discount_is_not_required(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="MARAZZO", variant="M2", components={"EX_SHOWROOM": "1400000"})
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="10000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, sales_discount_amount="8000")

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert _evidence_status(c, "CASH_DISCOUNT") == "NOT_REQUIRED"


def test_scrappage_amount_now_flows_to_actual_discount(journey) -> None:
    """Regression: scrappage_discount_amount was never mapped at all, so a
    scrappage amount on the Booking Form never reached actual_discount_amount."""
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV400", variant="EL", components={"EX_SHOWROOM": "1700000"})
    _seed_scheme(c, category="SCRAPPAGE", benefit_key="SCRAPPAGE_BONUS_DEALER", amount="20000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, scrappage_discount_amount="20000")

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    row = _discount(c, "SCRAPPAGE_BONUS_DEALER")
    assert row is not None
    assert row["actual_discount_amount"] == Decimal(20000)
    assert _evidence_status(c, "SCRAPPAGE_BONUS_DEALER") == "MISSING"

    _link_evidence(c, "scrappage_certificate_of_deposit")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _evidence_status(c, "SCRAPPAGE_BONUS_DEALER") == "VERIFIED"


def test_invoice_discount_survives_booking_form_reheal(journey) -> None:
    """Regression: once an invoice has won a discount's actual value, a routine
    sync_deal_reconciliation self-heal off the booking form must never regress
    it back — invoice always outranks the booking form (VAC-... invoice-first
    precedence), including on repeat runs, not just the first one."""
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV700", variant="AX7L", components={"EX_SHOWROOM": "2000000"})
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="30000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, sales_discount_amount="15000")

    # Only the booking form is on file so far.
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CASH_DISCOUNT")
    assert row["actual_discount_amount"] == Decimal(15000)

    # A tax invoice arrives reporting a different discount — invoice wins.
    im.materialize_reviewed_invoices(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, actor_id="tester",
        # 2026-10-20 (R1/R2): the invoice discount is read by its printed
        # description (a consumer offer) and shown GST-inclusive.
        documents=[_invoice_doc("tax_invoice_tally", {
            "invoice_purpose": "VEHICLE_SALE",
            "taxable_amount": "2000000",
            "cgst_amount": "180000",
            "sgst_amount": "180000",
            "invoice_discount_amount": "25000",
            "line_items": [{"line_category": "DISCOUNT_LINE", "description_raw": "Consumer Offer",
                            "taxable_amount": "25000"}],
        })],
    )
    row = _discount(c, "CASH_DISCOUNT")
    assert row["actual_discount_amount"] == Decimal(29500)

    # Re-running deal reconciliation (the routine self-heal on every overview
    # read) must not regress the actual back to the booking form's 15000.
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CASH_DISCOUNT")
    assert row["actual_discount_amount"] == Decimal(29500)
    assert row["eligibility_result"] == "ELIGIBLE"

    # A second, redundant self-heal is still a no-op on the invoice's value.
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CASH_DISCOUNT")
    assert row["actual_discount_amount"] == Decimal(29500)


def test_positive_variance_is_not_flagged(journey) -> None:
    """Actual net above standard net (customer effectively paying more, e.g.
    extra options) is informational only -- no flag, per explicit
    instruction."""
    c = journey
    _seed_pinned_sku(c, model="XUV700", variant="AX7L", components={"EX_SHOWROOM": "2000000"})
    # First run materializes the standard commercial line; only then can its
    # actual_amount be set directly to simulate an invoice-given value above
    # standard (extra options) without any discount involved.
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    c.execute(
        text("UPDATE auditcore.commercial_lines SET actual_amount=2100000, source_reference='booking_form:test' "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key='ex_showroom_price'"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    finding = c.execute(
        text("SELECT finding_status FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='DEAL_TOTAL_VARIANCE_NEGATIVE'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one_or_none()
    assert finding is None


def test_partially_reviewed_deal_does_not_false_flag_variance(journey) -> None:
    """Regression: a discount already reviewed while the vehicle price
    itself is not yet must never be compared -- an incomplete actual total
    (missing lines coalescing to zero) would otherwise always look like a
    huge false shortfall against the complete standard total."""
    c = journey
    seeded = _seed_pinned_sku(c, model="THAR", variant="LX", components={"EX_SHOWROOM": "1500000"})
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="20000",
                 model_id=seeded["model_id"])
    _seed_reviewed_booking(c, sales_discount_amount="15000")

    # The discount actual is reviewed, but ex_showroom_price's own actual
    # never was -- the deal is not ready for a total comparison yet.
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    assert c.execute(
        text("SELECT finding_status FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='DEAL_TOTAL_VARIANCE_NEGATIVE'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one_or_none() is None


def test_negative_variance_raises_high_finding_and_tl_task(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="THAR", variant="LX", components={"EX_SHOWROOM": "1500000"})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    c.execute(
        text("UPDATE auditcore.commercial_lines SET actual_amount=1400000, source_reference='booking_form:test' "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key='ex_showroom_price'"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")

    finding = c.execute(
        text("SELECT audit_finding_id, finding_class, severity, finding_status "
             "FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='DEAL_TOTAL_VARIANCE_NEGATIVE'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert finding["finding_class"] == "VIOLATION"
    assert finding["severity"] == "HIGH"
    assert finding["finding_status"] == "OPEN"

    task = c.execute(
        text("SELECT task_type, assigned_role_code FROM auditcore.workflow_tasks "
             "WHERE tenant_id=:t AND journey_id=:j AND related_finding_id=:f"),
        {"t": c.tenant_id, "j": c.journey_id, "f": finding["audit_finding_id"]},
    ).mappings().one()
    assert task["task_type"] == "DEAL_TOTAL_VARIANCE_REVIEW"
    assert task["assigned_role_code"] == "TL"


def test_negative_variance_resolves_once_corrected(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="BOLERO", variant="B4", components={"EX_SHOWROOM": "1000000"})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    c.execute(
        text("UPDATE auditcore.commercial_lines SET actual_amount=900000, source_reference='booking_form:test' "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key='ex_showroom_price'"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert c.execute(
        text("SELECT finding_status FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='DEAL_TOTAL_VARIANCE_NEGATIVE'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == "OPEN"

    c.execute(
        text("UPDATE auditcore.commercial_lines SET actual_amount=1000000, source_reference='booking_form:test' "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key='ex_showroom_price'"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert c.execute(
        text("SELECT finding_status FROM auditcore.audit_findings "
             "WHERE tenant_id=:t AND journey_id=:j AND rule_key='DEAL_TOTAL_VARIANCE_NEGATIVE'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalar_one() == "RESOLVED"


def test_skipped_when_sku_not_pinned(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="THAR ROXX", variant="MX", components={"EX_SHOWROOM": "1600000"},
                     pin=False)

    result = dr.sync_deal_reconciliation(
        c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="",
    )
    assert result.get("skipped") is True


def test_pricing_date_override_prices_the_deal_on_that_dates_master(journey) -> None:
    """Booking date -10 days falls under version 1; a pricing date of
    yesterday falls under version 2, which carries a different price."""
    c = journey
    seeded = _seed_pinned_sku(c, model="THAR", variant="LX", components={"EX_SHOWROOM": "1500000"})
    pl_id = c.execute(
        text("SELECT price_list_id FROM auditcore.price_list_versions WHERE tenant_id=:t AND price_list_version_id=:v"),
        {"t": c.tenant_id, "v": seeded["plv_id"]},
    ).scalar_one()
    v2 = c.execute(
        text("INSERT INTO auditcore.price_list_versions (tenant_id, price_list_id, version_no, lifecycle_status, "
             "effective_from) VALUES (:t, :pl, 2, 'DRAFT', CURRENT_DATE - 3) RETURNING price_list_version_id"),
        {"t": c.tenant_id, "pl": pl_id},
    ).scalar_one()
    c.execute(
        text("INSERT INTO auditcore.price_list_items (tenant_id, price_list_version_id, product_sku_id, "
             "component_key, standard_amount) VALUES (:t, :v, :s, 'EX_SHOWROOM', 1550000)"),
        {"t": c.tenant_id, "v": v2, "s": seeded["sku_id"]},
    )
    c.execute(text("UPDATE auditcore.price_list_versions SET lifecycle_status='PUBLISHED' "
                   "WHERE tenant_id=:t AND price_list_version_id=:v"), {"t": c.tenant_id, "v": v2})

    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _commercial(c, "ex_showroom_price") == Decimal(1500000)

    c.execute(text("UPDATE auditcore.bookings SET pricing_effective_on=CURRENT_DATE - 1 "
                   "WHERE tenant_id=:t AND journey_id=:j"), {"t": c.tenant_id, "j": c.journey_id})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _commercial(c, "ex_showroom_price") == Decimal(1550000)


# ── 2026-10-20 product owner rules: entitlement selection ────────────────────
_SEPT_SCHEMES = date(2026, 9, 3)
_UTKAL_PRICING_DATE = date(2026, 9, 10)


def _variant(c, model_id, name: str):
    return c.execute(
        text("INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
             "VALUES (:m, :vc, :vn) RETURNING variant_id"),
        {"m": model_id, "vc": f"V{uuid4().hex[:8]}", "vn": name},
    ).scalar_one()


def _scheme(c, *, code: str | None = None, category: str, effective_from, benefits: dict[str, str],
            eligibility: list[dict], version_no: int = 1, scheme_id=None, created_at=None, effective_to=None):
    """One published scheme version with its benefits and eligibility rows,
    the way the ingestion stores them (``criteria`` included)."""
    tenant_id = c.tenant_id
    if scheme_id is None:
        scheme_id = c.execute(
            text("INSERT INTO auditcore.discount_schemes (tenant_id, scheme_code, scheme_name, scheme_category) "
                 "VALUES (:t, :c, :c, :cat) RETURNING discount_scheme_id"),
            {"t": tenant_id, "c": code or f"SCH{uuid4().hex[:8]}", "cat": category},
        ).scalar_one()
    dsv_id = c.execute(
        text("INSERT INTO auditcore.discount_scheme_versions "
             "(tenant_id, discount_scheme_id, version_no, lifecycle_status, effective_from, effective_to, "
             " created_at_utc) "
             "VALUES (:t, :s, :n, 'DRAFT', :eff, :to, COALESCE(CAST(:created AS timestamptz), now())) "
             "RETURNING discount_scheme_version_id"),
        {"t": tenant_id, "s": scheme_id, "n": version_no, "eff": effective_from, "to": effective_to,
         "created": created_at},
    ).scalar_one()
    for key, amount in benefits.items():
        c.execute(
            text("INSERT INTO auditcore.discount_scheme_benefits "
                 "(tenant_id, discount_scheme_version_id, benefit_key, benefit_type, amount_value) "
                 "VALUES (:t, :v, :k, 'AMOUNT', :a)"),
            {"t": tenant_id, "v": dsv_id, "k": key, "a": amount},
        )
    for row in eligibility:
        c.execute(
            text("INSERT INTO auditcore.discount_scheme_eligibility "
                 "(tenant_id, discount_scheme_version_id, model_id, variant_id, dealer_id, customer_type_code, "
                 " criteria) VALUES (:t, :v, :m, :vr, :d, :ct, CAST(:cr AS jsonb))"),
            {"t": tenant_id, "v": dsv_id, "m": row["model_id"], "vr": row.get("variant_id"),
             "d": row.get("dealer_id"), "ct": row.get("customer_type_code"),
             "cr": json.dumps(row.get("criteria") or {})},
        )
    c.execute(
        text("UPDATE auditcore.discount_scheme_versions SET lifecycle_status='PUBLISHED' "
             "WHERE tenant_id=:t AND discount_scheme_version_id=:v"),
        {"t": tenant_id, "v": dsv_id},
    )
    return scheme_id, dsv_id


def _price_on(c, on) -> None:
    c.execute(text("UPDATE auditcore.bookings SET pricing_effective_on=:on WHERE tenant_id=:t AND journey_id=:j"),
              {"on": on, "t": c.tenant_id, "j": c.journey_id})


def _old_vehicle_document(c, *, document_type: str, fields: dict[str, str]) -> None:
    document_id = uuid4()
    c.execute(
        text("INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, di_subject_id, di_document_id, "
             "document_type_key, evidence_purpose) VALUES (:t, :j, :cu, :s, :d, :dt, 'DELIVERY')"),
        {"t": c.tenant_id, "j": c.journey_id, "cu": c.customer_id, "s": uuid4(), "d": document_id,
         "dt": document_type},
    )
    for key, value in fields.items():
        c.execute(
            text("INSERT INTO auditcore.journey_document_extracted_fields (tenant_id, journey_id, di_document_id, "
                 "source_fact_version, field_key, stage_code, source_document_type_key, extracted_value, "
                 "effective_value) VALUES (:t, :j, :d, 1, :f, 'BOOKING', :dt, CAST(:v AS jsonb), CAST(:v AS jsonb))"),
            {"t": c.tenant_id, "j": c.journey_id, "d": document_id, "f": key, "dt": document_type,
             "v": json.dumps(value)},
        )


def _utkal_masters(c):
    """XUV 3XO AX5 PM MT priced 1 Sep 2026, with the September consumer
    schemes as the old ingestion left them: the AX5 PM MT row (30,000) on its
    variant, the MX2 PRO "AT & MT" 60,000 row and the XUV400 BEV 2,50,000 row
    stored model-wide on XUV 3XO with the variant texts nothing resolved."""
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1029900.54"})
    model = seeded["model_id"]
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"CASH_DISCOUNT": "30000"},
            eligibility=[{"model_id": model, "variant_id": seeded["variant_id"],
                          "criteria": {"variantTexts": ["AX5 PM MT"], "variantText": "AX5 PM MT"}}])
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"CASH_DISCOUNT": "60000"},
            eligibility=[{"model_id": model, "criteria": {
                "scope": "MODEL", "variantTexts": ["MX2 PRO PM AT & MT"],
                "unresolvedVariantTexts": ["MX2 PRO PM AT & MT"]}}])
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"CASH_DISCOUNT": "250000"},
            eligibility=[{"model_id": model, "criteria": {
                "scope": "MODEL", "variantTexts": ["EC PRO - Battery 34.5 Kwh-E-NCH", "Charger 7.2 Kw - E"],
                "unresolvedVariantTexts": ["EC PRO - Battery 34.5 Kwh-E-NCH", "Charger 7.2 Kw - E"]}}])
    _price_on(c, _UTKAL_PRICING_DATE)
    return seeded


def test_utkal_xuv3xo_ax5_pm_mt_cash_entitlement_is_30000_every_time(journey) -> None:
    """Before: DISTINCT ON picked any of {30,000, 60,000, 2,50,000} on repeated
    calls with the same data. After: the variant's own 30,000, every time."""
    c = journey
    seeded = _utkal_masters(c)
    for _ in range(4):
        result = dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        assert result["pricingDate"] == "2026-09-10"
        row = _discount(c, "CASH_DISCOUNT")
        assert row["standard_eligible_amount"] == Decimal(30000)
        assert row["eligibility_result"] == "ELIGIBLE_UNCLAIMED"
    picks = {
        str(b["amount_value"])
        for _ in range(10)
        for b in dr._applicable_benefits(
            c, tenant_id=c.tenant_id, model_id=seeded["model_id"], variant_id=seeded["variant_id"],
            effective_on=_UTKAL_PRICING_DATE, basis="INDIVIDUAL", variant_name="AX5 PM MT",
        )
    }
    assert picks == {"30000.00"}


def test_a_model_level_row_wins_only_when_no_variant_row_exists(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1029900.54"})
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"ACCESSORIES_KIT": "5000"},
            eligibility=[{"model_id": seeded["model_id"], "criteria": {"scope": "MODEL"}}])
    _price_on(c, _UTKAL_PRICING_DATE)
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _discount(c, "ACCESSORIES_KIT")["standard_eligible_amount"] == Decimal(5000)


def _exchange_masters(c):
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1029900.54"})
    model = seeded["model_id"]
    _scheme(c, category="EXCHANGE", effective_from=_SEPT_SCHEMES, benefits={"EXCHANGE_BONUS": "50000"},
            code="MAH_EXCHANGE_EXCHANGE_PERSONAL_XUV_3XO",
            eligibility=[{"model_id": model, "criteria": {"scope": "MODEL", "section": "EXCHANGE_PERSONAL"}}])
    _scheme(c, category="EXCHANGE", effective_from=_SEPT_SCHEMES, benefits={"EXCHANGE_BONUS": "70000"},
            code="MAH_EXCHANGE_EXCHANGE_PERSONAL_XUV_3XO_OLD_MARUTI",
            eligibility=[{"model_id": model, "criteria": {
                "scope": "MODEL", "section": "EXCHANGE_PERSONAL", "requiredOldVehicle": {"brand": "MARUTI"}}}])
    _price_on(c, _UTKAL_PRICING_DATE)
    return seeded


def test_exchange_70000_needs_an_old_maruti_on_file(journey) -> None:
    c = journey
    _exchange_masters(c)
    _seed_reviewed_booking(c, exchange_discount_amount="50000")
    _link_evidence(c, "vehicle_rc")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    # An RC with no readable make proves an old vehicle, not a Maruti.
    assert _discount(c, "EXCHANGE_BONUS")["standard_eligible_amount"] == Decimal(50000)

    _old_vehicle_document(c, document_type="valuation_report", fields={"make": "Maruti Suzuki", "model": "Swift"})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _discount(c, "EXCHANGE_BONUS")["standard_eligible_amount"] == Decimal(70000)


def test_versions_one_upload_collided_take_the_lower_amount_and_say_so(journey) -> None:
    """Already-loaded data: the old ingestion stored the 50,000 and the 70,000
    "against old Maruti" rows as versions 1 and 2 of one scheme, same date,
    same upload. With no condition to tell them apart the lower is taken."""
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1029900.54"})
    eligibility = [{"model_id": seeded["model_id"], "criteria": {"scope": "MODEL"}}]
    scheme_id, _ = _scheme(c, category="EXCHANGE", effective_from=_SEPT_SCHEMES,
                           benefits={"EXCHANGE_BONUS": "50000"}, eligibility=eligibility)
    _scheme(c, category="EXCHANGE", effective_from=_SEPT_SCHEMES, benefits={"EXCHANGE_BONUS": "70000"},
            eligibility=eligibility, version_no=2, scheme_id=scheme_id)
    _price_on(c, _UTKAL_PRICING_DATE)
    _seed_reviewed_booking(c, exchange_discount_amount="70000")
    for _ in range(3):
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        assert _discount(c, "EXCHANGE_BONUS")["standard_eligible_amount"] == Decimal(50000)
    assert _details(c, "EXCHANGE_BONUS")["ambiguousAmounts"] == ["70000.00"]


def test_a_newer_version_retires_the_older_and_a_tombstone_withdraws_it(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1029900.54"})
    eligibility = [{"model_id": seeded["model_id"], "criteria": {"scope": "MODEL"}}]
    exchange, _ = _scheme(c, category="EXCHANGE", effective_from=_SEPT_SCHEMES,
                          benefits={"EXCHANGE_BONUS": "50000"}, eligibility=eligibility)
    _scheme(c, category="EXCHANGE", effective_from=date(2026, 10, 3), benefits={"EXCHANGE_BONUS": "40000"},
            eligibility=eligibility, version_no=2, scheme_id=exchange, created_at="2026-10-04T00:00:00Z")
    scrappage, _ = _scheme(c, category="SCRAPPAGE", effective_from=_SEPT_SCHEMES,
                           benefits={"SCRAPPAGE_BONUS_COD": "20000"}, eligibility=eligibility)
    _scheme(c, category="SCRAPPAGE", effective_from=date(2026, 10, 3), benefits={}, eligibility=[],
            version_no=2, scheme_id=scrappage, created_at="2026-10-04T00:00:00Z")  # withdrawn in October
    _seed_reviewed_booking(c, exchange_discount_amount="40000", scrappage_discount_amount="20000")

    _price_on(c, _UTKAL_PRICING_DATE)
    benefits = {b["canonical_key"]: str(b["amount_value"]) for b in dr._applicable_benefits(
        c, tenant_id=c.tenant_id, model_id=seeded["model_id"], variant_id=seeded["variant_id"],
        effective_on=_UTKAL_PRICING_DATE, basis="INDIVIDUAL")}
    assert benefits == {"EXCHANGE_BONUS": "50000.00", "SCRAPPAGE_BONUS_COD": "20000.00"}

    october = date(2026, 10, 10)
    benefits = {b["canonical_key"]: str(b["amount_value"]) for b in dr._applicable_benefits(
        c, tenant_id=c.tenant_id, model_id=seeded["model_id"], variant_id=seeded["variant_id"],
        effective_on=october, basis="INDIVIDUAL")}
    assert benefits == {"EXCHANGE_BONUS": "40000.00"}


def test_eligibility_exclusions_dealer_and_unresolved_rows_are_honoured(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="REVX EDGE PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1100000"})
    model = seeded["model_id"]
    _scheme(c, category="EXCHANGE", effective_from=_SEPT_SCHEMES, benefits={"EXCHANGE_BONUS": "40000"},
            eligibility=[{"model_id": model, "criteria": {"scope": "MODEL",
                                                          "excludedVariantTexts": ["EDGE", "REVX M(O) PM AT"]}}])
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"CASH_DISCOUNT": "25000"},
            eligibility=[{"model_id": model, "criteria": {"scope": "UNRESOLVED",
                                                          "unresolvedVariantTexts": ["REVX EDGE"]}}])
    other_dealer = c.execute(
        text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) VALUES (:t, :c, 'X') "
             "RETURNING dealer_id"),
        {"t": c.tenant_id, "c": f"OTHER-{uuid4().hex[:6]}"},
    ).scalar_one()
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"ACCESSORIES_KIT": "9000"},
            eligibility=[{"model_id": model, "dealer_id": other_dealer}])
    _price_on(c, _UTKAL_PRICING_DATE)
    benefits = dr._applicable_benefits(
        c, tenant_id=c.tenant_id, model_id=model, variant_id=seeded["variant_id"], effective_on=_UTKAL_PRICING_DATE,
        basis="INDIVIDUAL", variant_name="REVX EDGE PM MT", dealer_id=uuid4(),
    )
    assert benefits == []


def test_stale_exchange_and_price_standards_are_cleared_on_rerun(journey) -> None:
    """Before: an EXCHANGE_BONUS entitlement from an earlier run (or from a
    model-wide row since corrected) stayed for good. After: a re-run resets
    what it no longer produces and keeps the actuals."""
    c = journey
    seeded = _utkal_masters(c)
    stale_dsv = _scheme(c, category="EXCHANGE", effective_from=_SEPT_SCHEMES, benefits={}, eligibility=[])[1]
    c.execute(
        text("INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
             "standard_eligible_amount, discount_scheme_version_id, eligibility_result, actual_source_kind, details) "
             "VALUES (:t, :j, 'EXCHANGE_BONUS', 50000, :v, 'ELIGIBLE_UNCLAIMED', 'CALCULATED', "
             "'{\"origin\": \"DEAL_RECONCILIATION\"}')"),
        {"t": c.tenant_id, "j": c.journey_id, "v": stale_dsv},
    )
    c.execute(
        text("INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
             "standard_eligible_amount, actual_discount_amount, eligibility_result, actual_source_kind, details) "
             "VALUES (:t, :j, 'SCRAPPAGE_BONUS_COD', 20000, 20000, 'ELIGIBLE', 'CALCULATED', "
             "'{\"origin\": \"DEAL_RECONCILIATION\"}')"),
        {"t": c.tenant_id, "j": c.journey_id},
    )
    # a price standard left by another SKU's price list item
    other_item = c.execute(
        text("SELECT price_list_item_id FROM auditcore.price_list_items WHERE tenant_id=:t AND product_sku_id=:s"),
        {"t": c.tenant_id, "s": seeded["sku_id"]},
    ).scalar_one()
    c.execute(
        text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, standard_amount, "
             "actual_amount, price_list_item_id) VALUES (:t, :j, 'insurance_amount', 45224.73, 45225, :i)"),
        {"t": c.tenant_id, "j": c.journey_id, "i": other_item},
    )

    result = dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["cleared"] == ["EXCHANGE_BONUS", "SCRAPPAGE_BONUS_COD"]
    exchange = _discount(c, "EXCHANGE_BONUS")
    assert exchange["standard_eligible_amount"] is None and exchange["eligibility_result"] is None
    scrappage = _discount(c, "SCRAPPAGE_BONUS_COD")
    assert scrappage["standard_eligible_amount"] is None
    assert scrappage["actual_discount_amount"] == Decimal(20000)  # the actual stays
    assert scrappage["eligibility_result"] == "NOT_ELIGIBLE"
    line = c.execute(
        text("SELECT standard_amount, actual_amount, price_list_item_id FROM auditcore.commercial_lines "
             "WHERE tenant_id=:t AND journey_id=:j AND component_key='insurance_amount'"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).mappings().one()
    assert line["standard_amount"] is None and line["price_list_item_id"] is None
    assert line["actual_amount"] == Decimal(45225)
    assert _commercial(c, "ex_showroom_price") == Decimal("1029900.54")
    events = c.execute(
        text("SELECT subject_type FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
             "AND event_type='DEAL_STANDARDS_CLEARED' ORDER BY subject_type"),
        {"t": c.tenant_id, "j": c.journey_id},
    ).scalars().all()
    assert events == ["DISCOUNT", "PRICE"]

    # Idempotent: a second run has nothing left to clear.
    again = dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert again["cleared"] == []


def test_unclaimed_exchange_and_scrappage_are_not_counted_in_the_total(journey) -> None:
    """The retro's worked example: XUV 3XO AX5 PM MT with cash 30,000 owed
    and exchange 50,000 / scrappage 50,000 + 20,000 unclaimed. Before, the
    standard subtracted all of them, so 30,000 + 1,00,000 unexplained extra
    discount showed a +20,000 variance and no finding. After: the 1,00,000
    over-grant is a negative variance."""
    c = journey
    seeded = _utkal_masters(c)
    model = seeded["model_id"]
    for category, key, amount in (("EXCHANGE", "EXCHANGE_BONUS", "50000"),
                                  ("SCRAPPAGE", "SCRAPPAGE_BONUS_DEALER", "50000"),
                                  ("SCRAPPAGE", "SCRAPPAGE_BONUS_COD", "20000")):
        _scheme(c, category=category, effective_from=_SEPT_SCHEMES, benefits={key: amount},
                eligibility=[{"model_id": model, "criteria": {"scope": "MODEL"}}])
    _seed_reviewed_booking(c, sales_discount_amount="30000", other_discount_amount="100000")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    c.execute(text("UPDATE auditcore.commercial_lines SET actual_amount=standard_amount, "
                   "source_reference='booking_form:test' "
                   "WHERE tenant_id=:t AND journey_id=:j AND standard_amount IS NOT NULL"),
              {"t": c.tenant_id, "j": c.journey_id})
    result = dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["informational"] == 3
    for key in ("EXCHANGE_BONUS", "SCRAPPAGE_BONUS_DEALER", "SCRAPPAGE_BONUS_COD"):
        assert _discount(c, key)["standard_eligible_amount"] is None
    assert Decimal(result["totals"]["variance"]) == Decimal(-100000)
    assert _open_finding(c, "DEAL_TOTAL_VARIANCE_NEGATIVE") is not None


def test_corporate_privilege_follows_the_registry_category_and_is_never_assumed(journey) -> None:
    c = journey
    c.execute(text("UPDATE auditcore.customers SET customer_type_code='CORPORATE' WHERE customer_id=:id"),
              {"id": c.customer_id})
    oem_code = c.execute(text("SELECT o.oem_code FROM auditcore.projects p JOIN auditcore.oems o "
                              "ON o.oem_id = p.oem_id WHERE p.tenant_id=:t"), {"t": c.tenant_id}).scalar_one()
    seeded = _utkal_masters(c)
    for category, amount in (("B", "5000"), ("F", "10000"), ("Z", "30000")):
        _scheme(c, category="CORPORATE", effective_from=_SEPT_SCHEMES, benefits={"CORPORATE_PRIVILEGE": amount},
                eligibility=[{"model_id": seeded["model_id"], "customer_type_code": f"CORPORATE:{category}",
                              "criteria": {"scope": "MODEL", "privilegeCategory": category}}])
    _seed_reviewed_booking(c, corporate_discount_amount="10000")

    # A company buyer is not, by itself, a listed corporate: never assumed.
    for _ in range(2):
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        row = _discount(c, "CORPORATE_PRIVILEGE")
        assert row["eligibility_result"] == "NEEDS_CORPORATE_EVIDENCE"
        assert row["standard_eligible_amount"] is None
    assert _details(c, "CORPORATE_PRIVILEGE")["candidatesByCategory"] == {
        "B": "5000.00", "F": "10000.00", "Z": "30000.00"}

    code = f"T{uuid4().hex[:8]}"
    c.execute(
        text("INSERT INTO auditcore.corporate_privilege_registry (oem_code, corporate_code, corporate_name, "
             "privilege_category) VALUES (:o, :c, :n, 'F')"),
        {"o": oem_code, "c": code, "n": f"Focus Hospitals {code}"},
    )
    try:
        _old_vehicle_document(c, document_type="corporate_id", fields={"employer_name": f"Focus  Hospitals {code}"})
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        row = _discount(c, "CORPORATE_PRIVILEGE")
        assert row["standard_eligible_amount"] == Decimal(10000)
        assert row["eligibility_result"] == "ELIGIBLE"
    finally:
        c.execute(text("DELETE FROM auditcore.corporate_privilege_registry WHERE oem_code=:o AND corporate_code=:c"),
                  {"o": oem_code, "c": code})


def _grid(c, *, model_id, effective_from, rows: list[dict]) -> None:
    version_no = c.execute(
        text("SELECT COALESCE(MAX(version_no), 0) + 1 FROM auditcore.dealer_discount_grid_versions WHERE tenant_id=:t"),
        {"t": c.tenant_id},
    ).scalar_one()
    version = c.execute(
        text("INSERT INTO auditcore.dealer_discount_grid_versions (tenant_id, version_no, effective_from) "
             "VALUES (:t, :n, :e) RETURNING grid_version_id"),
        {"t": c.tenant_id, "n": version_no, "e": effective_from},
    ).scalar_one()
    for row in rows:
        c.execute(
            text("INSERT INTO auditcore.dealer_discount_grid_rows (tenant_id, grid_version_id, model_alias, model_id, "
                 "in_scope, agreed_buffer_amount, raw) VALUES (:t, :v, :a, :m, true, :b, CAST(:r AS jsonb))"),
            {"t": c.tenant_id, "v": version, "a": row["alias"], "m": model_id, "b": row["buffer"],
             "r": json.dumps(row.get("raw") or {})},
        )


def test_the_dealer_discount_is_entitled_up_to_the_grid_tier(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", effective_from=date(2026, 10, 1),
                              components={"EX_SHOWROOM": "999901.32"})
    _grid(c, model_id=seeded["model_id"], effective_from=date(2026, 10, 1), rows=[{
        "alias": "XUV 3XO", "buffer": "5000",
        "raw": {"tiers": {"BASE": "5000.00", "WITH_INSURANCE": "7000.00", "WITH_EW_INSURANCE": "10000.00"}}}])
    _price_on(c, date(2026, 10, 10))
    _seed_reviewed_booking(c, buffer_discount_amount="6000")
    try:
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        row = _discount(c, "ADDITIONAL_DISCOUNT")
        assert row["standard_eligible_amount"] == Decimal(5000)  # no in-house insurance on file: the base tier
        assert _details(c, "ADDITIONAL_DISCOUNT")["grid"]["tier"] == "BASE"

        c.execute(text("INSERT INTO auditcore.commercial_lines (tenant_id, journey_id, component_key, actual_amount) "
                       "VALUES (:t, :j, 'insurance_amount', 45225)"), {"t": c.tenant_id, "j": c.journey_id})
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        assert _discount(c, "ADDITIONAL_DISCOUNT")["standard_eligible_amount"] == Decimal(7000)
    finally:
        c.execute(text("DELETE FROM auditcore.dealer_discount_grid_rows WHERE tenant_id=:t"), {"t": c.tenant_id})
        c.execute(text("DELETE FROM auditcore.dealer_discount_grid_versions WHERE tenant_id=:t"), {"t": c.tenant_id})


def test_management_referral_is_its_own_line(journey) -> None:
    c = journey
    _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", components={"EX_SHOWROOM": "1029900.54"})
    _seed_reviewed_booking(c, mr_discount_amount="10000")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "MANAGEMENT_REFERRAL")
    assert row["actual_discount_amount"] == Decimal(10000)
    assert row["eligibility_result"] == "NOT_ELIGIBLE"  # no Team Lead approval yet
    assert _discount(c, "OTHER_SCHEME") is None


def test_an_unclassified_invoice_discount_is_compared_with_the_whole_entitlement(journey) -> None:
    """2026-10-20: an invoice discount whose words name no type (a header-only
    discount) is never "given with no entitlement"; it is measured against
    what the deal is entitled to and not already given under a named type."""
    c = journey
    _utkal_masters(c)

    def unclassified(amount: str) -> None:
        c.execute(text("DELETE FROM auditcore.discount_applications WHERE tenant_id=:t AND journey_id=:j "
                       "AND discount_key='UNCLASSIFIED_DISCOUNT'"), {"t": c.tenant_id, "j": c.journey_id})
        c.execute(
            text("INSERT INTO auditcore.discount_applications (tenant_id, journey_id, discount_key, "
                 "actual_discount_amount, actual_source_kind, details) VALUES (:t, :j, 'UNCLASSIFIED_DISCOUNT', "
                 ":a, 'EVIDENCE', '{\"origin\": \"INVOICE_MATERIALIZATION\"}')"),
            {"t": c.tenant_id, "j": c.journey_id, "a": amount},
        )

    unclassified("29500")
    result = dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["unclassified"] == "WITHIN_ENTITLEMENT"
    row = _discount(c, "UNCLASSIFIED_DISCOUNT")
    assert row["eligibility_result"] == "WITHIN_ENTITLEMENT" and row["standard_eligible_amount"] is None
    assert _details(c, "UNCLASSIFIED_DISCOUNT")["unclassified"]["headroom"] == "30000.00"

    unclassified("40000")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _discount(c, "UNCLASSIFIED_DISCOUNT")["eligibility_result"] == "EXCEEDS_ENTITLEMENT"
    assert _details(c, "UNCLASSIFIED_DISCOUNT")["unclassified"]["excess"] == "10000.00"


# ── review of PR 472 (2026-10-20 product owner rules) ────────────────────────
def _p2_tasks(c, task_type: str) -> list[dict]:
    return [dict(r) for r in c.execute(
        text("SELECT dedupe_key, task_status, assigned_role_code, reference FROM auditcore.p2_tasks "
             "WHERE tenant_id=:t AND task_type=:k ORDER BY dedupe_key"),
        {"t": c.tenant_id, "k": task_type},
    ).mappings().all()]


def _sheet_discount(c, key: str) -> dict:
    from audit_core.uc03_p2_journey360 import deal

    return next(r for r in deal(c, tenant_id=c.tenant_id, journey_id=c.journey_id)["discounts"] if r["key"] == key)


def test_b1_an_unmatched_master_row_is_not_known_never_an_over_grant(journey) -> None:
    """XUV 3XO MX3 PRO with 60,000 cash given; the only cash row on the model
    names variants the price list does not carry (stored UNRESOLVED). Before:
    NOT_ELIGIBLE, -60,000 and a HIGH finding. After: MASTER_UNMATCHED, no
    standard, taken as given on the Deal sheet, and ONE mapping task for the
    master row however many runs (or deals) meet it."""
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="MX3 PRO PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "900000"})
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"CASH_DISCOUNT": "60000"},
            eligibility=[{"model_id": seeded["model_id"], "criteria": {
                "scope": "UNRESOLVED", "modelCell": "XUV 3XO", "variantTexts": ["MX3 PRO PM AT & MT"],
                "unresolvedVariantTexts": ["MX3 PRO PM AT & MT"]}}])
    _price_on(c, _UTKAL_PRICING_DATE)
    _seed_reviewed_booking(c, sales_discount_amount="60000")
    for _ in range(2):
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CASH_DISCOUNT")
    assert row["eligibility_result"] == "MASTER_UNMATCHED"
    assert row["standard_eligible_amount"] is None and row["actual_discount_amount"] == Decimal(60000)
    assert _details(c, "CASH_DISCOUNT")["unmatchedMasters"][0]["variantTexts"] == ["MX3 PRO PM AT & MT"]
    tasks = _p2_tasks(c, "SCHEME_MASTER_UNMATCHED")
    assert len(tasks) == 1 and tasks[0]["assigned_role_code"] == "TL"
    assert tasks[0]["dedupe_key"].startswith("scheme-master-unmatched:")
    sheet = _sheet_discount(c, "CASH_DISCOUNT")
    assert sheet["entitled"] is None and sheet["takenAsGiven"] is True
    assert _open_finding(c, "DEAL_TOTAL_VARIANCE_NEGATIVE") is None

    # The bulletin re-uploaded with the wording mapped: a newer version of the
    # scheme replaces the row, and the mapping task closes.
    scheme_id = c.execute(text(
        "SELECT v.discount_scheme_id FROM auditcore.discount_scheme_versions v JOIN auditcore.discount_scheme_eligibility e "
        "ON e.discount_scheme_version_id = v.discount_scheme_version_id WHERE v.tenant_id=:t AND e.model_id=:m"),
        {"t": c.tenant_id, "m": seeded["model_id"]}).scalar_one()
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"CASH_DISCOUNT": "60000"},
            scheme_id=scheme_id, version_no=2,
            eligibility=[{"model_id": seeded["model_id"], "variant_id": seeded["variant_id"],
                          "criteria": {"scope": "VARIANT", "variantText": "MX3 PRO PM MT"}}])
    assert dr.close_superseded_master_tasks(c, tenant_id=c.tenant_id) == 1
    assert _p2_tasks(c, "SCHEME_MASTER_UNMATCHED")[0]["task_status"] == "VERIFIED_COMPLETE"
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CASH_DISCOUNT")
    assert row["eligibility_result"] == "ELIGIBLE" and row["standard_eligible_amount"] == Decimal(60000)


def test_b1_no_grid_line_and_out_of_scope_are_not_over_grants(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="PICK UP", variant="BOL PIK UP FB 1.3T - BS6.2 - PS",
                              effective_from=date(2026, 9, 1), components={"EX_SHOWROOM": "900000"})
    _price_on(c, _UTKAL_PRICING_DATE)
    _seed_reviewed_booking(c, buffer_discount_amount="7000")
    try:
        # The August grid stored by the old ingestion: the Camper "Nil" line
        # and the Pick-Up line both model-wide on PICK UP, no group recorded.
        _grid(c, model_id=seeded["model_id"], effective_from=date(2026, 9, 1), rows=[
            {"alias": "Camper", "buffer": "0"}, {"alias": "Pick-Up", "buffer": "7000"}])
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        row = _discount(c, "ADDITIONAL_DISCOUNT")
        assert row["standard_eligible_amount"] == Decimal(7000)  # never the Camper line
        assert _details(c, "ADDITIONAL_DISCOUNT")["grid"]["modelAlias"] == "Pick-Up"

        # A newer grid with no line for the model: not known, and one task.
        _grid(c, model_id=None, effective_from=date(2026, 9, 5), rows=[{"alias": "XUV 7XO", "buffer": "5000"}])
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        row = _discount(c, "ADDITIONAL_DISCOUNT")
        assert row["eligibility_result"] == "MASTER_UNMATCHED" and row["standard_eligible_amount"] is None
        assert [t["dedupe_key"].startswith("grid-line-missing:") for t in _p2_tasks(c, "SCHEME_MASTER_UNMATCHED")] == [True]

        # "Out of scope" of the discount control program is not "no discount".
        c.execute(text("UPDATE auditcore.dealer_discount_grid_rows SET in_scope = false WHERE tenant_id=:t"),
                  {"t": c.tenant_id})
        c.execute(text("UPDATE auditcore.dealer_discount_grid_versions SET effective_from = '2026-09-20' "
                       "WHERE tenant_id=:t AND effective_from = '2026-09-05'"), {"t": c.tenant_id})
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        assert _discount(c, "ADDITIONAL_DISCOUNT")["eligibility_result"] == "OUT_OF_GRID_SCOPE"
    finally:
        c.execute(text("DELETE FROM auditcore.dealer_discount_grid_rows WHERE tenant_id=:t"), {"t": c.tenant_id})
        c.execute(text("DELETE FROM auditcore.dealer_discount_grid_versions WHERE tenant_id=:t"), {"t": c.tenant_id})


def test_b1_a_grid_line_stored_without_its_model_is_read_by_the_grid_spelling(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV3XO", variant="AX5 PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1029900.54"})
    _price_on(c, _UTKAL_PRICING_DATE)
    _seed_reviewed_booking(c, buffer_discount_amount="7000")
    try:
        _grid(c, model_id=None, effective_from=date(2026, 9, 1), rows=[{"alias": "3XO", "buffer": "7000"}])
        dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
        assert _discount(c, "ADDITIONAL_DISCOUNT")["standard_eligible_amount"] == Decimal(7000)
        assert seeded
    finally:
        c.execute(text("DELETE FROM auditcore.dealer_discount_grid_rows WHERE tenant_id=:t"), {"t": c.tenant_id})
        c.execute(text("DELETE FROM auditcore.dealer_discount_grid_versions WHERE tenant_id=:t"), {"t": c.tenant_id})


def test_b2_the_rc_maker_and_model_text_meets_the_old_maruti_condition(journey) -> None:
    c = journey
    _exchange_masters(c)
    _seed_reviewed_booking(c, exchange_discount_amount="70000")
    _old_vehicle_document(c, document_type="vehicle_rc",
                          fields={"vehicle_model": "MARUTI SUZUKI INDIA LTD / SWIFT DZIRE VXI"})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _discount(c, "EXCHANGE_BONUS")["standard_eligible_amount"] == Decimal(70000)


def test_b2_maker_spellings() -> None:
    assert dr._old_vehicle_meets({"brand": "MARUTI"}, [{"model": "MARUTI SUZUKI INDIA LTD / SWIFT DZIRE VXI"}])
    assert dr._old_vehicle_meets({"brand": "MARUTI"}, [{"make": "MSIL", "model": "Alto"}])
    assert dr._old_vehicle_meets({"brand": "TATA", "model": "ACE"}, [{"model": "tata motors ltd / ace gold"}])
    assert not dr._old_vehicle_meets({"brand": "TATA", "model": "ACE"}, [{"model": "TATA MOTORS / NEXON"}])
    assert not dr._old_vehicle_meets({"brand": "MARUTI"}, [{"model": "HYUNDAI / I20"}])
    assert not dr._old_vehicle_meets({"brand": "MARUTI"}, [])


def test_b3_a_deal_that_can_no_longer_be_reconciled_keeps_no_over_grant_and_no_shortfall(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="THAR", variant="LX", components={"EX_SHOWROOM": "1500000"})
    _seed_scheme(c, category="CONSUMER", benefit_key="CASH_DISCOUNT", amount="10000", model_id=seeded["model_id"])
    _seed_reviewed_booking(c, sales_discount_amount="30000")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    c.execute(text("UPDATE auditcore.commercial_lines SET actual_amount=standard_amount, "
                   "source_reference='booking_form:test' WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    finding = _open_finding(c, "DEAL_TOTAL_VARIANCE_NEGATIVE")
    assert finding is not None

    # The SKU is unpinned (an automatic pin replaced, say): not reconciled.
    c.execute(text("UPDATE auditcore.journey_products SET product_sku_id = NULL WHERE tenant_id=:t AND journey_id=:j"),
              {"t": c.tenant_id, "j": c.journey_id})
    result = dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert result["skipped"] is True and result["varianceClosed"] is True
    row = _discount(c, "CASH_DISCOUNT")
    assert row["eligibility_result"] == "NOT_EVALUATED" and row["standard_eligible_amount"] is None
    assert _details(c, "CASH_DISCOUNT")["entitlementUnknown"] == "no SKU resolved for the deal yet"
    assert _open_finding(c, "DEAL_TOTAL_VARIANCE_NEGATIVE") is None
    statuses = c.execute(text("SELECT task_status FROM auditcore.workflow_tasks WHERE tenant_id=:t AND journey_id=:j "
                              "AND task_type='DEAL_TOTAL_VARIANCE_REVIEW'"),
                         {"t": c.tenant_id, "j": c.journey_id}).scalars().all()
    assert statuses == ["CANCELLED"]


def test_s2_one_old_vehicle_earns_one_benefit_and_unclaimed_proof_is_information(journey) -> None:
    c = journey
    seeded = _exchange_masters(c)
    _scheme(c, category="WELCOME", effective_from=_SEPT_SCHEMES, benefits={"WELCOME_BONUS": "20000"},
            eligibility=[{"model_id": seeded["model_id"], "criteria": {"scope": "MODEL"}}])
    _link_evidence(c, "vehicle_rc")
    _seed_reviewed_booking(c, exchange_discount_amount="50000", loyalty_discount_amount="20000")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _discount(c, "EXCHANGE_BONUS")["standard_eligible_amount"] == Decimal(50000)
    welcome = _discount(c, "WELCOME_BONUS")
    assert welcome["eligibility_result"] == "NOT_ELIGIBLE" and welcome["standard_eligible_amount"] is None
    assert _details(c, "WELCOME_BONUS")["notCombinableWith"] == "EXCHANGE_BONUS"


def test_s2_a_proven_but_unclaimed_exchange_is_not_in_the_standard(journey) -> None:
    c = journey
    _exchange_masters(c)
    _link_evidence(c, "vehicle_rc")
    _seed_reviewed_booking(c, sales_discount_amount="1")
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "EXCHANGE_BONUS")
    assert row["eligibility_result"] == "ELIGIBLE_UNCLAIMED" and row["standard_eligible_amount"] is None
    assert _details(c, "EXCHANGE_BONUS")["potentialAmount"] == "50000.00"


def test_s1_a_bulletin_answers_only_to_its_valid_to_date(journey) -> None:
    c = journey
    seeded = _seed_pinned_sku(c, model="XUV 3XO", variant="AX5 PM MT", effective_from=date(2026, 9, 1),
                              components={"EX_SHOWROOM": "1029900.54"})
    _scheme(c, category="CONSUMER", effective_from=_SEPT_SCHEMES, benefits={"CASH_DISCOUNT": "30000"},
            eligibility=[{"model_id": seeded["model_id"], "variant_id": seeded["variant_id"], "criteria": {}}],
            effective_to=date(2026, 9, 30))
    _seed_reviewed_booking(c, sales_discount_amount="30000")
    _price_on(c, date(2026, 10, 10))
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CASH_DISCOUNT")
    assert row["eligibility_result"] == "SCHEME_NOT_IN_FORCE" and row["standard_eligible_amount"] is None
    (task,) = _p2_tasks(c, "DISCOUNT_SCHEME_NOT_IN_FORCE")
    assert task["task_status"] == "READY"

    _price_on(c, _UTKAL_PRICING_DATE)
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _discount(c, "CASH_DISCOUNT")["standard_eligible_amount"] == Decimal(30000)
    assert _p2_tasks(c, "DISCOUNT_SCHEME_NOT_IN_FORCE")[0]["task_status"] == "VERIFIED_COMPLETE"


def test_s5_the_corporate_list_is_dated_per_tenant_and_an_inexact_name_is_a_pc_task(journey) -> None:
    from audit_core.oem_price_masters import _insert_registry_entries

    c = journey
    c.execute(text("UPDATE auditcore.customers SET customer_type_code='CORPORATE' WHERE customer_id=:id"),
              {"id": c.customer_id})
    oem_code = c.execute(text("SELECT o.oem_code FROM auditcore.projects p JOIN auditcore.oems o "
                              "ON o.oem_id = p.oem_id WHERE p.tenant_id=:t"), {"t": c.tenant_id}).scalar_one()
    seeded = _utkal_masters(c)
    for category, amount in (("F", "10000"), ("Z", "30000")):
        _scheme(c, category="CORPORATE", effective_from=_SEPT_SCHEMES, benefits={"CORPORATE_PRIVILEGE": amount},
                eligibility=[{"model_id": seeded["model_id"], "customer_type_code": f"CORPORATE:{category}",
                              "criteria": {"scope": "MODEL"}}])

    class _Company:
        def __init__(self, code, name, category):
            self.corporate_code, self.corporate_name, self.privilege_category = code, name, category
            self.corporate_type = None

    _insert_registry_entries(c, tenant_id=c.tenant_id, oem_code=oem_code, upload_id=uuid4(),
                             effective_from=date(2026, 9, 3), companies=[_Company("C1", "Focus Hospitals Limited", "F")])
    _insert_registry_entries(c, tenant_id=c.tenant_id, oem_code=oem_code, upload_id=uuid4(),
                             effective_from=date(2026, 10, 3), companies=[_Company("C1", "Focus Hospitals Limited", "Z")])
    _seed_reviewed_booking(c, corporate_discount_amount="10000")
    _old_vehicle_document(c, document_type="corporate_id", fields={"employer_name": "Focus Hospitals Ltd."})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    assert _discount(c, "CORPORATE_PRIVILEGE")["eligibility_result"] == "NEEDS_CORPORATE_EVIDENCE"
    (task,) = _p2_tasks(c, "CORPORATE_CATEGORY_CONFIRM")
    assert task["assigned_role_code"] == "PC"
    assert task["reference"]["shortlist"] == [{"code": "C1", "name": "Focus Hospitals Limited", "category": "F"}]
    assert _sheet_discount(c, "CORPORATE_PRIVILEGE")["takenAsGiven"] is True

    # The exact name on file: the September list's category on a September
    # deal, although the October list re-categorised the company.
    _old_vehicle_document(c, document_type="corporate_id", fields={"employer_name": "Focus Hospitals Limited"})
    dr.sync_deal_reconciliation(c, tenant_id=c.tenant_id, journey_id=c.journey_id, correlation_id="")
    row = _discount(c, "CORPORATE_PRIVILEGE")
    assert row["eligibility_result"] == "ELIGIBLE" and row["standard_eligible_amount"] == Decimal(10000)
    assert _p2_tasks(c, "CORPORATE_CATEGORY_CONFIRM")[0]["task_status"] == "VERIFIED_COMPLETE"
