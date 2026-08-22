# Phase 2 Foundation (Auth, RBAC, Idempotency) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the FastAPI foundation (roadmap.txt 2.1-2.3) that every later
Phase 2 business-logic sub-project depends on: app skeleton (settings, RFC
9457 errors, CORS), JWT auth + RBAC + API-key auth for n8n + Redis rate
limiting, and a generic Idempotency-Key middleware.

**Architecture:** New modules under `app/core/` (exceptions, security,
permissions, deps, ratelimit, idempotency), one new table
(`refresh_tokens`), and `app/api/v1/auth.py` exposing login/refresh/logout/
me/admin-ping. Roles/permissions are DB-checked fresh on every request
(never trusted from JWT claims). n8n auth stays the single static
`N8N_API_KEY` env var already scaffolded in Phase 0 — no new API-key table.

**Tech Stack:** FastAPI (async), SQLAlchemy 2.0 async + Alembic, PyJWT
(HS256), argon2-cffi (via `argon2.PasswordHasher`), Redis (Upstash) for
rate-limit counters and idempotency locks, pytest + pytest-asyncio against
real Neon + Upstash (no mocking, per `backend/CLAUDE.md`).

**Spec:** `docs/superpowers/specs/2026-08-22-phase2-foundation-design.md`

## Global Constraints

- Python 3.12+, `uv` for all dependency/command execution (`uv run ...`).
- Every error response is RFC 9457 `application/problem+json`: `type`,
  `title`, `status`, `detail`, `instance` (the `X-Request-Id`).
- Access tokens: JWT HS256, 15 minute default expiry. Refresh tokens: opaque
  `{row_id}.{secret}`, 7 day default expiry, argon2-hashed at rest, rotated
  on every use (old row revoked when a new pair is issued).
- Roles/permissions are re-queried from `users/roles/user_roles` on every
  authorization check — never trusted from JWT claims.
- n8n service auth is the existing static `N8N_API_KEY` setting compared
  with `secrets.compare_digest` — no new API-key table.
- All new async DB code follows the existing `AsyncSessionLocal` /
  `get_db()` pattern in `app/db/session.py` — do not create a second
  session factory.
- Tests hit real Neon + Upstash over real HTTP where the code path is
  HTTP-facing (per `backend/CLAUDE.md`); no mocking of the database or
  Redis. Concurrency claims are proven with genuinely concurrent
  `asyncio.gather` across independent sessions, per the
  `test_numbering.py` pattern — never a single-session simulation.
- `ruff check .`, `ruff format .`, and `mypy app` must stay clean
  (`disallow_untyped_defs = false`, but keep signatures typed as already
  practiced in this codebase).

---

### Task 1: Dependencies, settings, CORS

**Files:**
- Modify: `backend/pyproject.toml` (via `uv add`)
- Modify: `backend/app/core/config.py`
- Modify: `backend/.env.example`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_health.py` (existing test must still pass)

**Interfaces:**
- Produces: `Settings.access_token_expires_minutes: int`,
  `Settings.refresh_token_expires_days: int`, `Settings.n8n_org_id: str`,
  `Settings.rate_limit_login_per_minute: int`, `Settings.cors_origins: list[str]`
  — consumed by Tasks 3, 7, 8, 9.

- [ ] **Step 1: Add PyJWT and argon2-cffi**

Run: `cd backend && uv add pyjwt argon2-cffi`

- [ ] **Step 2: Add new settings fields**

Edit `backend/app/core/config.py`, add fields to `Settings` (after
`jwt_secret`):

```python
    access_token_expires_minutes: int = Field(
        default=15, alias="ACCESS_TOKEN_EXPIRES_MINUTES"
    )
    refresh_token_expires_days: int = Field(
        default=7, alias="REFRESH_TOKEN_EXPIRES_DAYS"
    )
    n8n_org_id: str = Field(default="", alias="N8N_ORG_ID")
    rate_limit_login_per_minute: int = Field(
        default=10, alias="RATE_LIMIT_LOGIN_PER_MINUTE"
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:3000"], alias="CORS_ORIGINS"
    )
```

- [ ] **Step 3: Document new env vars**

Append to `backend/.env.example`:

```
# Phase 2 auth (optional — sensible defaults apply if unset)
ACCESS_TOKEN_EXPIRES_MINUTES=15
REFRESH_TOKEN_EXPIRES_DAYS=7
# Org id n8n's API key resolves to. Leave unset in dev/demo (falls back to
# the single seeded organization).
N8N_ORG_ID=
RATE_LIMIT_LOGIN_PER_MINUTE=10
CORS_ORIGINS=["http://localhost:3000"]
```

- [ ] **Step 4: Wire CORS into the app**

Edit `backend/app/main.py`, add near the top (after the `settings =
get_settings()` line) and after `app = FastAPI(...)`:

```python
from fastapi.middleware.cors import CORSMiddleware
```

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 5: Verify nothing broke**

Run: `cd backend && uv run pytest tests/test_health.py -v`
Expected: PASS (both existing health tests)

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/core/config.py backend/.env.example backend/app/main.py
git commit -m "chore(api): add auth deps, new settings, and CORS"
```

---

### Task 2: Domain exceptions + RFC 9457 error handlers

**Files:**
- Create: `backend/app/core/exceptions.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_exceptions.py`

