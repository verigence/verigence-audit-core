"""Excel onboarding of Projects, Dealers and Outlets (SuperAdmin).

GET  /v1/onboarding/workbook?data=false|true   blank template, or the current data
POST /v1/onboarding/imports                    upload -> validated preview (nothing changes)
GET  /v1/onboarding/imports/{id}               the preview / the result
POST /v1/onboarding/imports/{id}:apply         apply the confirmed preview

Apply re-validates against the data as it is at that moment and refuses if
anything became an error since the preview. New Projects go through the
normal provisioning (Security tenant, DI) via project_provisioning; Projects
marked Active are activated through project_activation once their readiness
checks pass. Dealers and outlets are written like the admin screens write
them. Every row reports its own outcome; a failure in one Project does not
stop the others.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import Connection, Engine, text

from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.errors import (
    AuditCoreError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from audit_core.onboarding_workbook import (
    ExistingState,
    ParsedSheet,
    ParsedWorkbook,
    build_workbook,
    export_rows,
    parse_workbook,
    plan_import,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])

_MAX_BYTES = 5 * 1024 * 1024
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _require_super_admin(admin_request: HumanAdminRequest) -> None:
    if not admin_request.admin_context.is_super_admin:
        raise AuthorizationError(error_code="VAC-AUTH-002", status_code=403, title="Permission denied")


def _json(value: Any) -> Any:
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def load_state(connection: Connection) -> ExistingState:
    connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
    set_platform_super_admin_context(connection)
    oems = [dict(r) for r in connection.execute(text(
        "SELECT oem_id, oem_code, oem_name FROM auditcore.oems WHERE is_active=true ORDER BY oem_name"
    )).mappings()]
    segments = [dict(r) for r in connection.execute(text(
        "SELECT segment_id, segment_code, segment_name FROM auditcore.segments WHERE is_active=true "
        "ORDER BY segment_code"
    )).mappings()]
    projects = [dict(r) for r in connection.execute(text(
        """
        SELECT p.tenant_id, p.business_code, p.project_code, p.project_name, o.oem_code,
               p.effective_start_date, p.effective_end_date, p.timezone_name, p.project_status
        FROM auditcore.projects p
        LEFT JOIN auditcore.oems o ON o.oem_id = p.oem_id
        ORDER BY lower(p.project_name)
        """
    )).mappings()]
    dealers: dict[str, list[dict[str, Any]]] = {}
    outlets: dict[str, list[dict[str, Any]]] = {}
    for project in projects:
        tenant_id = project["tenant_id"]
        set_tenant_context(connection, tenant_id)
        project["segment_codes"] = list(connection.execute(text(
            """
            SELECT s.segment_code FROM auditcore.project_segments ps
            JOIN auditcore.segments s ON s.segment_id = ps.segment_id
            WHERE ps.tenant_id = :t ORDER BY s.segment_code
            """
        ), {"t": tenant_id}).scalars())
        dealers[tenant_id] = [dict(r) for r in connection.execute(text(
            "SELECT dealer_id, dealer_code, dealer_name, status FROM auditcore.dealers WHERE tenant_id=:t"
        ), {"t": tenant_id}).mappings()]
        outlets[tenant_id] = [dict(r) for r in connection.execute(text(
            """
            SELECT outlet_id, dealer_id, outlet_code, outlet_name, outlet_classification, address_text, city,
                   state_region, postal_code, latitude, longitude, monthly_vehicle_volume, status
            FROM auditcore.dealer_outlets WHERE tenant_id=:t
            """
        ), {"t": tenant_id}).mappings()]
    return ExistingState(oems=oems, segments=segments, projects=projects, dealers=dealers, outlets=outlets)


@router.get("/workbook")
def download_workbook(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
    data: bool = Query(default=False),
) -> Response:
    """The blank template, or (data=true) every Project, Dealer and Outlet."""
    _require_super_admin(admin_request)
    with engine.begin() as connection:
        state = load_state(connection)
    projects, dealerships = export_rows(state) if data else ([], [])
    content = build_workbook(oems=state.oems, projects=projects, dealerships=dealerships)
    name = f"verigence-onboarding-{datetime.now(UTC).date().isoformat()}.xlsx" if data else "verigence-onboarding-template.xlsx"
    return Response(content, media_type=_XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _store(connection: Connection, *, import_id: UUID, actor_id: str, filename: str, sha256: str,
           status: str, plan: dict[str, Any]) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.onboarding_imports
                (import_id, created_by_actor_id, original_filename, file_sha256, import_status, plan)
            VALUES (:id, :actor, :name, :sha, :status, CAST(:plan AS jsonb))
            """
        ),
        {"id": import_id, "actor": actor_id, "name": filename[:260], "sha": sha256, "status": status,
         "plan": json.dumps(plan, default=_json)},
    )


