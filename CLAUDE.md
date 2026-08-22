# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

VyaparaFlow AI: an AI-powered procure-to-pay (P2P) and order-to-cash (O2C)
automation platform for an Indian hardware retail business (reference
tenant: Sri Lakshmi Hardware & Electricals). When a customer orders more
than is in stock, the system automatically raises a purchase requisition,
routes it for approval, emails the supplier, records the goods receipt,
three-way-matches the supplier invoice, reserves stock for the original
order, invoices the customer with a GST e-Invoice (IRN + QR), and
reconciles the incoming UPI payment — with a human in the loop wherever
money or irreversible decisions are involved.

- `roadmap.txt` (gitignored, local-only) is the authoritative phase-by-phase
  build plan — work through it phase by phase, don't jump ahead.
- `docs/adr/` holds architecture decision records — read ADR 0001 before
  changing any part of the core stack.
- `n8nworkflow.md` documents n8n operating lessons (how to edit workflows
  via n8n-mcp, common error patterns, verification method) carried over
  from a prior project — read it before touching any n8n workflow.
- `backend/CLAUDE.md` has backend-specific commands and conventions.

## Monorepo layout

```text
backend/           FastAPI service — see backend/CLAUDE.md
frontend/          Next.js 16 dashboard (not yet built)
n8n/workflows/      exported workflow JSON, version-controlled
docs/adr/           architecture decision records
infra/terraform/    GCP infrastructure as code (not yet built)
docker-compose.yml   local dev: minio + n8n (NOT postgres/redis — see below)
```

## Architecture (non-obvious, spans multiple files/services)

```text
Next.js 16 (dashboard)
        │
        ▼
      n8n  ──────────────►  external services
   (orchestration,          (Gmail, Slack, WhatsApp,
    queue mode, Phase 3)     Razorpay, GST IRP)
        │
        ▼  HTTP + Idempotency-Key
   FastAPI (business logic — the source of truth for rules)
        │
        ▼
   PostgreSQL (Neon) ── Redis (Upstash) ── Object storage (MinIO/GCS)
```

**Business logic never lives in n8n.** n8n (Phase 3+) is the orchestration/
integration layer only — webhooks, notifications, approval routing. Every
rule (pricing, GST, inventory reservation, three-way match, reorder
quantity) lives in `backend/app/services/` and n8n calls it over HTTP.

**Inventory and money only change through an append-only ledger row inside
a database transaction** (`stock_ledger`, `ledger_entries` — Phase 1). No
direct `UPDATE` to a quantity or balance column. This is the audit-trail
design and it is load-bearing for the three-way-match and reconciliation
logic later in the roadmap — don't bypass it for a "quick" write.

**Managed cloud services, not local containers, for stateful infra.**
Postgres is Neon (not a local container) and Redis is Upstash (not a local
container) — `docker-compose.yml` only runs minio and n8n locally. Both
connection strings live in `backend/.env` (gitignored); see
`backend/.env.example` for the required shape. This project has the Neon
MCP server available — prefer it (`mcp__neon__*`) over asking the user to
paste connection details when you need to inspect/branch/query the database.

## Cross-cutting conventions

- Every POST that creates money or stock must be idempotent
  (`Idempotency-Key` header — middleware added in Phase 2).
- AI (Phase 4+) never takes an irreversible action alone — a deterministic
  rule/threshold gates it before anything commits.
- All money is `NUMERIC(14,2)`, never float.
- A phase in `roadmap.txt` is done only when its "Definition of Done" is
  fully verified with a real request/response, not just "tests pass" — see
  the Phase 0 entries for the pattern (health check bugs were only caught
  by testing over real HTTP, not the test client alone).
