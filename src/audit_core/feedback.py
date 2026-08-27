from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_platform_super_admin_context, set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_human_principal,
    require_super_admin_request,
)
from audit_core.errors import NotFoundError, ValidationError
from audit_core.security import HumanPrincipal

router = APIRouter(tags=["feedback"])

_MAX_SCREENSHOT_BYTES = 1024 * 1024
_ALLOWED_SCREENSHOT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_ALLOWED_FEEDBACK_ROLES = frozenset({"PC", "TL", "PM"})


class FeedbackSubmittedResponse(BaseModel):
    feedbackId: UUID
    createdAtUtc: datetime


class AdminFeedbackItem(BaseModel):
    feedbackId: UUID
    tenantId: str
    projectName: str
    submittedByUserId: str
    submittedByDisplayName: str | None
    submittedByRole: str
    feedbackText: str
    pagePath: str | None
    hasScreenshot: bool
    screenshotFileName: str | None
    screenshotContentType: str | None
    screenshotSizeBytes: int | None
    createdAtUtc: datetime


class AdminFeedbackPage(BaseModel):
    items: list[AdminFeedbackItem]
    offset: int
    limit: int
    total: int


def _submission_context(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
) -> tuple[str, str]:
    row = connection.execute(
        text(
            """
            SELECT p.project_name, ba.business_role_code
            FROM auditcore.projects p
            JOIN auditcore.business_assignments ba
              ON ba.tenant_id = p.tenant_id
            WHERE p.tenant_id = :tenant_id
              AND p.project_status = 'ACTIVE'
              AND ba.security_actor_id = :actor_id
              AND ba.business_role_code IN ('PC', 'TL', 'PM')
              AND ba.assignment_status = 'ACTIVE'
              AND ba.effective_from <= now()
              AND (ba.effective_to IS NULL OR ba.effective_to >= now())
            ORDER BY CASE ba.business_role_code
                WHEN 'PC' THEN 1
                WHEN 'TL' THEN 2
                WHEN 'PM' THEN 3
                ELSE 99
            END
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id},
    ).mappings().one_or_none()
    if row is None or str(row["business_role_code"]).upper() not in _ALLOWED_FEEDBACK_ROLES:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
            detail="Feedback submission is available to active PC, TL and PM users in this Project.",
        )
    return str(row["project_name"]), str(row["business_role_code"]).upper()


async def _read_screenshot(screenshot: UploadFile | None) -> tuple[str | None, str | None, bytes | None]:
    if screenshot is None:
        return None, None, None
    content_type = (screenshot.content_type or "").lower().strip()
    if content_type not in _ALLOWED_SCREENSHOT_TYPES:
        raise ValidationError(detail="Screenshot must be a PNG, JPEG or WebP image.")
    data = await screenshot.read(_MAX_SCREENSHOT_BYTES + 1)
    if len(data) > _MAX_SCREENSHOT_BYTES:
        raise ValidationError(detail="Screenshot must be smaller than 1 MB.")
    if not data:
        raise ValidationError(detail="Screenshot file is empty.")
    file_name = (screenshot.filename or "screenshot").strip()[:255] or "screenshot"
    return file_name, content_type, data


@router.post(
    "/v1/tenants/{tenant_id}/feedback",
    response_model=FeedbackSubmittedResponse,
    status_code=201,
)
async def submit_feedback(
    tenant_id: str,
    feedback_text: Annotated[str, Form(alias="feedbackText", min_length=1, max_length=4000)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    submitted_by_display_name: Annotated[
        str | None,
        Form(alias="submittedByDisplayName", max_length=160),
    ] = None,
    page_path: Annotated[str | None, Form(alias="pagePath", max_length=1024)] = None,
    screenshot: Annotated[UploadFile | None, File()] = None,
) -> FeedbackSubmittedResponse:
    normalized_text = feedback_text.strip()
    if not normalized_text:
        raise ValidationError(detail="Feedback text is required.")

    set_tenant_context(connection, tenant_id)
    project_name, operating_role = _submission_context(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
    )
    file_name, content_type, screenshot_data = await _read_screenshot(screenshot)

    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.user_feedback (
                tenant_id,
                project_name_snapshot,
                submitted_by_user_id,
                submitted_by_display_name,
                submitted_by_role,
                feedback_text,
                page_path,
                screenshot_file_name,
                screenshot_content_type,
                screenshot_data
            ) VALUES (
                :tenant_id,
                :project_name,
                :user_id,
                :display_name,
                :role,
                :feedback_text,
                :page_path,
                :file_name,
                :content_type,
                :screenshot_data
            )
            RETURNING feedback_id, created_at_utc
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_name": project_name,
            "user_id": human_principal.subject,
            "display_name": (submitted_by_display_name or "").strip() or None,
            "role": operating_role,
            "feedback_text": normalized_text,
            "page_path": (page_path or "").strip() or None,
            "file_name": file_name,
            "content_type": content_type,
            "screenshot_data": screenshot_data,
        },
    ).mappings().one()
    return FeedbackSubmittedResponse(
        feedbackId=row["feedback_id"],
        createdAtUtc=row["created_at_utc"],
    )


@router.get("/v1/admin/feedback", response_model=AdminFeedbackPage)
def list_feedback(
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AdminFeedbackPage:
    del admin_request
    set_platform_super_admin_context(connection)
    total = int(connection.execute(text("SELECT count(*) FROM auditcore.user_feedback")).scalar_one())
    rows = connection.execute(
        text(
            """
            SELECT feedback_id, tenant_id, project_name_snapshot,
                   submitted_by_user_id, submitted_by_display_name, submitted_by_role,
                   feedback_text, page_path, screenshot_file_name,
                   screenshot_content_type,
                   CASE WHEN screenshot_data IS NULL THEN NULL
                        ELSE octet_length(screenshot_data) END AS screenshot_size_bytes,
                   created_at_utc
            FROM auditcore.user_feedback
            ORDER BY created_at_utc DESC, feedback_id DESC
            OFFSET :offset LIMIT :limit
            """
        ),
        {"offset": offset, "limit": limit},
    ).mappings().all()
    return AdminFeedbackPage(
        items=[
            AdminFeedbackItem(
                feedbackId=row["feedback_id"],
                tenantId=str(row["tenant_id"]),
                projectName=str(row["project_name_snapshot"]),
                submittedByUserId=str(row["submitted_by_user_id"]),
                submittedByDisplayName=(
                    str(row["submitted_by_display_name"])
                    if row["submitted_by_display_name"] is not None
                    else None
                ),
                submittedByRole=str(row["submitted_by_role"]),
                feedbackText=str(row["feedback_text"]),
                pagePath=str(row["page_path"]) if row["page_path"] is not None else None,
                hasScreenshot=row["screenshot_size_bytes"] is not None,
                screenshotFileName=(
                    str(row["screenshot_file_name"])
                    if row["screenshot_file_name"] is not None
                    else None
                ),
                screenshotContentType=(
                    str(row["screenshot_content_type"])
                    if row["screenshot_content_type"] is not None
                    else None
                ),
                screenshotSizeBytes=(
                    int(row["screenshot_size_bytes"])
                    if row["screenshot_size_bytes"] is not None
                    else None
                ),
                createdAtUtc=row["created_at_utc"],
            )
            for row in rows
        ],
        offset=offset,
        limit=limit,
        total=total,
    )


@router.get("/v1/admin/feedback/{feedback_id}/screenshot")
def get_feedback_screenshot(
    feedback_id: UUID,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Response:
    del admin_request
    set_platform_super_admin_context(connection)
    row = connection.execute(
        text(
            """
            SELECT screenshot_content_type, screenshot_data
            FROM auditcore.user_feedback
            WHERE feedback_id = :feedback_id
            """
        ),
        {"feedback_id": feedback_id},
    ).mappings().one_or_none()
    if row is None or row["screenshot_data"] is None:
        raise NotFoundError(
            error_code="VAC-NF-010",
            title="Feedback screenshot not found",
            detail="The requested feedback screenshot does not exist.",
        )
    return Response(
        content=bytes(row["screenshot_data"]),
        media_type=str(row["screenshot_content_type"]),
        headers={"Cache-Control": "private, no-store"},
    )
