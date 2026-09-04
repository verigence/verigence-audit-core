from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from uuid import UUID
import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)


class RoutingError(Exception):
    """Raised when a message cannot be safely attributed to an actor."""
    def __init__(self, reason: str, *, ignore: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.ignore = ignore


@dataclass(frozen=True)
class ResolvedContext:
    tenant_id: UUID
    user_id: UUID
    contact_id: UUID
    org_unit_id: UUID
    locale: str
    route_id: UUID


def resolve_route(connection: Connection, *, phone_number_id: str | None) -> tuple[UUID, UUID]:
    if not phone_number_id:
        raise RoutingError("no phone_number_id in payload", ignore=True)
    row = connection.execute(
        text("SELECT id, tenant_id FROM wa.route WHERE phone_number_id = :pnid AND active = true"),
        {"pnid": phone_number_id},
    ).mappings().one_or_none()
    if row is None:
        raise RoutingError(f"phone_number_id {phone_number_id!r} not registered", ignore=True)
    return UUID(str(row["id"])), UUID(str(row["tenant_id"]))


def resolve_contact(connection: Connection, *, sender_phone: str, tenant_id: UUID) -> dict[str, Any]:
    """Look up wa.contact by phone. Explicitly scoped to tenant_id (no RLS on this table)."""
    row = connection.execute(
        text("""SELECT c.id AS contact_id, c.user_id, c.locale, c.org_unit_id, c.status
               FROM wa.contact c
               WHERE c.phone_e164 = :phone AND c.tenant_id = :tenant_id"""),
        {"phone": sender_phone, "tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        raise RoutingError(f"phone {sender_phone!r} not bound to tenant {tenant_id}")
    if row["status"] != "active":
        raise RoutingError(f"contact status is {row['status']!r}")
    return dict(row)


def resolve_user(connection: Connection, *, user_id: UUID) -> None:
    row = connection.execute(
        text("SELECT status FROM iam.app_user WHERE id = :uid"), {"uid": user_id}
    ).mappings().one_or_none()
    if row is None or row["status"] != "active":
        raise RoutingError(f"user {user_id} is not active")


def check_org_unit_assignment(connection: Connection, *, tenant_id: UUID, user_id: UUID, org_unit_id: UUID) -> None:
    """Two-dimensional auth check: VAC-WA-SD-001 §8.1."""
    row = connection.execute(
        text("""SELECT 1 FROM iam.role_assignment ra
               WHERE ra.tenant_id = :tenant_id AND ra.subject_kind = 'user'
               AND ra.subject_id = :user_id AND ra.scope_unit_id = :org_unit_id
               AND (ra.valid_to IS NULL OR ra.valid_to > now()) LIMIT 1"""),
        {"tenant_id": tenant_id, "user_id": user_id, "org_unit_id": org_unit_id},
    ).scalar_one_or_none()
    if row is None:
        raise RoutingError(f"user {user_id} has no active assignment at org_unit {org_unit_id}")


def extract_sender_phone(payload: dict[str, Any]) -> str | None:
    try:
        entries = payload.get("entry", [])
        changes = entries[0].get("changes", [])
        contacts = changes[0].get("value", {}).get("contacts", [])
        if contacts:
            wa_id = contacts[0].get("wa_id", "")
            return f"+{wa_id}" if wa_id and not wa_id.startswith("+") else wa_id
    except (IndexError, AttributeError, KeyError):
        pass
    return None


def resolve_full_context(connection: Connection, *, phone_number_id: str | None, payload: dict[str, Any]) -> ResolvedContext:
    """Full pipeline: route -> contact -> user -> assignment. Sets app.tenant_id."""
    route_id, tenant_id = resolve_route(connection, phone_number_id=phone_number_id)
    sender_phone = extract_sender_phone(payload)
    if not sender_phone:
        raise RoutingError("no sender phone in payload", ignore=True)
    contact = resolve_contact(connection, sender_phone=sender_phone, tenant_id=tenant_id)
    user_id = UUID(str(contact["user_id"]))
    contact_id = UUID(str(contact["contact_id"]))
    org_unit_id = UUID(str(contact["org_unit_id"]))
    resolve_user(connection, user_id=user_id)
    check_org_unit_assignment(connection, tenant_id=tenant_id, user_id=user_id, org_unit_id=org_unit_id)
    connection.execute(text("SET LOCAL app.tenant_id = :tid"), {"tid": str(tenant_id)})
    logger.info("wa_context_resolved", tenant_id=str(tenant_id), contact_id=str(contact_id))
    return ResolvedContext(tenant_id=tenant_id, user_id=user_id, contact_id=contact_id,
                          org_unit_id=org_unit_id, locale=contact.get("locale", "en"), route_id=route_id)
