# VyaparaFlow AI

AI-powered procure-to-pay (P2P) and order-to-cash (O2C) automation platform
for an Indian hardware retail business. Built for **Sri Lakshmi Hardware &
Electricals** as the reference tenant.

When a customer orders more than is in stock, VyaparaFlow automatically
raises a purchase requisition, routes it for approval, emails the supplier,
records the goods receipt, three-way-matches the supplier invoice, reserves
stock for the original order, invoices the customer with a GST e-Invoice
(IRN + QR), and reconciles the incoming UPI payment — with a human in the
loop wherever money or irreversible decisions are involved.

See [`roadmap.txt`](./roadmap.txt) for the full phase-by-phase build plan,
[`n8nworkflow.md`](./n8nworkflow.md) for n8n operating lessons carried over
from a prior project, and [`docs/adr/`](./docs/adr/) for architecture
decisions.

## Architecture

```text
Next.js 16 (dashboard)
        │
        ▼
      n8n  ──────────────►  external services
   (orchestration,          (Gmail, Slack, WhatsApp,
    queue mode)              Razorpay, GST IRP)
        │
        ▼  HTTP + Idempotency-Key
   FastAPI (business logic, the source of truth for rules)
        │
        ▼
   PostgreSQL (Neon) ── Redis (Upstash) ── Object storage (MinIO/GCS)
```

Business logic lives in FastAPI `services/`, never in n8n Code nodes.
Inventory and money only change through an append-only ledger row inside a
database transaction — see `docs/adr/` for why.

## Stack

- **Backend**: FastAPI (async), SQLAlchemy 2.0, Alembic, Pydantic v2, uv
- **Database**: PostgreSQL on [Neon](https://neon.tech) (serverless, branchable)
- **Orchestration**: n8n (queue mode)
- **Frontend**: Next.js 16, TypeScript, Tailwind, shadcn/ui, TanStack Query
- **AI**: Claude (structured extraction + evals), vision-LLM document intelligence
- **Infra**: Docker Compose (local), Terraform + GCP Cloud Run (production)

## Local setup

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker Desktop, a
[Neon](https://neon.tech) account (or any Postgres 16+ connection string),
an [Upstash](https://upstash.com) Redis database (or any Redis 7+ URL).

```bash
# 1. Backend env
cp backend/.env.example backend/.env
# edit backend/.env with your Neon DATABASE_URL / DIRECT_DATABASE_URL
# and your Upstash REDIS_URL (the "Redis client" rediss:// string, not the REST API URL)

# 2. Install deps
cd backend && uv sync && cd ..

# 3. Bring up MinIO and n8n via Docker
make up

# 4. Run the backend locally with hot reload (see note below on Windows)
make dev

# 5. Run migrations (once models exist — Phase 1)
make migrate

# 6. Verify
curl http://localhost:8000/health
```

> **Windows note**: don't run `uvicorn app.main:app` or `fastapi dev` directly.
> uvicorn ≥0.36 hard-codes `ProactorEventLoop` on Windows, which breaks
> psycopg3's async mode against Neon. `make dev` (`app/dev.py`) installs a
> `SelectorEventLoop` factory to work around it — see `app/core/winloop.py`.
> Not an issue in Docker/Linux/production.

## Common commands

| Command        | What it does                                  |
|----------------|------------------------------------------------|
| `make up`      | Start minio, n8n via Docker                    |
| `make dev`     | Run the backend locally with hot reload        |
| `make down`    | Stop everything                                |
| `make test`    | Run the backend test suite (pytest + coverage) |
| `make lint`    | Ruff lint check                                |
| `make format`  | Ruff auto-format                               |
| `make typecheck` | mypy over `app/`                             |
| `make migrate` | Apply Alembic migrations (direct Neon URL)     |
| `make revision m="message"` | Generate a new Alembic migration |
| `make seed`    | Seed demo data (Phase 1)                       |

## Repository layout

```text
backend/
  app/
    core/          settings, logging, security (Phase 0-2)
    db/             session, Base, models/ (Phase 1)
    schemas/        Pydantic request/response contracts
    api/v1/         FastAPI routers
    services/       business logic — the real value of the project
    workers/        background jobs (arq/celery)
    ai/             document extraction, prompts, eval harness
    integrations/   gst/, razorpay/, whatsapp/, storage/
  alembic/          versioned schema migrations
  tests/
frontend/           Next.js 16 dashboard (Phase 6)
n8n/workflows/       exported workflow JSON, version-controlled
infra/terraform/     GCP infrastructure as code (Phase 9)
docs/adr/            architecture decision records
```
