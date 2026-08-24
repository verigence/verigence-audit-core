from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Engine, text

from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)

router = APIRouter(prefix="/v1", tags=["project-reference-data"])


class SegmentReferenceResponse(BaseModel):
    segmentId: UUID
    segmentCode: str
    segmentName: str


class OemReferenceResponse(BaseModel):
    oemId: UUID
    oemCode: str
    oemName: str


class ProjectReferenceDataResponse(BaseModel):
    oems: list[OemReferenceResponse]
    segments: list[SegmentReferenceResponse]


@router.get("/project-reference-data", response_model=ProjectReferenceDataResponse)
def get_project_reference_data(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ProjectReferenceDataResponse:
    del admin_request
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        oem_rows = connection.execute(
            text(
                """
                SELECT oem_id, oem_code, oem_name
                FROM auditcore.oems
                WHERE is_active = true
                  AND oem_code IN (
                      'MAHINDRA', 'HYUNDAI', 'MARUTI', 'MERCEDES_BENZ',
                      'BMW', 'SKODA', 'VOLKSWAGEN', 'TATA_MOTORS'
                  )
                ORDER BY oem_name, oem_code
                """
            )
        ).mappings().all()
        segment_rows = connection.execute(
            text(
                """
                SELECT segment_id, segment_code, segment_name
                FROM auditcore.segments
                WHERE is_active = true
                  AND segment_code IN (
                      'PASSENGER_VEHICLE', 'COMMERCIAL', 'BATTERY_ELECTRIC'
                  )
                ORDER BY CASE segment_code
                    WHEN 'PASSENGER_VEHICLE' THEN 1
                    WHEN 'COMMERCIAL' THEN 2
                    WHEN 'BATTERY_ELECTRIC' THEN 3
                    ELSE 99
                END, segment_name
                """
            )
        ).mappings().all()

    return ProjectReferenceDataResponse(
        oems=[
            OemReferenceResponse(
                oemId=row["oem_id"],
                oemCode=row["oem_code"],
                oemName=row["oem_name"],
            )
            for row in oem_rows
        ],
        segments=[
            SegmentReferenceResponse(
                segmentId=row["segment_id"],
                segmentCode=row["segment_code"],
                segmentName=row["segment_name"],
            )
            for row in segment_rows
        ],
    )