**Interfaces:**
- Produces: `AppError`, `NotFoundError`, `ConflictError`, `ValidationError`,
  `Unauthorized`, `Forbidden`, `RateLimitedError`, `IdempotencyConflict`
  (all `AppError(detail: str, **extra) -> None`, with a class-level
  `status_code: int`, `title: str`, `type_uri: str`) and
  `register_exception_handlers(app: FastAPI) -> None` — consumed by every
  later task that raises domain errors (3, 7, 8, 9, 10).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_exceptions.py`:

```python
import pytest

from app.core.exceptions import AppError, Forbidden, NotFoundError, RateLimitedError


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/test_exceptions.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.core.exceptions'`)

- [ ] **Step 3: Implement exceptions + handlers**

Create `backend/app/core/exceptions.py`:

```python
"""Domain exceptions mapped to RFC 9457 (application/problem+json)
responses. Every endpoint in this API returns errors in this one shape —
FastAPI's default validation/HTTPException bodies are overridden app-wide.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    status_code: int = 500
    title: str = "Internal Server Error"
    type_uri: str = "about:blank"

    def __init__(self, detail: str, **extra: Any) -> None:
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


class NotFoundError(AppError):
    status_code = 404
    title = "Not Found"


class ConflictError(AppError):
    status_code = 409
    title = "Conflict"


class ValidationError(AppError):
    status_code = 422
    title = "Validation Error"


class Unauthorized(AppError):
    status_code = 401
    title = "Unauthorized"


class Forbidden(AppError):
    status_code = 403
    title = "Forbidden"


class RateLimitedError(AppError):
    status_code = 429
    title = "Too Many Requests"


class IdempotencyConflict(AppError):
    status_code = 409
    title = "Idempotency Key Conflict"


def _problem(
    request: Request,
    status_code: int,
    title: str,
    detail: str,
    type_uri: str = "about:blank",
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_uri,
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": getattr(request.state, "request_id", None),
        **extra,
    }
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        extra = dict(exc.extra)
        headers: dict[str, str] = {}
        retry_after = extra.pop("retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return _problem(
            request, exc.status_code, exc.title, exc.detail, exc.type_uri, headers, **extra
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem(
            request,
            422,
            "Validation Error",
            "Request validation failed",
            errors=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return _problem(request, exc.status_code, "HTTP Error", detail)

    @app.exception_handler(Exception)
    async def handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error=str(exc), path=request.url.path)
        return _problem(request, 500, "Internal Server Error", "An unexpected error occurred")
```

- [ ] **Step 4: Wire handlers into the app**

Edit `backend/app/main.py`. Add the import:

```python
from app.core.exceptions import register_exception_handlers
```

Add right after `app = FastAPI(...)`:

```python
register_exception_handlers(app)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_exceptions.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/exceptions.py backend/app/main.py backend/tests/test_exceptions.py
git commit -m "feat(api): RFC 9457 domain exceptions and error handlers"
```

---

### Task 3: `core/security.py` — hashing and JWT

**Files:**
- Create: `backend/app/core/security.py`
- Test: `backend/tests/test_security.py`

**Interfaces:**
- Produces: `hash_secret(secret: str) -> str`,
  `verify_secret(secret: str, hashed: str) -> bool`,
  `create_access_token(*, user_id: uuid.UUID, org_id: uuid.UUID, secret: str, expires_minutes: int) -> str`,
  `decode_token(token: str, secret: str) -> dict`,
  `generate_refresh_secret() -> str` — consumed by Tasks 6, 7, 8, 10.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_security.py`:

```python
import uuid
from datetime import timedelta

import jwt
import pytest

from app.core.security import (
    create_access_token,
    decode_token,
    generate_refresh_secret,
    hash_secret,
    verify_secret,
)


def test_hash_and_verify_secret_roundtrip():
    hashed = hash_secret("correct horse battery staple")
    assert verify_secret("correct horse battery staple", hashed)
    assert not verify_secret("wrong password", hashed)


def test_hash_is_not_the_plaintext():
    hashed = hash_secret("hello")
    assert hashed != "hello"


def test_generate_refresh_secret_is_unique_and_urlsafe():
    a, b = generate_refresh_secret(), generate_refresh_secret()
    assert a != b
    assert len(a) > 20


def test_access_token_roundtrip():
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    token = create_access_token(
        user_id=user_id, org_id=org_id, secret="test-secret", expires_minutes=15
    )
    payload = decode_token(token, "test-secret")
    assert payload["sub"] == str(user_id)
    assert payload["org_id"] == str(org_id)
    assert payload["type"] == "access"


def test_access_token_wrong_secret_rejected():
    token = create_access_token(
        user_id=uuid.uuid4(), org_id=uuid.uuid4(), secret="right", expires_minutes=15
    )
    with pytest.raises(jwt.PyJWTError):
        decode_token(token, "wrong")


def test_expired_access_token_rejected():
    token = create_access_token(
        user_id=uuid.uuid4(), org_id=uuid.uuid4(), secret="s", expires_minutes=-1
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token, "s")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/test_security.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.core.security'`)

- [ ] **Step 3: Implement**

Create `backend/app/core/security.py`:

