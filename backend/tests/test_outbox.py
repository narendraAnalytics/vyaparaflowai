import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete

from app.db.models.workflow import OutboxEvent
from app.db.session import AsyncSessionLocal
from app.services.outbox import write_event
from app.workers.outbox_publisher import (
    DEFAULT_MAX_ATTEMPTS,
    compute_retry_decision,
    fetch_due_events,
    publish_pending,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Pure compute_retry_decision() table-driven tests
# ---------------------------------------------------------------------------


def test_never_attempted_is_immediately_eligible():
    decision = compute_retry_decision(attempts=0, last_attempted_at=None, now=NOW)
    assert decision.eligible_now is True
    assert decision.exhausted is False


def test_first_retry_not_due_immediately_after_attempt():
    decision = compute_retry_decision(attempts=1, last_attempted_at=NOW, now=NOW)
    assert decision.eligible_now is False
    assert decision.next_attempt_due == NOW + timedelta(seconds=30)


def test_first_retry_due_after_base_backoff_elapses():
    decision = compute_retry_decision(
        attempts=1, last_attempted_at=NOW - timedelta(seconds=31), now=NOW
    )
    assert decision.eligible_now is True


def test_first_retry_not_due_one_second_before_backoff_elapses():
    decision = compute_retry_decision(
        attempts=1, last_attempted_at=NOW - timedelta(seconds=29), now=NOW
    )
    assert decision.eligible_now is False


def test_second_retry_backoff_doubles():
    decision = compute_retry_decision(
        attempts=2, last_attempted_at=NOW - timedelta(seconds=61), now=NOW
    )
    assert decision.eligible_now is True
    decision_not_yet = compute_retry_decision(
        attempts=2, last_attempted_at=NOW - timedelta(seconds=59), now=NOW
    )
    assert decision_not_yet.eligible_now is False


def test_backoff_caps_at_max_seconds():
    # attempts=10 would be 30*2^9=15360s uncapped; capped to 3600s
    decision = compute_retry_decision(
        attempts=10,
        last_attempted_at=NOW - timedelta(seconds=3601),
        now=NOW,
        max_attempts=20,
    )
    assert decision.eligible_now is True
    decision_not_yet = compute_retry_decision(
        attempts=10,
        last_attempted_at=NOW - timedelta(seconds=3599),
        now=NOW,
        max_attempts=20,
    )
    assert decision_not_yet.eligible_now is False


def test_exhausted_at_max_attempts():
    decision = compute_retry_decision(
        attempts=DEFAULT_MAX_ATTEMPTS,
        last_attempted_at=NOW - timedelta(days=365),
        now=NOW,
    )
    assert decision.exhausted is True
    assert decision.eligible_now is False


def test_just_below_max_attempts_not_exhausted():
    decision = compute_retry_decision(
        attempts=DEFAULT_MAX_ATTEMPTS - 1, last_attempted_at=NOW, now=NOW
    )
    assert decision.exhausted is False


# ---------------------------------------------------------------------------
# DB-backed integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def cleanup_outbox():
    created_ids: list[uuid.UUID] = []
    yield created_ids
    if created_ids:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OutboxEvent).where(OutboxEvent.id.in_(created_ids)))
            await session.commit()


@pytest.mark.asyncio
async def test_write_event_persists_row(cleanup_outbox):
    aggregate_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="purchase_order",
            aggregate_id=aggregate_id,
            event_type="purchase_order.created",
            payload={"org_id": str(uuid.uuid4()), "po_number": "PO-2026-00001"},
        )
        await session.commit()
        cleanup_outbox.append(event.id)

    async with AsyncSessionLocal() as session:
        row = await session.get(OutboxEvent, event.id)
        assert row.aggregate_type == "purchase_order"
        assert row.aggregate_id == aggregate_id
        assert row.event_type == "purchase_order.created"
        assert row.payload["po_number"] == "PO-2026-00001"
        assert row.attempts == 0
        assert row.published_at is None
        assert row.last_attempted_at is None


@pytest.mark.asyncio
async def test_fetch_due_events_only_returns_unpublished(cleanup_outbox):
    async with AsyncSessionLocal() as session:
        unpublished = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        published = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        published.published_at = NOW
        await session.commit()
        cleanup_outbox.extend([unpublished.id, published.id])

    async with AsyncSessionLocal() as session:
        due = await fetch_due_events(session, batch_size=100)
    due_ids = {e.id for e in due}
    assert unpublished.id in due_ids
    assert published.id not in due_ids


def _mock_client(status_code: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"ok": status_code < 300})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_publish_pending_marks_success_as_published(cleanup_outbox):
    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={"x": 1},
        )
        await session.commit()
        cleanup_outbox.append(event.id)

    async with AsyncSessionLocal() as session, _mock_client(200) as client:
        result = await publish_pending(
            session, client=client, webhook_url="https://n8n.example/webhook", now=NOW
        )

    [outcome] = [o for o in result.attempted if o.event_id == event.id]
    assert outcome.success is True
    assert outcome.status_code == 200

    async with AsyncSessionLocal() as session:
        row = await session.get(OutboxEvent, event.id)
        assert row.published_at == NOW
        assert row.attempts == 1
        assert row.last_attempted_at == NOW


