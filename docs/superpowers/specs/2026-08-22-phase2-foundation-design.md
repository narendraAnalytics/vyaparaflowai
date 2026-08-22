# Phase 2, Sub-project 1: FastAPI Foundation — Design

Status: Approved — 2026-08-22
Roadmap items covered: 2.1 (app skeleton), 2.2 (auth & security), 2.3 (idempotency middleware)

## Context

Phase 1 delivered the full domain model (38 tables) on Neon Postgres but
`app/schemas/`, `app/api/v1/`, and most of `app/services/` are still empty.
Phase 2 as a whole is too large for one spec (14 sub-items spanning auth,
five business-logic services, payments, approvals, and an outbox worker),
so it is being built as a sequence of sub-projects. This is sub-project 1:
the foundation every later sub-project depends on — app skeleton, auth/RBAC,
API-key auth for n8n, rate limiting, and the idempotency middleware. No
business-logic services (pricing/inventory/sales/procurement/matching) are
in scope here.

## Goals

- Every business-logic sub-project built after this one can assume: a
  working DI/session pattern, RFC 9457 error responses, an authenticated
  and RBAC-checked user or an authenticated n8n service caller, and an
  `Idempotency-Key` middleware ready to wrap any POST that creates money or
  stock (per the repo-root guardrail).
- Real HTTP-level verification (per `backend/CLAUDE.md` testing
  conventions), not just the ASGI test client in isolation where it hides
  bugs — mirrors how Phase 0/1 caught their real bugs.

## Non-goals

- No business-logic services or their routers.
- No frontend/session-cookie flow — API is bearer-token only for now (the
  Next.js dashboard in Phase 6 will consume the same JWT endpoints).
- No permissions table in Postgres — role→permission mapping is a static
  Python dict for now (YAGNI; promote to a DB table only if a future phase
  needs runtime-editable permissions).
- No OAuth/SSO. Custom JWT only, per ADR 0001's locked auth choice.

## Architecture

```text
app/core/
  exceptions.py    Domain exception classes (NotFoundError, ConflictError,
                    ValidationError, RateLimitedError, IdempotencyConflict,
                    Unauthorized, Forbidden) + RFC 9457 exception handlers
                    registered on the FastAPI app in main.py.
  security.py       argon2 password hashing (pwdlib), JWT encode/decode for
                     access (15m) and refresh (7d) tokens (PyJWT, HS256).
  permissions.py     ROLE_PERMISSIONS: dict[str, frozenset[str]] mapping the
                     5 seeded role names (Owner/Manager/Sales/Warehouse/
                     Accounts) to permission strings like "po.approve".
  deps.py            DI providers:
                       get_db()            — async session per request
                       get_current_user()  — decode+verify JWT, load active User
                       require_role(*names)
                       require_perm(*perms)
                       get_api_key_org()   — n8n service auth via X-API-Key
                       RateLimiter(bucket, capacity, refill_rate) — dependency
  ratelimit.py       Redis token-bucket: INCR + PEXPIRE per key, on
                     app.state.redis (Upstash).
  idempotency.py     ASGI middleware: Postgres idempotency_keys row +
                     short-lived Redis lock for the request-hash + response.

app/db/models/auth.py   New table (new Alembic migration):
  refresh_tokens   id, user_id FK, token_hash (argon2), issued_at,
                   expires_at, revoked_at (nullable)
                   (No api_keys table: Phase 0 already scaffolded a single
                   static N8N_API_KEY env var for the one service caller
                   that exists before Phase 3 — a DB-backed multi-key table
                   is YAGNI until n8n needs per-workflow keys/rotation.)

app/schemas/auth.py     LoginRequest, TokenPair, RefreshRequest, ApiKeyOut
app/api/v1/auth.py       POST /auth/login, POST /auth/refresh, POST /auth/logout
app/api/v1/router.py     assembles versioned router, mounted in main.py

app/main.py             wires: RFC 9457 handlers, CORS, existing request-id
                         middleware, new idempotency middleware, DB/Redis
                         lifespan (already present).
```

## Data flow

**JWT-authenticated request**
`Authorization: Bearer <access>` → `get_current_user` verifies signature +
expiry → loads `User` (must be `is_active`) → `require_role`/`require_perm`
queries `user_roles` join `roles` fresh from the DB on every call (not
trusted from JWT claims, so a role change or deactivation takes effect on
the very next request, not after a 15-minute token expiry) → `org_id` from
the token claim is bound onto the request-scoped session/context for the
repo layer to filter every query by.