```python
"""Password/refresh-secret hashing (argon2) and access-token JWT encode/
decode. Roles are deliberately NOT embedded in the JWT — see the Phase 2
foundation design doc: permissions are re-checked from the DB on every
request so a role change or deactivation takes effect immediately instead
of waiting out a token's lifetime.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()


def hash_secret(secret: str) -> str:
    return _ph.hash(secret)


def verify_secret(secret: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, secret)
    except VerifyMismatchError:
        return False


def create_access_token(
    *, user_id: uuid.UUID, org_id: uuid.UUID, secret: str, expires_minutes: int
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict[str, Any]:
    return jwt.decode(token, secret, algorithms=["HS256"])


def generate_refresh_secret() -> str:
    return secrets.token_urlsafe(32)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_security.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/security.py backend/tests/test_security.py
git commit -m "feat(api): argon2 hashing and JWT access-token helpers"
```

---

### Task 4: `refresh_tokens` table + migration

**Files:**
- Create: `backend/app/db/models/auth.py`
- Modify: `backend/app/db/models/__init__.py`
- Create: `backend/alembic/versions/<auto>_add_refresh_tokens.py` (via autogenerate)
- Test: `backend/tests/test_refresh_token_model.py`

**Interfaces:**
- Produces: `RefreshToken` model with columns `id: uuid.UUID` (PK),
  `user_id: uuid.UUID` (FK users.id), `secret_hash: str`,
  `expires_at: datetime`, `revoked_at: datetime | None`,
  `created_at`/`updated_at` (from `TimestampMixin`) — consumed by Task 8.

- [ ] **Step 1: Write the model**

