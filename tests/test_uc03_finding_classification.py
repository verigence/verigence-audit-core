from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import audit_core.uc03_finding_classification as fc


@pytest.fixture
def connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for finding-classification integration tests")
    engine = create_engine(database_url)
    fc._registry_cache = None
    with engine.begin() as conn:
        yield conn
    engine.dispose()


# ── registry drives classification ─────────────────────────────────────────────

def test_seeded_type_resolves_from_registry(connection) -> None:
    cls, owner = fc.resolve_class(
        connection, rule_key=None, finding_type_code="DUPLICATE_BOOKING"
    )
    assert cls == "VIOLATION"
    assert owner == "TL"


def test_document_gap_type_owned_by_pc(connection) -> None:
    cls, owner = fc.resolve_class(
        connection, rule_key=None, finding_type_code="DOCUMENT_EXCEPTION"
    )
    assert cls == "DOCUMENT_GAP"
    assert owner == "PC"


def test_rule_key_overrides_the_type(connection) -> None:
    # BK_PAN_PRESENT is a document gap even though the type reads like a violation
    cls, owner = fc.resolve_class(
        connection,
        rule_key="BK_PAN_PRESENT",
        finding_type_code="CUSTOMER_IDENTITY_CONCERN",
    )
    assert cls == "DOCUMENT_GAP"
    assert owner == "PC"


def test_explicit_override_wins(connection) -> None:
    cls, owner = fc.resolve_class(
        connection,
        rule_key=None,
        finding_type_code="DUPLICATE_BOOKING",
        class_override="DATA_GAP",
    )
    assert cls == "DATA_GAP"
    assert owner == "PC"


# ── unknown types are recorded, not silently defaulted ────────────────────────

def test_unknown_type_is_auto_registered_as_unclassified(connection) -> None:
    made_up = f"MADE_UP_TYPE_{uuid4().hex[:8].upper()}"
    cls, owner = fc.resolve_class(
        connection, rule_key=None, finding_type_code=made_up
    )
    assert cls == "VIOLATION"  # safe default — goes to a human
    assert owner == "TL"

    row = connection.execute(
        text(
            "SELECT finding_class, status FROM auditcore.finding_types "
            "WHERE finding_type_code = :code"
        ),
        {"code": made_up},
    ).mappings().one_or_none()
    assert row is not None
    assert row["status"] == "UNCLASSIFIED"


def test_a_reclassified_type_is_honoured(connection) -> None:
    made_up = f"MADE_UP_TYPE_{uuid4().hex[:8].upper()}"
    fc.resolve_class(connection, rule_key=None, finding_type_code=made_up)
    connection.execute(
        text(
            "UPDATE auditcore.finding_types "
            "SET finding_class='DOCUMENT_GAP', default_owner_role='PC', status='ACTIVE' "
            "WHERE finding_type_code = :code"
        ),
        {"code": made_up},
    )
    fc._registry_cache = None
    cls, owner = fc.resolve_class(connection, rule_key=None, finding_type_code=made_up)
    assert cls == "DOCUMENT_GAP"
    assert owner == "PC"
