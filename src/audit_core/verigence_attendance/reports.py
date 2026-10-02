from __future__ import annotations

from datetime import date
from io import BytesIO
from uuid import UUID

from openpyxl import Workbook
from sqlalchemy import Connection, text

from audit_core.verigence_attendance.errors import AttendanceNotFoundError


def _workbook_bytes(workbook: Workbook) -> bytes:
    out = BytesIO()
    workbook.save(out)
    return out.getvalue()


def attendance_report(
    connection: Connection,
    *,
    start_date: date,
    end_date: date,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Attendance"
    sheet.append(
        [
            "Employee Code",
            "Employee Name",
            "Date",
            "Status",
            "Present Fraction",
            "Check In",
            "Check Out",
        ]
    )
    rows = connection.execute(
        text(
            """
            SELECT e.employee_code,e.display_name,a.attendance_date,a.status,
                   a.present_fraction,a.check_in_at_utc,a.check_out_at_utc
            FROM verigence_attendance.employees e
            LEFT JOIN verigence_attendance.attendance_days a
              ON a.employee_id=e.employee_id
             AND a.attendance_date BETWEEN :start_date AND :end_date
            WHERE e.employment_status='ACTIVE'
            ORDER BY e.employee_code,a.attendance_date
            """
        ),
        {"start_date": start_date, "end_date": end_date},
    ).mappings()
    for row in rows:
        sheet.append(
            [
                row["employee_code"],
                row["display_name"],
                row["attendance_date"],
                row["status"],
                float(row["present_fraction"]) if row["present_fraction"] is not None else None,
                row["check_in_at_utc"],
                row["check_out_at_utc"],
            ]
        )
    return _workbook_bytes(workbook)


def payroll_report(connection: Connection, *, run_id: UUID) -> bytes:
    run = connection.execute(
        text(
            """
            SELECT payroll_run_id,payroll_month,status
            FROM verigence_attendance.payroll_runs
            WHERE payroll_run_id=:run_id
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if run is None:
        raise AttendanceNotFoundError("Payroll run not found.")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Payroll"
    sheet.append(
        [
            "Employee Code",
            "Employee Name",
            "Payroll Month",
            "Status",
            "Scheduled Days",
            "Present Days",
            "Paid Leave",
            "Unpaid Leave",
            "Payable Days",
            "Gross Amount",
            "Deductions",
            "Net Amount",
        ]
    )
    rows = connection.execute(
        text(
            """
            SELECT e.employee_code,e.display_name,i.scheduled_days,i.present_days,
                   i.paid_leave_days,i.unpaid_leave_days,i.payable_days,
                   i.gross_amount,i.deduction_amount,i.net_amount
            FROM verigence_attendance.payroll_items i
            JOIN verigence_attendance.employees e ON e.employee_id=i.employee_id
            WHERE i.payroll_run_id=:run_id
            ORDER BY e.employee_code
            """
        ),
        {"run_id": run_id},
    ).mappings()
    for row in rows:
        sheet.append(
            [
                row["employee_code"],
                row["display_name"],
                run["payroll_month"],
                run["status"],
                float(row["scheduled_days"]),
                float(row["present_days"]),
                float(row["paid_leave_days"]),
                float(row["unpaid_leave_days"]),
                float(row["payable_days"]),
                float(row["gross_amount"]),
                float(row["deduction_amount"]),
                float(row["net_amount"]),
            ]
        )
    return _workbook_bytes(workbook)
