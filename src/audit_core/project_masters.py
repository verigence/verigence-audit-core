from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.discount_schemes import (
    publish_discount_scheme_version,
    retire_discount_scheme_version,
)
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.price_lists import publish_price_list_version, retire_price_list_version
from audit_core.product_masters import (
    publish_project_product_master_version,
    retire_project_product_master_version,
)
from audit_core.versioned_masters import publish_master_version, retire_master_version

router = APIRouter(prefix="/v1/tenants/{tenant_id}/project-masters", tags=["project-masters"])

OwnerModule = Literal["AUDIT_CORE", "DI"]
UploadMode = Literal["EXCEL", "FORM"]


class MasterDescriptor(BaseModel):
    ownerModule: OwnerModule
    masterKey: str
    displayName: str
    uploadMode: UploadMode
    administrationModes: list[str]
    requiresWef: bool
    templateVersion: str | None
    currentVersionId: UUID | None
    currentWef: date | None
    lifecycleStatus: str | None


class MasterVersionResponse(BaseModel):
    versionId: UUID
    versionNo: int
    effectiveFrom: date | None
    effectiveTo: date | None
    lifecycleStatus: str
    publishedAtUtc: datetime | None
    retiredAtUtc: datetime | None
    overlapWarning: bool = False


_AUDIT_CORE_DESCRIPTORS = (
    {
        "masterKey": "PRODUCT_MASTER",
        "displayName": "Product Master",
        "uploadMode": "EXCEL",
        "administrationModes": ["EXCEL"],
        "requiresWef": True,
        "templateVersion": "1.0",
    },
    {
        "masterKey": "PRICE_LIST",
        "displayName": "Price Lists",
        "uploadMode": "EXCEL",
        "administrationModes": ["EXCEL"],
        "requiresWef": True,
        "templateVersion": "1.0",
    },
    {
        "masterKey": "DISCOUNT_SCHEME",
        "displayName": "Discount Schemes",
        "uploadMode": "EXCEL",
        "administrationModes": ["EXCEL"],
        "requiresWef": True,
        "templateVersion": "1.0",
    },
    {
        "masterKey": "DOCUMENT_REQUIREMENT_PROFILE",
        "displayName": "Document Requirement Profiles",
        "uploadMode": "FORM",
        "administrationModes": ["FORM"],
        "requiresWef": True,
        "templateVersion": None,
    },
    {
        "masterKey": "AUDIT_CONTROL",
        "displayName": "Audit Controls",
        "uploadMode": "FORM",
        "administrationModes": ["FORM"],
        "requiresWef": True,
        "templateVersion": None,
    },
    {
        "masterKey": "PROJECT_POLICY",
        "displayName": "Project Policy",
        "uploadMode": "FORM",
        "administrationModes": ["FORM"],
        "requiresWef": True,
        "templateVersion": None,
    },
    {
        "masterKey": "BUSINESS_STATUS_CODES",
        "displayName": "Business Status Codes",
        "uploadMode": "FORM",
        "administrationModes": ["FORM"],
        "requiresWef": False,
        "templateVersion": None,
    },
)


def descriptor_definition(master_key: str) -> dict[str, object]:
    normalized = master_key.strip().upper()
    for descriptor in _AUDIT_CORE_DESCRIPTORS:
        if descriptor["masterKey"] == normalized:
            return dict(descriptor)
    raise NotFoundError(
        error_code="VAC-NF-006",
        title="Project Master not found",
        detail="The requested Audit Core Project Master is not registered.",
    )


def excel_master_keys() -> frozenset[str]:
    return frozenset(
        str(descriptor["masterKey"])
        for descriptor in _AUDIT_CORE_DESCRIPTORS
        if descriptor["uploadMode"] == "EXCEL"
    )


def _current_version_summary(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
) -> dict[str, object] | None:
    queries = {
        "PRODUCT_MASTER": """
            SELECT version_id, effective_from, lifecycle_status
            FROM auditcore.project_product_master_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC
            LIMIT 1
        """,
        "PRICE_LIST": """
            SELECT price_list_version_id AS version_id, effective_from, lifecycle_status
            FROM auditcore.price_list_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, price_list_version_id
            LIMIT 1
        """,
        "DISCOUNT_SCHEME": """
            SELECT discount_scheme_version_id AS version_id, effective_from, lifecycle_status
            FROM auditcore.discount_scheme_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, discount_scheme_version_id
            LIMIT 1
        """,
        "DOCUMENT_REQUIREMENT_PROFILE": """
            SELECT document_requirement_profile_version_id AS version_id,
                   effective_from, lifecycle_status
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC,
                     document_requirement_profile_version_id
            LIMIT 1
        """,
        "AUDIT_CONTROL": """
            SELECT audit_control_version_id AS version_id, effective_from, lifecycle_status
            FROM auditcore.audit_control_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, audit_control_version_id
            LIMIT 1
        """,
        "PROJECT_POLICY": """
            SELECT policy_version_id AS version_id, effective_from, lifecycle_status
            FROM auditcore.project_policy_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, policy_version_id
            LIMIT 1
        """,
    }
    query = queries.get(master_key)
    if query is None:
        return None
    row = connection.execute(text(query), {"tenant_id": tenant_id}).mappings().one_or_none()
    return dict(row) if row is not None else None


