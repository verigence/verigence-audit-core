from __future__ import annotations
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from channel_whatsapp.config import WhatsAppSettings, get_wa_settings
from channel_whatsapp.wa.webhook import insert_inbox, verify_signature

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/wa", tags=["whatsapp-channel"])


@router.get("/webhook")
def verify_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
    settings: WhatsAppSettings = Depends(get_wa_settings),
) -> Response:
    if (hub_mode == "subscribe"
            and hub_verify_token == settings.wa_verify_token.get_secret_value()
            and hub_challenge):
        logger.info("wa_webhook_verified")
        return Response(content=hub_challenge, media_type="text/plain")
    logger.warning("wa_webhook_verify_failed", mode=hub_mode)
    return Response(status_code=403)


@router.post("/webhook", status_code=200)
async def receive_webhook(
    request: Request,
    settings: WhatsAppSettings = Depends(get_wa_settings),
) -> dict:
    """Receive inbound WhatsApp events. Always returns 200."""
    raw_body = await request.body()
    hub_sig = request.headers.get("X-Hub-Signature-256")
    sig_ok = verify_signature(raw_body=raw_body, hub_signature=hub_sig,
                               app_secret=settings.wa_app_secret.get_secret_value())
    if not sig_ok:
        logger.warning("wa_webhook_signature_invalid",
                       remote=request.client.host if request.client else "unknown")
    from channel_whatsapp.worker import dispatch_inbox_processing
    from audit_core.db import get_sync_engine  # type: ignore[attr-defined]
    engine = get_sync_engine()
    with engine.begin() as connection:
        inbox_id = insert_inbox(connection=connection, raw_body=raw_body, signature_ok=sig_ok)
    if inbox_id is not None:
        try:
            await dispatch_inbox_processing(inbox_id=inbox_id)
        except Exception as exc:
            logger.error("wa_dispatch_failed", inbox_id=inbox_id, error=str(exc))
    return {}
