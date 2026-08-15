from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def append_audit_event(
    connection: Connection,
    *,
    tenant_id: str,
    entity_type: str,
    entity_id: str,
    event_type: str,
    event_payload: dict[str, Any],
    actor_id: str | None,
    actor_type: str = "USER",
    correlation_id: str | None = None,
) -> UUID:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_chain_heads (
                tenant_id, entity_type, entity_id
            ) VALUES (
                :tenant_id, :entity_type, :entity_id
            ) ON CONFLICT (tenant_id, entity_type, entity_id) DO NOTHING
            """
        ),
        {
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
    )
    head = connection.execute(
        text(
            """
            SELECT last_sequence_no, last_event_hash
            FROM auditcore.audit_chain_heads
            WHERE tenant_id = :tenant_id
              AND entity_type = :entity_type
              AND entity_id = :entity_id
            FOR UPDATE
            """
        ),
        {
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
    ).mappings().one()
    sequence_no = head["last_sequence_no"] + 1
    canonical = json.dumps(
        {
            "entityType": entity_type,
            "entityId": entity_id,
            "sequenceNo": sequence_no,
            "eventType": event_type,
            "eventPayload": event_payload,
            "previousEventHash": head["last_event_hash"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    event_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_events (
                tenant_id, entity_type, entity_id, sequence_no,
                event_type, event_payload, previous_event_hash, event_hash,
                actor_id, actor_type, correlation_id
            ) VALUES (
                :tenant_id, :entity_type, :entity_id, :sequence_no,
                :event_type, CAST(:event_payload AS jsonb),
                :previous_event_hash, :event_hash,
                :actor_id, :actor_type, :correlation_id
            ) RETURNING audit_event_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "sequence_no": sequence_no,
            "event_type": event_type,
            "event_payload": json.dumps(event_payload),
            "previous_event_hash": head["last_event_hash"],
            "event_hash": event_hash,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "correlation_id": correlation_id,
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            UPDATE auditcore.audit_chain_heads
            SET last_sequence_no = :sequence_no,
                last_event_hash = :event_hash,
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND entity_type = :entity_type
              AND entity_id = :entity_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "sequence_no": sequence_no,
            "event_hash": event_hash,
        },
    )
    return event_id


def append_outbox_event(
    connection: Connection,
    *,
    tenant_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    journey_id: UUID | None,
    event_payload: dict[str, Any],
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.outbox_events (
                tenant_id, event_type, aggregate_type, aggregate_id,
                journey_id, event_payload
            ) VALUES (
                :tenant_id, :event_type, :aggregate_type, :aggregate_id,
                :journey_id, CAST(:event_payload AS jsonb)
            ) RETURNING outbox_event_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "journey_id": journey_id,
            "event_payload": json.dumps(event_payload),
        },
    ).scalar_one()