@pytest.mark.asyncio
async def test_publish_pending_records_failure_and_leaves_unpublished(cleanup_outbox):
    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        await session.commit()
        cleanup_outbox.append(event.id)

    async with AsyncSessionLocal() as session, _mock_client(500) as client:
        result = await publish_pending(
            session, client=client, webhook_url="https://n8n.example/webhook", now=NOW
        )

    [outcome] = [o for o in result.attempted if o.event_id == event.id]
    assert outcome.success is False
    assert outcome.status_code == 500

    async with AsyncSessionLocal() as session:
        row = await session.get(OutboxEvent, event.id)
        assert row.published_at is None
        assert row.attempts == 1
        assert row.last_attempted_at == NOW


@pytest.mark.asyncio
async def test_publish_pending_skips_event_not_yet_due_for_retry(cleanup_outbox):
    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        event.attempts = 1
        event.last_attempted_at = NOW - timedelta(seconds=5)
        await session.commit()
        cleanup_outbox.append(event.id)

    async with AsyncSessionLocal() as session, _mock_client(200) as client:
        result = await publish_pending(
            session, client=client, webhook_url="https://n8n.example/webhook", now=NOW
        )

    assert result.skipped_not_due == 1
    assert result.attempted == []

    async with AsyncSessionLocal() as session:
        row = await session.get(OutboxEvent, event.id)
        assert row.attempts == 1  # untouched — not re-attempted


@pytest.mark.asyncio
async def test_publish_pending_skips_exhausted_event(cleanup_outbox):
    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        event.attempts = DEFAULT_MAX_ATTEMPTS
        event.last_attempted_at = NOW - timedelta(days=365)
        await session.commit()
        cleanup_outbox.append(event.id)

    async with AsyncSessionLocal() as session, _mock_client(200) as client:
        result = await publish_pending(
            session, client=client, webhook_url="https://n8n.example/webhook", now=NOW
        )

    assert result.skipped_exhausted == 1
    assert result.attempted == []


@pytest.mark.asyncio
async def test_publish_pending_retries_previously_failed_event_once_due(cleanup_outbox):
    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        event.attempts = 1
        event.last_attempted_at = NOW - timedelta(seconds=31)
        await session.commit()
        cleanup_outbox.append(event.id)

    later = NOW
    async with AsyncSessionLocal() as session, _mock_client(200) as client:
        result = await publish_pending(
            session, client=client, webhook_url="https://n8n.example/webhook", now=later
        )

    [outcome] = [o for o in result.attempted if o.event_id == event.id]
    assert outcome.success is True

    async with AsyncSessionLocal() as session:
        row = await session.get(OutboxEvent, event.id)
        assert row.attempts == 2
        assert row.published_at == later


@pytest.mark.asyncio
async def test_publish_pending_sends_idempotency_and_event_type_headers(cleanup_outbox):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["idempotency_key"] = request.headers.get("Idempotency-Key")
        captured["event_type_header"] = request.headers.get("X-Outbox-Event-Type")
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="sales_order.created",
            payload={},
        )
        await session.commit()
        cleanup_outbox.append(event.id)

    async with (
        AsyncSessionLocal() as session,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
    ):
        await publish_pending(
            session,
            client=client,
            webhook_url="https://n8n.example/webhook",
            webhook_secret="s3cret",
            now=NOW,
        )

    assert captured["idempotency_key"] == str(event.id)
    assert captured["event_type_header"] == "sales_order.created"
    assert captured["authorization"] == "Bearer s3cret"


@pytest.mark.asyncio
async def test_publish_pending_processes_oldest_first(cleanup_outbox):
    async with AsyncSessionLocal() as session:
        older = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        await session.flush()
        newer = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        await session.commit()
        cleanup_outbox.extend([older.id, newer.id])

    async with AsyncSessionLocal() as session, _mock_client(200) as client:
        result = await publish_pending(
            session, client=client, webhook_url="https://n8n.example/webhook", now=NOW
        )

    order = [o.event_id for o in result.attempted]
    assert order.index(older.id) < order.index(newer.id)


@pytest.mark.asyncio
async def test_publish_pending_handles_connection_error_as_failure(cleanup_outbox):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with AsyncSessionLocal() as session:
        event = await write_event(
            session,
            aggregate_type="test",
            aggregate_id=uuid.uuid4(),
            event_type="test.event",
            payload={},
        )
        await session.commit()
        cleanup_outbox.append(event.id)

    async with (
        AsyncSessionLocal() as session,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
    ):
        result = await publish_pending(
            session, client=client, webhook_url="https://n8n.example/webhook", now=NOW
        )

    [outcome] = [o for o in result.attempted if o.event_id == event.id]
    assert outcome.success is False
    assert outcome.status_code is None
    assert outcome.error is not None
