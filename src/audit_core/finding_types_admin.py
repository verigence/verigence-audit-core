"""finding_types_admin.py — Super Admin surface for the audit finding-type registry.

  GET /v1/admin/finding-types[?status=UNCLASSIFIED]
  PUT /v1/admin/finding-types/{finding_type_code}   { findingClass, ... }

A type auto-registered as UNCLASSIFIED on first use shows up here for a Super
Admin to give it a class; the change takes effect on the next finding (the
in-process registry cache has a short TTL).
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from audit_core.db import set_platform_super_admin_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.errors import NotFoundError

router = APIRouter(prefix="/v1/admin", tags=["admin-finding-types"])

FindingClass = Literal["DATA_GAP", "DOCUMENT_GAP", "VIOLATION"]
_OWNER = {"DATA_GAP": "PC", "DOCUMENT_GAP": "PC", "VIOLATION": "TL"}
_MODE = {"DATA_GAP": "SELF_SERVICE", "DOCUMENT_GAP": "SELF_SERVICE", "VIOLATION": "ADJUDICATED"}


class FindingTypeView(BaseModel):
    findingTypeCode: str
    findingClass: str
    defaultOwnerRole: str
    resolutionMode: str
    status: str
    description: str | None
    updatedAtUtc: str


class FindingTypeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findingClass: FindingClass
    defaultOwnerRole: str | None = None
    status: Literal["ACTIVE", "RETIRED"] = "ACTIVE"


def _row_to_view(row) -> FindingTypeView:
    return FindingTypeView(
        findingTypeCode=row["finding_type_code"],
        findingClass=row["finding_class"],
        defaultOwnerRole=row["default_owner_role"],
        resolutionMode=row["resolution_mode"],
        status=row["status"],
        description=row["description"],
        updatedAtUtc=row["updated_at_utc"].isoformat(),
    )


@router.get("/finding-types", response_model=list[FindingTypeView])
def list_finding_types(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    status: Annotated[str | None, Query()] = None,
) -> list[FindingTypeView]:
    del admin_request
    set_platform_super_admin_context(connection)
    clause = "WHERE status = :status" if status else ""
    rows = connection.execute(
        text(
            f"""
            SELECT finding_type_code, finding_class, default_owner_role,
                   resolution_mode, status, description, updated_at_utc
            FROM auditcore.finding_types
            {clause}
            ORDER BY (status = 'UNCLASSIFIED') DESC, finding_type_code
            """
        ),
        {"status": status.strip().upper()} if status else {},
    ).mappings().all()
    return [_row_to_view(row) for row in rows]


@router.put("/finding-types/{finding_type_code}", response_model=FindingTypeView)
def set_finding_type_class(
    finding_type_code: str,
    payload: FindingTypeUpdate,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FindingTypeView:
    del admin_request
    set_platform_super_admin_context(connection)
    code = finding_type_code.strip().upper()
    owner = payload.defaultOwnerRole or _OWNER[payload.findingClass]
    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.finding_types
                (finding_type_code, finding_class, default_owner_role, resolution_mode, status)
            VALUES (:code, :cls, :owner, :mode, :status)
            ON CONFLICT (finding_type_code) DO UPDATE SET
                finding_class = EXCLUDED.finding_class,
                default_owner_role = EXCLUDED.default_owner_role,
                resolution_mode = EXCLUDED.resolution_mode,
                status = EXCLUDED.status
            RETURNING finding_type_code, finding_class, default_owner_role,
                      resolution_mode, status, description, updated_at_utc
            """
        ),
        {
            "code": code,
            "cls": payload.findingClass,
            "owner": owner,
            "mode": _MODE[payload.findingClass],
            "status": payload.status,
        },
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-021",
            title="Finding type not found",
            detail="The finding type could not be written.",
        )
    return _row_to_view(row)
