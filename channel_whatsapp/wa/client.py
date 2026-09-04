"""WaClient — thin async wrapper around the Meta Cloud API v19 send endpoint.

This client is intentionally async (httpx.AsyncClient) because every outgoing
message is dispatched from the Procrastinate worker, which runs its own asyncio
event loop.  For the FastAPI path (webhook acknowledgement), the client is NOT
used — only the outbox table is written so the worker can fan-out.

Only call shapes used by the channel are implemented::

    - send_text(phone, body)             → plain text or caption
    - send_interactive_buttons(...)      → up to 3 quick-reply buttons
    - send_interactive_list(...)         → up to 10 list rows
    - send_template(...)                 → pre-approved HSM template
    - send_audio(phone, media_id)        → audio (voice note reply)

All methods return the upstream ``wamid`` (``messages[0].id``) on success.
On failure they raise ``WaApiError`` which carries ``status_code``, ``code``,
and ``retryable`` for the outbox backoff logic.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from channel_whatsapp.config import WhatsAppSettings

logger = structlog.get_logger(__name__)

_BASE = "https://graph.facebook.com/v19.0"
_TIMEOUT = httpx.Timeout(timeout=15.0, connect=5.0)


class WaApiError(Exception):
    """Raised when the Meta API returns an error or a network error occurs."""

    def __init__(self, *, status_code: int, code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class SendResult:
    wamid: str


class WaClient:
    """Async Meta Cloud API client — one instance per process, shared across workers."""

    def __init__(self, settings: WhatsAppSettings) -> None:
        self._token = settings.wa_access_token.get_secret_value()
        self._client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Public send methods
    # ------------------------------------------------------------------

    async def send_text(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        preview_url: bool = False,
    ) -> SendResult:
        """Send a plain-text message. ``body`` must be ≤ 4096 chars."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        return await self._post(phone_number_id, payload)

    async def send_interactive_buttons(
        self,
        *,
        phone_number_id: str,
        to: str,
        header_text: str | None,
        body_text: str,
        footer_text: str | None,
        buttons: list[dict[str, str]],
    ) -> SendResult:
        """Send an interactive message with up to 3 quick-reply buttons.

        Each entry in ``buttons`` is ``{"id": "<payload>", "title": "<label>"``}``.
        ``id`` must be ≤ 256 chars; ``title`` must be ≤ 20 chars.
        """
        if len(buttons) > 3:
            raise ValueError("Meta allows at most 3 quick-reply buttons")
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons
                ]
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        return await self._post(phone_number_id, payload)

    async def send_interactive_list(
        self,
        *,
        phone_number_id: str,
        to: str,
        header_text: str | None,
        body_text: str,
        footer_text: str | None,
        button_label: str,
        sections: list[dict[str, Any]],
    ) -> SendResult:
        """Send an interactive list message.  ``sections`` follows Meta schema."""
        interactive: dict[str, Any] = {
            "type": "list",
            "body": {"text": body_text},
            "action": {"button": button_label, "sections": sections},
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
        return await self._post(phone_number_id, payload)

    async def send_template(
        self,
        *,
        phone_number_id: str,
        to: str,
        template_name: str,
        language_code: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        """Send a pre-approved HSM template (e.g. booking_confirmation, delivery_reminder)."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components or [],
            },
        }
        return await self._post(phone_number_id, payload)

    async def send_audio(
        self,
        *,
        phone_number_id: str,
        to: str,
        media_id: str,
    ) -> SendResult:
        """Reply with a WhatsApp-hosted audio message (e.g. voice confirmation)."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "audio",
            "audio": {"id": media_id},
        }
        return await self._post(phone_number_id, payload)

    async def mark_read(
        self,
        *,
        phone_number_id: str,
        wamid: str,
    ) -> None:
        """Mark an inbound message as read (blue ticks). Best-effort — errors are swallowed."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wamid,
        }
        url = f"{_BASE}/{phone_number_id}/messages"
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wa_mark_read_failed", wamid=wamid, error=str(exc))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post(self, phone_number_id: str, payload: dict[str, Any]) -> SendResult:
        url = f"{_BASE}/{phone_number_id}/messages"
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise WaApiError(
                status_code=0,
                code="NETWORK_ERROR",
                message=str(exc),
                retryable=True,
            ) from exc
        if resp.status_code == 200:
            try:
                data = resp.json()
                wamid: str = data["messages"][0]["id"]
                logger.info(
                    "wa_send_ok",
                    phone_number_id=phone_number_id,
                    to=payload.get("to"),
                    kind=payload.get("type"),
                    wamid=wamid,
                )
                return SendResult(wamid=wamid)
            except (KeyError, IndexError, ValueError) as exc:
                raise WaApiError(
                    status_code=resp.status_code,
                    code="CONTRACT_ERROR",
                    message=f"unexpected response shape: {exc}",
                    retryable=False,
                ) from exc
        # Error path — parse Meta error envelope
        code = "HTTP_ERROR"
        message = resp.text
        try:
            err = resp.json().get("error", {})
            code = str(err.get("code", code))
            message = err.get("message", message)
        except Exception:  # noqa: BLE001
            pass
        retryable = resp.status_code >= 500 or resp.status_code == 429
        logger.error(
            "wa_send_failed",
            phone_number_id=phone_number_id,
            status=resp.status_code,
            meta_code=code,
            meta_message=message,
        )
        raise WaApiError(
            status_code=resp.status_code,
            code=code,
            message=message,
            retryable=retryable,
        )


# ---------------------------------------------------------------------------
# Module-level singleton (set during lifespan startup)
# ---------------------------------------------------------------------------
_wa_client: WaClient | None = None


def set_wa_client(client: WaClient) -> None:
    global _wa_client  # noqa: PLW0603
    _wa_client = client


def get_wa_client() -> WaClient:
    if _wa_client is None:
        raise RuntimeError("WaClient not initialised — call set_wa_client() in lifespan startup")
    return _wa_client


def clear_wa_client() -> None:
    global _wa_client  # noqa: PLW0603
    _wa_client = None
