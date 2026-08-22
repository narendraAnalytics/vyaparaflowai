import asyncio
import uuid

import pytest
from sqlalchemy import delete

from app.db.models.workflow import IdempotencyKey
from app.db.seed import DEMO_USER_PASSWORD
from app.db.session import AsyncSessionLocal


@pytest.fixture(autouse=True)
async def _cleanup_idempotency_keys():
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(delete(IdempotencyKey).where(IdempotencyKey.key.like("%:test-%")))
        await session.commit()


@pytest.mark.asyncio
async def test_repeat_request_replays_stored_response(client):
    key = f"test-{uuid.uuid4()}"
    body = {"email": "owner@srilakshmi.example.com", "password": DEMO_USER_PASSWORD}

    first = await client.post("/api/v1/auth/login", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 200
    first_refresh = first.json()["refresh_token"]

    second = await client.post("/api/v1/auth/login", json=body, headers={"Idempotency-Key": key})
    assert second.status_code == 200
    assert second.json()["refresh_token"] == first_refresh, "expected a byte-identical replay"


@pytest.mark.asyncio
async def test_same_key_different_body_is_conflict(client):
    key = f"test-{uuid.uuid4()}"
    ok = {"email": "owner@srilakshmi.example.com", "password": DEMO_USER_PASSWORD}
    different = {"email": "owner@srilakshmi.example.com", "password": "not-the-same"}

    first = await client.post("/api/v1/auth/login", json=ok, headers={"Idempotency-Key": key})
    assert first.status_code == 200

    second = await client.post(
        "/api/v1/auth/login", json=different, headers={"Idempotency-Key": key}
    )
    assert second.status_code == 409
    assert second.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_concurrent_identical_requests_create_exactly_one_side_effect(client):
    key = f"test-{uuid.uuid4()}"
    body = {"email": "sales@srilakshmi.example.com", "password": DEMO_USER_PASSWORD}

    async def call():
        return await client.post("/api/v1/auth/login", json=body, headers={"Idempotency-Key": key})

    responses = await asyncio.gather(*[call() for _ in range(10)])
    statuses = {r.status_code for r in responses}
    # Every response is either the successful replay (200) or a
    # "request in flight, try again" conflict (409) for the caller that
    # lost the lock race — never a second independent 200 with a
    # different refresh_token.
    assert statuses <= {200, 409}
    successful_bodies = {r.json()["refresh_token"] for r in responses if r.status_code == 200}
    assert len(successful_bodies) == 1, "expected all successful replies to share one refresh_token"
