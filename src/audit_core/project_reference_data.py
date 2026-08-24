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


class OemSegmentReferenceResponse(BaseModel):
    segmentId: UUID
    segmentCode: str
    segmentName: str


class OemReferenceResponse(BaseModel):
    oemId: UUID
    oemCode: str
    oemName: str
    segments: list[OemSegmentReferenceResponse]


class ProjectReferenceDataResponse(BaseModel):
    oems: list[OemReferenceResponse]


@router.get("/project-reference-data", response_model=ProjectReferenceDataResponse)
def get_project_reference_data(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> ProjectReferenceDataResponse:
    del admin_request
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        rows = connection.execute(
            text(
                """
                SELECT o.oem_id, o.oem_code, o.oem_name,
                       s.segment_id, s.segment_code, s.segment_name
                FROM auditcore.oems o
                LEFT JOIN auditcore.oem_segments s
                  ON s.oem_id = o.oem_id AND s.is_active = true
                WHERE o.is_active = true
                  AND o.oem_code IN (
                      'MAHINDRA', 'HYUNDAI', 'MARUTI', 'MERCEDES_BENZ',
                      'BMW', 'SKODA', 'VOLKSWAGEN', 'TATA_MOTORS'
                  )
                ORDER BY o.oem_name, o.oem_code, s.segment_name, s.segment_code
                """
            )
        ).mappings().all()

    grouped: dict[UUID, OemReferenceResponse] = {}
    for row in rows:
        oem_id = row["oem_id"]
        oem = grouped.get(oem_id)
        if oem is None:
            oem = OemReferenceResponse(
                oemId=oem_id,
                oemCode=row["oem_code"],
                oemName=row["oem_name"],
                segments=[],
            )
            grouped[oem_id] = oem
        if row["segment_id"] is not None:
            oem.segments.append(
                OemSegmentReferenceResponse(
                    segmentId=row["segment_id"],
                    segmentCode=row["segment_code"],
                    segmentName=row["segment_name"],
                )
            )

    return ProjectReferenceDataResponse(oems=list(grouped.values()))
