import pytest

from app.core.exceptions import Forbidden, NotFoundError, RateLimitedError


def test_app_error_stores_detail_and_extra():
    exc = RateLimitedError("too many requests", retry_after=30)
    assert exc.detail == "too many requests"
    assert exc.extra == {"retry_after": 30}
    assert exc.status_code == 429
    assert exc.title == "Too Many Requests"


def test_not_found_and_forbidden_status_codes():
    assert NotFoundError("x").status_code == 404
    assert Forbidden("x").status_code == 403


@pytest.mark.asyncio
async def test_unknown_route_returns_problem_json(client):
    response = await client.get("/no-such-route")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["type"] == "about:blank"
    assert "instance" in body
