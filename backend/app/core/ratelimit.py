"""Redis-backed fixed-window request counter (INCR + EXPIRE per key). Not
a true leaky/token bucket — the simpler fixed-window pattern is the
pragmatic standard for this kind of per-route limiting and is what
app.state.redis (Upstash) already supports without extra libraries. See
the Phase 2 foundation design doc for the decision.
"""

from starlette.requests import Request

from app.core.exceptions import RateLimitedError


class RateLimiter:
    def __init__(self, key_prefix: str, limit: int, window_seconds: int) -> None:
        self.key_prefix = key_prefix
        self.limit = limit
        self.window_seconds = window_seconds

    async def __call__(self, request: Request) -> None:
        redis_client = request.app.state.redis
        identity = request.headers.get("X-API-Key") or (
            request.client.host if request.client else "unknown"
        )
        key = f"ratelimit:{self.key_prefix}:{identity}"
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, self.window_seconds)
        if count > self.limit:
            ttl = await redis_client.ttl(key)
            raise RateLimitedError("rate limit exceeded", retry_after=max(ttl, 1))
