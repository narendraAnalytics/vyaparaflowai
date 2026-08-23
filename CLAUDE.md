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

**This is a focused hardware-retail "mini ERP"** (Sales + Purchase +
Inventory + Finance) with n8n automation and AI built in — not an n8n
integration demo with a database attached, and not an attempt to clone
Odoo/ERPNext. Payroll, HR, manufacturing/BOM, CRM, project management,
fixed assets, a full general-ledger accounting suite, employee expenses,
and fleet management are permanently out of scope — see roadmap.txt's "ERP
POSITIONING" block for the full rationale. The ERP (Postgres, via FastAPI
services) owns the state; n8n reacts to it.

- `roadmap.txt` (gitignored, local-only) is the authoritative phase-by-phase
  build plan — work through it phase by phase, don't jump ahead. Phases 0
  (foundation) and 1 (domain model + seed data) are done; Phase 2 (FastAPI
  business logic) is next.
- `docs/adr/` holds architecture decision records — read ADR 0001 before
  changing any part of the core stack.
- `docs/er-diagram.md` has the full schema as Mermaid ERDs, grouped the
  same way as `roadmap.txt` Phase 1 — read it before adding or changing a
  table, not just the model file in isolation.
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
GST/pricing (`services/pricing.py`, 2.5), inventory movements
(`services/inventory.py`, 2.6), and sales (`services/sales.py`, 2.7 —
binding orders, non-binding quotations confirmed later, and direct
walk-in counter sales) are built; procurement/matching are next.

**Inventory and money only change through an append-only ledger row inside
a database transaction** (`stock_ledger`, `ledger_entries`). No direct
`UPDATE` to a quantity or balance column — enforced in code as of Phase 2.6
for inventory (`backend/app/services/inventory.py` is the only code
allowed to touch `inventory_items.on_hand`/`reserved`; every mutation is
one atomic `UPDATE ... WHERE <guard> RETURNING` plus a `stock_ledger` row
in the same transaction — nothing else should write those columns). The
equivalent for `ledger_entries` (money) is still just convention pending a
`services/` module. `inventory_items.available` is a real Postgres
`GENERATED` column (`on_hand - reserved`), not app-computed — don't try to
set it directly. This is load-bearing for the three-way-match and
reconciliation logic later in the roadmap — don't bypass it for a "quick"
write.

**Document numbers (`PO-2026-00001` etc.) are gapless and concurrency-safe**
via `backend/app/services/numbering.py` — one atomic `UPDATE ...
SET last_value = last_value + 1 RETURNING` per (org, doc_type, financial
year), relying on Postgres's own row lock rather than an explicit `SELECT
... FOR UPDATE`. A plain Postgres `SEQUENCE` was deliberately rejected —
see the module docstring for why. Every service that creates a
PO/PR/SO/invoice/etc. should go through this rather than inventing its own
numbering.

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
- All money is `NUMERIC(14,2)`, never float; quantities are `NUMERIC(14,3)`
  (hardware is sold both as whole pieces and continuous units like wire
  meters/coils). In Python this means `Decimal`, not `float`, end to end —
  SQLAlchemy's `Numeric` type defaults to `asdecimal=True`, so ORM money
  columns already round-trip as `Decimal` at runtime even where an older
  `Mapped[float]` type hint says otherwise (cosmetic drift, not the real
  contract); `services/pricing.py` is the reference for doing this right.
- Status columns are plain `VARCHAR` + an explicit `CHECK (... IN (...))`
  against a Python `StrEnum` in `app/db/models/enums.py`, never a native
  Postgres `ENUM` type — adding a new status later is a plain migration,
  not an `ALTER TYPE` dance.
- A phase in `roadmap.txt` is done only when its "Definition of Done" is
  fully verified against the real system, not just "tests pass" — e.g.
  Phase 0's health-check bugs were only caught by testing over real HTTP,
  not the test client alone; Phase 1's migrations were downgrade/upgrade
  round-tripped for real against Neon, and its numbering logic was proven
  gapless under genuine concurrent load, not asserted from a single call.
