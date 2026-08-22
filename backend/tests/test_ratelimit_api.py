import pytest


@pytest.mark.asyncio
async def test_login_rate_limited_after_threshold(client):
    # Task 8 configured /auth/login with limit=10 per 60s, keyed by client
    # IP. httpx's ASGITransport reports a fixed test client host, so all
    # these calls share one bucket.
    last_status = None
    for _ in range(11):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "manager@srilakshmi.example.com", "password": "wrong-on-purpose"},
        )
        last_status = response.status_code

    assert last_status == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "Retry-After" in response.headers
