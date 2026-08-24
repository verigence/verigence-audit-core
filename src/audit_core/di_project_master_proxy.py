from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import date
from io import BytesIO
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection, text

from audit_core import project_master_imports as local_imports
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_project_admin_request,
)
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import AuditCoreError, ValidationError
from audit_core.idempotency import stable_request_hash
from audit_core.project_masters import audit_core_catalogue

router = APIRouter(tags=["di-project-master-proxy"])

_ALLOWED_IMPORT_STATES = {
    "UPLOADED",
    "PARSING",
    "PREVIEW_READY",
    "VALIDATION_FAILED",
    "CONFIRMED",
    "CANCELLED",
    "FAILED",
}


def get_di_admin_client() -> Iterator[DiClient | None]:
    """Yield DI when configured without making local-master reads depend on DI config.

    Generic Project Master import routes serve both Audit Core and DI-owned imports.
    FastAPI resolves dependencies before the route body, so raising here when DI is
    unconfigured would incorrectly break an Audit Core-owned import before ownership
    can be inspected. DI-specific paths call _require_di_client and fail closed.
    """
    base_url = os.environ.get("DI_BASE_URL", "").strip()
    if not base_url:
        yield None
        return
    with DiClient(base_url=base_url) as client:
        yield client


def _require_di_client(di_client: DiClient | None) -> DiClient:
    if di_client is None:
        raise AuditCoreError(
            error_code="VAC-DI-001",
            status_code=503,
            title="Document Intelligence unavailable",
            detail="Document Intelligence administration is not configured.",
        )
    return di_client


def _raise_di_proxy_error(exc: DiClientError) -> None:
    del exc
    raise AuditCoreError(
        error_code="VAC-SYS-001",
        status_code=503,
        title="Document Intelligence administration failed",
        detail="Document Intelligence could not complete the Project Master operation.",
    )


def _di_version_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "versionId": raw.get("versionId"),
        "versionNo": raw.get("versionNo"),
        "effectiveFrom": None,
        "effectiveTo": None,
        "lifecycleStatus": raw.get("status"),
        "publishedAtUtc": raw.get("publishedAtUtc"),
        "retiredAtUtc": None,
        "overlapWarning": False,
        "businessKey": raw.get("businessKey"),
        "displayName": raw.get("displayName"),
    }


