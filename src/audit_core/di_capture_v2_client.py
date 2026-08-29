from __future__ import annotations

from typing import Any, Self

import httpx


class DiCaptureV2Error(RuntimeError):
    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class DiCaptureV2Client:
    """Audit Core adapter for the additive DI Document Capture V2 contract."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("DI base URL is required")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def create_upload_intents(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        phase: str,
        candidate_document_type_keys: list[str],
        files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v2/tenants/{tenant_id}/audit-storage-contexts/"
            f"{external_context_ref}/capture-documents:init",
            token=token,
            json={
                "phase": phase,
                "candidateDocumentTypeKeys": candidate_document_type_keys,
                "files": files,
            },
        )

    def finalize_document(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        document_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v2/tenants/{tenant_id}/audit-storage-contexts/"
            f"{external_context_ref}/capture-documents/{document_id}:finalize",
            token=token,
        )

    def list_documents(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        phase: str,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v2/tenants/{tenant_id}/audit-storage-contexts/"
            f"{external_context_ref}/capture-documents",
            token=token,
            params={"phase": phase},
        )

    def delete_document(
        self,
        *,
        token: str,
        tenant_id: str,
        external_context_ref: str,
        document_id: str,
    ) -> None:
        self._request(
            "DELETE",
            f"/v2/tenants/{tenant_id}/audit-storage-contexts/"
            f"{external_context_ref}/capture-documents/{document_id}",
            token=token,
            expect_body=False,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expect_body: bool = True,
    ) -> dict[str, Any]:
        response = self._client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            json=json,
            params=params,
        )
        if not 200 <= response.status_code < 300:
            detail = response.text[:1000] or f"HTTP {response.status_code}"
            raise DiCaptureV2Error(status_code=response.status_code, detail=detail)
        if not expect_body or response.status_code == 204:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise DiCaptureV2Error(
                status_code=502,
                detail="DI V2 returned an invalid response payload",
            )
        return payload
