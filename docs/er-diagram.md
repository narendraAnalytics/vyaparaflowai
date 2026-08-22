# VyaparaFlow AI — Entity Relationship Diagram

Phase 1 domain model. 38 tables, grouped by the same sections as
`roadmap.txt` Phase 1. Types are the actual Postgres types from the
SQLAlchemy models in `backend/app/db/models/`.

## Master data

```mermaid
erDiagram
    organizations ||--o{ users : has
    organizations ||--o{ customers : has
    organizations ||--o{ suppliers : has
    organizations ||--o{ products : has
    organizations ||--o{ warehouses : has
    roles ||--o{ user_roles : ""
    users ||--o{ user_roles : ""
    products ||--o{ product_suppliers : "sourced from"
    suppliers ||--o{ product_suppliers : "supplies"

    organizations {
        uuid id PK
        varchar name
        varchar gstin
        varchar state_code
    }
    users {
        uuid id PK
        uuid org_id FK
        varchar email
        varchar hashed_password
    }
    customers {
        uuid id PK
        uuid org_id FK
        varchar gstin
        numeric credit_limit
        int payment_terms_days
    }
    suppliers {
        uuid id PK
        uuid org_id FK
        varchar gstin
        int lead_time_days
        numeric reliability_score
    }
    products {
        uuid id PK
        uuid org_id FK
        varchar sku
        varchar hsn_code
        varchar uom
        numeric gst_rate
    }
    product_suppliers {
        uuid id PK
        uuid product_id FK
        uuid supplier_id FK
        numeric unit_price
        int moq
    }
    warehouses {
        uuid id PK
        uuid org_id FK
        varchar code
    }
```

## Inventory — the load-bearing design decision

```mermaid
erDiagram
    products ||--o{ inventory_items : "stock of"
    warehouses ||--o{ inventory_items : "at"
    products ||--o{ stock_ledger : ""
    warehouses ||--o{ stock_ledger : ""

    inventory_items {
        uuid id PK
        uuid product_id FK
        uuid warehouse_id FK
        numeric on_hand
        numeric reserved
        numeric available "GENERATED: on_hand - reserved"
        numeric reorder_level
        numeric safety_stock
    }
    stock_ledger {
        uuid id PK
        uuid product_id FK
        uuid warehouse_id FK
        varchar movement_type "receipt/issue/reserve/release/adjustment/return_in/return_out"
        numeric qty_delta
        numeric balance_after
        varchar ref_type
        uuid ref_id
        timestamptz created_at "append-only, no updated_at"
    }
```

`inventory_items` is the current-state snapshot; `stock_ledger` is the
append-only audit trail everything reconciles against. **Application code
never UPDATEs `on_hand`/`reserved` directly** — every change is a ledger
row written inside the same transaction (enforced in `services/inventory.py`,
Phase 2, not the DB — Postgres can't require "this UPDATE must be
accompanied by an INSERT elsewhere" without a trigger, which is deferred
until it proves necessary).

## Sales side (Order-to-Cash)

```mermaid
erDiagram
    customers ||--o{ sales_orders : places
    sales_orders ||--o{ sales_order_items : contains
    sales_orders ||--o{ deliveries : fulfilled_by
    deliveries ||--o{ delivery_items : contains
    sales_order_items ||--o{ delivery_items : "delivers"
    sales_orders ||--o{ customer_invoices : "invoiced as"
    customer_invoices ||--o{ customer_invoice_items : contains

    sales_orders {
        uuid id PK
        uuid customer_id FK
        varchar order_number
        varchar status "draft/confirmed/partially_reserved/reserved/dispatched/delivered/invoiced/cancelled"
        numeric total
    }
    sales_order_items {
        uuid id PK
        uuid sales_order_id FK
        uuid product_id FK
        numeric quantity
        numeric reserved_qty
    }
    customer_invoices {
        uuid id PK
        uuid customer_id FK
        uuid sales_order_id FK
        varchar invoice_number
        varchar status
        numeric amount_paid
        varchar irn "GST e-Invoice IRN, Phase 5"
    }
```

## Purchase side (Procure-to-Pay)

