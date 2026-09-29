from typing import Self
from uuid import uuid4

import pytest
from fastapi import HTTPException

from audit_core import journey_housekeeping
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


def test_di_purge_sends_documents_in_small_batches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One request per few documents, so DI finishes each inside the 30 second
    limit; the totals are the sum over every batch."""
    sent: list[int] = []

    class _Response:
        is_success = True

        def __init__(self, count: int) -> None:
            self._count = count

        def json(self) -> dict:  # type: ignore[type-arg]
            return {"errorCode": "000", "data": {"deletedDocuments": self._count, "deletedStorageObjects": self._count}}

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def post(self, _url: str, *, json: dict, **_: object) -> _Response:  # type: ignore[type-arg]
            sent.append(len(json["documentIds"]))
            return _Response(len(json["documentIds"]))

    monkeypatch.setenv("DI_BASE_URL", "http://di.test")
    monkeypatch.setattr(journey_housekeeping.httpx, "Client", _Client)

    documents, storage = journey_housekeeping._purge_di_documents(
        tenant_id="t1", document_ids=[uuid4() for _ in range(45)], human_token="token",
    )

    assert sent == [20, 20, 5]
    assert (documents, storage) == (45, 45)
