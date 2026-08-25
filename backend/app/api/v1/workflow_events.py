"""Workflow-event logging HTTP surface: wraps services/workflow_events.py.
Writes gated by workflow_event.create — granted only to the Automation
role (Phase 3.10, WF-99) since no human ever calls this directly.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_perm
from app.db.models.org import User
from app.db.session import get_db
from app.services.workflow_events import (
    LogWorkflowEventRequest,
    LogWorkflowEventResult,
    log_workflow_event,
)

router = APIRouter(tags=["workflow-events"])


@router.post(
    "/workflow-events",
    response_model=LogWorkflowEventResult,
    status_code=201,
    operation_id="logWorkflowEvent",
    summary="Log an n8n workflow execution outcome",
    description=(
        "Records one row per n8n execution — used by WF-99's global error handler to "
        "log failed executions (workflow name, n8n execution id, status, error detail) "
        "before it alerts. org_id comes from the caller's resolved automation identity, "
        "never from the payload."
    ),
)
async def log_workflow_event_endpoint(
    payload: LogWorkflowEventRequest,
    user: User = Depends(require_perm("workflow_event.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LogWorkflowEventResult:
    result = await log_workflow_event(db, org_id=user.org_id, request=payload)
    await db.commit()
    return result