def _mirror_di_import(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
    payload: dict[str, Any],
    actor_id: str,
    idempotency_key: str | None = None,
) -> local_imports.ProjectMasterImportResponse:
    set_tenant_context(connection, tenant_id)
    import_id = UUID(str(payload["importId"]))
    current = connection.execute(
        text(
            """
            SELECT idempotency_key, semantic_request_hash
            FROM auditcore.project_master_imports
            WHERE tenant_id=:tenant_id AND import_id=:import_id
            """
        ),
        {"tenant_id": tenant_id, "import_id": import_id},
    ).mappings().one_or_none()

    effective_idempotency_key = (
        idempotency_key
        if idempotency_key is not None
        else (str(current["idempotency_key"]) if current is not None else f"di:{import_id}")
    )
    file_hash = str(payload.get("fileHashSha256") or "")
    semantic_hash = stable_request_hash(
        {
            "ownerModule": "DI",
            "masterKey": master_key,
            "fileHash": file_hash,
        }
    )
    status = str(payload.get("status") or "FAILED")
    if status not in _ALLOWED_IMPORT_STATES:
        status = "FAILED"

    if current is None:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.project_master_imports (
                    import_id, tenant_id, owner_module, master_key, effective_from,
                    template_version, original_file_name, file_hash,
                    idempotency_key, semantic_request_hash, status,
                    rows_parsed, valid_rows, warning_rows, error_rows,
                    created_by_user_id, confirmed_by_user_id, confirmed_at_utc
                ) VALUES (
                    :import_id, :tenant_id, 'DI', :master_key, NULL,
                    :template_version, :file_name, :file_hash,
                    :idempotency_key, :semantic_hash, :status,
                    :rows_parsed, :valid_rows, :warning_rows, :error_rows,
                    :created_by, :confirmed_by, :confirmed_at
                )
                """
            ),
            {
                "import_id": import_id,
                "tenant_id": tenant_id,
                "master_key": master_key,
                "template_version": payload.get("templateVersion"),
                "file_name": str(payload.get("fileName") or "di-import.xlsx"),
                "file_hash": file_hash,
                "idempotency_key": effective_idempotency_key,
                "semantic_hash": semantic_hash,
                "status": status,
                "rows_parsed": int(payload.get("rowsParsed") or 0),
                "valid_rows": int(payload.get("validRows") or 0),
                "warning_rows": int(payload.get("warningRows") or 0),
                "error_rows": int(payload.get("errorRows") or 0),
                "created_by": str(payload.get("createdByUserId") or actor_id),
                "confirmed_by": payload.get("confirmedByUserId"),
                "confirmed_at": payload.get("confirmedAtUtc"),
            },
        )
    else:
        connection.execute(
            text(
                """
                UPDATE auditcore.project_master_imports
                SET template_version=:template_version,
                    original_file_name=:file_name,
                    file_hash=:file_hash,
                    status=:status,
                    rows_parsed=:rows_parsed,
                    valid_rows=:valid_rows,
                    warning_rows=:warning_rows,
                    error_rows=:error_rows,
                    confirmed_by_user_id=:confirmed_by,
                    confirmed_at_utc=:confirmed_at,
                    version_no=version_no+1
                WHERE tenant_id=:tenant_id AND import_id=:import_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "import_id": import_id,
                "template_version": payload.get("templateVersion"),
                "file_name": str(payload.get("fileName") or "di-import.xlsx"),
                "file_hash": file_hash,
                "status": status,
                "rows_parsed": int(payload.get("rowsParsed") or 0),
                "valid_rows": int(payload.get("validRows") or 0),
                "warning_rows": int(payload.get("warningRows") or 0),
                "error_rows": int(payload.get("errorRows") or 0),
                "confirmed_by": payload.get("confirmedByUserId"),
                "confirmed_at": payload.get("confirmedAtUtc"),
            },
        )

    rows = payload.get("rows")
    if isinstance(rows, list):
        connection.execute(
            text(
                "DELETE FROM auditcore.project_master_import_rows "
                "WHERE tenant_id=:tenant_id AND import_id=:import_id"
            ),
            {"tenant_id": tenant_id, "import_id": import_id},
        )
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            validation_status = str(raw.get("validationStatus") or "ERROR")
            if validation_status not in {"VALID", "WARNING", "ERROR"}:
                validation_status = "ERROR"
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.project_master_import_rows (
                        tenant_id, import_id, row_number, parsed_data,
                        validation_status, validation_messages
                    ) VALUES (
                        :tenant_id, :import_id, :row_number,
                        CAST(:parsed_data AS jsonb), :validation_status,
                        CAST(:messages AS jsonb)
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "import_id": import_id,
                    "row_number": int(raw.get("rowNumber") or 1),
                    "parsed_data": json.dumps(raw.get("parsedData") or {}, default=str),
                    "validation_status": validation_status,
                    "messages": json.dumps(raw.get("validationMessages") or []),
                },
            )

    return local_imports._response(
        local_imports._import_row(connection, tenant_id=tenant_id, import_id=import_id)
    )


def _di_import_identity(connection: Connection, *, tenant_id: str, import_id: UUID):
    set_tenant_context(connection, tenant_id)
    return connection.execute(
        text(
            """
            SELECT owner_module, master_key
            FROM auditcore.project_master_imports
            WHERE tenant_id=:tenant_id AND import_id=:import_id
            """
        ),
        {"tenant_id": tenant_id, "import_id": import_id},
    ).mappings().one_or_none()