```mermaid
erDiagram
    suppliers ||--o{ purchase_orders : "issued to"
    purchase_requisitions ||--o{ purchase_requisition_items : contains
    purchase_requisitions ||--o| purchase_orders : "converts to"
    purchase_orders ||--o{ purchase_order_items : contains
    purchase_orders ||--o{ goods_receipts : "received against"
    goods_receipts ||--o{ goods_receipt_items : contains
    purchase_order_items ||--o{ goods_receipt_items : ""
    suppliers ||--o{ supplier_invoices : bills
    purchase_orders ||--o{ supplier_invoices : "invoiced against"
    supplier_invoices ||--o{ supplier_invoice_items : contains
    purchase_orders ||--o{ three_way_match_results : ""
    goods_receipts ||--o{ three_way_match_results : ""
    supplier_invoices ||--o{ three_way_match_results : ""

    purchase_requisitions {
        uuid id PK
        varchar status "draft/pending_approval/approved/rejected/converted/cancelled"
        uuid triggered_by_sales_order_id FK "nullable — manual reorders too"
    }
    purchase_orders {
        uuid id PK
        uuid supplier_id FK
        varchar po_number
        varchar status
        numeric total
    }
    goods_receipt_items {
        uuid id PK
        numeric ordered_quantity
        numeric received_quantity
        numeric accepted_quantity
        numeric rejected_quantity
        numeric damaged_quantity
    }
    supplier_invoices {
        uuid id PK
        uuid supplier_id FK
        varchar invoice_number
        varchar status "received/matched/approved/blocked/paid"
    }
    three_way_match_results {
        uuid id PK
        numeric qty_variance
        numeric price_variance
        int risk_score "0-100"
        varchar verdict "auto_approve/review/block"
        jsonb reason_codes
    }
```

`goods_receipt_items` carries all four quantities separately (ordered,
received, accepted, rejected, damaged) per the roadmap's explicit
requirement — a shipment of 50 with 48 accepted and 2 damaged is not
collapsed into a single "received 48" number; the disposition is preserved.

## Money

```mermaid
erDiagram
    customers ||--o{ payments : pays
    suppliers ||--o{ payments : "paid by"
    payments ||--o{ payment_allocations : allocates
    customer_invoices ||--o{ payment_allocations : "settled by"
    supplier_invoices ||--o{ payment_allocations : "settled by"
    customer_invoices ||--o{ credit_debit_notes : ""
    supplier_invoices ||--o{ credit_debit_notes : ""

    payments {
        uuid id PK
        varchar direction "inbound/outbound"
        uuid customer_id FK "exactly one of customer_id/supplier_id set"
        uuid supplier_id FK
        numeric amount
        varchar razorpay_payment_id
    }
    payment_allocations {
        uuid id PK
        uuid payment_id FK
        uuid customer_invoice_id FK "exactly one set"
        uuid supplier_invoice_id FK
        numeric amount
    }
    ledger_entries {
        uuid id PK
        varchar account "AR/AP/inventory/sales_revenue/cogs/gst_output/gst_input/cash_bank"
        varchar entry_side "debit/credit"
        numeric amount
        uuid transaction_group_id "balances within this group"
    }
```

`ledger_entries` is double-entry: every business event posts a balanced set
of rows (`sum(debit) == sum(credit)` within one `transaction_group_id`) —
separate from `stock_ledger`, which tracks physical units, not money.

## Cross-cutting

```mermaid
erDiagram
    organizations ||--o{ approvals : ""
    organizations ||--o{ audit_logs : ""
    organizations ||--o{ workflow_events : ""
    organizations ||--o{ outbox_events : ""
    organizations ||--o{ documents : ""
    organizations ||--o{ notifications : ""
    organizations ||--o{ document_sequences : ""

    approvals {
        uuid id PK
        varchar entity_type "polymorphic — no dedicated FK per approvable type"
        uuid entity_id
        int level
        varchar status "pending/approved/rejected/delegated/escalated"
        timestamptz sla_due_at
    }
    idempotency_keys {
        varchar key PK
        varchar request_hash
        jsonb response
        timestamptz expires_at
    }
    outbox_events {
        uuid id PK
        varchar aggregate_type
        uuid aggregate_id
        varchar event_type
        jsonb payload
        timestamptz published_at "null until a worker confirms delivery to n8n"
    }
    document_sequences {
        uuid id PK
        uuid org_id FK
        varchar doc_type
        varchar financial_year
        int last_value "SELECT...FOR UPDATE serializes allocation — gapless"
    }
```

## Constraints worth calling out (not visible in the diagrams above)

- `ck_inventory_items_reserved_lte_on_hand`: `reserved <= on_hand` — can
  never reserve more than physically present.
- `ck_grn_items_disposition_sums_lte_received`:
  `accepted + rejected + damaged <= received_quantity`.
- `ck_payments_single_party` / `ck_payment_allocations_single_invoice`:
  exactly one of the two nullable FK columns is set (Postgres boolean-cast
  arithmetic: `(a IS NOT NULL)::int + (b IS NOT NULL)::int = 1`).
- GSTIN columns: format-checked by regex CHECK (nullable — unregistered
  "URP" counterparties are valid), not full checksum validation (that's an
  app-layer concern, Phase 4).
- Every status column is a `CHECK (... IN (...))` against a Python
  `StrEnum` in `app/db/models/enums.py`, not a native Postgres `ENUM` type
  — adding a new status later is a plain migration, not an `ALTER TYPE`.
