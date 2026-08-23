"""Procure-to-pay entry points: turn a stock shortage into a requisition,
and a requisition into one or more purchase orders.

Four capabilities, matching the roadmap's four bullets one-to-one:

1. **Shortage detection** — `detect_shortages()` (proactive: every product
   at a warehouse currently below its reorder point) and
   `detect_shortage_from_sales_order()` (reactive: a specific order's
   unfulfilled lines, the "customer ordered 40, stock was 18" case). Both
   return `ShortageLine`s with a `recommended_qty` already computed via
   #2 below — read-only, no DB writes.
2. **Reorder quantity calculator** — `reorder_quantity()`, a pure function:
   `shortage + (avg_daily_sales * lead_time_days) + safety_stock`, exactly
   the roadmap's formula. `avg_daily_sales` comes from
   `services/inventory.average_daily_issued()` (stock_ledger ISSUE rows
   over a trailing window — both a delivery dispatch and a counter sale
   write that movement type, so this captures true demand either way).
   MOQ rounding is deliberately NOT part of this function: a requisition
   states an internal need, not a supplier order — MOQ only makes sense
   once a specific supplier is chosen, which happens at PO creation (#4).
3. **Supplier selection score** — `score_suppliers()`: an explainable 0-100
   ranking per candidate supplier for a product, weighted across price
   (40%), lead time (25%) and reliability (35%), plus a small preferred-
   supplier bonus. Each `SupplierScore` carries a human-readable
   `reasoning` list so a caller (dashboard, approval card) can show WHY a
   supplier was picked, not just the number. "Last price change" from the
   roadmap's bullet is NOT scored — Phase 1's schema has no price-history
   table for product_suppliers (only the current `unit_price`), so there
   is nothing to compute a trend from. Documented here rather than
   silently dropped: a future `product_supplier_price_history` table
   would be the natural way to add it.
4. **Requisition -> PO** — `create_requisition()` persists a
   purchase_requisition (status=PENDING_APPROVAL — approvals.py, Phase
   2.11, doesn't exist yet, so nothing currently moves it past that
   status; this module does not gate PO creation on approval, since
   half-building an enforcement rule ahead of the module that owns it
   would just be dead weight to redo later). `create_purchase_orders_from_
   requisition()` groups the requisition's lines by best-scored supplier
   (one PO per supplier — a single PO can only have one supplier_id), then
   for each group: rounds every line's quantity up to that supplier's MOQ,
   prices via services/pricing.py (supplier state = origin, org state =
   place of supply — the P2P mirror of sales.py's O2C pricing direction),
   allocates a PO number, persists the PO + items, and marks the
   requisition CONVERTED.

Like every other services/ module, functions here never commit — the
caller owns the transaction (numbering.py/pricing.py/inventory.py/
sales.py convention).
"""

import math
import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.catalog import Product, ProductSupplier
from app.db.models.enums import (
    PurchaseOrderStatus,
    PurchaseRequisitionStatus,
    SalesOrderStatus,
)
from app.db.models.org import Organization
from app.db.models.partners import Supplier
from app.db.models.purchase import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequisition,
    PurchaseRequisitionItem,
)
from app.db.models.sales import SalesOrder, SalesOrderItem
from app.services import inventory
from app.services.numbering import next_document_number
from app.services.pricing import PricingLineInput, price_order

ZERO = Decimal("0")

# Supplier-score weights (must sum to 1). Tunable, but explainable: each
# component is surfaced in SupplierScore.reasoning regardless of weight.
_PRICE_WEIGHT = Decimal("0.40")
_LEAD_TIME_WEIGHT = Decimal("0.25")
_RELIABILITY_WEIGHT = Decimal("0.35")
_PREFERRED_BONUS = Decimal("5")


class ShortageLine(BaseModel):
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    on_hand: Decimal
    reserved: Decimal
    available: Decimal
    reorder_level: Decimal
    safety_stock: Decimal
    shortage_qty: Decimal
    recommended_qty: Decimal


