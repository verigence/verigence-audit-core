from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_project_admin_request,
)
from audit_core.errors import NotFoundError, ValidationError

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/project-master-forms",
    tags=["project-master-forms"],
)


class DocumentRequirementItemInput(BaseModel):
    requirementKey: str = Field(min_length=1, max_length=120)
    documentTypeKey: str = Field(min_length=1, max_length=120)
    processArea: str = Field(min_length=1, max_length=80)
    requirementLevel: Literal["REQUIRED", "CONDITIONAL", "OPTIONAL"] = "REQUIRED"
    sortOrder: int = 0


class DocumentRequirementProfileInput(BaseModel):
    profileCode: str = Field(min_length=1, max_length=120)
    profileName: str = Field(min_length=1, max_length=240)
    effectiveFrom: date
    effectiveTo: date | None = None
    items: list[DocumentRequirementItemInput] = Field(default_factory=list)


class AuditControlInput(BaseModel):
    controlKey: str = Field(min_length=1, max_length=120)
    controlName: str = Field(min_length=1, max_length=240)
    processArea: str = Field(min_length=1, max_length=80)
    effectiveFrom: date
    effectiveTo: date | None = None
    evaluatorKey: str = Field(min_length=1, max_length=160)
    executionMode: Literal["ON_SAVE", "NIGHTLY", "ON_DEMAND"] = "ON_SAVE"
    defaultSeverity: str = Field(default="MEDIUM", min_length=1, max_length=30)


class ProjectPolicyInput(BaseModel):
    effectiveFrom: date
    effectiveTo: date | None = None
    satelliteMonthlyVolumeThreshold: int | None = Field(default=None, ge=0)
    policySettings: dict[str, object] = Field(default_factory=dict)


class BusinessStatusCodeInput(BaseModel):
    domainKey: str = Field(min_length=1, max_length=80)
    statusCode: str = Field(min_length=1, max_length=100)
    statusLabel: str = Field(min_length=1, max_length=240)
    description: str | None = None
    effectiveFrom: date | None = None
    effectiveTo: date | None = None
    isActive: bool = True


class FormMasterState(BaseModel):
    masterKey: str
    lifecycleStatus: str | None = None
    versionId: UUID | None = None
    versionNo: int | None = None
    data: dict[str, object] | list[dict[str, object]] | None = None


def _ensure_project(connection: Connection, tenant_id: str) -> None:
    exists = connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM auditcore.projects WHERE tenant_id=:tenant_id)"),
        {"tenant_id": tenant_id},
    ).scalar_one()
    if not exists:
        raise NotFoundError(
            error_code="VAC-NF-001",
            title="Project not found",
            detail="Project not found for the requested tenant.",
        )