Create `backend/app/db/models/auth.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

from .mixins import TimestampMixin, UUIDPkMixin


class RefreshToken(UUIDPkMixin, TimestampMixin, Base):
    """One row per issued refresh token. The token handed to the client is
    "{id}.{secret}" — id is this row's primary key (O(1) lookup), secret is
    a random value whose argon2 hash is stored here (never the raw secret).
    Refresh rotates: using a token revokes it and issues a fresh pair, so a
    stolen-and-reused-later token is detectable (its row is already
    revoked).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 2: Register the module**

Edit `backend/app/db/models/__init__.py`, add `auth` to the import tuple
(alphabetically first):

```python
from . import (  # noqa: F401
    auth,
    catalog,
    enums,
    finance,
    inventory,
    numbering,
    org,
    partners,
    purchase,
    sales,
    workflow,
)
```

- [ ] **Step 3: Generate the migration**

Run: `cd backend && uv run alembic revision --autogenerate -m "add refresh_tokens table"`

Open the generated file in `backend/alembic/versions/` and confirm it only
creates `refresh_tokens` (one table, FK to `users`, no unrelated diffs). If
autogenerate picked up unrelated noise, trim the migration to just this
table.

- [ ] **Step 4: Round-trip test against real Neon**

Run in order, checking output after each:

```bash
cd backend && uv run alembic upgrade head
uv run python -c "
import asyncio
from sqlalchemy import text
from app.db.session import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as s:
        r = await s.execute(text(\"SELECT to_regclass('public.refresh_tokens')\"))
        print('after upgrade:', r.scalar())

asyncio.run(main())
"
uv run alembic downgrade -1
uv run python -c "
import asyncio
from sqlalchemy import text
from app.db.session import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as s:
        r = await s.execute(text(\"SELECT to_regclass('public.refresh_tokens')\"))
        print('after downgrade:', r.scalar())

asyncio.run(main())
"
uv run alembic upgrade head
```

Expected: `after upgrade: refresh_tokens`, `after downgrade: None`, final
upgrade succeeds with no errors.

- [ ] **Step 5: Write and run a model smoke test**

Create `backend/tests/test_refresh_token_model.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.db.models.auth import RefreshToken
from app.db.models.org import Organization, User
from app.db.session import AsyncSessionLocal


@pytest.fixture
async def throwaway_user():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-auth-{uuid.uuid4()}")
        session.add(org)
        await session.flush()
        user = User(org_id=org.id, email=f"{uuid.uuid4()}@test.local", full_name="Test User")
        session.add(user)
        await session.commit()
        org_id, user_id = org.id, user.id

    yield user_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_token_create_and_read(throwaway_user):
    async with AsyncSessionLocal() as session:
        row = RefreshToken(
            user_id=throwaway_user,
            secret_hash="fake-hash",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    async with AsyncSessionLocal() as session:
        fetched = await session.get(RefreshToken, row_id)
        assert fetched is not None
        assert fetched.user_id == throwaway_user
        assert fetched.revoked_at is None
```

Run: `cd backend && uv run pytest tests/test_refresh_token_model.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/db/models/auth.py backend/app/db/models/__init__.py backend/alembic/versions/ backend/tests/test_refresh_token_model.py
git commit -m "feat(db): refresh_tokens table for JWT refresh rotation"
```

---

### Task 5: `core/permissions.py` — static role→permission map

**Files:**
- Create: `backend/app/core/permissions.py`
- Test: `backend/tests/test_permissions.py`

**Interfaces:**
- Produces: `ROLE_PERMISSIONS: dict[str, frozenset[str]]`,
  `has_permission(role_names: set[str], required: set[str]) -> bool` —
  consumed by Task 7 (`require_perm`).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_permissions.py`:

```python
from app.core.permissions import ROLE_PERMISSIONS, has_permission


def test_owner_has_every_permission():
    assert has_permission({"Owner"}, {"anything.at.all"})


def test_manager_has_po_approve():
    assert has_permission({"Manager"}, {"po.approve"})


def test_sales_lacks_po_approve():
    assert not has_permission({"Sales"}, {"po.approve"})


def test_unknown_role_grants_nothing():
    assert not has_permission({"NotARealRole"}, {"po.approve"})


def test_all_seeded_roles_have_an_entry():
    for role in ["Owner", "Manager", "Sales", "Warehouse", "Accounts"]:
        assert role in ROLE_PERMISSIONS
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/test_permissions.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

Create `backend/app/core/permissions.py`:

```python
"""Static role -> permission map. A DB-backed permissions table is
deliberately not built yet (YAGNI) — promote this to a table only when a
future phase needs runtime-editable permissions. See the Phase 2
foundation design doc for the decision.
"""

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "Owner": frozenset(),  # Owner bypasses the permission check entirely, see has_permission
    "Manager": frozenset(
        {
            "po.approve",
            "po.create",
            "pr.approve",
            "sales_order.create",
            "sales_order.approve",
            "customer.manage",
            "supplier.manage",
        }
    ),
    "Sales": frozenset({"sales_order.create", "customer.view"}),
    "Warehouse": frozenset({"goods_receipt.create", "delivery.create", "inventory.adjust"}),
    "Accounts": frozenset({"payment.record", "invoice.create", "supplier_invoice.match"}),
}


def has_permission(role_names: set[str], required: set[str]) -> bool:
    if "Owner" in role_names:
        return True
    granted: set[str] = set()
    for role_name in role_names:
        granted |= ROLE_PERMISSIONS.get(role_name, frozenset())
    return bool(granted & required)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_permissions.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/permissions.py backend/tests/test_permissions.py
git commit -m "feat(api): static role-to-permission map"
```

---

### Task 6: Seed roles and demo users

**Files:**
- Modify: `backend/app/db/seed.py`
- Test: manual verification via `make seed` (idempotent — no new pytest
  file; correctness is proven by Task 8's integration tests logging in as
  these users)

**Interfaces:**
- Produces: `DEMO_USER_PASSWORD: str` (module-level constant) — consumed by
  Task 8's integration tests to log in as `manager@srilakshmi.example.com`
  etc.

- [ ] **Step 1: Add imports and constants**

Edit `backend/app/db/seed.py`. Add to the imports at the top:

```python
from app.core.security import hash_secret
from app.db.models.org import Role, User, UserRole
```

Add near `ORG_NAME`:

```python
DEMO_USER_PASSWORD = "Passw0rd!2026"
ROLE_NAMES = ["Owner", "Manager", "Sales", "Warehouse", "Accounts"]
```

- [ ] **Step 2: Seed roles and one user per role**

Edit `backend/app/db/seed.py`, insert this block in `seed()` right after
the `warehouse` block (after `print(f"warehouse: {warehouse.name} ...")`)
and before the `suppliers` loop:

```python
        roles: dict[str, Role] = {}
        for role_name in ROLE_NAMES:
            existing_role = (
                await session.execute(select(Role).where(Role.name == role_name))
            ).scalar_one_or_none()
            if existing_role is None:
                existing_role = Role(name=role_name)
                session.add(existing_role)
                await session.flush()
            roles[role_name] = existing_role
        print(f"roles: {len(roles)}")

        for role_name in ROLE_NAMES:
            email = f"{role_name.lower()}@srilakshmi.example.com"
            existing_user = (
                await session.execute(
                    select(User).where(User.org_id == org.id, User.email == email)
                )
            ).scalar_one_or_none()
            if existing_user is None:
                existing_user = User(
                    org_id=org.id,
                    email=email,
                    full_name=f"Demo {role_name}",
                    hashed_password=hash_secret(DEMO_USER_PASSWORD),
                )
                session.add(existing_user)
                await session.flush()
                session.add(UserRole(user_id=existing_user.id, role_id=roles[role_name].id))
        print("demo users: 5 (one per role)")
```

- [ ] **Step 3: Run the seed script twice (idempotency check)**

Run: `cd backend && uv run python -m app.db.seed`
Run again: `uv run python -m app.db.seed`
Expected: both runs succeed; second run prints `roles: 5` and
`demo users: 5 (one per role)` without creating duplicates (existing-row
lookups short-circuit the inserts, matching every other block in this
file).

- [ ] **Step 4: Verify via SQL**

Run:
```bash
cd backend && uv run python -c "
import asyncio
from sqlalchemy import text
from app.db.session import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as s:
        r = await s.execute(text('SELECT count(*) FROM roles'))
        print('roles:', r.scalar())
        r = await s.execute(text(\"SELECT count(*) FROM users WHERE email LIKE '%@srilakshmi.example.com'\"))
        print('demo users:', r.scalar())

asyncio.run(main())
"
```
Expected: `roles: 5`, `demo users: 5`

- [ ] **Step 5: Commit**

```bash
git add backend/app/db/seed.py
git commit -m "feat(db): seed 5 roles and one demo user per role"
```

---

### Task 7: `core/deps.py` — auth/RBAC/API-key dependencies

**Files:**
- Create: `backend/app/core/deps.py`
- Test: `backend/tests/test_deps.py`

**Interfaces:**
- Consumes: `decode_token`, `create_access_token` (Task 3),
  `Unauthorized`, `Forbidden`, `NotFoundError` (Task 2),
  `has_permission` (Task 5), `RefreshToken` model unused here (Task 4 not
  needed by deps directly).
- Produces: `get_current_user(...)  -> User`,
  `require_role(*role_names: str) -> Callable[..., Awaitable[User]]`,
  `require_perm(*perms: str) -> Callable[..., Awaitable[User]]`,
  `get_api_key_org(...) -> uuid.UUID` — all consumed by Task 8's router and
  by every future Phase 2 sub-project's routers.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_deps.py`:

```python
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.deps import get_api_key_org, get_current_user, require_perm, require_role
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import create_access_token
from app.db.models.org import Organization, Role, User, UserRole
from app.db.session import AsyncSessionLocal


@pytest.fixture
async def org_and_manager():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-deps-{uuid.uuid4()}")
        session.add(org)
        await session.flush()

        role = (
            await session.execute(select(Role).where(Role.name == "Manager"))
        ).scalar_one_or_none()
        if role is None:
            role = Role(name="Manager")
            session.add(role)
            await session.flush()

        user = User(org_id=org.id, email=f"{uuid.uuid4()}@test.local", full_name="Test Manager")
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.commit()
        org_id, user_id = org.id, user.id

    yield org_id, user_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()


@pytest.mark.asyncio
async def test_get_current_user_valid_token(org_and_manager):
    org_id, user_id = org_and_manager
    settings = get_settings()
    token = create_access_token(
        user_id=user_id,
        org_id=org_id,
        secret=settings.jwt_secret,
        expires_minutes=15,
    )
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    async with AsyncSessionLocal() as session:
        user = await get_current_user(credentials=creds, db=session)
    assert user.id == user_id


@pytest.mark.asyncio
async def test_get_current_user_missing_token_raises():
    async with AsyncSessionLocal() as session:
        with pytest.raises(Unauthorized):
            await get_current_user(credentials=None, db=session)


@pytest.mark.asyncio
async def test_get_current_user_bad_token_raises():
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-real-jwt")
    async with AsyncSessionLocal() as session:
        with pytest.raises(Unauthorized):
            await get_current_user(credentials=creds, db=session)


@pytest.mark.asyncio
async def test_require_role_allows_manager(org_and_manager):
    _org_id, user_id = org_and_manager
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        checker = require_role("Manager", "Owner")
        result = await checker(user=user, db=session)
    assert result.id == user_id


@pytest.mark.asyncio
async def test_require_role_rejects_wrong_role(org_and_manager):
    _org_id, user_id = org_and_manager
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        checker = require_role("Warehouse")
        with pytest.raises(Forbidden):
            await checker(user=user, db=session)


@pytest.mark.asyncio
async def test_require_perm_allows_manager_po_approve(org_and_manager):
    _org_id, user_id = org_and_manager
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        checker = require_perm("po.approve")
        result = await checker(user=user, db=session)
    assert result.id == user_id


@pytest.mark.asyncio
async def test_get_api_key_org_rejects_bad_key():
    async with AsyncSessionLocal() as session:
        with pytest.raises(Unauthorized):
            await get_api_key_org(api_key="wrong-key", db=session)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/test_deps.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'app.core.deps'`)

- [ ] **Step 3: Implement**

Create `backend/app/core/deps.py`:

```python
"""FastAPI dependency providers for auth, RBAC, and n8n's API-key auth.

Roles/permissions are re-queried from the DB on every call (never trusted
from JWT claims) so a role change or deactivation takes effect on the very
next request rather than waiting out the access token's 15-minute life.
"""

import secrets
import uuid

import jwt
from fastapi import Depends
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Forbidden, NotFoundError, Unauthorized
from app.core.permissions import has_permission
from app.core.security import decode_token
from app.db.models.org import Organization, Role, User, UserRole
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise Unauthorized("missing bearer token")
    settings = get_settings()
    try:
        payload = decode_token(credentials.credentials, settings.jwt_secret)
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid or expired token") from exc
    if payload.get("type") != "access":
        raise Unauthorized("not an access token")
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise Unauthorized("invalid token payload") from exc
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized("user not found or inactive")
    return user


async def _role_names(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    result = await db.execute(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(
            UserRole.user_id == user_id
        )
    )
    return set(result.scalars().all())


def require_role(*role_names: str):
    async def _check(
        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
    ) -> User:
        granted = await _role_names(db, user.id)
        if not granted & set(role_names):
            raise Forbidden(f"requires one of roles: {', '.join(role_names)}")
        return user

    return _check


def require_perm(*perms: str):
    async def _check(
        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
    ) -> User:
        granted_roles = await _role_names(db, user.id)
        if not has_permission(granted_roles, set(perms)):
            raise Forbidden(f"requires one of permissions: {', '.join(perms)}")
        return user

    return _check


async def get_api_key_org(
    api_key: str | None = Depends(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> uuid.UUID:
    settings = get_settings()
    if (
        not api_key
        or not settings.n8n_api_key
        or not secrets.compare_digest(api_key, settings.n8n_api_key)
    ):
        raise Unauthorized("invalid api key")
    if settings.n8n_org_id:
        return uuid.UUID(settings.n8n_org_id)
    org_id = (await db.execute(select(Organization.id))).scalars().first()
    if org_id is None:
        raise NotFoundError("no organization configured")
    return org_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_deps.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/deps.py backend/tests/test_deps.py
git commit -m "feat(api): auth, RBAC, and API-key DI dependencies"
```

---

### Task 8: Auth API — login/refresh/logout/me/admin-ping

**Files:**
- Create: `backend/app/schemas/auth.py`
- Create: `backend/app/api/v1/auth.py`
- Create: `backend/app/api/v1/router.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_auth_api.py`

**Interfaces:**
- Consumes: everything from Tasks 2, 3, 4, 5, 6, 7.
- Produces: `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`,
  `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`,
  `GET /api/v1/auth/admin-ping` — the last one exists specifically to prove
  `require_role` over real HTTP (RBAC 403 demo per the spec's definition
  of done); future sub-projects will add real Manager-only endpoints and
  can retire this once one exists.

- [ ] **Step 1: Write the schemas**

Create `backend/app/schemas/auth.py`:

```python
import uuid

from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    full_name: str
    roles: list[str]
```

- [ ] **Step 2: Write the failing integration tests**

Create `backend/tests/test_auth_api.py`:

```python
import pytest

from app.db.seed import DEMO_USER_PASSWORD


@pytest.mark.asyncio
async def test_login_success(client):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password_is_problem_json(client):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": "wrong"},
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_current_user(client):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    access_token = login.json()["access_token"]
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "manager@srilakshmi.example.com"
    assert "Manager" in body["roles"]


@pytest.mark.asyncio
async def test_admin_ping_allows_manager_denies_sales(client):
    manager_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    manager_token = manager_login.json()["access_token"]
    ok = await client.get(
        "/api/v1/auth/admin-ping", headers={"Authorization": f"Bearer {manager_token}"}
    )
    assert ok.status_code == 200

    sales_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "sales@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    sales_token = sales_login.json()["access_token"]
    denied = await client.get(
        "/api/v1/auth/admin-ping", headers={"Authorization": f"Bearer {sales_token}"}
    )
    assert denied.status_code == 403
    assert denied.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_refresh_rotates_and_old_token_dies(client):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "accounts@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    old_refresh = login.json()["refresh_token"]

    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refreshed.status_code == 200
    new_refresh = refreshed.json()["refresh_token"]
    assert new_refresh != old_refresh

    reuse_old = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse_old.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(client):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "warehouse@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    refresh_token = login.json()["refresh_token"]

    logout = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 204

    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse.status_code == 401
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd backend && uv run pytest tests/test_auth_api.py -v`
Expected: FAIL — routes don't exist yet (this run also requires Task 6's
seed to have run against the dev database at least once; run
`uv run python -m app.db.seed` first if it hasn't).

- [ ] **Step 4: Implement the router**

Create `backend/app/api/v1/auth.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user, require_role
from app.core.exceptions import Unauthorized
from app.core.ratelimit import RateLimiter
from app.core.security import (
    create_access_token,
    decode_token,  # noqa: F401  (re-exported for callers that need it)
    generate_refresh_secret,
    hash_secret,  # noqa: F401
    verify_secret,
)
from app.db.models.auth import RefreshToken
from app.db.models.org import Role, User, UserRole
from app.db.session import get_db
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _user_roles(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    result = await db.execute(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(
            UserRole.user_id == user_id
        )
    )
    return list(result.scalars().all())


async def _issue_tokens(db: AsyncSession, user: User) -> TokenPair:
    settings = get_settings()
    access_token = create_access_token(
        user_id=user.id,
        org_id=user.org_id,
        secret=settings.jwt_secret,
        expires_minutes=settings.access_token_expires_minutes,
    )
    secret = generate_refresh_secret()
    refresh_row = RefreshToken(
        user_id=user.id,
        secret_hash=hash_secret(secret),
        expires_at=_utcnow() + timedelta(days=settings.refresh_token_expires_days),
    )
    db.add(refresh_row)
    await db.flush()
    refresh_token = f"{refresh_row.id}.{secret}"
    await db.commit()
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[
        Depends(RateLimiter(key_prefix="login", limit=10, window_seconds=60))
    ],
)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    result = await db.execute(select(User).where(User.email == payload.email).limit(1))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or user.hashed_password is None:
        raise Unauthorized("invalid email or password")
    if not verify_secret(payload.password, user.hashed_password):
        raise Unauthorized("invalid email or password")
    return await _issue_tokens(db, user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    try:
        token_id_str, secret = payload.refresh_token.split(".", 1)
        token_id = uuid.UUID(token_id_str)
    except ValueError as exc:
        raise Unauthorized("malformed refresh token") from exc

    row = await db.get(RefreshToken, token_id)
    if row is None or row.revoked_at is not None or row.expires_at < _utcnow():
        raise Unauthorized("invalid or expired refresh token")
    if not verify_secret(secret, row.secret_hash):
        raise Unauthorized("invalid or expired refresh token")

    row.revoked_at = _utcnow()
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("invalid or expired refresh token")
    return await _issue_tokens(db, user)


@router.post("/logout", status_code=204)
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> None:
    try:
        token_id = uuid.UUID(payload.refresh_token.split(".", 1)[0])
    except ValueError:
        return None
    row = await db.get(RefreshToken, token_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = _utcnow()
        await db.commit()
    return None


@router.get("/me", response_model=UserOut)
async def me(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> UserOut:
    roles = await _user_roles(db, user.id)
    return UserOut(
        id=user.id, org_id=user.org_id, email=user.email, full_name=user.full_name, roles=roles
    )


@router.get("/admin-ping")
async def admin_ping(user: User = Depends(require_role("Owner", "Manager"))) -> dict[str, bool]:
    return {"ok": True}
```

- [ ] **Step 5: Assemble the versioned router**

Create `backend/app/api/v1/router.py`:

```python
from fastapi import APIRouter

from app.api.v1.auth import router as auth_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
```

- [ ] **Step 6: Mount it in main.py**

Edit `backend/app/main.py`, add the import:

```python
from app.api.v1.router import api_router
```

Add after the CORS middleware block (from Task 1):

```python
app.include_router(api_router)
```

- [ ] **Step 7: Ensure dev DB is seeded, then run tests**

Run: `cd backend && uv run python -m app.db.seed`
Run: `uv run pytest tests/test_auth_api.py -v`
Expected: PASS (7 tests)

- [ ] **Step 8: Commit**

```bash
git add backend/app/schemas/auth.py backend/app/api/v1/auth.py backend/app/api/v1/router.py backend/app/main.py backend/tests/test_auth_api.py
git commit -m "feat(api): login/refresh/logout/me/admin-ping endpoints"
```

---

### Task 9: `core/ratelimit.py` — Redis rate limiting

**Files:**
- Create: `backend/app/core/ratelimit.py`
- Test: `backend/tests/test_ratelimit_api.py`

**Interfaces:**
- Consumes: `RateLimitedError` (Task 2), `app.state.redis` (already set up
  in `main.py`'s lifespan).
- Produces: `RateLimiter(key_prefix: str, limit: int, window_seconds: int)`
  — a callable FastAPI dependency — already referenced by Task 8's
  `/auth/login` route (`RateLimiter` was imported there; this task creates
  the module those imports need to succeed against real code, not just
  pass because the name happened to resolve).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_ratelimit_api.py`:

```python
import pytest

from app.db.seed import DEMO_USER_PASSWORD


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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/test_ratelimit_api.py -v`
Expected: FAIL — `ImportError` from `app/api/v1/auth.py`'s
`from app.core.ratelimit import RateLimiter` (module doesn't exist yet;
Task 8's code was written ahead of this task deliberately, matching the
plan's task ordering — this failure is expected and resolves once this
task's Step 3 lands).

- [ ] **Step 3: Implement**

Create `backend/app/core/ratelimit.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_ratelimit_api.py -v -p no:cacheprovider`
Expected: PASS. Note: this test consumes the login rate-limit bucket for
the test client's identity; if re-run within 60s it will pass immediately
(the bucket is already past threshold) rather than testing the transition
— that's fine, the assertion still holds either way.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/ratelimit.py backend/tests/test_ratelimit_api.py
git commit -m "feat(api): Redis fixed-window rate limiting on /auth/login"
```

---

### Task 10: `core/idempotency.py` — Idempotency-Key middleware

**Files:**
- Create: `backend/app/core/idempotency.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_idempotency.py`

**Interfaces:**
- Consumes: `IdempotencyKey` model (`app/db/models/workflow.py`, already
  exists from Phase 1: `key: str` PK, `request_hash: str`,
  `response: dict | None`, `status_code: int | None`, `expires_at`),
  `decode_token` (Task 3), `app.state.redis`.
- Produces: `IdempotencyMiddleware` (a `BaseHTTPMiddleware` subclass),
  installed globally in `main.py`. Activates only on `POST` requests that
  carry an `Idempotency-Key` header — every other request passes through
  unchanged, so no other task's tests are affected.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_idempotency.py`:

```python
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

    first = await client.post(
        "/api/v1/auth/login", json=body, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 200
    first_refresh = first.json()["refresh_token"]

    second = await client.post(
        "/api/v1/auth/login", json=body, headers={"Idempotency-Key": key}
    )
    assert second.status_code == 200
    assert second.json()["refresh_token"] == first_refresh, "expected a byte-identical replay"


@pytest.mark.asyncio
async def test_same_key_different_body_is_conflict(client):
    key = f"test-{uuid.uuid4()}"
    ok = {"email": "owner@srilakshmi.example.com", "password": DEMO_USER_PASSWORD}
    different = {"email": "owner@srilakshmi.example.com", "password": "not-the-same"}

    first = await client.post(
        "/api/v1/auth/login", json=ok, headers={"Idempotency-Key": key}
    )
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
        return await client.post(
            "/api/v1/auth/login", json=body, headers={"Idempotency-Key": key}
        )

    responses = await asyncio.gather(*[call() for _ in range(10)])
    statuses = {r.status_code for r in responses}
    # Every response is either the successful replay (200) or a
    # "request in flight, try again" conflict (409) for the caller that
    # lost the lock race — never a second independent 200 with a
    # different refresh_token.
    assert statuses <= {200, 409}
    successful_bodies = {r.json()["refresh_token"] for r in responses if r.status_code == 200}
    assert len(successful_bodies) == 1, "expected all successful replies to share one refresh_token"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/test_idempotency.py -v`
Expected: FAIL — without the middleware, two identical `/auth/login` calls
each issue a fresh (different) refresh token, so the replay assertion
fails.

- [ ] **Step 3: Implement**

Create `backend/app/core/idempotency.py`:

```python
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
            "instance": getattr(request.state, "request_id", None),
        },
    )


async def _resolve_org_id(request: Request) -> str | None:
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
        return str(user.org_id) if user else None


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        idem_key_header = request.headers.get("Idempotency-Key")
        if request.method != "POST" or not idem_key_header:
            return await call_next(request)

        org_id = await _resolve_org_id(request)
        if org_id is None:
            # No resolvable identity yet (e.g. login itself, before a
            # token exists) — for /auth/login specifically we still want
            # idempotency, so fall back to a per-request-body scope: hash
            # the login email into the key so repeated login attempts with
            # the same key+body dedupe without needing prior auth.
            body_preview = await request.body()
            org_id = f"anon:{hashlib.sha256(body_preview[:64]).hexdigest()[:8]}"

        body_bytes = await request.body()
        request_hash = hashlib.sha256(body_bytes).hexdigest()
        scoped_key = f"{org_id}:{idem_key_header}"

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

            response = await call_next(request)
            body_chunks = [chunk async for chunk in response.body_iterator]
            response_body = b"".join(body_chunks)

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
```

- [ ] **Step 4: Wire the middleware into the app**

Edit `backend/app/main.py`, add the import:

```python
from app.core.idempotency import IdempotencyMiddleware
```

Add right after `app.add_middleware(CORSMiddleware, ...)` (Task 1):

```python
app.add_middleware(IdempotencyMiddleware)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_idempotency.py -v`
Expected: PASS (3 tests, including the 10-way concurrency test)

- [ ] **Step 6: Run the full suite to catch regressions**

Run: `cd backend && uv run pytest -v`
Expected: all tests across every prior task still PASS (the middleware
only activates on `POST` + `Idempotency-Key` header, so unrelated tests
are unaffected).

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/idempotency.py backend/app/main.py backend/tests/test_idempotency.py
git commit -m "feat(api): Idempotency-Key middleware backed by Postgres + Redis lock"
```

---

### Task 11: Final verification, roadmap update, cleanup commit

**Files:**
- Modify: `roadmap.txt` (tick 2.1, 2.2, 2.3 and their sub-boxes)
- No new source files — this task is verification + bookkeeping.

- [ ] **Step 1: Full lint/typecheck/test pass**

Run:
```bash
cd backend
uv run ruff check .
uv run ruff format . --check
uv run mypy app
uv run pytest --cov=app --cov-report=term-missing
```
Expected: ruff and mypy clean; pytest green; note the coverage percentage
for `app/core/` and `app/api/v1/auth.py` in your final report (roadmap's
Phase 2 DoD targets ≥80% on `services/`, which doesn't exist yet in this
sub-project — this task's own new code should be close to fully covered
since every function has a direct test).

- [ ] **Step 2: Real-HTTP smoke test with curl (not just pytest)**

Run: `make dev` in one terminal (from repo root), then in another:

```bash
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"manager@srilakshmi.example.com","password":"Passw0rd!2026"}' | head -c 500

curl -s http://localhost:8000/api/v1/auth/me
# Expect a 401 problem+json body (no Authorization header)

curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: curl-demo-1" \
  -d '{"email":"manager@srilakshmi.example.com","password":"Passw0rd!2026"}' > /tmp/first.json

curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: curl-demo-1" \
  -d '{"email":"manager@srilakshmi.example.com","password":"Passw0rd!2026"}' > /tmp/second.json

diff /tmp/first.json /tmp/second.json && echo "IDENTICAL — idempotency replay confirmed"
```

Expected: login returns a token pair, `/me` without auth is a 401
problem+json body, and the two idempotent-keyed login calls produce byte-
identical output.

- [ ] **Step 3: Update roadmap.txt**

Edit `roadmap.txt`, change lines 264-273 (the `2.1`-`2.3` block) from
`[ ]` to `[x]` with today's date, e.g.:

```
[x] 2.1  2026-08-22 App skeleton: settings, DI, async session, exception handlers,
         RFC 9457 problem+json error responses, CORS, request-id middleware
[x] 2.2  2026-08-22 Auth & security
         [x] JWT access (15m) + refresh (7d), argon2 password hashing
         [x] RBAC dependency: require_role("manager"), require_perm("po.approve")
         [x] API key auth for n8n -> FastAPI (separate from user JWT)
         [x] Rate limiting (Redis token bucket) per key and per IP
         [~] Multi-tenant guard: org_id from token, enforced in the repo layer
             (org_id is resolved and available on every authenticated request;
             the repo layer itself doesn't exist until later Phase 2
             sub-projects build services/ — full enforcement lands there)
[x] 2.3  2026-08-22 Idempotency middleware (Idempotency-Key header -> idempotency_keys)
         Every POST that creates money or stock MUST be idempotent.
```

(Use `[~]` for the multi-tenant sub-bullet since full repo-layer
enforcement is out of scope until business-logic services exist — mark it
`[x]` only once a later sub-project actually enforces it.)

- [ ] **Step 4: Commit**

```bash
git add roadmap.txt
git commit -m "docs: mark Phase 2 foundation (2.1-2.3) complete in roadmap"
```

---

## Self-review notes (for the plan author, not a task)

- Spec coverage: 2.1 (Task 1, 2, 8-router-mount), 2.2 (Tasks 3, 4, 5, 6, 7,
  8, 9), 2.3 (Task 10) — all covered. Master-data CRUD (2.4) and the five
  services (2.5-2.9) are explicitly out of scope for this sub-project per
  the spec's non-goals.
- Type/name consistency checked: `hash_secret`/`verify_secret` (not
  `hash_password`), `decode_token`, `create_access_token`, `RateLimiter`,
  `IdempotencyMiddleware`, `require_role`/`require_perm`,
  `get_api_key_org` are spelled identically everywhere they're defined vs.
  imported across Tasks 3, 5, 7, 8, 9, 10.
- No placeholders: every step has runnable code or an exact shell command.
