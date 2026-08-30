from audit_core import dependencies


def test_postgres_engine_pre_pings_pooled_connections(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create_engine(database_url: str, **options: object) -> object:
        captured["database_url"] = database_url
        captured["options"] = options
        return sentinel

    dependencies._engine.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db.example/verigence")
    monkeypatch.setattr(dependencies, "create_engine", fake_create_engine)
    try:
        assert dependencies.get_engine() is sentinel
        options = captured["options"]
        assert isinstance(options, dict)
        assert options["pool_pre_ping"] is True
        assert options["pool_recycle"] == 600
        assert options["pool_use_lifo"] is True
    finally:
        dependencies._engine.cache_clear()
