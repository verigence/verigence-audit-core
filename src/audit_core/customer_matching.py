import hashlib
import hmac
from uuid import UUID

from sqlalchemy import Connection, text


def protect_normalized_match_key(normalized_value: str, secret: str) -> str:
    """Protect an already-normalized identity value without storing the raw identifier."""
    return hmac.new(
        secret.encode("utf-8"),
        normalized_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def add_customer_match_key(
    connection: Connection,
    *,
    tenant_id: str,
    customer_id: UUID,
    identity_type: str,
    match_hash: str,
    source_kind: str,
    match_hint: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.customer_identity_index (
                tenant_id, customer_id, identity_type, match_hash,
                match_hint, source_kind
            ) VALUES (
                :tenant_id, :customer_id, :identity_type, :match_hash,
                :match_hint, :source_kind
            )
            RETURNING identity_index_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "identity_type": identity_type,
            "match_hash": match_hash,
            "match_hint": match_hint,
            "source_kind": source_kind,
        },
    ).scalar_one()


def find_customer_matches(
    connection: Connection,
    *,
    tenant_id: str,
    identity_type: str,
    match_hash: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT c.customer_id, c.dealer_id, c.outlet_id,
                            c.display_name, i.identity_type
            FROM auditcore.customer_identity_index i
            JOIN auditcore.customers c
              ON c.tenant_id = i.tenant_id AND c.customer_id = i.customer_id
            WHERE i.tenant_id = :tenant_id
              AND i.identity_type = :identity_type
              AND i.match_hash = :match_hash
              AND c.status = 'ACTIVE'
            ORDER BY c.customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "identity_type": identity_type,
            "match_hash": match_hash,
        },
    ).mappings()
    return [dict(row) for row in rows]
