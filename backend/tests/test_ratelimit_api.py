import pytest

from app.core.config import get_settings


@pytest.mark.asyncio
async def test_login_rate_limited_after_threshold(client):
    # /auth/login is rate-limited to settings.rate_limit_login_per_minute
    # per 60s, keyed by client IP. httpx's ASGITransport reports a fixed
    # test client host, so all these calls share one bucket. Read the
    # configured limit rather than hardcoding it — CI overrides it via
    # RATE_LIMIT_LOGIN_PER_MINUTE (see backend/scripts/ci_test_runner.py)
    # so the whole suite's login calls don't cascade-429 each other.
    limit = get_settings().rate_limit_login_per_minute
    last_status = None
    for _ in range(limit + 1):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "manager@srilakshmi.example.com", "password": "wrong-on-purpose"},
        )
        last_status = response.status_code

    assert last_status == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "Retry-After" in response.headers
