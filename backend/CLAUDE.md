# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See the repo-root `CLAUDE.md` for what this project is and how this
service fits into the overall architecture.

## Commands

Run from `backend/`, or via the root `Makefile` (`make <target>` — see
comments below for the equivalent raw command).

```bash
uv sync                          # install deps (make: implicit)

uv run python -m app.dev         # dev server, hot reload      (make dev)
uv run pytest                    # full test suite + coverage  (make test)
uv run pytest tests/test_health.py::test_health_ok   # single test
uv run pytest -k health          # tests matching "health"

uv run ruff check .              # lint                        (make lint)
uv run ruff check . --fix        # lint, auto-fix
uv run ruff format .             # format                      (make format)
uv run mypy app                  # typecheck                   (make typecheck)

uv run alembic upgrade head              # apply migrations    (make migrate)
uv run alembic revision --autogenerate -m "message"  # new migration (make revision m="message")
uv run alembic downgrade -1              # roll back one migration
uv run python -m app.db.seed             # seed demo data      (make seed)
```

**Do not run `uvicorn app.main:app` or `fastapi dev` directly on Windows** —
see the winloop note below. Always use `make dev` / `python -m app.dev`.

## Environment

Copy `.env.example` to `.env` and fill in real values (`.env` is
gitignored, never commit it). Required: `DATABASE_URL` and
`DIRECT_DATABASE_URL` (Neon Postgres — pooled vs. direct, see below),
`REDIS_URL` (Upstash, must be `rediss://`). `app/core/config.py` treats
all three as required with no fallback — a missing/misconfigured value
fails fast at startup rather than silently trying to reach a local
container that doesn't exist in this project.

## Architecture

```text
app/
  core/          settings (config.py), structlog setup (logging.py),
                 Windows event-loop workaround (winloop.py)
  db/
    session.py   async SQLAlchemy engine/session, Base — also sets the
                 Windows event-loop policy on import, see below
    models/      the domain schema (Phase 1, done) — org.py, partners.py,
                 catalog.py, inventory.py, sales.py, purchase.py,
                 finance.py, workflow.py, numbering.py, enums.py, mixins.py.
                 __init__.py imports all of them so Base.metadata is fully
                 populated for Alembic autogenerate / create_all
    seed.py      idempotent demo data — Sri Lakshmi Hardware, 5 suppliers,
                 10 customers, 60 SKUs (make seed)
  schemas/       Pydantic request/response contracts (empty until Phase 2)
  api/v1/        FastAPI routers (empty until Phase 2)
  services/      business logic — the real value of the project, called by
                 n8n over HTTP, never duplicated into n8n Code nodes.
                 numbering.py (Phase 1) is built; pricing/inventory/sales/
                 procurement/matching land in Phase 2
  workers/       background jobs (arq/celery — not yet built)
  ai/            document extraction, prompts, eval harness (Phase 4)
  integrations/  gst/, razorpay/, whatsapp/, storage/ (Phase 4-5)
  main.py        FastAPI app, lifespan (owns the Redis client on
                 app.state.redis), request-id middleware, /health
  dev.py         local dev entrypoint — see winloop.py
```

See `docs/er-diagram.md` (repo root) for the full schema as Mermaid ERDs —
read it before adding or changing a table.

**Neon: two connection strings, two purposes.** `DATABASE_URL` is the
pooled endpoint (`-pooler` in the hostname) — used by the app at runtime
for many short-lived connections. `DIRECT_DATABASE_URL` is the unpooled
endpoint — used only by Alembic (`alembic/env.py` overrides
`sqlalchemy.url` from it at import time), because DDL and advisory locks
don't reliably work through the pooler. Don't swap these.

**Windows + psycopg3 async + uvicorn ≥0.36 is broken by default.** uvicorn
hard-codes `ProactorEventLoop` as its loop factory on `win32` (bypassing
`asyncio.set_event_loop_policy()` entirely), which breaks psycopg3's async
mode against Neon with `psycopg.InterfaceError`. Two separate fixes, for
two separate code paths:
- `app/db/session.py` sets the event loop policy at import time. Since
  every `asyncio.run()`-based entrypoint that touches the DB imports this
  module transitively — pytest, `alembic/env.py`, `app/db/seed.py` — this
  fix is centralized here rather than duplicated per entrypoint. It does
  **not** fix `uvicorn`/`fastapi dev` serving directly, because uvicorn's
  own `asyncio.run()` call (inside `Server.run()`) constructs the
  `ProactorEventLoop` and starts the loop *before* it imports and runs
  `app/main.py`, i.e. before this policy is ever set.
- `app/core/winloop.py` provides a `SelectorEventLoop` factory that
  `app/dev.py` passes into `uvicorn.run(loop=...)` for the actual serving
  case — always use `make dev` / `python -m app.dev`, never a bare
  `uvicorn app.main:app` or `fastapi dev` on Windows.

Not an issue in Docker/Linux — the container's `CMD` (`fastapi run`) is
unaffected.

**Health check testing gotcha:** `httpx.ASGITransport` does not run
FastAPI's lifespan (startup/shutdown) on its own — `tests/conftest.py`'s
`client` fixture drives `app.router.lifespan_context(app)` manually so
`app.state.redis` actually exists during tests. Without this, `/health`'s
Redis check silently reports `"error"` even though the app works fine for
real requests. `tests/test_health.py` asserts the *exact* response body
(not just that the keys are present) — a looser assertion previously
masked both this and the Windows event-loop bug.

## Migrations

`alembic/env.py` overrides `sqlalchemy.url` from `DIRECT_DATABASE_URL` (not
`DATABASE_URL`) at import time — DDL and advisory locks don't reliably work
through Neon's pooler. `alembic/versions/` currently has two migrations:
the initial 38-table schema, and a follow-up adding 9 partial indexes on
hot status-filtered queries (approvals inbox, open POs, unpaid invoices,
etc. — see `docs/er-diagram.md`'s "constraints worth calling out" section).
Both were downgrade/upgrade round-trip tested for real against Neon before
being considered done — `alembic downgrade base` really did drop all 38
tables, not just "ran without error". Do the same for any new migration:
`autogenerate` → review the diff → `upgrade head` → `downgrade -1` →
`upgrade head` again, checking actual table/index state via the Neon MCP
tools (`mcp__neon__run_sql`), not just Alembic's own exit code.

## Testing

`pytest.ini_options` in `pyproject.toml` sets `asyncio_mode = "auto"`
(no `@pytest.mark.asyncio` boilerplate needed beyond what's already there)
and `--cov=app --cov-report=term-missing` runs on every `pytest` invocation.
Tests hit real Neon + Upstash (no mocking) — this is intentional for now
(Phase 0/1 has limited domain logic to unit-test in isolation yet); once
more of `services/` exists, prefer unit tests with a Neon branch or
testcontainers for isolation rather than hitting the shared dev database.

`tests/test_numbering.py::test_numbering_gapless_under_concurrency` is the
pattern to copy for anything claiming to be concurrency-safe: spin up N
independent `AsyncSessionLocal()` sessions (separate transactions, not one
session calling itself) and `asyncio.gather` them against the real
database — a single-session test cannot exercise real row-lock contention.
Assertions here should verify actual system state (a real HTTP response
body, a real Postgres row, a set of allocated numbers with no gaps), not
just "no exception was raised" — see the Phase 0 health-check bugs and the
Phase 1 seed-script mypy issues, both of which were only caught this way.
