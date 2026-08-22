import asyncio
import json
import uuid

import pytest
from sqlalchemy import delete, select
from starlette.requests import Request

from app.core.config import get_settings
from app.core.idempotency import IdempotencyMiddleware, _resolve_identity_scope
from app.core.security import create_access_token
from app.db.models.org import User
from app.db.models.workflow import IdempotencyKey
from app.db.seed import DEMO_USER_PASSWORD
from app.db.session import AsyncSessionLocal
from app.main import app as fastapi_app


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


def _bearer_request(token: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/whatever",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "app": fastapi_app,
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_bearer_scope_distinguishes_different_users_same_org(client):
    # Regression test for the "org-only scoping" finding: two different
    # users authenticated in the SAME org must resolve to two different
    # idempotency scopes, or one user's cached response could be replayed
    # back to another user on any future authenticated POST endpoint that
    # adds the Idempotency-Key header.
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User).order_by(User.email).limit(2))).scalars().all()
    assert len(users) == 2, "seed data must provide at least 2 users to prove this"
    user_a, user_b = users
    assert user_a.org_id == user_b.org_id, "this test only proves something if both share an org"

    token_a = create_access_token(
        user_id=user_a.id,
        org_id=user_a.org_id,
        secret=settings.jwt_secret,
        expires_minutes=15,
    )
    token_b = create_access_token(
        user_id=user_b.id,
        org_id=user_b.org_id,
        secret=settings.jwt_secret,
        expires_minutes=15,
    )

    scope_a = await _resolve_identity_scope(_bearer_request(token_a))
    scope_b = await _resolve_identity_scope(_bearer_request(token_b))

    assert scope_a is not None
    assert scope_b is not None
    assert scope_a != scope_b, "two different users in the same org must not collide"


@pytest.mark.asyncio
async def test_downstream_exception_does_not_leave_dangling_row(client):
    # Regression test for the "dangling in-progress row" finding: if
    # call_next raises instead of returning a response, the placeholder
    # IdempotencyKey row must be deleted rather than left behind for
    # RECORD_TTL_HOURS, which would otherwise permanently 409 every retry
    # with this key even though the original attempt never completed.
    key = f"test-{uuid.uuid4()}"
    body = json.dumps({"email": "boom@srilakshmi.example.com"}).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/whatever",
        "headers": [
            (b"idempotency-key", key.encode()),
            (b"content-type", b"application/json"),
        ],
        "app": fastapi_app,
    }
    request = Request(scope, receive=receive)

    async def dummy_asgi_app(scope, receive, send) -> None:  # pragma: no cover — never invoked
        raise AssertionError("dispatch() is called directly; self.app should never run")

    async def raising_call_next(_request: Request) -> None:
        raise RuntimeError("downstream boom")

    middleware = IdempotencyMiddleware(app=dummy_asgi_app)
    with pytest.raises(RuntimeError, match="downstream boom"):
        await middleware.dispatch(request, raising_call_next)

    async with AsyncSessionLocal() as session:
        leftover = (
            (
                await session.execute(
                    select(IdempotencyKey).where(IdempotencyKey.key.like(f"%:{key}"))
                )
            )
            .scalars()
            .all()
        )
    assert leftover == [], "placeholder row must be cleaned up when the downstream call raises"
