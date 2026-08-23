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


def set_security_actor_context(connection: Connection, security_actor_id: str) -> None:
    """Set the validated global USER actor for narrow C0 Project discovery reads."""
    normalized = security_actor_id.strip()
    if not normalized:
        raise ValueError("security_actor_id is required")
    if len(normalized) > 160:
        raise ValueError("security_actor_id exceeds 160 characters")

    connection.execute(
        text("SELECT set_config('app.security_actor_id', :actor_id, true)"),
        {"actor_id": normalized},
    )


def set_platform_super_admin_context(connection: Connection) -> None:
    """Enable the narrow cross-Tenant Project SELECT path after Security attestation."""
    connection.execute(
        text("SELECT set_config('app.platform_super_admin', 'true', true)")
    )