class SupplierScore(BaseModel):
    supplier_id: uuid.UUID
    supplier_name: str
    unit_price: Decimal
    lead_time_days: int
    reliability_score: Decimal
    is_preferred: bool
    price_score: Decimal
    lead_time_score: Decimal
    reliability_component: Decimal
    preferred_bonus: Decimal
    total_score: Decimal
    reasoning: list[str]


class RequisitionLineInput(BaseModel):
    product_id: uuid.UUID
    quantity: Decimal = Field(gt=0)
    reason: str | None = None


class CreateRequisitionRequest(BaseModel):
    lines: list[RequisitionLineInput]
    triggered_by_sales_order_id: uuid.UUID | None = None
    notes: str | None = None


class CreateRequisitionResult(BaseModel):
    purchase_requisition_id: uuid.UUID
    requisition_number: str
    status: str


class PurchaseOrderLineResult(BaseModel):
    product_id: uuid.UUID
    requisitioned_qty: Decimal
    ordered_qty: Decimal
    unit_price: Decimal
    gst_rate: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal


class CreatePurchaseOrderResult(BaseModel):
    purchase_order_id: uuid.UUID
    po_number: str
    supplier_id: uuid.UUID
    supplier_name: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    lines: list[PurchaseOrderLineResult]


def reorder_quantity(
    *,
    shortage: Decimal,
    avg_daily_sales: Decimal,
    lead_time_days: int,
    safety_stock: Decimal,
) -> Decimal:
    """shortage + (avg_daily_sales * lead_time_days) + safety_stock."""
    if shortage < ZERO or avg_daily_sales < ZERO or lead_time_days < 0 or safety_stock < ZERO:
        raise ValueError("reorder_quantity inputs must all be non-negative")
    return shortage + (avg_daily_sales * Decimal(lead_time_days)) + safety_stock


async def score_suppliers(session: AsyncSession, *, product_id: uuid.UUID) -> list[SupplierScore]:
    """Rank every active supplier of a product, best first. Empty list
    means no supplier is configured for this product at all.
    """
    rows = (
        await session.execute(
            select(ProductSupplier, Supplier)
            .join(Supplier, Supplier.id == ProductSupplier.supplier_id)
            .where(ProductSupplier.product_id == product_id, Supplier.is_active)
        )
    ).all()
    if not rows:
        return []

    prices = [Decimal(ps.unit_price) for ps, _ in rows]
    lead_times = [ps.lead_time_days for ps, _ in rows]
    min_price = min(prices)
    min_lead_time = min(lead_times)

    scores: list[SupplierScore] = []
    for product_supplier, supplier in rows:
        unit_price = Decimal(product_supplier.unit_price)
        lead_time_days = product_supplier.lead_time_days
        reliability = Decimal(supplier.reliability_score)

        price_score = (Decimal(100) * min_price / unit_price) if unit_price > ZERO else Decimal(100)
        price_score = min(price_score, Decimal(100))
        lead_time_score = (
            (Decimal(100) * Decimal(min_lead_time) / Decimal(lead_time_days))
            if lead_time_days > 0
            else Decimal(100)
        )
        lead_time_score = min(lead_time_score, Decimal(100))
        reliability_component = reliability  # already 0-100
        preferred_bonus = _PREFERRED_BONUS if product_supplier.is_preferred else ZERO

        total_score = (
            _PRICE_WEIGHT * price_score
            + _LEAD_TIME_WEIGHT * lead_time_score
            + _RELIABILITY_WEIGHT * reliability_component
            + preferred_bonus
        )
        total_score = min(total_score, Decimal(100))

        reasoning = [
            f"Price Rs.{unit_price} (cheapest available is Rs.{min_price})"
            + (" - cheapest" if unit_price == min_price else ""),
            f"Lead time {lead_time_days} day(s) (fastest available is {min_lead_time})"
            + (" - fastest" if lead_time_days == min_lead_time else ""),
            f"Reliability score {reliability}/100",
        ]
        if product_supplier.is_preferred:
            reasoning.append("Marked as preferred supplier for this product (+5 bonus)")

        scores.append(
            SupplierScore(
                supplier_id=supplier.id,
                supplier_name=supplier.name,
                unit_price=unit_price,
                lead_time_days=lead_time_days,
                reliability_score=reliability,
                is_preferred=product_supplier.is_preferred,
                price_score=price_score,
                lead_time_score=lead_time_score,
                reliability_component=reliability_component,
                preferred_bonus=preferred_bonus,
                total_score=total_score,
                reasoning=reasoning,
            )
        )

    scores.sort(key=lambda s: s.total_score, reverse=True)
    return scores


