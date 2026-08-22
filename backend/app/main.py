import asyncio
import sys
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Request
from sqlalchemy import text

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import AsyncSessionLocal

if sys.platform == "win32":
    # psycopg3's async mode requires a SelectorEventLoop; Windows defaults to
    # ProactorEventLoop, which raises psycopg.InterfaceError on every query.
    # This covers pytest and any plain `asyncio.run()` entrypoint. It does
    # NOT cover `uvicorn app.main:app` / `fastapi dev` directly — uvicorn
    # >=0.36 constructs ProactorEventLoop as its own loop factory on Windows,
    # bypassing this policy. Use `make dev` (app/dev.py) for real serving.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("app_startup", env=settings.env)
    yield
    await app.state.redis.aclose()
    logger.info("app_shutdown")


app = FastAPI(title="VyaparaFlow AI", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/health")
async def health(request: Request) -> dict:
    status: dict[str, str] = {"status": "ok"}

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        status["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["db"] = "error"
        status["status"] = "degraded"
        logger.error("health_db_failed", error=str(exc))

    try:
        await request.app.state.redis.ping()
        status["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["redis"] = "error"
        status["status"] = "degraded"
        logger.error("health_redis_failed", error=str(exc))

    return status
