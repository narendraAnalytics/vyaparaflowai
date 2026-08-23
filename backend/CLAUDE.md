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
  schemas/       Pydantic request/response contracts — auth.py, plus
                 master_data.py (Customer/Supplier/Product/Warehouse
                 Create/Update/Out + a generic Page[T] wrapper)
  api/v1/        FastAPI routers — auth.py, plus customers.py/suppliers.py/
                 products.py/warehouses.py (Phase 2.4 master data CRUD:
                 paginated list with q search + is_active filter, get,
                 create, patch, soft-delete/deactivate; org-scoped off the
                 JWT user, reads open to any authenticated org member,
                 writes gated by require_perm(<entity>.manage))
  services/      business logic — the real value of the project, called by
                 n8n over HTTP, never duplicated into n8n Code nodes.
                 numbering.py (Phase 1) is built. pricing.py (Phase 2.5):
                 `price_order()`, a pure Decimal function, no DB access —
                 CGST/SGST vs IGST from origin state vs place-of-supply,
                 line + pro-rata header discounts applied before tax
                 (Sec 15(3) CGST Act), cess, HSN-wise summary grouped by
                 (hsn_code, gst_rate), rupee-rounding with an explicit
                 round_off (Rule 46(r) / e-invoice RndOffAmt). Symmetric,
                 so both sales.py (O2C) and procurement.py (P2P) call it.
                 inventory.py (Phase 2.6): the sole place on_hand/reserved
                 change. reserve/release/receive/issue/adjust each take a
                 LIST of lines (a whole order/GRN/delivery/stock-take),
                 sorted by (product_id, warehouse_id) before applying —
                 deadlock avoidance when one transaction locks multiple
                 inventory_items rows. Each line is one atomic
                 `UPDATE ... WHERE <availability guard> RETURNING` (no
                 explicit SELECT FOR UPDATE needed — Postgres locks the row
                 for the statement's duration and the WHERE guard makes
                 overselling structurally impossible), plus one append-only
                 stock_ledger row per line in the same transaction. Callers
                 own commit/rollback (this module never commits, same as
                 numbering.py) — mutations raise ConflictError and leave no
                 partial state on the first line that fails its guard.
                 `issue(..., from_reservation=True|False)`: True (default)
                 dispatches against a prior reservation (decreases on_hand
                 AND reserved, guarded by reserved>=qty); False is a direct
                 sale with nothing reserved (decreases on_hand only,
                 guarded by on_hand>=qty) — same StockMovementType.ISSUE
                 ledger entry either way, added for sales.py's counter-sale
                 flow (Phase 2.7). Also (Phase 2.8): read-only
                 `get_snapshot()`/`list_below_reorder_level()` (on_hand/
                 reserved/available/reorder_level/safety_stock, the latter
                 scoped by org+warehouse for shortage scans) and
                 `average_daily_issued()` (stock_ledger ISSUE rows over a
                 trailing window) — procurement.py builds its shortage
                 detection and reorder-quantity calculator on these rather
                 than querying inventory_items/stock_ledger directly, so
                 this module stays the sole read/write gateway to both
                 tables.
                 sales.py (Phase 2.7): `create_sales_order()` — validates
                 customer/warehouse/products, prices via pricing.py (org
                 state = origin, customer state = place of supply), checks
                 credit (unpaid customer_invoices + this order vs
                 credit_limit) BEFORE allocating a document number or
                 persisting anything, then best-effort reserves stock per
                 line via inventory.py (try full qty, fall back to what's
                 actually available, record the shortage —
                 sales_order_items.reserved_qty's CHECK constraint,
                 0<=reserved_qty<=quantity, exists for exactly this).
                 Resulting status is RESERVED/PARTIALLY_RESERVED/CONFIRMED;
                 turning a shortage into a PR is procurement.py's job
                 (2.8), not this module's. Also: `create_sales_order(...,
                 is_quote=True)` / `confirm_sales_order()` — a quotation
                 reuses `status=DRAFT` rather than a new table (Odoo does
                 the same — a quotation IS a draft Sales Order, not a
                 separate object); a quote skips inventory entirely and
                 confirm() later reserves at the price locked in at quote
                 time. And `create_counter_sale()` — walk-in/till flow,
                 skips SO+Delivery, creates a customer_invoice directly
                 (`sales_order_id` is nullable for exactly this), reduces
                 stock immediately and all-or-nothing via
                 `inventory.issue(..., from_reservation=False)` (decreases
                 on_hand only, no prior reservation required — see
                 inventory.py above).
                 procurement.py (Phase 2.8): shortage -> requisition -> PO.
                 detect_shortages() (proactive, below reorder_level) and
                 detect_shortage_from_sales_order() (reactive, an order's
                 unfulfilled lines) both read-only and both attach a
                 recommended_qty via reorder_quantity() — pure function,
                 shortage + avg_daily_sales*lead_time_days + safety_stock
                 (avg_daily_sales from new inventory.average_daily_issued(),
                 stock_ledger ISSUE rows over a trailing window). MOQ
                 rounding is NOT in reorder_quantity() — a requisition is
                 an internal need; MOQ only applies once a supplier is
                 picked. score_suppliers() ranks candidates 0-100
                 (price 40% + lead time 25% + reliability 35% + preferred
                 bonus) with a human-readable `reasoning` list per
                 supplier — "last price change" is explicitly NOT scored,
                 documented inline as to why (no price-history table in
                 the Phase 1 schema). create_requisition() persists
                 status=PENDING_APPROVAL (nothing moves it further yet —
                 approvals.py, 2.11, doesn't exist). create_purchase_
                 orders_from_requisition() groups lines by best-scored
                 supplier (one PO per supplier), rounds each line to that
                 supplier's MOQ, prices via pricing.py (supplier state =
                 origin, org state = place of supply — P2P mirror of
                 sales.py's O2C direction), and marks the requisition
                 CONVERTED.
                 matching.py lands later in Phase 2
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

Because tests hit real, persistent Neon data (not a fresh DB per run), any
test that creates a row under a unique constraint (product SKU, warehouse
code, etc.) must generate a fresh value per run (e.g. a `uuid.uuid4().hex`
suffix) rather than a fixed literal — a fixed literal passes the first
time but then permanently 409s on every later run once that row exists.
`tests/test_master_data_api.py` is the pattern to copy.

Not every `services/` module needs Neon, though: a pure-logic module with
no DB access (pricing.py) gets plain synchronous `pytest` tests — no
`async def`, no fixtures, no `asyncio_mode` involved. `tests/test_pricing.py`
is the pattern to copy for the next pure-calculation module; reach for the
Neon-backed pattern below only once a service actually touches the database.

`tests/test_numbering.py::test_numbering_gapless_under_concurrency` is the
pattern to copy for anything claiming to be concurrency-safe: spin up N
independent `AsyncSessionLocal()` sessions (separate transactions, not one
session calling itself) and `asyncio.gather` them against the real
database — a single-session test cannot exercise real row-lock contention.
Assertions here should verify actual system state (a real HTTP response
body, a real Postgres row, a set of allocated numbers with no gaps), not
just "no exception was raised" — see the Phase 0 health-check bugs and the
Phase 1 seed-script mypy issues, both of which were only caught this way.

For higher concurrency counts, the shared app engine's default pool (5 +
10 overflow = 15 connections) isn't enough — most of N=100 "concurrent"
tasks would just queue behind a handful of real connections and never
actually overlap in Postgres, giving a false pass that looks like correct
serialization but never tests real row-lock contention (a documented
asyncpg/psycopg gotcha, not specific to this repo).
`tests/test_inventory.py::test_100_concurrent_reservations_never_oversell`
is the pattern to copy for this: build a dedicated
`create_async_engine(..., pool_size=100, max_overflow=0)` local to that one
test (dispose it in a `finally`) so all N tasks can genuinely hold open
connections at once, rather than reusing `AsyncSessionLocal`.