def _validate_period(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise ValidationError(detail="Effective To must be on or after Effective From.")


def _next_version(connection: Connection, table: str, tenant_id: str) -> int:
    allowed = {
        "project_policy_versions",
        "document_requirement_profile_versions",
        "audit_control_versions",
    }
    if table not in allowed:
        raise ValueError("Unsupported version table")
    return int(
        connection.execute(
            text(f"SELECT COALESCE(max(version_no), 0) + 1 FROM auditcore.{table} WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one()
    )


def _document_profile_state(connection: Connection, tenant_id: str) -> FormMasterState:
    row = connection.execute(
        text(
            """
            SELECT p.profile_code, p.profile_name,
                   v.document_requirement_profile_version_id AS version_id,
                   v.version_no, v.lifecycle_status, v.effective_from, v.effective_to
            FROM auditcore.document_requirement_profiles p
            JOIN auditcore.document_requirement_profile_versions v
              ON v.tenant_id=p.tenant_id
             AND v.document_requirement_profile_id=p.document_requirement_profile_id
            WHERE p.tenant_id=:tenant_id
            ORDER BY (v.lifecycle_status='DRAFT') DESC, v.version_no DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        return FormMasterState(masterKey="DOCUMENT_REQUIREMENT_PROFILE")
    items = connection.execute(
        text(
            """
            SELECT requirement_key, document_type_key, process_area,
                   requirement_level, sort_order
            FROM auditcore.document_requirement_items
            WHERE tenant_id=:tenant_id
              AND document_requirement_profile_version_id=:version_id
            ORDER BY sort_order, requirement_key
            """
        ),
        {"tenant_id": tenant_id, "version_id": row["version_id"]},
    ).mappings().all()
    return FormMasterState(
        masterKey="DOCUMENT_REQUIREMENT_PROFILE",
        lifecycleStatus=str(row["lifecycle_status"]),
        versionId=row["version_id"],
        versionNo=int(row["version_no"]),
        data={
            "profileCode": row["profile_code"],
            "profileName": row["profile_name"],
            "effectiveFrom": row["effective_from"].isoformat(),
            "effectiveTo": row["effective_to"].isoformat() if row["effective_to"] else None,
            "items": [
                {
                    "requirementKey": item["requirement_key"],
                    "documentTypeKey": item["document_type_key"],
                    "processArea": item["process_area"],
                    "requirementLevel": item["requirement_level"],
                    "sortOrder": item["sort_order"],
                }
                for item in items
            ],
        },
    )


def _audit_control_state(connection: Connection, tenant_id: str) -> FormMasterState:
    row = connection.execute(
        text(
            """
            SELECT c.control_key, c.control_name, c.process_area,
                   v.audit_control_version_id AS version_id,
                   v.version_no, v.lifecycle_status, v.effective_from, v.effective_to,
                   v.evaluator_key, v.execution_mode, v.default_severity
            FROM auditcore.audit_controls c
            JOIN auditcore.audit_control_versions v
              ON v.tenant_id=c.tenant_id AND v.audit_control_id=c.audit_control_id
            WHERE c.tenant_id=:tenant_id
            ORDER BY (v.lifecycle_status='DRAFT') DESC, v.version_no DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        return FormMasterState(masterKey="AUDIT_CONTROL")
    return FormMasterState(
        masterKey="AUDIT_CONTROL",
        lifecycleStatus=str(row["lifecycle_status"]),
        versionId=row["version_id"],
        versionNo=int(row["version_no"]),
        data={
            "controlKey": row["control_key"],
            "controlName": row["control_name"],
            "processArea": row["process_area"],
            "effectiveFrom": row["effective_from"].isoformat(),
            "effectiveTo": row["effective_to"].isoformat() if row["effective_to"] else None,
            "evaluatorKey": row["evaluator_key"],
            "executionMode": row["execution_mode"],
            "defaultSeverity": row["default_severity"],
        },
    )


def _project_policy_state(connection: Connection, tenant_id: str) -> FormMasterState:
    row = connection.execute(
        text(
            """
            SELECT policy_version_id AS version_id, version_no, lifecycle_status,
                   effective_from, effective_to, satellite_monthly_volume_threshold,
                   policy_settings
            FROM auditcore.project_policy_versions
            WHERE tenant_id=:tenant_id
            ORDER BY (lifecycle_status='DRAFT') DESC, version_no DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().one_or_none()
    if row is None:
        return FormMasterState(masterKey="PROJECT_POLICY")
    return FormMasterState(
        masterKey="PROJECT_POLICY",
        lifecycleStatus=str(row["lifecycle_status"]),
        versionId=row["version_id"],
        versionNo=int(row["version_no"]),
        data={
            "effectiveFrom": row["effective_from"].isoformat(),
            "effectiveTo": row["effective_to"].isoformat() if row["effective_to"] else None,
            "satelliteMonthlyVolumeThreshold": row["satellite_monthly_volume_threshold"],
            "policySettings": row["policy_settings"] or {},
        },
    )


def _business_status_state(connection: Connection, tenant_id: str) -> FormMasterState:
    rows = connection.execute(
        text(
            """
            SELECT domain_key, status_code, status_label, description,
                   effective_from, effective_to, is_active
            FROM auditcore.business_status_codes
            WHERE tenant_id=:tenant_id
            ORDER BY domain_key, status_code
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()
    return FormMasterState(
        masterKey="BUSINESS_STATUS_CODES",
        lifecycleStatus="CONFIGURED" if rows else None,
        data=[
            {
                "domainKey": row["domain_key"],
                "statusCode": row["status_code"],
                "statusLabel": row["status_label"],
                "description": row["description"],
                "effectiveFrom": row["effective_from"].isoformat() if row["effective_from"] else None,
                "effectiveTo": row["effective_to"].isoformat() if row["effective_to"] else None,
                "isActive": bool(row["is_active"]),
            }
            for row in rows
        ],
    )


@router.get("/{master_key}", response_model=FormMasterState)
def get_form_master(
    tenant_id: str,
    master_key: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FormMasterState:
    del admin_request
    set_tenant_context(connection, tenant_id)
    _ensure_project(connection, tenant_id)
    key = master_key.strip().upper()
    readers = {
        "DOCUMENT_REQUIREMENT_PROFILE": _document_profile_state,
        "AUDIT_CONTROL": _audit_control_state,
        "PROJECT_POLICY": _project_policy_state,
        "BUSINESS_STATUS_CODES": _business_status_state,
    }
    reader = readers.get(key)
    if reader is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Project Master not found",
            detail="The requested form-managed Project Master is not registered.",
        )
    return reader(connection, tenant_id)


@router.put("/DOCUMENT_REQUIREMENT_PROFILE", response_model=FormMasterState)
def save_document_requirement_profile(
    tenant_id: str,
    payload: DocumentRequirementProfileInput,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FormMasterState:
    _validate_period(payload.effectiveFrom, payload.effectiveTo)
    set_tenant_context(connection, tenant_id)
    _ensure_project(connection, tenant_id)
    profile_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.document_requirement_profiles (
                tenant_id, profile_code, profile_name, created_by_actor_id
            ) VALUES (:tenant_id, :code, :name, :actor_id)
            ON CONFLICT (tenant_id, profile_code)
            DO UPDATE SET profile_name=EXCLUDED.profile_name, updated_at_utc=now()
            RETURNING document_requirement_profile_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "code": payload.profileCode.strip(),
            "name": payload.profileName.strip(),
            "actor_id": admin_request.user_id,
        },
    ).scalar_one()
    draft = connection.execute(
        text(
            """
            SELECT document_requirement_profile_version_id
            FROM auditcore.document_requirement_profile_versions
            WHERE tenant_id=:tenant_id
              AND document_requirement_profile_id=:profile_id
              AND lifecycle_status='DRAFT'
            ORDER BY version_no DESC LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "profile_id": profile_id},
    ).scalar_one_or_none()
    if draft is None:
        draft = connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_profile_versions (
                    tenant_id, document_requirement_profile_id, version_no,
                    effective_from, effective_to, created_by_actor_id
                ) VALUES (
                    :tenant_id, :profile_id, :version_no,
                    :effective_from, :effective_to, :actor_id
                ) RETURNING document_requirement_profile_version_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "profile_id": profile_id,
                "version_no": _next_version(connection, "document_requirement_profile_versions", tenant_id),
                "effective_from": payload.effectiveFrom,
                "effective_to": payload.effectiveTo,
                "actor_id": admin_request.user_id,
            },
        ).scalar_one()
    else:
        connection.execute(
            text(
                """
                UPDATE auditcore.document_requirement_profile_versions
                SET effective_from=:effective_from, effective_to=:effective_to, updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND document_requirement_profile_version_id=:version_id
                  AND lifecycle_status='DRAFT'
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_id": draft,
                "effective_from": payload.effectiveFrom,
                "effective_to": payload.effectiveTo,
            },
        )
    connection.execute(
        text(
            "DELETE FROM auditcore.document_requirement_items "
            "WHERE tenant_id=:tenant_id AND document_requirement_profile_version_id=:version_id"
        ),
        {"tenant_id": tenant_id, "version_id": draft},
    )
    for index, item in enumerate(payload.items):
        connection.execute(
            text(
                """
                INSERT INTO auditcore.document_requirement_items (
                    tenant_id, document_requirement_profile_version_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, sort_order
                ) VALUES (
                    :tenant_id, :version_id, :requirement_key, :document_type_key,
                    :process_area, :requirement_level, :sort_order
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_id": draft,
                "requirement_key": item.requirementKey.strip(),
                "document_type_key": item.documentTypeKey.strip(),
                "process_area": item.processArea.strip(),
                "requirement_level": item.requirementLevel,
                "sort_order": item.sortOrder if item.sortOrder else index,
            },
        )
    return _document_profile_state(connection, tenant_id)


@router.put("/AUDIT_CONTROL", response_model=FormMasterState)
def save_audit_control(
    tenant_id: str,
    payload: AuditControlInput,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FormMasterState:
    _validate_period(payload.effectiveFrom, payload.effectiveTo)
    set_tenant_context(connection, tenant_id)
    _ensure_project(connection, tenant_id)
    control_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_controls (
                tenant_id, control_key, control_name, process_area, created_by_actor_id
            ) VALUES (:tenant_id, :key, :name, :process_area, :actor_id)
            ON CONFLICT (tenant_id, control_key)
            DO UPDATE SET control_name=EXCLUDED.control_name,
                          process_area=EXCLUDED.process_area,
                          updated_at_utc=now()
            RETURNING audit_control_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "key": payload.controlKey.strip(),
            "name": payload.controlName.strip(),
            "process_area": payload.processArea.strip(),
            "actor_id": admin_request.user_id,
        },
    ).scalar_one()
    draft = connection.execute(
        text(
            """
            SELECT audit_control_version_id
            FROM auditcore.audit_control_versions
            WHERE tenant_id=:tenant_id AND audit_control_id=:control_id
              AND lifecycle_status='DRAFT'
            ORDER BY version_no DESC LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "control_id": control_id},
    ).scalar_one_or_none()
    if draft is None:
        draft = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_control_versions (
                    tenant_id, audit_control_id, version_no, effective_from, effective_to,
                    evaluator_key, execution_mode, default_severity, created_by_actor_id
                ) VALUES (
                    :tenant_id, :control_id, :version_no, :effective_from, :effective_to,
                    :evaluator_key, :execution_mode, :default_severity, :actor_id
                ) RETURNING audit_control_version_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "control_id": control_id,
                "version_no": _next_version(connection, "audit_control_versions", tenant_id),
                "effective_from": payload.effectiveFrom,
                "effective_to": payload.effectiveTo,
                "evaluator_key": payload.evaluatorKey.strip(),
                "execution_mode": payload.executionMode,
                "default_severity": payload.defaultSeverity.strip().upper(),
                "actor_id": admin_request.user_id,
            },
        ).scalar_one()
    else:
        connection.execute(
            text(
                """
                UPDATE auditcore.audit_control_versions
                SET effective_from=:effective_from, effective_to=:effective_to,
                    evaluator_key=:evaluator_key, execution_mode=:execution_mode,
                    default_severity=:default_severity, updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND audit_control_version_id=:version_id
                  AND lifecycle_status='DRAFT'
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_id": draft,
                "effective_from": payload.effectiveFrom,
                "effective_to": payload.effectiveTo,
                "evaluator_key": payload.evaluatorKey.strip(),
                "execution_mode": payload.executionMode,
                "default_severity": payload.defaultSeverity.strip().upper(),
            },
        )
    return _audit_control_state(connection, tenant_id)


@router.put("/PROJECT_POLICY", response_model=FormMasterState)
def save_project_policy(
    tenant_id: str,
    payload: ProjectPolicyInput,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FormMasterState:
    _validate_period(payload.effectiveFrom, payload.effectiveTo)
    set_tenant_context(connection, tenant_id)
    _ensure_project(connection, tenant_id)
    draft = connection.execute(
        text(
            """
            SELECT policy_version_id FROM auditcore.project_policy_versions
            WHERE tenant_id=:tenant_id AND lifecycle_status='DRAFT'
            ORDER BY version_no DESC LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()
    params = {
        "tenant_id": tenant_id,
        "effective_from": payload.effectiveFrom,
        "effective_to": payload.effectiveTo,
        "threshold": payload.satelliteMonthlyVolumeThreshold,
        "settings": json.dumps(payload.policySettings),
        "actor_id": admin_request.user_id,
    }
    if draft is None:
        draft = connection.execute(
            text(
                """
                INSERT INTO auditcore.project_policy_versions (
                    tenant_id, version_no, effective_from, effective_to,
                    satellite_monthly_volume_threshold, policy_settings, created_by_actor_id
                ) VALUES (
                    :tenant_id, :version_no, :effective_from, :effective_to,
                    :threshold, CAST(:settings AS jsonb), :actor_id
                ) RETURNING policy_version_id
                """
            ),
            {**params, "version_no": _next_version(connection, "project_policy_versions", tenant_id)},
        ).scalar_one()
    else:
        connection.execute(
            text(
                """
                UPDATE auditcore.project_policy_versions
                SET effective_from=:effective_from, effective_to=:effective_to,
                    satellite_monthly_volume_threshold=:threshold,
                    policy_settings=CAST(:settings AS jsonb), updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND policy_version_id=:version_id
                  AND lifecycle_status='DRAFT'
                """
            ),
            {**params, "version_id": draft},
        )
    return _project_policy_state(connection, tenant_id)


@router.put("/BUSINESS_STATUS_CODES", response_model=FormMasterState)
def save_business_status_code(
    tenant_id: str,
    payload: BusinessStatusCodeInput,
    admin_request: Annotated[HumanAdminRequest, Depends(require_project_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FormMasterState:
    _validate_period(payload.effectiveFrom, payload.effectiveTo)
    set_tenant_context(connection, tenant_id)
    _ensure_project(connection, tenant_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.business_status_codes (
                tenant_id, domain_key, status_code, status_label, description,
                effective_from, effective_to, is_active, created_by_actor_id
            ) VALUES (
                :tenant_id, :domain_key, :status_code, :status_label, :description,
                :effective_from, :effective_to, :is_active, :actor_id
            )
            ON CONFLICT (tenant_id, domain_key, status_code)
            DO UPDATE SET status_label=EXCLUDED.status_label,
                          description=EXCLUDED.description,
                          effective_from=EXCLUDED.effective_from,
                          effective_to=EXCLUDED.effective_to,
                          is_active=EXCLUDED.is_active,
                          updated_at_utc=now()
            """
        ),
        {
            "tenant_id": tenant_id,
            "domain_key": payload.domainKey.strip().upper(),
            "status_code": payload.statusCode.strip().upper(),
            "status_label": payload.statusLabel.strip(),
            "description": payload.description,
            "effective_from": payload.effectiveFrom,
            "effective_to": payload.effectiveTo,
            "is_active": payload.isActive,
            "actor_id": admin_request.user_id,
        },
    )
    return _business_status_state(connection, tenant_id)