def audit_core_catalogue(
    connection: Connection,
    *,
    tenant_id: str,
) -> list[MasterDescriptor]:
    descriptors: list[MasterDescriptor] = []
    for definition in _AUDIT_CORE_DESCRIPTORS:
        master_key = str(definition["masterKey"])
        current = _current_version_summary(
            connection,
            tenant_id=tenant_id,
            master_key=master_key,
        )
        descriptors.append(
            MasterDescriptor(
                ownerModule="AUDIT_CORE",
                masterKey=master_key,
                displayName=str(definition["displayName"]),
                uploadMode=str(definition["uploadMode"]),  # type: ignore[arg-type]
                administrationModes=list(definition["administrationModes"]),
                requiresWef=bool(definition["requiresWef"]),
                templateVersion=(
                    str(definition["templateVersion"])
                    if definition["templateVersion"] is not None
                    else None
                ),
                currentVersionId=(current["version_id"] if current else None),
                currentWef=(current["effective_from"] if current else None),
                lifecycleStatus=(str(current["lifecycle_status"]) if current else None),
            )
        )
    return descriptors


def _version_rows(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
) -> list[dict[str, object]]:
    queries = {
        "PRODUCT_MASTER": """
            SELECT version_id, version_no, effective_from, NULL::date AS effective_to,
                   lifecycle_status, published_at_utc, retired_at_utc
            FROM auditcore.project_product_master_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC
        """,
        "PRICE_LIST": """
            SELECT price_list_version_id AS version_id, version_no, effective_from,
                   effective_to, lifecycle_status, published_at_utc, retired_at_utc
            FROM auditcore.price_list_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, price_list_version_id
        """,
        "DISCOUNT_SCHEME": """
            SELECT discount_scheme_version_id AS version_id, version_no, effective_from,
                   effective_to, lifecycle_status, published_at_utc, retired_at_utc
            FROM auditcore.discount_scheme_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, discount_scheme_version_id
        """,
        "DOCUMENT_REQUIREMENT_PROFILE": """
            SELECT document_requirement_profile_version_id AS version_id, version_no,
                   effective_from, effective_to, lifecycle_status,
                   published_at_utc, retired_at_utc
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC,
                     document_requirement_profile_version_id
        """,
        "AUDIT_CONTROL": """
            SELECT audit_control_version_id AS version_id, version_no, effective_from,
                   effective_to, lifecycle_status, published_at_utc, retired_at_utc
            FROM auditcore.audit_control_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, audit_control_version_id
        """,
        "PROJECT_POLICY": """
            SELECT policy_version_id AS version_id, version_no, effective_from,
                   effective_to, lifecycle_status, published_at_utc, retired_at_utc
            FROM auditcore.project_policy_versions
            WHERE tenant_id=:tenant_id
            ORDER BY effective_from DESC, version_no DESC, policy_version_id
        """,
    }
    query = queries.get(master_key)
    if query is None:
        if master_key == "BUSINESS_STATUS_CODES":
            return []
        descriptor_definition(master_key)
        return []
    return [
        dict(row)
        for row in connection.execute(text(query), {"tenant_id": tenant_id}).mappings().all()
    ]


def _periods_overlap(
    left_start: date | None,
    left_end: date | None,
    right_start: date | None,
    right_end: date | None,
) -> bool:
    if left_start is None or right_start is None:
        return False
    return (right_end is None or left_start <= right_end) and (
        left_end is None or right_start <= left_end
    )


def master_versions(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
) -> list[MasterVersionResponse]:
    rows = _version_rows(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
    )
    response: list[MasterVersionResponse] = []
    for row in rows:
        overlap = False
        if row["lifecycle_status"] == "PUBLISHED":
            for other in rows:
                if other["version_id"] == row["version_id"]:
                    continue
                if other["lifecycle_status"] != "PUBLISHED":
                    continue
                if _periods_overlap(
                    row["effective_from"],
                    row["effective_to"],
                    other["effective_from"],
                    other["effective_to"],
                ):
                    overlap = True
                    break
        response.append(
            MasterVersionResponse(
                versionId=row["version_id"],
                versionNo=int(row["version_no"]),
                effectiveFrom=row["effective_from"],
                effectiveTo=row["effective_to"],
                lifecycleStatus=str(row["lifecycle_status"]),
                publishedAtUtc=row["published_at_utc"],
                retiredAtUtc=row["retired_at_utc"],
                overlapWarning=overlap,
            )
        )
    return response


def _ensure_version_exists(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
    version_id: UUID,
) -> MasterVersionResponse:
    for version in master_versions(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
    ):
        if version.versionId == version_id:
            return version
    raise NotFoundError(
        error_code="VAC-NF-007",
        title="Project Master version not found",
        detail="The requested Project Master version does not exist for this Project.",
    )


