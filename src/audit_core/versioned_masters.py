from datetime import date
from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import AuditCoreError


def create_project_policy_version(
    connection: Connection,
    *,
    tenant_id: str,
    version_no: int,
    effective_from: date,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.project_policy_versions (
                tenant_id, version_no, effective_from, created_by_actor_id
            ) VALUES (:tenant_id, :version_no, :effective_from, :actor_id)
            RETURNING policy_version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_no": version_no,
            "effective_from": effective_from,
            "actor_id": actor_id,
        },
    ).scalar_one()


def create_document_profile(
    connection: Connection,
    *,
    tenant_id: str,
    code: str,
    name: str,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.document_requirement_profiles (
                tenant_id, profile_code, profile_name, created_by_actor_id
            ) VALUES (:tenant_id, :code, :name, :actor_id)
            RETURNING document_requirement_profile_id
            """
        ),
        {"tenant_id": tenant_id, "code": code, "name": name, "actor_id": actor_id},
    ).scalar_one()


def create_document_profile_version(
    connection: Connection,
    *,
    tenant_id: str,
    profile_id: UUID,
    version_no: int,
    effective_from: date,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.document_requirement_profile_versions (
                tenant_id, document_requirement_profile_id, version_no,
                effective_from, created_by_actor_id
            ) VALUES (
                :tenant_id, :profile_id, :version_no, :effective_from, :actor_id
            ) RETURNING document_requirement_profile_version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "profile_id": profile_id,
            "version_no": version_no,
            "effective_from": effective_from,
            "actor_id": actor_id,
        },
    ).scalar_one()


def add_document_requirement(
    connection: Connection,
    *,
    tenant_id: str,
    profile_version_id: UUID,
    requirement_key: str,
    document_type_key: str,
    process_area: str,
    requirement_level: str,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.document_requirement_items (
                tenant_id, document_requirement_profile_version_id,
                requirement_key, document_type_key, process_area, requirement_level
            ) VALUES (
                :tenant_id, :version_id, :requirement_key,
                :document_type_key, :process_area, :requirement_level
            ) RETURNING document_requirement_item_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": profile_version_id,
            "requirement_key": requirement_key,
            "document_type_key": document_type_key,
            "process_area": process_area,
            "requirement_level": requirement_level,
        },
    ).scalar_one()


def create_audit_control(
    connection: Connection,
    *,
    tenant_id: str,
    key: str,
    name: str,
    process_area: str,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_controls (
                tenant_id, control_key, control_name, process_area, created_by_actor_id
            ) VALUES (:tenant_id, :key, :name, :process_area, :actor_id)
            RETURNING audit_control_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "key": key,
            "name": name,
            "process_area": process_area,
            "actor_id": actor_id,
        },
    ).scalar_one()


def create_audit_control_version(
    connection: Connection,
    *,
    tenant_id: str,
    audit_control_id: UUID,
    version_no: int,
    effective_from: date,
    evaluator_key: str,
    actor_id: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_control_versions (
                tenant_id, audit_control_id, version_no, effective_from,
                evaluator_key, created_by_actor_id
            ) VALUES (
                :tenant_id, :control_id, :version_no, :effective_from,
                :evaluator_key, :actor_id
            ) RETURNING audit_control_version_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "control_id": audit_control_id,
            "version_no": version_no,
            "effective_from": effective_from,
            "evaluator_key": evaluator_key,
            "actor_id": actor_id,
        },
    ).scalar_one()


def _transition(
    connection: Connection,
    *,
    table: str,
    id_column: str,
    tenant_id: str,
    version_id: UUID,
    from_status: str,
    to_status: str,
    actor_id: str,
) -> None:
    if table not in {
        "project_policy_versions",
        "document_requirement_profile_versions",
        "audit_control_versions",
    }:
        raise ValueError("Unsupported master table")
    if id_column not in {
        "policy_version_id",
        "document_requirement_profile_version_id",
        "audit_control_version_id",
    }:
        raise ValueError("Unsupported master identifier")
    actor_column = "published_by_actor_id" if to_status == "PUBLISHED" else "retired_by_actor_id"
    time_column = "published_at_utc" if to_status == "PUBLISHED" else "retired_at_utc"
    result = connection.execute(
        text(
            f"""
            UPDATE auditcore.{table}
            SET lifecycle_status = :to_status,
                {actor_column} = :actor_id,
                {time_column} = now(),
                updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND {id_column} = :version_id
              AND lifecycle_status = :from_status
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": version_id,
            "from_status": from_status,
            "to_status": to_status,
            "actor_id": actor_id,
        },
    )
    if result.rowcount != 1:
        raise AuditCoreError(
            error_code="VAC-MASTER-003",
            status_code=409,
            title="Master lifecycle conflict",
            detail="Master version cannot transition from its current state.",
        )


def publish_master_version(
    connection: Connection,
    *,
    master_type: str,
    tenant_id: str,
    version_id: UUID,
    actor_id: str,
) -> None:
    table, id_column = _master_target(master_type)
    _transition(
        connection,
        table=table,
        id_column=id_column,
        tenant_id=tenant_id,
        version_id=version_id,
        from_status="DRAFT",
        to_status="PUBLISHED",
        actor_id=actor_id,
    )


def retire_master_version(
    connection: Connection,
    *,
    master_type: str,
    tenant_id: str,
    version_id: UUID,
    actor_id: str,
) -> None:
    table, id_column = _master_target(master_type)
    _transition(
        connection,
        table=table,
        id_column=id_column,
        tenant_id=tenant_id,
        version_id=version_id,
        from_status="PUBLISHED",
        to_status="RETIRED",
        actor_id=actor_id,
    )


def _master_target(master_type: str) -> tuple[str, str]:
    targets = {
        "POLICY": ("project_policy_versions", "policy_version_id"),
        "DOCUMENT_PROFILE": (
            "document_requirement_profile_versions",
            "document_requirement_profile_version_id",
        ),
        "AUDIT_CONTROL": ("audit_control_versions", "audit_control_version_id"),
    }
    try:
        return targets[master_type]
    except KeyError as exc:
        raise ValueError("Unsupported master type") from exc
