from uuid import uuid4

import pytest
from fastapi import HTTPException

from audit_core.journey_housekeeping import _scope_id


def test_tenant_scope_uses_tenant_id() -> None:
    assert (
        _scope_id(
            tenant_id="tenant-a",
            scope="TENANT",
            outlet_id=None,
            journey_id=None,
        )
        == "tenant-a"
    )


def test_outlet_scope_uses_outlet_id() -> None:
    outlet_id = uuid4()
    assert (
        _scope_id(
            tenant_id="tenant-a",
            scope="OUTLET",
            outlet_id=outlet_id,
            journey_id=None,
        )
        == str(outlet_id)
    )


def test_journey_scope_uses_journey_id() -> None:
    journey_id = uuid4()
    assert (
        _scope_id(
            tenant_id="tenant-a",
            scope="JOURNEY",
            outlet_id=None,
            journey_id=journey_id,
        )
        == str(journey_id)
    )


@pytest.mark.parametrize(
    ("scope", "outlet_id", "journey_id"),
    [
        ("TENANT", uuid4(), None),
        ("TENANT", None, uuid4()),
        ("OUTLET", None, None),
        ("OUTLET", uuid4(), uuid4()),
        ("JOURNEY", None, None),
        ("JOURNEY", uuid4(), uuid4()),
    ],
)
def test_scope_rejects_ambiguous_identifiers(scope, outlet_id, journey_id) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(HTTPException):
        _scope_id(
            tenant_id="tenant-a",
            scope=scope,
            outlet_id=outlet_id,
            journey_id=journey_id,
        )
