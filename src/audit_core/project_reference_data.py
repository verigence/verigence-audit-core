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


class OemReferenceResponse(BaseModel):
    oemId: UUID
    oemCode: str
    oemName: str


class ProductCategoryReferenceResponse(BaseModel):
    productCategoryId: UUID
    categoryCode: str
    categoryName: str


class ProjectReferenceDataResponse(BaseModel):
    oems: list[OemReferenceResponse]
    productCategories: list[ProductCategoryReferenceResponse]


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
                ORDER BY oem_name, oem_code
                """
            )
        ).mappings().all()
        category_rows = connection.execute(
            text(
                """
                SELECT product_category_id, category_code, category_name
                FROM auditcore.product_categories
                WHERE is_active = true
                ORDER BY category_name, category_code
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
        productCategories=[
            ProductCategoryReferenceResponse(
                productCategoryId=row["product_category_id"],
                categoryCode=row["category_code"],
                categoryName=row["category_name"],
            )
            for row in category_rows
        ],
    )
