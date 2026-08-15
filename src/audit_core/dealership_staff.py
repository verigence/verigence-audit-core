from uuid import UUID

from sqlalchemy import Connection, text

from audit_core.errors import NotFoundError


def create_staff_reference(
    connection: Connection,
    *,
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
    staff_role_code: str,
    display_name: str,
    employee_reference: str | None = None,
) -> UUID:
    return connection.execute(
        text(
            """
            INSERT INTO auditcore.dealership_staff (
                tenant_id, dealer_id, outlet_id, staff_role_code,
                display_name, employee_reference
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, :staff_role_code,
                :display_name, :employee_reference
            )
            RETURNING dealership_staff_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "staff_role_code": staff_role_code,
            "display_name": display_name,
            "employee_reference": employee_reference,
        },
    ).scalar_one()


def get_staff_reference(
    connection: Connection,
    *,
    tenant_id: str,
    dealership_staff_id: UUID,
) -> dict[str, object]:
    row = connection.execute(
        text(
            """
            SELECT dealership_staff_id, dealer_id, outlet_id, staff_role_code,
                   display_name, employee_reference, status
            FROM auditcore.dealership_staff
            WHERE tenant_id = :tenant_id
              AND dealership_staff_id = :dealership_staff_id
            """
        ),
        {"tenant_id": tenant_id, "dealership_staff_id": dealership_staff_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-003",
            title="Outlet-scoped resource not found",
            detail="Dealership staff reference was not found.",
        )
    return dict(row)


def inactivate_staff_reference(
    connection: Connection,
    *,
    tenant_id: str,
    dealership_staff_id: UUID,
) -> None:
    result = connection.execute(
        text(
            """
            UPDATE auditcore.dealership_staff
            SET status = 'INACTIVE', updated_at_utc = now()
            WHERE tenant_id = :tenant_id
              AND dealership_staff_id = :dealership_staff_id
            """
        ),
        {"tenant_id": tenant_id, "dealership_staff_id": dealership_staff_id},
    )
    if result.rowcount == 0:
        raise NotFoundError(
            error_code="VAC-NF-003",
            title="Outlet-scoped resource not found",
            detail="Dealership staff reference was not found.",
        )
