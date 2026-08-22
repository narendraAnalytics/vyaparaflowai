import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncClient:
    # httpx's ASGITransport does not run FastAPI's lifespan (startup/shutdown)
    # on its own, so app.state.redis would never get created — drive the
    # lifespan context manually to match real request handling.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
