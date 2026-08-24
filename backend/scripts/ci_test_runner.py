"""CI-only entrypoint (Phase 2.14): starts ephemeral Postgres + Redis via
testcontainers, migrates and seeds the fresh Postgres, then runs the full
pytest suite as a SEPARATE subprocess.

**Why a subprocess, not an in-process pytest fixture.** app/db/session.py
creates its async engine at MODULE IMPORT time, bound to whatever
DATABASE_URL happens to be in the environment right then:
`engine = create_async_engine(get_settings().database_url, ...)`. A
pytest conftest.py fixture (even a `pytest_configure` hook) cannot
reliably guarantee it runs before that module gets imported — conftest.py
files themselves routinely import `app.main`/`app.db.session` at their
own top level (see tests/conftest.py), and `get_settings()` is
`@lru_cache`d, so whichever env vars exist at the FIRST call wins for the
rest of the process. Rather than fight that ordering, this script sets
the environment variables and then launches `pytest` as a brand-new
subprocess — which reads the environment fresh from process start, no
import-order or cache-staleness risk at all.

**Not used for local dev.** `make test` / `pytest` still hit live Neon +
Upstash exactly as documented in backend/CLAUDE.md's Testing section —
this script is wired into .github/workflows/ci.yml only, so a CI run is
isolated and repeatable (no shared-state flakiness, no Neon compute
usage) without changing how any existing test file or local workflow
behaves.

**Two real settings this script overrides, discovered by actually running
the full suite against a fresh container rather than assuming it would
just work:**

1. `RATE_LIMIT_LOGIN_PER_MINUTE` — httpx's `ASGITransport` gives every
   test client the same effective identity for `RateLimiter`'s IP-based
   key, so the whole suite's `/auth/login` calls share one bucket.
   Against live Neon (seconds of network latency per call) that bucket
   never filled within its 60s window; against a local container
   (near-zero latency) the suite blows through the default limit of 10
   almost immediately, cascading into unrelated failures. Raised here,
   in the test env only — the production default in `.env.example`
   is untouched.
2. Postgres `max_connections` — the default container image caps it at
   100, and `tests/test_inventory.py`'s 100-genuinely-concurrent-
   reservations test opens a dedicated pool of exactly 100 connections
   on top of whatever pytest's own session already holds, so it
   intermittently starves. Neon's managed instance has headroom this
   never hit. Raised via Postgres's own startup flag.
"""

import os
import subprocess
import sys

from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

_POSTGRES_IMAGE = "postgres:18-alpine"
_REDIS_IMAGE = "redis:7-alpine"


def main() -> int:
    with (
        PostgresContainer(_POSTGRES_IMAGE, driver="psycopg").with_command(
            "postgres -c max_connections=300"
        ) as postgres,
        RedisContainer(_REDIS_IMAGE) as redis,
    ):
        db_url = postgres.get_connection_url()
        redis_host = redis.get_container_host_ip()
        redis_port = redis.get_exposed_port(6379)

        env = os.environ.copy()
        env["DATABASE_URL"] = db_url
        # No pooler in a throwaway container — direct and pooled are the
        # same endpoint, same as any local single-instance Postgres.
        env["DIRECT_DATABASE_URL"] = db_url
        env["REDIS_URL"] = f"redis://{redis_host}:{redis_port}/0"
        # See module docstring point 1 — production default (10) is
        # untouched, this only raises the ceiling for this test run.
        # High enough that ~80 login calls across the whole suite (fast,
        # low-latency container) never cascade-429 each other; low enough
        # that tests/test_ratelimit_api.py's own threshold-loop test
        # (limit+1 requests) still runs in a fraction of a second.
        env.setdefault("RATE_LIMIT_LOGIN_PER_MINUTE", "300")

        print(f"[ci_test_runner] Postgres ready at {db_url}", flush=True)
        print(f"[ci_test_runner] Redis ready at {env['REDIS_URL']}", flush=True)

        migrate = subprocess.run(["uv", "run", "alembic", "upgrade", "head"], env=env)
        if migrate.returncode != 0:
            return migrate.returncode

        seed = subprocess.run(["uv", "run", "python", "-m", "app.db.seed"], env=env)
        if seed.returncode != 0:
            return seed.returncode

        result = subprocess.run(["uv", "run", "pytest", *sys.argv[1:]], env=env)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
