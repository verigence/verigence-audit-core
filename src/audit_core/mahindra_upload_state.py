from __future__ import annotations

from datetime import date
from io import BytesIO
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import Connection, text

from audit_core import mahindra_masters as masters
from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_project_admin_request,
)
from audit_core.errors import ValidationError

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/mahindra-masters",
    tags=["mahindra-masters"],
)


def _effective_from(
    validated_rows: list[tuple[int, dict[str, Any], list[str]]],
    supplied: date | None,
) -> date:
    detected = {
        str(row.get("source_effective_from")).strip()
        for _, row, _ in validated_rows
        if row.get("source_effective_from")
    }
    if len(detected) == 1:
        try:
            return date.fromisoformat(next(iter(detected)))
        except ValueError:
            pass
    if supplied is not None:
        return supplied
    raise ValidationError(
        detail=(
            "Effective From / WEF could not be detected from this workbook. "
            "Enter Effective From only for a workbook that does not carry its own WEF."
        )
    )


def _lifecycle_status(connection: Connection, tenant_id: str, row) -> str | None:
    if row["status"] != "CONFIRMED":
        return None
    receipt = dict(row["confirmation_receipt"] or {})
    if row["master_key"] == masters._SEGMENT_MASTER_KEY:
        product_id = receipt.get("productMasterVersionId")
        price_id = receipt.get("priceListVersionId")
        if not product_id or not price_id:
            return "DRAFT"
        product_status = connection.execute(
            text(
                "SELECT lifecycle_status FROM auditcore.project_product_master_versions "
                "WHERE tenant_id=:tenant_id AND version_id=CAST(:version_id AS uuid)"
            ),
            {"tenant_id": tenant_id, "version_id": str(product_id)},
        ).scalar_one_or_none()
        price_status = connection.execute(
            text(
                "SELECT lifecycle_status FROM auditcore.price_list_versions "
                "WHERE tenant_id=:tenant_id AND price_list_version_id=CAST(:version_id AS uuid)"
            ),
            {"tenant_id": tenant_id, "version_id": str(price_id)},
        ).scalar_one_or_none()
        return "PUBLISHED" if product_status == "PUBLISHED" and price_status == "PUBLISHED" else "DRAFT"
    if row["master_key"] == masters._DISCOUNT_POLICY_KEY:
        policy_id = receipt.get("discountPolicyVersionId")
        if not policy_id:
            return "DRAFT"
        status = connection.execute(
            text(
                "SELECT lifecycle_status FROM auditcore.discount_policy_versions "
                "WHERE tenant_id=:tenant_id AND discount_policy_version_id=CAST(:version_id AS uuid)"
            ),
            {"tenant_id": tenant_id, "version_id": str(policy_id)},
        ).scalar_one_or_none()
        return str(status) if status else "DRAFT"
    return None


@router.get("/imports", response_model=list[masters.MahindraImportResponse])
def list_latest_imports(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[masters.MahindraImportResponse]:
    del admin_request
    set_tenant_context(connection, tenant_id)
    masters._project_context(connection, tenant_id)
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (i.master_key, COALESCE(i.segment_id::text, ''))
                   i.import_id, i.master_key, i.segment_id, i.effective_from,
                   i.original_file_name, i.status, i.rows_parsed, i.valid_rows,
                   i.error_rows, i.confirmation_receipt, s.segment_code
            FROM auditcore.project_master_imports i
            LEFT JOIN auditcore.segments s ON s.segment_id=i.segment_id
            WHERE i.tenant_id=:tenant_id
              AND i.owner_module='AUDIT_CORE'
              AND i.master_key IN ('MAHINDRA_SEGMENT_MASTER','DISCOUNT_POLICY')
            ORDER BY i.master_key, COALESCE(i.segment_id::text, ''),
                     i.created_at_utc DESC, i.import_id DESC
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()
    return [
        masters._response(
            row,
            lifecycle_status=_lifecycle_status(connection, tenant_id, row),
        )
        for row in rows
    ]


@router.get("/imports/{import_id}/validation-report")
def validation_report(
    tenant_id: str,
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> StreamingResponse:
    del admin_request
    set_tenant_context(connection, tenant_id)
    masters._project_context(connection, tenant_id)
    record = masters._import_record(connection, tenant_id, import_id)
    rows = connection.execute(
        text(
            """
            SELECT row_number, parsed_data, validation_status, validation_messages
            FROM auditcore.project_master_import_rows
            WHERE tenant_id=:tenant_id AND import_id=:import_id
            ORDER BY row_number
            """
        ),
        {"tenant_id": tenant_id, "import_id": import_id},
    ).mappings().all()

    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in dict(row["parsed_data"]):
            if key not in seen:
                seen.add(key)
                keys.append(key)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Validation"
    sheet.append(["row_number", "validation_status", "messages", *keys])
    for row in rows:
        parsed = dict(row["parsed_data"])
        sheet.append(
            [
                row["row_number"],
                row["validation_status"],
                " | ".join(row["validation_messages"]),
                *[parsed.get(key) for key in keys],
            ]
        )
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    filename = f"{str(record['master_key']).lower()}-{import_id}-validation.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/segments/{segment_id}/native-imports",
    response_model=masters.MahindraImportResponse,
    status_code=201,
)
async def upload_segment_master(
    tenant_id: str,
    segment_id: UUID,
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    effective_from: Annotated[date | None, Form(alias="effectiveFrom")] = None,
) -> masters.MahindraImportResponse:
    set_tenant_context(connection, tenant_id)
    segment = masters._require_selected_segment(connection, tenant_id, segment_id)
    content = await file.read()
    if not content:
        raise ValidationError(detail="Master workbook is empty.")
    rows = masters._workbook_rows(
        content,
        master_key=masters._SEGMENT_MASTER_KEY,
        segment_code=segment["segment_code"],
    )
    validated = masters._validate_segment_rows(rows)
    resolved_wef = _effective_from(validated, effective_from)
    import_id = masters._stage_import(
        connection,
        tenant_id=tenant_id,
        master_key=masters._SEGMENT_MASTER_KEY,
        segment_id=segment_id,
        effective_from=resolved_wef,
        file_name=file.filename or "mahindra-segment-master.xlsx",
        file_content=content,
        idempotency_key=idempotency_key,
        actor_id=admin_request.user_id,
        validated_rows=validated,
    )
    return masters._response(masters._import_record(connection, tenant_id, import_id))


@router.post(
    "/discount-policy/native-imports",
    response_model=masters.MahindraImportResponse,
    status_code=201,
)
async def upload_discount_policy(
    tenant_id: str,
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    effective_from: Annotated[date | None, Form(alias="effectiveFrom")] = None,
) -> masters.MahindraImportResponse:
    set_tenant_context(connection, tenant_id)
    masters._project_context(connection, tenant_id)
    content = await file.read()
    if not content:
        raise ValidationError(detail="Master workbook is empty.")
    rows = masters._workbook_rows(content, master_key=masters._DISCOUNT_POLICY_KEY)
    validated = masters._validate_policy_rows(connection, tenant_id, rows)
    resolved_wef = _effective_from(validated, effective_from)
    import_id = masters._stage_import(
        connection,
        tenant_id=tenant_id,
        master_key=masters._DISCOUNT_POLICY_KEY,
        segment_id=None,
        effective_from=resolved_wef,
        file_name=file.filename or "mahindra-discount-policy.xlsx",
        file_content=content,
        idempotency_key=idempotency_key,
        actor_id=admin_request.user_id,
        validated_rows=validated,
    )
    return masters._response(masters._import_record(connection, tenant_id, import_id))
