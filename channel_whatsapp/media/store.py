from __future__ import annotations
from dataclasses import dataclass
from uuid import UUID
import httpx, structlog

logger = structlog.get_logger(__name__)


class StoreError(Exception):
    def __init__(self, *, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class StoreResult:
    evidence_id: UUID


def store_via_evidence_service(*, evidence_service_base_url, tenant_id, journey_id,
                                wamid, session_correlation_id, user_id, org_unit_id,
                                content, declared_name, mime, sha256, fidelity, service_token,
                                transport=None):
    headers = {"Authorization": f"Bearer {service_token}",
               "Idempotency-Key": f"wa:{wamid}",
               "X-Correlation-ID": session_correlation_id}
    data = {"evidencePurpose": "WHATSAPP_CAPTURE", "fidelity": fidelity, "sha256": sha256,
            "actor": f'{{"userId":"{user_id}","role":"PC"}}'}
    if journey_id is not None:
        data["journeyId"] = str(journey_id)
    files = {"file": (declared_name or "document", content, mime)}
    url = f"{evidence_service_base_url.rstrip('/')}/internal/evidence"
    kwargs = {"timeout": 30.0}
    if transport:
        kwargs["transport"] = transport
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.post(url, headers=headers, data=data, files=files)
    except httpx.HTTPError as exc:
        raise StoreError(code="EVIDENCE_NETWORK_ERROR", retryable=True) from exc
    if resp.status_code == 201:
        try:
            return StoreResult(evidence_id=UUID(resp.json()["evidenceId"]))
        except (ValueError, KeyError) as exc:
            raise StoreError(code="EVIDENCE_CONTRACT_ERROR", retryable=False) from exc
    if resp.status_code == 409:
        try:
            return StoreResult(evidence_id=UUID(resp.json()["evidenceId"]))
        except (ValueError, KeyError):
            pass
    raise StoreError(code=f"EVIDENCE_HTTP_{resp.status_code}", retryable=resp.status_code >= 500)