def publish_audit_core_version(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
    version_id: UUID,
    actor_id: str,
) -> MasterVersionResponse:
    current = _ensure_version_exists(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
        version_id=version_id,
    )
    if current.lifecycleStatus == "PUBLISHED":
        return current
    if master_key == "PRODUCT_MASTER":
        publish_project_product_master_version(
            connection,
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "PRICE_LIST":
        publish_price_list_version(
            connection,
            tenant_id=tenant_id,
            price_list_version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "DISCOUNT_SCHEME":
        publish_discount_scheme_version(
            connection,
            tenant_id=tenant_id,
            discount_scheme_version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "DOCUMENT_REQUIREMENT_PROFILE":
        publish_master_version(
            connection,
            master_type="DOCUMENT_PROFILE",
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "AUDIT_CONTROL":
        publish_master_version(
            connection,
            master_type="AUDIT_CONTROL",
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "PROJECT_POLICY":
        publish_master_version(
            connection,
            master_type="POLICY",
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    else:
        raise AuditCoreError(
            error_code="VAC-MASTER-004",
            status_code=422,
            title="Master lifecycle unsupported",
            detail="This Project Master does not use the version publish lifecycle.",
        )
    return _ensure_version_exists(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
        version_id=version_id,
    )


def retire_audit_core_version(
    connection: Connection,
    *,
    tenant_id: str,
    master_key: str,
    version_id: UUID,
    actor_id: str,
) -> MasterVersionResponse:
    current = _ensure_version_exists(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
        version_id=version_id,
    )
    if current.lifecycleStatus == "RETIRED":
        return current
    if master_key == "PRODUCT_MASTER":
        retire_project_product_master_version(
            connection,
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "PRICE_LIST":
        retire_price_list_version(
            connection,
            tenant_id=tenant_id,
            price_list_version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "DISCOUNT_SCHEME":
        retire_discount_scheme_version(
            connection,
            tenant_id=tenant_id,
            discount_scheme_version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "DOCUMENT_REQUIREMENT_PROFILE":
        retire_master_version(
            connection,
            master_type="DOCUMENT_PROFILE",
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "AUDIT_CONTROL":
        retire_master_version(
            connection,
            master_type="AUDIT_CONTROL",
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    elif master_key == "PROJECT_POLICY":
        retire_master_version(
            connection,
            master_type="POLICY",
            tenant_id=tenant_id,
            version_id=version_id,
            actor_id=actor_id,
        )
    else:
        raise AuditCoreError(
            error_code="VAC-MASTER-004",
            status_code=422,
            title="Master lifecycle unsupported",
            detail="This Project Master does not use the version retire lifecycle.",
        )
    return _ensure_version_exists(
        connection,
        tenant_id=tenant_id,
        master_key=master_key,
        version_id=version_id,
    )


@router.get("", response_model=list[MasterDescriptor])
def get_project_master_catalogue(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[MasterDescriptor]:
    set_tenant_context(connection, tenant_id)
    return audit_core_catalogue(connection, tenant_id=tenant_id)


@router.get(
    "/{owner_module}/{master_key}/versions",
    response_model=list[MasterVersionResponse],
)
def get_master_versions(
    tenant_id: str,
    owner_module: OwnerModule,
    master_key: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[MasterVersionResponse]:
    set_tenant_context(connection, tenant_id)
    if owner_module != "AUDIT_CORE":
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Project Master not found",
            detail="DI-owned Project Master versions are supplied by DI through its facade.",
        )
    normalized = master_key.strip().upper()
    descriptor_definition(normalized)
    return master_versions(connection, tenant_id=tenant_id, master_key=normalized)


@router.post(
    "/{owner_module}/{master_key}/versions/{version_id}/publish",
    response_model=MasterVersionResponse,
)
def publish_master_facade(
    tenant_id: str,
    owner_module: OwnerModule,
    master_key: str,
    version_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> MasterVersionResponse:
    set_tenant_context(connection, tenant_id)
    if owner_module != "AUDIT_CORE":
        raise AuditCoreError(
            error_code="VAC-MASTER-004",
            status_code=422,
            title="Owning module required",
            detail="DI-owned Project Master publication must be performed by DI.",
        )
    return publish_audit_core_version(
        connection,
        tenant_id=tenant_id,
        master_key=master_key.strip().upper(),
        version_id=version_id,
        actor_id=admin_request.user_id,
    )


@router.post(
    "/{owner_module}/{master_key}/versions/{version_id}/retire",
    response_model=MasterVersionResponse,
)
def retire_master_facade(
    tenant_id: str,
    owner_module: OwnerModule,
    master_key: str,
    version_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> MasterVersionResponse:
    set_tenant_context(connection, tenant_id)
    if owner_module != "AUDIT_CORE":
        raise AuditCoreError(
            error_code="VAC-MASTER-004",
            status_code=422,
            title="Owning module required",
            detail="DI-owned Project Master retirement must be performed by DI.",
        )
    return retire_audit_core_version(
        connection,
        tenant_id=tenant_id,
        master_key=master_key.strip().upper(),
        version_id=version_id,
        actor_id=admin_request.user_id,
    )
