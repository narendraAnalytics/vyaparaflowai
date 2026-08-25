"""Workflow-event logging — the write side WF-99 (Phase 3.10) needs.

`WorkflowEvent` (app/db/models/workflow.py) has existed since Phase 1 but
had no service/route until now, same "schema landed ahead of the code that
uses it" gap Phase 2's receiving.py and Phase 2.12's outbox.py each closed
for their own tables. One row per n8n execution WF-99's Error Trigger
catches — org_id comes from the caller's resolved identity (the same
X-API-Key -> Automation-user resolution every other n8n-called endpoint
uses), not from the error payload itself, so this never needs n8n to know
or forward a tenant id.

Like every other services/ module, this never commits — the caller owns
the transaction.
"""

import uuid

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.workflow import WorkflowEvent


class LogWorkflowEventRequest(BaseModel):
    n8n_execution_id: str | None = None
    workflow_name: str
    status: str
    payload: dict | None = None


class LogWorkflowEventResult(BaseModel):
    id: uuid.UUID
    workflow_name: str
    status: str


async def log_workflow_event(
    db: AsyncSession, *, org_id: uuid.UUID, request: LogWorkflowEventRequest
) -> LogWorkflowEventResult:
    event = WorkflowEvent(
        org_id=org_id,
        n8n_execution_id=request.n8n_execution_id,
        workflow_name=request.workflow_name,
        status=request.status,
        payload=request.payload,
    )
    db.add(event)
    await db.flush()
    return LogWorkflowEventResult(
        id=event.id, workflow_name=event.workflow_name, status=event.status
    )
