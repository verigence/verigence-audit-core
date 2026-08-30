from types import SimpleNamespace
from uuid import uuid4

from audit_core import uc03_di_core_persistence as persistence


def test_review_scope_sets_authenticated_actor_context(monkeypatch) -> None:
    connection = object()
    principal = SimpleNamespace(subject="actor-123")
    calls: dict[str, object] = {}

    def fake_scope(conn, *args, **kwargs):
        calls["scope_connection"] = conn
        calls["scope_kwargs"] = kwargs
        return {"operating_role": "PC"}

    def fake_set_actor_context(conn, actor_id: str) -> None:
        calls["actor_connection"] = conn
        calls["actor_id"] = actor_id

    monkeypatch.setattr(persistence, "_original_review_scope", fake_scope)
    monkeypatch.setattr(persistence, "set_security_actor_context", fake_set_actor_context)

    result = persistence._scope_with_actor_context(
        connection,
        tenant_id="tenant-1",
        journey_id=uuid4(),
        human_principal=principal,
        authorization_client=object(),
    )

    assert result == {"operating_role": "PC"}
    assert calls["scope_connection"] is connection
    assert calls["actor_connection"] is connection
    assert calls["actor_id"] == "actor-123"