**Refresh flow**
`POST /auth/refresh` with the refresh token → look up by hash in
`refresh_tokens`, check not expired/revoked → issue a new access token
(and, per rotation best practice, a new refresh token; revoke the old row).
`POST /auth/logout` sets `revoked_at` on the current refresh token row.

**n8n service call**
`X-API-Key: <key>` → `get_api_key_org()` compares against
`settings.n8n_api_key` using `secrets.compare_digest` (constant-time,
timing-attack-safe), resolves `org_id` from a new `settings.n8n_org_id`
(the single seeded org for now). No user/roles attached — this is a
service identity, used only by endpoints that accept service callers
(defined per-router, not globally). A mismatch or missing header is 401.

**Idempotent POST**
1. Middleware reads `Idempotency-Key` header (required only on routes that
   opt in — enforced per-router in later sub-projects when the actual
   money/stock endpoints exist; the middleware itself is generic).
2. Redis `SETNX` lock scoped to `(org_id, idempotency_key)`, short TTL
   (e.g. 30s) to bound how long a concurrent duplicate waits.
3. Look up `idempotency_keys` row by `(org_id, key)`:
   - Row exists + has a stored response → replay it verbatim, skip the
     handler, release the lock.
   - Row exists but no response yet (another request is mid-flight and
     the lock is held) → 409 Conflict (RFC 9457 body), release nothing
     (the in-flight request owns the lock).
   - No row → proceed to the handler.
4. On success, the handler's own transaction (in a later sub-project, e.g.
   sales order creation) inserts the `idempotency_keys` row with the
   response body in the *same transaction* as the business write, so a
   crash between "wrote the order" and "recorded the idempotency key" is
   impossible. For this foundation sub-project, a generic wrapper records
   request hash + response for whatever route opts in.
5. Redis lock released in a `finally`.

Request-hash mismatch (same key, different body) → 422/409 per RFC 9457,
matching the idempotency research: keys must be scoped to
operation+caller, and a reused key with a different payload is an error,
not a silent replay.

## Error handling

All exceptions funnel through RFC 9457 (`application/problem+json`)
handlers: `type`, `title`, `status`, `detail`, `instance` (carries the
existing `X-Request-Id`). Domain exceptions in `core/exceptions.py` map to
specific statuses (404/403/409/429/422); anything unhandled is logged via
structlog with the request id and returned as a generic 500 problem body
with no internals leaked. This replaces FastAPI's default validation-error
and HTTPException shapes app-wide — every endpoint returns one consistent
error shape from day one.

## Testing

- Unit: `security.py` (hash/verify, JWT encode/decode round-trip,
  expiry/invalid-signature rejection), `permissions.py` (map lookups),
  `ratelimit.py` (bucket math against a fake/real Redis).
- Integration (real HTTP, `make dev` + httpx client per `backend/CLAUDE.md`
  conventions, real Neon + Upstash — no mocking, consistent with Phase 0/1):
  - login → access+refresh pair; refresh rotates; logout revokes.
  - RBAC: a Sales-role user hitting a Manager-only route gets 403.
  - API-key auth: valid key resolves org; wrong/missing key gets 401.
  - Rate limit: N+1th request in a window gets 429 with `Retry-After`.
  - Idempotency: concurrency test in the `test_numbering.py` pattern — M
    concurrent identical POSTs to a toy idempotent route produce exactly
    one underlying side effect and M identical response bodies; a repeat
    with a different body under the same key is rejected.
- Seed script extended: 5 roles (Owner/Manager/Sales/Warehouse/Accounts)
  and one test user per role, idempotent like the existing seed.

## Definition of done (this sub-project)

- `/health` still passes; new Alembic migration for `refresh_tokens` +
  `api_keys` round-trip tested (upgrade → downgrade → upgrade) against Neon.
- `make test` green including the new auth/RBAC/rate-limit/idempotency
  integration tests and the idempotency concurrency test.
- `make lint` / `make typecheck` clean.
- A user can log in, get a 403 on a role they don't have, and a duplicate
  POST with the same `Idempotency-Key` replays instead of double-executing
  — demonstrated over real HTTP with curl, not just pytest.
- Committed: a scoped commit for this sub-project (not all of Phase 2).
