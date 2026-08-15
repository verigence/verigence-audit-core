from __future__ import annotations

import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_DELIVERY_PATH = re.compile(r"^/v1/tenants/[^/]+/journeys/[^/]+/delivery$")


def install_contract_guards(app: FastAPI) -> None:
    @app.middleware("http")
    async def required_contract_headers(request: Request, call_next):
        if request.method == "PUT" and _DELIVERY_PATH.match(request.url.path):
            idempotency_key = request.headers.get("Idempotency-Key", "")
            if not 8 <= len(idempotency_key) <= 200:
                correlation_id = request.headers.get("X-Correlation-ID")
                body = {
                    "type": "about:blank",
                    "title": "Validation failed",
                    "status": 400,
                    "detail": "Idempotency-Key header is required for delivery observation updates.",
                    "errorCode": "VAC-VAL-001",
                }
                if correlation_id:
                    body["correlationId"] = correlation_id
                return JSONResponse(
                    status_code=400,
                    content=body,
                    media_type="application/problem+json",
                )
        return await call_next(request)
