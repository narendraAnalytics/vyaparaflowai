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
  business logic) is done, including the goods-receipt/supplier-invoice
  intake endpoints that closed its last Definition-of-Done gap; Phase 3
  (n8n workflows) is in progress — 3.1–3.6 done (n8n queue-mode Docker
  Compose stack; backend `X-API-Key`-or-JWT auth; n8n credentials; WF-01
  Sales Order Intake, WF-02 Inventory Shortage Router, WF-03 Purchase
  Order Approval — all built AND verified against real infra, including a
  real human Telegram-approval click resuming a paused workflow via
  ngrok). 3.7 (WF-04 Send PO to Supplier) is PARTIAL: all backend work
  is done and verified for real (PO approve/reject endpoints — closing a
  real gap where PurchaseOrder.status never updated after approval — PDF
  generation via reportlab, MinIO upload), but the n8n side (wiring WF-03
  to call the new endpoints, the actual WF-04 workflow) isn't built yet.
  **Next session resumes exactly there** — see roadmap.txt's NEXT ACTION.
  Phase 3 was enhanced 2026-08-24
  against current n8n production guidance and a review of `backend/.env`;
  locked-in decisions worth knowing before touching it: Telegram (not
  WhatsApp/Slack) is the channel for internal approvals/alerts (WF-03,
  WF-99) — free, no Meta review, native n8n inline-button support; Slack
  is dropped for now (re-add only if a real need shows up); WhatsApp for
  the customer-facing leg (WF-06) is deliberately LAST, added once the
  core flow works on email/Telegram alone, and will use **WasenderAPI**
  (plain Bearer-token HTTP Request node, no native n8n node) instead of
  the official WhatsApp Business Cloud API, chosen for zero Meta
  verification overhead on a portfolio-sized project.
  **Gmail → Resend, 2026-08-24**: outbound email (WF-04/WF-06) uses
  Resend (Bearer-token HTTP Request, no OAuth), not Gmail — same
  zero-verification-overhead reasoning as WasenderAPI. Gmail comes back
  later, scoped only to Phase 4.3's inbound invoice-receiving trigger.
- `docs/adr/` holds architecture decision records — read ADR 0001 before
  changing any part of the core stack.
- `docs/er-diagram.md` has the full schema as Mermaid ERDs, grouped the
  same way as `roadmap.txt` Phase 1 — read it before adding or changing a
  table, not just the model file in isolation.
- `n8nworkflow.md` documents n8n operating lessons (how to edit workflows
  via n8n-mcp, common error patterns, verification method) carried over
  from a prior project — read it before touching any n8n workflow.
  n8n queue-mode gotchas found 2026-08-24 (added to `n8nworkflow.md` too):
  worker/webhook must `depends_on: n8n-main` with `condition:
  service_healthy` (a real `/healthz` check), not `service_started` — all
  three processes migrate the DB on boot and race if they start together.
  A workflow calling another via Execute Sub-workflow must have that
  sub-workflow ACTIVE, not just saved, before the caller can activate.
  Telegram's Bot API rejects any inline-button URL whose host is
  `localhost` — local testing of `sendAndWait` (or anything handing a
  callback URL to a third party) needs a real tunnel (ngrok); see root
  `.env`'s `N8N_WEBHOOK_PUBLIC_URL`.
- **Starting a new session, n8n side**: usually nothing to do — all four
  n8n containers (`n8n-postgres`, `n8n-main`, `n8n-worker`, `n8n-webhook`)
  have `restart: unless-stopped`, so they stay running (and self-heal on
  a Docker Desktop restart/reboot) as long as Docker Desktop itself is
  running. If Docker Desktop was closed, just reopen it — n8n-mcp
  reconnects to the already-running instance. Two things are NOT
  Dockerized and DO need a manual restart every session: the backend dev
  server (`make dev` / `uv run python -m app.dev`, since n8n workflows
  call it at `host.docker.internal:8000`) and, only if testing WF-03's
  Telegram approval buttons, ngrok (`ngrok http 5679` — its URL changes
  every restart, requiring an update to `.env`'s `N8N_WEBHOOK_PUBLIC_URL`
  and a `docker compose up -d n8n-main n8n-worker n8n-webhook
  --force-recreate`).
- `backend/CLAUDE.md` has backend-specific commands and conventions.

## Monorepo layout

```text
backend/           FastAPI service — see backend/CLAUDE.md
frontend/          Next.js 16 dashboard (not yet built)
n8n/workflows/      exported workflow JSON, version-controlled
docs/adr/           architecture decision records
infra/               deploy config for Render (LOCKED 2026-08-24, was GCP/
                     Terraform) — render.yaml Blueprint, not yet built
docker-compose.yml   local dev: minio + n8n queue-mode stack (main/worker/
                     webhook + its own Postgres) — NOT the app's own
                     postgres/redis (Neon/Upstash) — see below
```

## Architecture (non-obvious, spans multiple files/services)

```text
Next.js 16 (dashboard)
        │
        ▼
      n8n  ──────────────►  external services
   (orchestration,          (Telegram, Resend, WhatsApp,
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
(`services/inventory.py`, 2.6), sales (`services/sales.py`, 2.7 — binding
orders, non-binding quotations confirmed later, and direct walk-in counter
sales), procurement (`services/procurement.py`, 2.8 — shortage detection,
an explainable reorder-quantity calculator, a scored supplier selection,
and requisition -> PO creation), the three-way match
(`services/matching.py`, 2.9 — PO vs goods receipt vs supplier invoice),
payments/aging (`services/payments.py`, 2.10), the approval chain
(`services/approvals.py`, 2.11), and goods-receipt/supplier-invoice intake
(`services/receiving.py` — never its own 2.x line item; added as a
follow-up once Phase 2's Definition of Done flagged that without it the
P2P curl cycle couldn't reach matching.py on its own) are all built.

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