def _import_row(connection: Connection, import_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            "SELECT import_id, original_filename, import_status, plan, result, created_at_utc, applied_at_utc "
            "FROM auditcore.onboarding_imports WHERE import_id=:id"
        ),
        {"id": import_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(error_code="VAC-NF-010", title="Import not found",
                            detail="That onboarding upload no longer exists.")
    return dict(row)


def _response(row: dict[str, Any]) -> dict[str, Any]:
    plan = dict(row["plan"])
    plan.pop("input", None)
    return {
        "importId": str(row["import_id"]),
        "filename": row["original_filename"],
        "status": row["import_status"],
        "plan": plan,
        "result": row.get("result"),
        "createdAtUtc": row["created_at_utc"].isoformat() if row.get("created_at_utc") else None,
        "appliedAtUtc": row["applied_at_utc"].isoformat() if row.get("applied_at_utc") else None,
    }


@router.post("/imports", status_code=201)
async def upload_workbook(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
    file: Annotated[UploadFile, File(description="Onboarding workbook (.xlsx)")],
) -> dict[str, Any]:
    """Validate an uploaded workbook and return the preview. Nothing changes."""
    _require_super_admin(admin_request)
    content = await file.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise ValidationError(detail="The workbook is larger than 5 MB.")
    try:
        parsed = parse_workbook(content)
    except ValueError as exc:
        raise ValidationError(detail=str(exc)) from exc
    import_id = uuid4()
    with engine.begin() as connection:
        plan = plan_import(parsed, load_state(connection))
        plan["input"] = {"projects": parsed.projects.rows, "dealerships": parsed.dealerships.rows}
        status = "VALIDATION_FAILED" if plan["summary"]["errors"] else "PREVIEW_READY"
        _store(connection, import_id=import_id, actor_id=admin_request.user_id,
               filename=file.filename or "onboarding.xlsx", sha256=hashlib.sha256(content).hexdigest(),
               status=status, plan=plan)
        row = _import_row(connection, import_id)
    logger.info("onboarding_import_previewed", import_id=str(import_id), status=status, **{
        f"{k}_rows": sum(v.values()) for k, v in plan["summary"].items() if isinstance(v, dict)})
    return _response(row)


@router.get("/imports/{import_id}")
def get_import(
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    _require_super_admin(admin_request)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        return _response(_import_row(connection, import_id))


# -------------------------------------------------------------------- apply


def _segment_ids(state: ExistingState, codes: list[str] | None) -> list[UUID]:
    wanted = {c.upper() for c in codes} if codes else None
    return [UUID(str(s["segment_id"])) for s in state.segments
            if wanted is None or str(s["segment_code"]).upper() in wanted]


def _create_project(entry: dict[str, Any], state: ExistingState, *, import_id: UUID,
                    admin_request: HumanAdminRequest, engine: Engine) -> str:
    from audit_core.project_provisioning import ProjectCreateRequest, create_project

    oem = next(o for o in state.oems if o["oem_code"] == entry["oemCode"])
    request = ProjectCreateRequest(
        projectName=entry["name"],
        businessCode=entry["code"],
        oemId=UUID(str(oem["oem_id"])),
        segmentIds=_segment_ids(state, entry.get("segments")),
        effectiveStartDate=date.fromisoformat(entry["startDate"]),
        effectiveEndDate=date.fromisoformat(entry["endDate"]) if entry.get("endDate") else None,
        timezoneName=entry.get("timezone") or "Asia/Kolkata",
    )
    created = create_project(
        request=request,
        idempotency_key=f"onboarding:{import_id}:{entry['code'].upper()}",
        admin_request=admin_request,
        engine=engine,
    )
    return created.tenantId


def _update_project(connection: Connection, tenant_id: str, entry: dict[str, Any], actor_id: str) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.projects
            SET business_code=:code, project_name=:name, effective_end_date=:end_date,
                timezone_name=:timezone, updated_by_actor_id=:actor, updated_at_utc=now(),
                version_no=version_no + 1
            WHERE tenant_id=:t
              AND (business_code IS DISTINCT FROM :code OR project_name IS DISTINCT FROM :name
                   OR effective_end_date IS DISTINCT FROM :end_date OR timezone_name IS DISTINCT FROM :timezone)
            """
        ),
        {"t": tenant_id, "code": entry["code"], "name": entry["name"],
         "end_date": date.fromisoformat(entry["endDate"]) if entry.get("endDate") else None,
         "timezone": entry.get("timezone") or "Asia/Kolkata", "actor": actor_id},
    )


def _apply_dealers_and_outlets(connection: Connection, tenant_id: str, dealers: list[dict[str, Any]],
                               outlets: list[dict[str, Any]], actor_id: str) -> dict[str, str]:
    """Returns dealer code -> dealer id for this project."""
    ids: dict[str, str] = {}
    for dealer in dealers:
        if dealer["dealerId"] is None:
            dealer_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealers (tenant_id, dealer_id, dealer_code, dealer_name, created_by_actor_id)
                    VALUES (:t, :id, :code, :name, :actor)
                    """
                ),
                {"t": tenant_id, "id": dealer_id, "code": dealer["dealerCode"], "name": dealer["dealerName"],
                 "actor": actor_id},
            )
            ids[dealer["dealerCode"].upper()] = str(dealer_id)
        else:
            if dealer["changes"]:
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.dealers
                        SET dealer_code=:code, dealer_name=:name, updated_at_utc=now(), version_no=version_no + 1
                        WHERE tenant_id=:t AND dealer_id=:id
                        """
                    ),
                    {"t": tenant_id, "id": dealer["dealerId"], "code": dealer["dealerCode"],
                     "name": dealer["dealerName"]},
                )
            ids[dealer["dealerCode"].upper()] = dealer["dealerId"]
    for outlet in outlets:
        values = {
            "t": tenant_id, "dealer_id": ids[outlet["dealerCode"].upper()], "code": outlet["outletCode"],
            "name": outlet["outletName"], "classification": outlet["outletClassification"],
            "address": outlet["addressText"], "city": outlet["city"], "state": outlet["stateRegion"],
            "postal": outlet["postalCode"], "lat": outlet["latitude"], "lng": outlet["longitude"],
            "volume": outlet["monthlyVehicleVolume"], "status": "ACTIVE" if outlet["active"] else "INACTIVE",
            "actor": actor_id,
        }
        if outlet["outletId"] is None:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealer_outlets (
                        tenant_id, dealer_id, outlet_id, outlet_code, outlet_name, outlet_classification,
                        address_text, city, state_region, postal_code, latitude, longitude,
                        monthly_vehicle_volume, status, created_by_actor_id
                    ) VALUES (
                        :t, :dealer_id, :id, :code, :name, :classification, :address, :city, :state, :postal,
                        CAST(:lat AS numeric), CAST(:lng AS numeric), :volume, :status, :actor
                    )
                    """
                ),
                {**values, "id": uuid4()},
            )
        elif outlet["changes"]:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.dealer_outlets
                    SET outlet_code=:code, outlet_name=:name, outlet_classification=:classification,
                        address_text=:address, city=:city, state_region=:state,
                        postal_code=COALESCE(:postal, postal_code),
                        latitude=COALESCE(CAST(:lat AS numeric), latitude),
                        longitude=COALESCE(CAST(:lng AS numeric), longitude),
                        monthly_vehicle_volume=COALESCE(:volume, monthly_vehicle_volume),
                        status=:status, updated_at_utc=now(), version_no=version_no + 1
                    WHERE tenant_id=:t AND outlet_id=:id
                    """
                ),
                {**values, "id": outlet["outletId"]},
            )
    return ids


def _activate(tenant_id: str, *, import_id: UUID, admin_request: HumanAdminRequest, engine: Engine) -> str | None:
    """None when active; else why it stays in setup."""
    from audit_core.project_activation import activate_project

    try:
        activate_project(tenant_id=tenant_id, idempotency_key=f"onboarding:{import_id}:{tenant_id}:activate",
                         admin_request=admin_request, engine=engine)
        return None
    except ConflictError as exc:
        return str(getattr(exc, "detail", "") or "Readiness checks are not complete yet.")
    except AuditCoreError as exc:
        return str(getattr(exc, "detail", "") or "Activation could not be completed.")


@router.post("/imports/{import_id}:apply")
def apply_import(
    import_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    _require_super_admin(admin_request)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        stored = connection.execute(
            text(
                """
                UPDATE auditcore.onboarding_imports SET import_status='APPLYING'
                WHERE import_id=:id AND import_status='PREVIEW_READY'
                RETURNING plan
                """
            ),
            {"id": import_id},
        ).scalar_one_or_none()
        if stored is None:
            current = _import_row(connection, import_id)
            raise ConflictError(
                error_code="VAC-CONFLICT-001", title="This upload cannot be applied",
                detail=("It has errors; fix the file and upload it again." if current["import_status"] == "VALIDATION_FAILED"
                        else f"It is already {current['import_status'].replace('_', ' ').lower()}."),
            )
        parsed = ParsedWorkbook(projects=ParsedSheet(rows=stored["input"]["projects"]),
                                dealerships=ParsedSheet(rows=stored["input"]["dealerships"]))
        state = load_state(connection)
        plan = plan_import(parsed, state)
        if plan["summary"]["errors"]:
            connection.execute(
                text("UPDATE auditcore.onboarding_imports SET import_status='VALIDATION_FAILED', plan=CAST(:p AS jsonb) "
                     "WHERE import_id=:id"),
                {"id": import_id, "p": json.dumps({**plan, "input": stored["input"]}, default=_json)},
            )
    if plan["summary"]["errors"]:
        raise ConflictError(error_code="VAC-CONFLICT-001", title="The data changed since the preview",
                            detail="Some rows are no longer valid. Review the new preview and upload again.")

    results: dict[str, Any] = {"projects": [], "outlets": []}
    tenant_of: dict[str, str] = {}
    failed_projects: set[str] = set()
    for entry in plan["projects"]:
        outcome = {"row": entry["row"], "code": entry["code"], "action": entry["action"], "status": "DONE",
                   "message": None, "active": entry["projectStatus"] == "ACTIVE"}
        try:
            if entry["action"] == "CREATE":
                tenant_of[entry["code"].upper()] = _create_project(entry, state, import_id=import_id,
                                                                    admin_request=admin_request, engine=engine)
                entry["tenantId"] = tenant_of[entry["code"].upper()]
            else:
                tenant_of[entry["code"].upper()] = entry["tenantId"]
            with engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
                set_tenant_context(connection, entry["tenantId"])
                _update_project(connection, entry["tenantId"], entry, admin_request.user_id)
        except Exception as exc:  # noqa: BLE001 -- one project's failure is reported, others continue
            logger.warning("onboarding_project_failed", import_id=str(import_id), row=entry["row"],
                           exc_type=type(exc).__name__)
            outcome.update(status="FAILED", message=str(getattr(exc, "detail", "") or "The project could not be saved."))
            failed_projects.add((entry["code"] or "").upper())
        results["projects"].append(outcome)

    for project_code in sorted({(o["projectCode"] or "").upper() for o in plan["outlets"]}):
        rows = [o for o in plan["outlets"] if (o["projectCode"] or "").upper() == project_code]
        tenant_id = tenant_of.get(project_code) or next((o["tenantId"] for o in rows if o["tenantId"]), None)
        dealers = [d for d in plan["dealers"] if (d["projectCode"] or "").upper() == project_code]
        if project_code in failed_projects or tenant_id is None:
            for o in rows:
                results["outlets"].append({"row": o["row"], "code": o["outletCode"], "status": "FAILED",
                                           "message": "Skipped: the project could not be saved."})
            continue
        try:
            with engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
                set_tenant_context(connection, tenant_id)
                _apply_dealers_and_outlets(connection, tenant_id, dealers, rows, admin_request.user_id)
            for o in rows:
                results["outlets"].append({"row": o["row"], "code": o["outletCode"], "action": o["action"],
                                           "status": "DONE", "message": None})
        except Exception as exc:  # noqa: BLE001
            logger.warning("onboarding_outlets_failed", import_id=str(import_id), project=project_code,
                           exc_type=type(exc).__name__)
            for o in rows:
                results["outlets"].append({"row": o["row"], "code": o["outletCode"], "status": "FAILED",
                                           "message": "The project's dealers and outlets could not be saved; "
                                                      "nothing was changed for it."})

    for entry, outcome in zip(plan["projects"], results["projects"], strict=True):
        if outcome["status"] == "DONE" and entry["activate"] and entry.get("tenantId"):
            reason = _activate(entry["tenantId"], import_id=import_id, admin_request=admin_request, engine=engine)
            outcome["active"] = reason is None
            outcome["message"] = None if reason is None else f"Stays in setup: {reason}"

    failed = any(r["status"] == "FAILED" for r in results["projects"] + results["outlets"])
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        connection.execute(
            text(
                """
                UPDATE auditcore.onboarding_imports
                SET import_status=:status, result=CAST(:result AS jsonb), applied_at_utc=now(),
                    plan=CAST(:plan AS jsonb)
                WHERE import_id=:id
                """
            ),
            {"id": import_id, "status": "APPLIED_WITH_ERRORS" if failed else "APPLIED",
             "result": json.dumps(results, default=_json),
             "plan": json.dumps({**plan, "input": stored["input"]}, default=_json)},
        )
        row = _import_row(connection, import_id)
    logger.info("onboarding_import_applied", import_id=str(import_id), failed=failed)
    return _response(row)
