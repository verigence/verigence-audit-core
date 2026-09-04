from __future__ import annotations
import hashlib
import hmac
import json
from typing import Any
import structlog
from sqlalchemy import Connection, text

logger = structlog.get_logger(__name__)


def verify_signature(*, raw_body: bytes, hub_signature: str | None, app_secret: str) -> bool:
    """Return True if X-Hub-Signature-256 header matches the payload HMAC."""
    if not hub_signature:
        return False
    if not hub_signature.startswith("sha256="):
        return False
    received = hub_signature.removeprefix("sha256=")
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def _extract_wamid(payload: dict[str, Any]) -> str | None:
    try:
        entries = payload.get("entry", [])
        changes = entries[0].get("changes", [])
        messages = changes[0].get("value", {}).get("messages", [])
        if messages:
            return messages[0].get("id")
    except (IndexError, AttributeError, KeyError):
        pass
    return None


def _extract_phone_number_id(payload: dict[str, Any]) -> str | None:
    try:
        entries = payload.get("entry", [])
        changes = entries[0].get("changes", [])
        return changes[0].get("value", {}).get("metadata", {}).get("phone_number_id")
    except (IndexError, AttributeError, KeyError):
        return None


def insert_inbox(*, connection: Connection, raw_body: bytes, signature_ok: bool) -> int | None:
    """Write one row to wa.inbox; return the row id. Duplicate wamids are no-ops."""
    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except ValueError:
        logger.warning("wa_webhook_unparseable_body")
        return None

    wamid = _extract_wamid(payload)
    phone_number_id = _extract_phone_number_id(payload)

    if wamid is not None:
        exists = connection.execute(
            text("SELECT 1 FROM wa.inbox WHERE wamid = :wamid"), {"wamid": wamid}
        ).scalar_one_or_none()
        if exists is not None:
            logger.debug("wa_inbox_duplicate_skipped", wamid=wamid)
            return None

    row = connection.execute(
        text("""
            INSERT INTO wa.inbox (wamid, phone_number_id, payload, signature_ok)
            VALUES (:wamid, :phone_number_id, CAST(:payload AS jsonb), :sig_ok)
            RETURNING id
        """),
        {"wamid": wamid, "phone_number_id": phone_number_id,
         "payload": json.dumps(payload), "sig_ok": signature_ok},
    ).scalar_one()

    logger.info("wa_inbox_written", inbox_id=row, wamid=wamid,
                signature_ok=signature_ok, phone_number_id=phone_number_id)
    return row