async def _shortage_line(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    shortage_qty: Decimal,
    snapshot: inventory.InventorySnapshot,
    lookback_days: int,
) -> ShortageLine:
    daily_sales = await inventory.average_daily_issued(
        session, product_id=product_id, warehouse_id=warehouse_id, lookback_days=lookback_days
    )
    suppliers = await score_suppliers(session, product_id=product_id)
    lead_time_days = suppliers[0].lead_time_days if suppliers else 0
    recommended_qty = reorder_quantity(
        shortage=shortage_qty,
        avg_daily_sales=daily_sales,
        lead_time_days=lead_time_days,
        safety_stock=snapshot.safety_stock,
    )
    return ShortageLine(
        product_id=product_id,
        warehouse_id=warehouse_id,
        on_hand=snapshot.on_hand,
        reserved=snapshot.reserved,
        available=snapshot.available,
        reorder_level=snapshot.reorder_level,
        safety_stock=snapshot.safety_stock,
        shortage_qty=shortage_qty,
        recommended_qty=recommended_qty,
    )


async def detect_shortages(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    lookback_days: int = 30,
) -> list[ShortageLine]:
    """Proactive: every product at this warehouse currently below its
    reorder point, each with a recommended reorder quantity attached.
    """
    below_reorder = await inventory.list_below_reorder_level(
        session, org_id=org_id, warehouse_id=warehouse_id
    )
    lines: list[ShortageLine] = []
    for snapshot in below_reorder:
        shortage_qty = snapshot.reorder_level - snapshot.available
        lines.append(
            await _shortage_line(
                session,
                product_id=snapshot.product_id,
                warehouse_id=warehouse_id,
                shortage_qty=shortage_qty,
                snapshot=snapshot,
                lookback_days=lookback_days,
            )
        )
    return lines


async def detect_shortage_from_sales_order(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    sales_order_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    lookback_days: int = 30,
) -> list[ShortageLine]:
    """Reactive: this specific order's unfulfilled lines — the "customer
    ordered 40, stock was 18, shortage is 22" case. `warehouse_id` must be
    supplied by the caller: sales_orders/sales_order_items don't persist
    which warehouse a reservation was attempted against (see
    services/sales.py), so this can't be derived from the order alone.
    """
    sales_order = await session.get(SalesOrder, sales_order_id)
    if sales_order is None or sales_order.org_id != org_id:
        raise NotFoundError(f"sales order {sales_order_id} not found")
    if sales_order.status == SalesOrderStatus.DRAFT:
        raise ConflictError(
            f"sales order {sales_order_id} is an unconfirmed quote — nothing has been "
            "reserved yet, so it has no committed shortage to act on"
        )

    items = (
        (
            await session.execute(
                select(SalesOrderItem).where(SalesOrderItem.sales_order_id == sales_order.id)
            )
        )
        .scalars()
        .all()
    )

    lines: list[ShortageLine] = []
    for item in items:
        shortage_qty = Decimal(item.quantity) - Decimal(item.reserved_qty)
        if shortage_qty <= ZERO:
            continue
        snapshot = await inventory.get_snapshot(
            session, product_id=item.product_id, warehouse_id=warehouse_id
        )
        if snapshot is None:
            snapshot = inventory.InventorySnapshot(
                product_id=item.product_id,
                warehouse_id=warehouse_id,
                on_hand=ZERO,
                reserved=ZERO,
                available=ZERO,
                reorder_level=ZERO,
                safety_stock=ZERO,
            )
        lines.append(
            await _shortage_line(
                session,
                product_id=item.product_id,
                warehouse_id=warehouse_id,
                shortage_qty=shortage_qty,
                snapshot=snapshot,
                lookback_days=lookback_days,
            )
        )
    return lines


