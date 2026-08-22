"""Generic Idempotency-Key middleware: activates only on POST requests
carrying the header. Backed by the idempotency_keys table (Phase 1) plus a
short Redis lock to serialize genuinely concurrent duplicate requests.

Scoping: idempotency_keys.key has no org_id column (Phase 1 schema), so
this middleware scopes keys itself as "{org_id}:{header_value}" rather
than adding a migration to a table Phase 1 already shipped.

This sub-project's implementation stores the response as a generic
wrapper around whatever the route returns. A later sub-project building an
actual money/stock-creating endpoint should move that endpoint's own
idempotency-key row write into the same DB transaction as its business
write (per the design doc) rather than relying on this generic
post-hoc version — this version is correct but has a narrow window where
the business write could commit and the process crash before the
idempotency row's response is recorded, causing a harmless one-time
non-replay (not a duplicate write) on retry.
"""

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import decode_token
from app.db.models.org import Organization, User
from app.db.models.workflow import IdempotencyKey
from app.db.session import AsyncSessionLocal

logger = get_logger(__name__)

LOCK_TTL_SECONDS = 30
RECORD_TTL_HOURS = 24


def _conflict(request: Request, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": "Idempotency Key Conflict",
            "status": 409,
            "detail": detail,
            # NOTE (controller ruling): this middleware, once wired in
            # main.py, ends up OUTERMOST relative to the pre-existing
            # request-id middleware (last-added == outermost). This
            # early-return path runs BEFORE that middleware ever sets
            # request.state.request_id, so it is always None here. Fall
            # back to reading the caller-supplied header directly — still
            # None if the caller didn't send one, but at least reflects a
            # caller-supplied id when present. Do not "fix" this by
            # reordering main.py's middleware stack — out of scope.
            "instance": request.headers.get("X-Request-Id"),
        },
    )


async def _resolve_identity_scope(request: Request) -> str | None:
    """Resolve the caller's identity scope for idempotency-key isolation.

    Returns a scope string to prefix the key with, or None if no identity
    could be resolved (the caller falls back to an anonymous scope in that
    case). For the n8n service (X-API-Key) branch this is org-only — that
    key represents a single service identity, not multiple end-users
    sharing an org's auth, so there's no user to further scope by. For the
    Bearer-token branch it MUST include the user id, not just org_id:
    org-only scoping would let two different users in the same org collide
    on org+key+matching-body and replay each other's cached response on
    any future authenticated POST endpoint that adds this header — this
    middleware is wired globally and is documented reusable infrastructure,
    so that gap has to be closed here rather than left for every future
    endpoint to remember.
    """
    settings = get_settings()
    api_key = request.headers.get("X-API-Key")
    if api_key:
        if not settings.n8n_api_key or not secrets.compare_digest(api_key, settings.n8n_api_key):
            return None
        async with AsyncSessionLocal() as session:
            if settings.n8n_org_id:
                return settings.n8n_org_id
            org_id = (await session.execute(select(Organization.id))).scalars().first()
            return str(org_id) if org_id else None

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.removeprefix("Bearer ")
    try:
        payload = decode_token(token, settings.jwt_secret)
        user_id = uuid.UUID(payload["sub"])
    except Exception:  # noqa: BLE001 — any decode failure just means "no org context"
        return None
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        return f"{user.org_id}:{user.id}" if user else None


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        idem_key_header = request.headers.get("Idempotency-Key")
        if request.method != "POST" or not idem_key_header:
            return await call_next(request)

        scope = await _resolve_identity_scope(request)
        if scope is None:
            # No resolvable identity yet (e.g. login itself, before a
            # token exists) — for /auth/login specifically we still want
            # idempotency, so fall back to a per-request scope: hash the
            # login email (not the whole body — the whole body includes
            # the password, so hashing it would give two login attempts
            # with the same key but different passwords two *different*
            # anon scopes, silently bypassing the conflict check instead
            # of surfacing it) into the key. Anything that isn't a JSON
            # object with an "email" field falls back to hashing the raw
            # body, which is still stable across identical-body retries.
            body_preview = await request.body()
            email: str | None = None
            try:
                parsed_body = json.loads(body_preview) if body_preview else None
            except json.JSONDecodeError:
                parsed_body = None
            if isinstance(parsed_body, dict) and isinstance(parsed_body.get("email"), str):
                email = parsed_body["email"]
            scope_source = email.encode() if email is not None else body_preview[:64]
            scope = f"anon:{hashlib.sha256(scope_source).hexdigest()[:8]}"

        body_bytes = await request.body()
        request_hash = hashlib.sha256(body_bytes).hexdigest()
        scoped_key = f"{scope}:{idem_key_header}"

        async with AsyncSessionLocal() as session:
            existing = await session.get(IdempotencyKey, scoped_key)

        if existing is not None:
            if existing.request_hash != request_hash:
                return _conflict(request, "Idempotency-Key reused with a different request body")
            if existing.response is not None:
                return JSONResponse(
                    status_code=existing.status_code or 200, content=existing.response
                )
            return _conflict(request, "A request with this Idempotency-Key is already in progress")

        redis_client = request.app.state.redis
        lock_key = f"idem-lock:{scoped_key}"
        got_lock = await redis_client.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
        if not got_lock:
            return _conflict(request, "A request with this Idempotency-Key is already in progress")

        try:
            async with AsyncSessionLocal() as session:
                session.add(
                    IdempotencyKey(
                        key=scoped_key,
                        request_hash=request_hash,
                        response=None,
                        status_code=None,
                        expires_at=datetime.now(UTC) + timedelta(hours=RECORD_TTL_HOURS),
                    )
                )
                try:
                    await session.commit()
                except Exception:  # noqa: BLE001 — lost the insert race
                    await session.rollback()
                    return _conflict(
                        request, "A request with this Idempotency-Key is already in progress"
                    )

            try:
                response = await call_next(request)
                # call_next always hands back Starlette's internal
                # `_StreamingResponse` (that's how BaseHTTPMiddleware wraps
                # whatever the downstream endpoint returned) — it is not
                # the public `starlette.responses.StreamingResponse`
                # class, so this attribute isn't in the `Response` type
                # stub even though it's always present at runtime here.
                body_chunks = [
                    chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                    async for chunk in response.body_iterator  # type: ignore[attr-defined]
                ]
                response_body = b"".join(body_chunks)
            except Exception:
                # call_next raised instead of returning a response (an
                # exception register_exception_handlers didn't catch, or
                # one raised while draining body_iterator) — the
                # placeholder row must not survive past this request, or
                # every retry with this key would get a permanent
                # "already in progress" 409 for the full RECORD_TTL_HOURS
                # window even though the original attempt never finished.
                async with AsyncSessionLocal() as session:
                    row = await session.get(IdempotencyKey, scoped_key)
                    if row is not None:
                        await session.delete(row)
                        await session.commit()
                raise

            async with AsyncSessionLocal() as session:
                row = await session.get(IdempotencyKey, scoped_key)
                if row is not None:
                    if 200 <= response.status_code < 300:
                        try:
                            parsed = json.loads(response_body) if response_body else None
                        except json.JSONDecodeError:
                            parsed = None
                        if parsed is not None:
                            row.response = parsed
                            row.status_code = response.status_code
                            await session.commit()
                        else:
                            await session.delete(row)
                            await session.commit()
                    else:
                        await session.delete(row)
                        await session.commit()

            return Response(
                content=response_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
        finally:
            await redis_client.delete(lock_key)
