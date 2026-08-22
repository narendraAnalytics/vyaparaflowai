import pytest


@pytest.mark.asyncio
async def test_health_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    # This test requires real DATABASE_URL / REDIS_URL in the environment
    # (Neon + Upstash) — health is only meaningful when actually verified,
    # not just "the key was present in the response".
    assert body == {"status": "ok", "db": "ok", "redis": "ok"}


@pytest.mark.asyncio
async def test_health_sets_request_id_header(client):
    response = await client.get("/health")
    assert "x-request-id" in response.headers
