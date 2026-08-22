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
  db/            async SQLAlchemy engine/session (session.py), Base,
                 models/ (empty until Phase 1)
  schemas/       Pydantic request/response contracts (empty until Phase 2)
  api/v1/        FastAPI routers (empty until Phase 2)
  services/      business logic — the real value of the project, called by
                 n8n over HTTP, never duplicated into n8n Code nodes
  workers/       background jobs (arq/celery — not yet built)
  ai/            document extraction, prompts, eval harness (Phase 4)
  integrations/  gst/, razorpay/, whatsapp/, storage/ (Phase 4-5)
  main.py        FastAPI app, lifespan (owns the Redis client on
                 app.state.redis), request-id middleware, /health
  dev.py         local dev entrypoint — see winloop.py
```

**Neon: two connection strings, two purposes.** `DATABASE_URL` is the
pooled endpoint (`-pooler` in the hostname) — used by the app at runtime
for many short-lived connections. `DIRECT_DATABASE_URL` is the unpooled
endpoint — used only by Alembic (`alembic/env.py` overrides
`sqlalchemy.url` from it at import time), because DDL and advisory locks
don't reliably work through the pooler. Don't swap these.

**Windows + psycopg3 async + uvicorn ≥0.36 is broken by default.** uvicorn
hard-codes `ProactorEventLoop` as its loop factory on `win32` (bypassing
`asyncio.set_event_loop_policy()` entirely), which breaks psycopg3's async
mode against Neon with `psycopg.InterfaceError`. `app/core/winloop.py`
provides a `SelectorEventLoop` factory that `app/dev.py` passes into
`uvicorn.run(loop=...)` to work around it. `app/main.py` also sets the
event loop policy at import time, but that only covers pytest / plain
`asyncio.run()` callers (e.g. a future script) — it does **not** fix
`uvicorn`/`fastapi dev` invoked directly, which is why `dev.py` exists.
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

## Testing

`pytest.ini_options` in `pyproject.toml` sets `asyncio_mode = "auto"`
(no `@pytest.mark.asyncio` boilerplate needed beyond what's already there)
and `--cov=app --cov-report=term-missing` runs on every `pytest` invocation.
`tests/test_health.py` hits real Neon + Upstash (no mocking) — this is
intentional for now (Phase 0/1 has no domain logic to unit-test yet); once
`services/` exists, prefer unit tests with a Neon branch or testcontainers
for isolation rather than hitting the shared dev database.
