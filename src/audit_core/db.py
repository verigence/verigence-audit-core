from sqlalchemy import Connection, text


def set_tenant_context(connection: Connection, tenant_id: str) -> None:
    """Set transaction-local tenant context after the caller validates Security identity."""
    normalized = tenant_id.strip()
    if not normalized:
        raise ValueError("tenant_id is required")
    if len(normalized) > 128:
        raise ValueError("tenant_id exceeds 128 characters")

    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": normalized},
    )
