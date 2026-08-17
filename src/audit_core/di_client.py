from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class DiClientError(Exception):
    status_code: int
    code: str
    retryable: bool

    def __str__(self) -> str:
        return f"DI request failed: {self.code}"


@dataclass(frozen=True)
class DiSubject:
    tenant_id: str
    subject_id: str
    subject_type: str
    display_name: str | None
    status: str


@dataclass(frozen=True)
class DiDocument:
    tenant_id: str
    subject_id: str
    document_id: str
    upload_status: str
    processing_status: str
    confirmation_status: str | None
    verification_state: str | None
    confidence_score: float | None
    correlation_id: str | None


@dataclass(frozen=True)
class DiFact:
    canonical_field_id: str
    field_key: str
    value: Any
    confidence_score: float | None


class DiClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DiClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_subject(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_type: str,
        display_name: str | None,
    ) -> DiSubject:
        response = self._client.post(
            f"/v1/tenants/{quote(tenant_id, safe='')}/subjects",
            headers=_headers(token),
            json={"subjectType": subject_type, "displayName": display_name},
        )
        payload = self._request_json(response)
        return _subject(payload, tenant_id=tenant_id)

    def upload_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        source_channel: str,
        document_type_key: str | None = None,
    ) -> DiDocument:
        data: dict[str, str] = {}
        if source_channel:
            data["sourceChannel"] = source_channel
        if document_type_key:
            data["documentTypeKey"] = document_type_key
        response = self._client.post(
            (
                f"/v1/tenants/{quote(tenant_id, safe='')}/subjects/"
                f"{quote(subject_id, safe='')}/documents"
            ),
            headers=_headers(token),
            files={"file": (filename, content, content_type)},
            data=data,
        )
        payload = self._request_json(response)
        return _document(payload, tenant_id=tenant_id, subject_id=subject_id)

    def get_document(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> DiDocument:
        response = self._client.get(
            (
                f"/v1/tenants/{quote(tenant_id, safe='')}/subjects/"
                f"{quote(subject_id, safe='')}/documents/{quote(document_id, safe='')}"
            ),
            headers=_headers(token),
        )
        payload = self._request_json(response)
        return _document(payload, tenant_id=tenant_id, subject_id=subject_id)

    def get_document_facts(
        self,
        *,
        token: str,
        tenant_id: str,
        subject_id: str,
        document_id: str,
    ) -> tuple[DiFact, ...]:
        response = self._client.get(
            (
                f"/v1/tenants/{quote(tenant_id, safe='')}/subjects/"
                f"{quote(subject_id, safe='')}/documents/{quote(document_id, safe='')}/fields"
            ),
            headers=_headers(token),
        )
        payload = self._request_json(response)
        return _facts(payload)

    def _request_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DiClientError(
                status_code=response.status_code,
                code="DI_INVALID_JSON",
                retryable=response.status_code >= 500,
            ) from exc

        if not isinstance(payload, dict):
            raise DiClientError(
                status_code=response.status_code,
                code="DI_CONTRACT_ERROR",
                retryable=False,
            )

        # D8 DI APIs wrap both success and business errors in the same envelope.
        if "errorCode" in payload and "data" in payload:
            error_code = payload.get("errorCode")
            if error_code != "000":
                code = f"DI_{error_code}" if isinstance(error_code, str) and error_code else "DI_ERROR"
                status_code = 409 if response.status_code < 400 else response.status_code
                raise DiClientError(status_code=status_code, code=code, retryable=status_code >= 500)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise _contract_error()
            payload = data

        if response.status_code >= 400:
            raise _response_error(response, payload)
        return payload


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _subject(payload: dict[str, Any], *, tenant_id: str) -> DiSubject:
    return DiSubject(
        tenant_id=_optional_str(payload, "tenantId") or tenant_id,
        subject_id=_required_str(payload, "subjectId"),
        subject_type=_required_str(payload, "subjectType"),
        display_name=_optional_str(payload, "displayName"),
        status=_required_str(payload, "status"),
    )


def _document(
    payload: dict[str, Any],
    *,
    subject_id: str,
    tenant_id: str | None = None,
) -> DiDocument:
    # New public DI responses are intentionally slim.  Preserve compatibility
    # with older full responses while leaving removed fields unset.
    return DiDocument(
        tenant_id=_optional_str(payload, "tenantId") or tenant_id or "",
        subject_id=_optional_str(payload, "subjectId") or subject_id,
        document_id=_required_str(payload, "documentId"),
        upload_status=_required_str(payload, "uploadStatus"),
        processing_status=_optional_str(payload, "processingStatus") or "UNKNOWN",
        confirmation_status=_optional_str(payload, "confirmationStatus"),
        verification_state=(
            _optional_str(payload, "verificationState")
            or _optional_str(payload, "humanVerificationStatus")
        ),
        confidence_score=_optional_float(payload, "confidenceScore"),
        correlation_id=_optional_str(payload, "correlationId"),
    )


def _facts(payload: dict[str, Any]) -> tuple[DiFact, ...]:
    rows = payload.get("facts")
    if rows is None:
        rows = payload.get("fields")
    if not isinstance(rows, list):
        raise _contract_error()
    facts: list[DiFact] = []
    for row in rows:
        if not isinstance(row, dict):
            raise _contract_error()
        value = row.get("value") if "value" in row else row.get("currentValue")
        facts.append(
            DiFact(
                canonical_field_id=(
                    _optional_str(row, "canonicalFieldId")
                    or _optional_str(row, "fieldId")
                    or _required_str(row, "fieldKey")
                ),
                field_key=_required_str(row, "fieldKey"),
                value=value,
                confidence_score=_optional_float(row, "confidenceScore"),
            )
        )
    return tuple(facts)


def _response_error(response: httpx.Response, payload: dict[str, Any]) -> DiClientError:
    code = "DI_ERROR"
    retryable = response.status_code >= 500
    candidate = payload.get("code") or payload.get("errorCode")
    if isinstance(candidate, str) and candidate:
        code = candidate
    retryable_value = payload.get("retryable")
    if isinstance(retryable_value, bool):
        retryable = retryable_value
    return DiClientError(status_code=response.status_code, code=code, retryable=retryable)


def _contract_error() -> DiClientError:
    return DiClientError(status_code=502, code="DI_CONTRACT_ERROR", retryable=False)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _contract_error()
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _contract_error()
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _contract_error()
    return value


def _optional_float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise _contract_error()
    if isinstance(value, int | float):
        return float(value)
    # DI's public document schema uses Decimal for document-level confidence.
    # JSON serialization represents Decimal values as numeric strings, while
    # field-level confidence is already emitted as a JSON number.
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError as exc:
            raise _contract_error() from exc
    raise _contract_error()