async def create_requisition(
    session: AsyncSession, *, org_id: uuid.UUID, request: CreateRequisitionRequest
) -> CreateRequisitionResult:
    if not request.lines:
        raise ValidationError("requisition requires at least one line")

    product_ids = {line.product_id for line in request.lines}
    products = (
        (
            await session.execute(
                select(Product).where(Product.id.in_(product_ids), Product.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    found_ids = {p.id for p in products}
    missing = product_ids - found_ids
    if missing:
        raise NotFoundError(f"product(s) not found: {', '.join(str(m) for m in missing)}")

    if request.triggered_by_sales_order_id is not None:
        sales_order = await session.get(SalesOrder, request.triggered_by_sales_order_id)
        if sales_order is None or sales_order.org_id != org_id:
            raise NotFoundError(f"sales order {request.triggered_by_sales_order_id} not found")

    requisition_number = await next_document_number(
        session, org_id=org_id, doc_type="purchase_requisition"
    )
    requisition = PurchaseRequisition(
        org_id=org_id,
        requisition_number=requisition_number,
        status=PurchaseRequisitionStatus.PENDING_APPROVAL,
        triggered_by_sales_order_id=request.triggered_by_sales_order_id,
        notes=request.notes,
    )
    session.add(requisition)
    await session.flush()

    for line in request.lines:
        session.add(
            PurchaseRequisitionItem(
                purchase_requisition_id=requisition.id,
                product_id=line.product_id,
                quantity=line.quantity,
                reason=line.reason,
            )
        )
    await session.flush()

    return CreateRequisitionResult(
        purchase_requisition_id=requisition.id,
        requisition_number=requisition_number,
        status=requisition.status,
    )


async def create_purchase_orders_from_requisition(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    purchase_requisition_id: uuid.UUID,
    order_date: date | None = None,
) -> list[CreatePurchaseOrderResult]:
    """Auto-selects the best-scored supplier per line (score_suppliers()),
    groups lines by that supplier (one PO per supplier — a PO has exactly
    one supplier_id), rounds each line's quantity up to that supplier's
    MOQ, and prices/persists one PurchaseOrder per group. Marks the
    requisition CONVERTED once every line has a PO.
    """
    requisition = await session.get(PurchaseRequisition, purchase_requisition_id)
    if requisition is None or requisition.org_id != org_id:
        raise NotFoundError(f"purchase requisition {purchase_requisition_id} not found")
    if requisition.status in (
        PurchaseRequisitionStatus.CONVERTED,
        PurchaseRequisitionStatus.CANCELLED,
        PurchaseRequisitionStatus.REJECTED,
    ):
        raise ConflictError(
            f"purchase requisition {purchase_requisition_id} is {requisition.status} "
            "and cannot be converted to a purchase order"
        )

    items = (
        (
            await session.execute(
                select(PurchaseRequisitionItem).where(
                    PurchaseRequisitionItem.purchase_requisition_id == requisition.id
                )
            )
        )
        .scalars()
        .all()
    )
    if not items:
        raise ConflictError(f"purchase requisition {purchase_requisition_id} has no lines")

    organization = await session.get(Organization, org_id)
    if organization is None:
        raise NotFoundError(f"organization {org_id} not found")  # pragma: no cover - FK-guaranteed

    groups: dict[uuid.UUID, list[tuple[PurchaseRequisitionItem, SupplierScore]]] = {}
    for item in items:
        candidates = await score_suppliers(session, product_id=item.product_id)
        if not candidates:
            raise ConflictError(
                f"no supplier configured for product {item.product_id} — cannot auto-select"
            )
        best = candidates[0]
        groups.setdefault(best.supplier_id, []).append((item, best))

    order_date = order_date or date.today()
    results: list[CreatePurchaseOrderResult] = []

    for supplier_id, grouped_items in sorted(groups.items(), key=lambda g: str(g[0])):
        supplier = await session.get(Supplier, supplier_id)
        assert supplier is not None  # just fetched via score_suppliers's own join

        product_supplier_rows = (
            (
                await session.execute(
                    select(ProductSupplier).where(
                        ProductSupplier.supplier_id == supplier_id,
                        ProductSupplier.product_id.in_(
                            [item.product_id for item, _ in grouped_items]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        product_supplier_by_product = {ps.product_id: ps for ps in product_supplier_rows}

        products_by_id = {
            product.id: product
            for product in (
                (
                    await session.execute(
                        select(Product).where(
                            Product.id.in_([item.product_id for item, _ in grouped_items])
                        )
                    )
                )
                .scalars()
                .all()
            )
        }

        pricing_lines: list[PricingLineInput] = []
        ordered_qty_by_product: dict[uuid.UUID, Decimal] = {}
        for item, _score in grouped_items:
            product_supplier = product_supplier_by_product[item.product_id]
            product = products_by_id[item.product_id]
            moq = Decimal(product_supplier.moq)
            requisitioned_qty = Decimal(item.quantity)
            ordered_qty = (
                Decimal(math.ceil(requisitioned_qty / moq)) * moq
                if moq > ZERO
                else requisitioned_qty
            )
            ordered_qty_by_product[item.product_id] = ordered_qty
            pricing_lines.append(
                PricingLineInput(
                    product_id=item.product_id,
                    hsn_code=product.hsn_code,
                    uom=product.uom,
                    quantity=ordered_qty,
                    unit_price=Decimal(product_supplier.unit_price),
                    gst_rate=Decimal(product.gst_rate),
                )
            )

        priced = price_order(
            origin_state_code=supplier.state_code or "",
            place_of_supply_state_code=organization.state_code or supplier.state_code or "",
            lines=pricing_lines,
        )

        po_number = await next_document_number(session, org_id=org_id, doc_type="purchase_order")
        purchase_order = PurchaseOrder(
            org_id=org_id,
            supplier_id=supplier_id,
            purchase_requisition_id=requisition.id,
            po_number=po_number,
            order_date=order_date,
            status=PurchaseOrderStatus.DRAFT,
            subtotal=priced.subtotal,
            tax_total=priced.tax_total,
            total=priced.grand_total,
        )
        session.add(purchase_order)
        await session.flush()

        line_results: list[PurchaseOrderLineResult] = []
        for priced_line in priced.lines:
            session.add(
                PurchaseOrderItem(
                    purchase_order_id=purchase_order.id,
                    product_id=priced_line.product_id,
                    quantity=priced_line.quantity,
                    unit_price=priced_line.unit_price,
                    gst_rate=priced_line.gst_rate,
                    line_subtotal=priced_line.taxable_value,
                    line_tax=priced_line.tax_amount,
                    line_total=priced_line.line_total,
                )
            )
            requisitioned_qty = next(
                Decimal(item.quantity)
                for item, _score in grouped_items
                if item.product_id == priced_line.product_id
            )
            line_results.append(
                PurchaseOrderLineResult(
                    product_id=priced_line.product_id,
                    requisitioned_qty=requisitioned_qty,
                    ordered_qty=priced_line.quantity,
                    unit_price=priced_line.unit_price,
                    gst_rate=priced_line.gst_rate,
                    line_subtotal=priced_line.taxable_value,
                    line_tax=priced_line.tax_amount,
                    line_total=priced_line.line_total,
                )
            )

        results.append(
            CreatePurchaseOrderResult(
                purchase_order_id=purchase_order.id,
                po_number=po_number,
                supplier_id=supplier_id,
                supplier_name=supplier.name,
                subtotal=priced.subtotal,
                tax_total=priced.tax_total,
                total=priced.grand_total,
                lines=line_results,
            )
        )

    requisition.status = PurchaseRequisitionStatus.CONVERTED
    await session.flush()

    return results