@router.get("/v1/tenants/{tenant_id}/project-masters")
def get_project_master_catalogue_proxy(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> list[dict[str, Any]]:
    set_tenant_context(connection, tenant_id)
    result = [
        item.model_dump(mode="json")
        for item in audit_core_catalogue(connection, tenant_id=tenant_id)
    ]
    client = _require_di_client(di_client)
    try:
        di_catalogue = client.list_project_masters(
            human_token=admin_request.bearer_token,
            tenant_id=tenant_id,
        )
        for descriptor in di_catalogue:
            master_key = str(descriptor.get("masterKey") or "")
            versions_payload = client.list_project_master_versions(
                human_token=admin_request.bearer_token,
                tenant_id=tenant_id,
                master_key=master_key,
            )
            versions = versions_payload.get("versions")
            current = None
            if isinstance(versions, list):
                current = next(
                    (
                        item
                        for item in versions
                        if isinstance(item, dict)
                        and item.get("status") in {"ACTIVE", "PUBLISHED"}
                    ),
                    next((item for item in versions if isinstance(item, dict)), None),
                )
            result.append(
                {
                    "ownerModule": "DI",
                    "masterKey": master_key,
                    "displayName": str(descriptor.get("displayName") or master_key),
                    "uploadMode": "EXCEL",
                    "administrationModes": list(
                        descriptor.get("administrationModes") or ["EXCEL"]
                    ),
                    "requiresWef": bool(descriptor.get("requiresWEF", False)),
                    "templateVersion": None,
                    "currentVersionId": current.get("versionId") if current else None,
                    "currentWef": None,
                    "lifecycleStatus": current.get("status") if current else None,
                }
            )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    return result


@router.get("/v1/tenants/{tenant_id}/project-masters/DI/{master_key}/template")
def download_di_master_template(
    tenant_id: str,
    master_key: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> StreamingResponse:
    client = _require_di_client(di_client)
    try:
        content, content_type = client.get_project_master_template(
            human_token=admin_request.bearer_token,
            tenant_id=tenant_id,
            master_key=master_key,
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    return StreamingResponse(
        BytesIO(content),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{master_key.lower()}-template.xlsx"'
        },
    )


@router.post(
    "/v1/tenants/{tenant_id}/project-masters/DI/{master_key}/imports",
    response_model=local_imports.ProjectMasterImportResponse,
    status_code=201,
)
async def upload_di_master_import(
    tenant_id: str,
    master_key: str,
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
    effective_from: Annotated[date | None, Form(alias="effectiveFrom")] = None,
) -> local_imports.ProjectMasterImportResponse:
    if effective_from is not None:
        raise ValidationError(
            detail="DI-owned Project Masters do not use WEF in the approved Phase-1 lifecycle."
        )
    content = await file.read()
    if not content:
        raise ValidationError(detail="Project Master import workbook is empty.")
    client = _require_di_client(di_client)
    try:
        payload = client.upload_project_master_import(
            human_token=admin_request.bearer_token,
            tenant_id=tenant_id,
            master_key=master_key,
            idempotency_key=idempotency_key,
            filename=file.filename or "upload.xlsx",
            content=content,
            content_type=(
                file.content_type
                or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    return _mirror_di_import(
        connection,
        tenant_id=tenant_id,
        master_key=master_key.strip().upper(),
        payload=payload,
        actor_id=admin_request.user_id,
        idempotency_key=idempotency_key,
    )


@router.get("/v1/tenants/{tenant_id}/project-masters/DI/{master_key}/versions")
def get_di_master_versions(
    tenant_id: str,
    master_key: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> list[dict[str, Any]]:
    client = _require_di_client(di_client)
    try:
        payload = client.list_project_master_versions(
            human_token=admin_request.bearer_token,
            tenant_id=tenant_id,
            master_key=master_key,
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    versions = payload.get("versions")
    if not isinstance(versions, list):
        raise AuditCoreError(
            error_code="VAC-SYS-001",
            status_code=502,
            title="Invalid Document Intelligence response",
            detail="Document Intelligence returned an invalid Project Master version response.",
        )
    return [_di_version_payload(item) for item in versions if isinstance(item, dict)]


@router.post(
    "/v1/tenants/{tenant_id}/project-masters/DI/{master_key}/versions/{version_id}/publish"
)
def publish_di_master_version(
    tenant_id: str,
    master_key: str,
    version_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> dict[str, Any]:
    client = _require_di_client(di_client)
    try:
        payload = client.publish_project_master_version(
            human_token=admin_request.bearer_token,
            tenant_id=tenant_id,
            master_key=master_key,
            version_id=str(version_id),
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    raw = payload.get("version")
    if not isinstance(raw, dict):
        raise AuditCoreError(
            error_code="VAC-SYS-001",
            status_code=502,
            title="Invalid Document Intelligence response",
            detail="Document Intelligence returned an invalid publish response.",
        )
    return _di_version_payload(raw)


@router.get(
    "/v1/tenants/{tenant_id}/project-master-imports/{import_id}",
    response_model=local_imports.ProjectMasterImportResponse,
)
def get_project_master_import_proxy(
    tenant_id: str,
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> local_imports.ProjectMasterImportResponse:
    identity = _di_import_identity(connection, tenant_id=tenant_id, import_id=import_id)
    if identity is None or identity["owner_module"] != "DI":
        return local_imports.get_master_import(
            tenant_id=tenant_id,
            import_id=import_id,
            admin_request=admin_request,
            connection=connection,
        )
    client = _require_di_client(di_client)
    master_key = str(identity["master_key"])
    try:
        payload = client.get_project_master_import(
            human_token=admin_request.bearer_token,
            tenant_id=tenant_id,
            master_key=master_key,
            import_id=str(import_id),
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    return _mirror_di_import(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
        payload=payload,
        actor_id=admin_request.user_id,
    )


@router.get(
    "/v1/tenants/{tenant_id}/project-master-imports/{import_id}/rows",
    response_model=local_imports.ImportRowsPage,
)
def get_project_master_import_rows_proxy(
    tenant_id: str,
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    validation_status: Annotated[
        local_imports.ValidationStatus | None,
        Query(alias="validationStatus"),
    ] = None,
) -> local_imports.ImportRowsPage:
    identity = _di_import_identity(connection, tenant_id=tenant_id, import_id=import_id)
    if identity is not None and identity["owner_module"] == "DI":
        client = _require_di_client(di_client)
        master_key = str(identity["master_key"])
        try:
            payload = client.get_project_master_import(
                human_token=admin_request.bearer_token,
                tenant_id=tenant_id,
                master_key=master_key,
                import_id=str(import_id),
            )
        except DiClientError as exc:
            _raise_di_proxy_error(exc)
        _mirror_di_import(
            connection,
            tenant_id=tenant_id,
            master_key=master_key,
            payload=payload,
            actor_id=admin_request.user_id,
        )
    return local_imports.get_master_import_rows(
        tenant_id=tenant_id,
        import_id=import_id,
        admin_request=admin_request,
        connection=connection,
        offset=offset,
        limit=limit,
        validation_status=validation_status,
    )


@router.get("/v1/tenants/{tenant_id}/project-master-imports/{import_id}/error-report")
def download_project_master_error_report_proxy(
    tenant_id: str,
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> StreamingResponse:
    identity = _di_import_identity(connection, tenant_id=tenant_id, import_id=import_id)
    if identity is None or identity["owner_module"] != "DI":
        return local_imports.download_import_error_report(
            tenant_id=tenant_id,
            import_id=import_id,
            admin_request=admin_request,
            connection=connection,
        )
    client = _require_di_client(di_client)
    master_key = str(identity["master_key"])
    try:
        response = client._request_raw(
            "GET",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/imports/{import_id}/error-report",
            operation="get_project_master_error_report",
            token=admin_request.bearer_token,
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    return StreamingResponse(
        BytesIO(response.content),
        media_type=response.headers.get("content-type", "text/csv"),
        headers={
            "Content-Disposition": response.headers.get(
                "content-disposition",
                f'attachment; filename="{master_key.lower()}-{import_id}-validation.csv"',
            )
        },
    )


@router.post(
    "/v1/tenants/{tenant_id}/project-master-imports/{import_id}/confirm",
    response_model=local_imports.ProjectMasterImportResponse,
)
def confirm_project_master_import_proxy(
    tenant_id: str,
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> local_imports.ProjectMasterImportResponse:
    identity = _di_import_identity(connection, tenant_id=tenant_id, import_id=import_id)
    if identity is None or identity["owner_module"] != "DI":
        return local_imports.confirm_master_import(
            tenant_id=tenant_id,
            import_id=import_id,
            admin_request=admin_request,
            connection=connection,
        )
    client = _require_di_client(di_client)
    master_key = str(identity["master_key"])
    try:
        payload = client.confirm_project_master_import(
            human_token=admin_request.bearer_token,
            tenant_id=tenant_id,
            master_key=master_key,
            import_id=str(import_id),
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    return _mirror_di_import(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
        payload=payload,
        actor_id=admin_request.user_id,
    )


@router.delete(
    "/v1/tenants/{tenant_id}/project-master-imports/{import_id}",
    status_code=204,
)
def delete_project_master_import_proxy(
    tenant_id: str,
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    di_client: Annotated[DiClient | None, Depends(get_di_admin_client)],
) -> None:
    identity = _di_import_identity(connection, tenant_id=tenant_id, import_id=import_id)
    if identity is None or identity["owner_module"] != "DI":
        return local_imports.delete_master_import(
            tenant_id=tenant_id,
            import_id=import_id,
            admin_request=admin_request,
            connection=connection,
        )
    client = _require_di_client(di_client)
    master_key = str(identity["master_key"])
    try:
        client._request_data(
            "POST",
            f"/v1/tenants/{tenant_id}/project-masters/{master_key}/imports/{import_id}/cancel",
            operation="cancel_project_master_import",
            token=admin_request.bearer_token,
        )
    except DiClientError as exc:
        _raise_di_proxy_error(exc)
    connection.execute(
        text(
            "DELETE FROM auditcore.project_master_imports "
            "WHERE tenant_id=:tenant_id AND import_id=:import_id"
        ),
        {"tenant_id": tenant_id, "import_id": import_id},
    )