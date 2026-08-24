"""Publishes `outbox_events` to n8n over HTTP, with retry + exponential
backoff — the read/publish side of the transactional outbox (see
`app/services/outbox.py` for the write side and the pattern's rationale).

**Worker, not a services/ module — different transaction convention.**
Every services/ function in this codebase leaves commit/rollback to its
caller, because it runs inside a request's existing transaction. This
module has no such caller: it's invoked directly (`uv run python -m
app.workers.outbox_publisher`, or a future arq/cron schedule — Phase 3+
wires actual scheduling; this module just needs to be callable). It owns
its own session and commits once per event, deliberately, so a crash
mid-batch loses at most the in-flight event's progress rather than
silently re-attempting everything already handled successfully in the
same run.

**Backoff is computed from `last_attempted_at`, not `created_at`.**
`compute_retry_decision()` is a pure function: given how many times an
event has been tried and when the last attempt was, it returns whether
the event is due for another attempt right now, or (past `max_attempts`)
permanently exhausted. Exponential: `min(base_seconds * 2^(attempts-1),
max_seconds)` after the first attempt; never-attempted rows
(`attempts=0`) are always immediately eligible.

**Exhausted events are not deleted or specially marked** — they simply
stop being picked up by `fetch_due_events()` (their computed backoff
never becomes "due"). A proper dead-letter table plus a replay endpoint
is explicitly Phase 8.5 on the roadmap ("dead-letter queue for failed
events + a replay endpoint"); building that now, with no admin UI or
alerting to consume it, would be exactly the kind of ahead-of-its-
consumer half-building this codebase avoids elsewhere (see procurement.py
and outbox.py's own docstrings). `attempts`/`last_attempted_at` on the
row itself are enough of an audit trail for a human querying the table
directly until then.

**One shared webhook URL, not one per event type.** n8n receives every
event at a single trigger and branches internally (a Switch node on
`event_type`) — simpler to operate than registering a new webhook per
event type, and the standard n8n integration shape. `X-Outbox-Event-Type`
and `Idempotency-Key` (the event's own id — never re-randomized on
retry, so a replay after a timed-out-but-actually-succeeded call is safe
if n8n's webhook honors it) go out as headers; `N8N_WEBHOOK_SECRET`, if
configured, as a bearer token.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.workflow import OutboxEvent
from app.db.session import AsyncSessionLocal

logger = get_logger(__name__)

DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_ATTEMPTS = 8
_BASE_BACKOFF_SECONDS = 30
_MAX_BACKOFF_SECONDS = 3600
_REQUEST_TIMEOUT_SECONDS = 10.0


class RetryDecision(BaseModel):
    eligible_now: bool
    exhausted: bool
    next_attempt_due: datetime | None


class PublishOutcome(BaseModel):
    event_id: uuid.UUID
    event_type: str
    success: bool
    exhausted: bool
    status_code: int | None
    error: str | None


class PublishRunResult(BaseModel):
    attempted: list[PublishOutcome]
    skipped_not_due: int
    skipped_exhausted: int


def compute_retry_decision(
    *,
    attempts: int,
    last_attempted_at: datetime | None,
    now: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_seconds: int = _BASE_BACKOFF_SECONDS,
    max_seconds: int = _MAX_BACKOFF_SECONDS,
) -> RetryDecision:
    """Pure function — no DB, no HTTP."""
    if attempts >= max_attempts:
        return RetryDecision(eligible_now=False, exhausted=True, next_attempt_due=None)
    if attempts == 0 or last_attempted_at is None:
        return RetryDecision(eligible_now=True, exhausted=False, next_attempt_due=None)

    delay_seconds = min(base_seconds * (2 ** (attempts - 1)), max_seconds)
    due = last_attempted_at + timedelta(seconds=delay_seconds)
    return RetryDecision(eligible_now=now >= due, exhausted=False, next_attempt_due=due)


async def fetch_due_events(
    session: AsyncSession, *, batch_size: int = DEFAULT_BATCH_SIZE
) -> list[OutboxEvent]:
    """Every unpublished row, oldest first, bounded by `batch_size` —
    backoff filtering happens in Python (see `publish_pending()`) since
    it depends on `attempts`/`last_attempted_at` together, not a single
    indexed column.
    """
    rows = (
        (
            await session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.created_at)
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _post_event(
    client: httpx.AsyncClient,
    *,
    webhook_url: str,
    webhook_secret: str,
    event: OutboxEvent,
) -> tuple[bool, int | None, str | None]:
    headers = {
        "Idempotency-Key": str(event.id),
        "X-Outbox-Event-Type": event.event_type,
    }
    if webhook_secret:
        headers["Authorization"] = f"Bearer {webhook_secret}"

    body = {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": str(event.aggregate_id),
        "payload": event.payload,
    }
    try:
        response = await client.post(
            webhook_url, json=body, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        return False, None, str(exc)

    if 200 <= response.status_code < 300:
        return True, response.status_code, None
    return False, response.status_code, f"unexpected status {response.status_code}"


async def publish_pending(
    session: AsyncSession,
    *,
    client: httpx.AsyncClient,
    webhook_url: str,
    webhook_secret: str = "",
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> PublishRunResult:
    now = now or datetime.now(UTC)
    candidates = await fetch_due_events(session, batch_size=batch_size)

    outcomes: list[PublishOutcome] = []
    skipped_not_due = 0
    skipped_exhausted = 0

    for event in candidates:
        decision = compute_retry_decision(
            attempts=event.attempts,
            last_attempted_at=event.last_attempted_at,
            now=now,
            max_attempts=max_attempts,
        )
        if decision.exhausted:
            skipped_exhausted += 1
            continue
        if not decision.eligible_now:
            skipped_not_due += 1
            continue

        success, status_code, error = await _post_event(
            client, webhook_url=webhook_url, webhook_secret=webhook_secret, event=event
        )
        event.attempts += 1
        event.last_attempted_at = now
        if success:
            event.published_at = now
        await session.commit()

        if not success:
            logger.warning(
                "outbox_publish_failed",
                event_id=str(event.id),
                event_type=event.event_type,
                attempts=event.attempts,
                status_code=status_code,
                error=error,
            )

        outcomes.append(
            PublishOutcome(
                event_id=event.id,
                event_type=event.event_type,
                success=success,
                exhausted=(not success and event.attempts >= max_attempts),
                status_code=status_code,
                error=error,
            )
        )

    return PublishRunResult(
        attempted=outcomes, skipped_not_due=skipped_not_due, skipped_exhausted=skipped_exhausted
    )


async def run_once() -> PublishRunResult:
    """Entrypoint for `uv run python -m app.workers.outbox_publisher` or a
    future scheduled invocation. Builds its own session and HTTP client —
    Phase 3+ decides how this actually gets scheduled (arq, cron, n8n
    itself polling); this function is what gets called either way.
    """
    settings = get_settings()
    if not settings.n8n_webhook_url:
        logger.info("outbox_publish_skipped_no_webhook_url")
        return PublishRunResult(attempted=[], skipped_not_due=0, skipped_exhausted=0)

    async with AsyncSessionLocal() as session, httpx.AsyncClient() as client:
        return await publish_pending(
            session,
            client=client,
            webhook_url=settings.n8n_webhook_url,
            webhook_secret=settings.n8n_webhook_secret,
        )


if __name__ == "__main__":
    asyncio.run(run_once())
