"""The write side of the transactional outbox (see `OutboxEvent` in
`app/db/models/workflow.py` for the pattern's rationale). A caller writes
an event in the SAME transaction as the domain change it's announcing —
`write_event()` never commits, same convention as every other services/
module — so "the change committed" and "an event exists to tell n8n about
it" can never drift apart: either both happen or neither does.

The read/publish side (polling `outbox_events` for unpublished rows and
POSTing them to n8n with retry + exponential backoff) lives in
`app/workers/outbox_publisher.py`, a worker rather than a services/
module because it owns its own transactions (see that module's docstring).

**No existing service calls `write_event()` yet.** Wiring it into
sales.py/procurement.py/matching.py/payments.py/approvals.py happens
incrementally as each Phase 3 n8n workflow is built and needs a specific
event: WF-02 "Inventory Shortage Router" needs a `shortage.detected`
event with a specific payload shape, WF-03 "Purchase Order Approval"
needs `purchase_requisition.created`, and so on. Inventing those event
shapes now, with no workflow to consume them and nothing to verify the
payload shape against, would be exactly the kind of ahead-of-its-consumer
half-building procurement.py deliberately avoided for PO-creation
approval gating (see that module's docstring) — this module's job in
Phase 2.12 is to make writing an event trivial and correct, not to decide
what every future event should look like.

**Payload convention** (not schema-enforced — `payload` is a plain
JSONB column): always include `org_id` in the payload dict. `outbox_
events` has no `org_id` column of its own (a Phase 1 schema choice, not
this module's), so a single shared n8n webhook receiver processing every
org's events needs it in the body to route/filter per tenant.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.workflow import OutboxEvent


async def write_event(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
) -> OutboxEvent:
    event = OutboxEvent(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    await session.flush()
    return event
