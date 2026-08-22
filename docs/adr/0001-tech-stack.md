# ADR 0001: Core tech stack

Status: Accepted — 2026-08-22

## Context

VyaparaFlow AI needs to be a resume-grade, production-shaped system, not a
demo. See `roadmap.txt` Section 0 for the full research-backed rationale.

## Decision

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Already scaffolded; best AI/ML ecosystem |
| API framework | FastAPI (async) | Most-used Python web framework in 2026 (~38-40% adoption); async, type-safe, the standard for AI-facing services |
| Package manager | uv | 10-100x faster than pip; 2026 default |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic | Typed, async-native, versioned schema |
| Database | PostgreSQL via **Neon** | Serverless, branchable (instant copy-on-write branches for testing/preview), generous free tier, first-class MCP tooling for this project. Pooled endpoint for the app, direct endpoint for Alembic (DDL/advisory locks don't reliably work through the pooler). |
| Cache / queue | Redis via **Upstash** | Idempotency keys, distributed locks, rate limiting. Managed/serverless like Neon — no local Redis container in `docker-compose.yml`. Requires `rediss://` (TLS). n8n's own queue-mode Redis (Phase 3) is a separate decision, since Upstash's free-tier limits may not suit BullMQ under real load. |
| Orchestration | n8n (queue mode from Phase 3) | Integration layer only — webhooks, notifications, approvals routing. Business logic never lives in n8n; it calls FastAPI. |
| Frontend | Next.js 16 + TypeScript + Tailwind + shadcn/ui + TanStack Query | 2026 standard admin/dashboard stack |
| AI | Claude (Opus/Sonnet 5) primary, vision-LLM hybrid extraction, Pydantic-enforced structured output | 2026 consensus: vision LLMs beat classic OCR on real invoices; hybrid OCR-for-header + LLM-for-line-items |
| Observability | OpenTelemetry → Grafana (Tempo/Loki/Prometheus) + Sentry | OTel is the 2026 default; a single trace spanning n8n → FastAPI → Postgres → LLM is the flagship portfolio artifact |
| Deployment | Docker + Terraform + GCP Cloud Run | ATS-relevant IaC + containerized CI/CD story |

## Consequences

- Postgres is the single source of truth. Inventory and money change only
  via an append-only ledger row inside a transaction (see ADR 0002).
- Using Neon means no local Postgres container in `docker-compose.yml` —
  simpler local setup, but requires internet connectivity for local dev.
  Neon branching (`mcp__neon__create_branch`) will be used for isolated
  testing/CI instead of spinning up ephemeral local Postgres containers.
- Same pattern for Redis: Upstash instead of a local `redis` container.
  `app/core/config.py` treats `REDIS_URL` as required (no localhost
  fallback) so a missing/misconfigured value fails fast instead of silently
  trying to reach a container that no longer exists.
- `psycopg[binary]` (v3) is the driver, using the `postgresql+psycopg://`
  SQLAlchemy dialect — supports both sync and async engines, so we don't
  need `asyncpg` as a second driver dependency.
- n8n's own workflow-persistence Postgres (Phase 3, queue mode) is a
  separate concern from the business database — it stores n8n's internal
  execution/credential state, not VyaparaFlow's domain data, and will get
  its own Neon project/branch when Phase 3 sets up queue mode.

## Sources

See `roadmap.txt` → "RESEARCH SOURCES (2026)" for the full list of
citations backing each stack choice.
