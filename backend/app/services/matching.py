"""The three-way match: Purchase Order vs Goods Receipt vs Supplier Invoice.

Split into a pure evaluator (`evaluate_three_way_match()` — no DB access,
table-driven-testable, mirrors the pricing.py/procurement.py pattern of
keeping the actual business math free of I/O) and a thin async wrapper
(`match_three_way()`) that loads the three documents, builds line inputs,
calls the evaluator, and persists one `ThreeWayMatchResult` row.

**Why matching is keyed on a specific (PO, GRN, invoice) triple, not just
an invoice**: `three_way_match_results` has all three FKs as NOT NULL
(see `app/db/models/purchase.py`), which is the schema's own statement
that one match run compares one goods receipt against one PO and one
invoice — not "every receipt ever made against this PO". A PO received in
multiple partial GRNs gets one match run per (GRN, invoice) pair; nothing
here aggregates across GRNs.

**Tolerance config** (roadmap 2.9, overridable per call):
  qty   +/-2%   (invoiced quantity vs the goods receipt's accepted_quantity
                 — accepted, not received: rejected/damaged units were never
                 really delivered, so billing for them is exactly the kind
                 of variance this exists to catch)
  price +/-1%   (invoice unit price vs the PO's negotiated unit price)
  amount Rs.100 (invoice line total vs accepted_qty * PO unit price —
                 catches compounding rounding/calc errors qty and price
                 tolerances alone might each pass)

**Verdict** (`MatchVerdict`): BLOCK is forced, independent of risk_score,
whenever a line is structurally unmatched (invoiced but never on the PO,
invoiced but never received, or received but never invoiced) or a
duplicate invoice is suspected — these aren't "risky", they're documents
that don't actually agree with each other and a score-based threshold
could paper over that. Otherwise: risk_score >= 60 -> BLOCK, >= 20 or any
line outside tolerance -> REVIEW, else AUTO_APPROVE.

**Risk score** (0-100, explainable — every point traces to a reason code,
same "show your work" convention as procurement.py's SupplierScore):
  structural line mismatch     +20   (a line that's on only 1-2 of the 3 docs)
  qty variance beyond tolerance +15
  price variance beyond tolerance +15
  price spike (>3x the price tolerance — a severe subset of the above) +15
  amount variance beyond tolerance +10
  duplicate invoice suspected  +25   (same supplier, ~same amount, close date)
  new supplier                 +10   (this PO is the supplier's first ever)
  round-number total           +5    (total is an exact multiple of Rs.1000
                                       — a classic fabricated-invoice smell)
  weekend submission            +5   (invoice_date falls on Sat/Sun)
  capped at 100.

  **"bank-detail change" is deliberately NOT scored** — the roadmap lists
  it as a risk factor, but `supplier_invoices` has no bank-account/IFSC
  columns to compare against `suppliers.bank_account_number/bank_ifsc`
  (nothing on an invoice ever recorded what account it asked to be paid
  to). Same situation procurement.py documented for "last price change":
  the fact isn't derivable from the Phase 1 schema, not silently dropped.
  A `supplier_invoice_extraction` table (Phase 4, AI document intelligence
  extracts payment details from the PDF) is the natural place to add it.

Like every other services/ module, `match_three_way()` never commits —
the caller owns the transaction.
"""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.catalog import Product
from app.db.models.enums import MatchVerdict, SupplierInvoiceStatus
from app.db.models.purchase import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    SupplierInvoice,
    SupplierInvoiceItem,
    ThreeWayMatchResult,
)

ZERO = Decimal("0")

# ---- reason codes (also doubles as the risk-score explanation) ----------
PO_LINE_MISSING = "PO_LINE_MISSING"
GRN_LINE_MISSING = "GRN_LINE_MISSING"
INVOICE_LINE_MISSING = "INVOICE_LINE_MISSING"
UNMATCHED_INVOICE_LINE = "UNMATCHED_INVOICE_LINE"
QTY_VARIANCE = "QTY_VARIANCE"
PRICE_VARIANCE = "PRICE_VARIANCE"
PRICE_SPIKE = "PRICE_SPIKE"
AMOUNT_VARIANCE = "AMOUNT_VARIANCE"
DUPLICATE_INVOICE_SUSPECTED = "DUPLICATE_INVOICE_SUSPECTED"
NEW_SUPPLIER = "NEW_SUPPLIER"
ROUND_NUMBER_AMOUNT = "ROUND_NUMBER_AMOUNT"
WEEKEND_SUBMISSION = "WEEKEND_SUBMISSION"

# ---- risk-score point values (tunable, must sum to >100 headroom so a
# capped 100 is reachable without every factor firing) -------------------
_RISK_STRUCTURAL_MISMATCH = 20
_RISK_QTY_VARIANCE = 15
_RISK_PRICE_VARIANCE = 15
_RISK_PRICE_SPIKE = 15
_RISK_AMOUNT_VARIANCE = 10
_RISK_DUPLICATE_INVOICE = 25
_RISK_NEW_SUPPLIER = 10
_RISK_ROUND_NUMBER_AMOUNT = 5
_RISK_WEEKEND_SUBMISSION = 5
_RISK_CAP = 100

# A price deviation past this multiple of the price tolerance counts as a
# "spike" (severe), not just an ordinary out-of-tolerance variance.
_PRICE_SPIKE_MULTIPLIER = Decimal("3")

_BLOCK_RISK_THRESHOLD = 60
_REVIEW_RISK_THRESHOLD = 20
_ROUND_NUMBER_STEP = Decimal("1000")


class MatchTolerances(BaseModel):
    qty_variance_pct: Decimal = Decimal("2")
    price_variance_pct: Decimal = Decimal("1")
    amount_variance_abs: Decimal = Decimal("100")


DEFAULT_TOLERANCES = MatchTolerances()


class MatchLineInput(BaseModel):
    product_id: uuid.UUID
    product_sku: str
    po_quantity: Decimal | None = None
    po_unit_price: Decimal | None = None
    grn_accepted_quantity: Decimal | None = None
    invoice_quantity: Decimal | None = None
    invoice_unit_price: Decimal | None = None
    invoice_line_total: Decimal | None = None


class MatchLineResult(BaseModel):
    product_id: uuid.UUID
    product_sku: str
    qty_variance: Decimal
    price_variance: Decimal
    amount_variance: Decimal
    within_tolerance: bool
    reason_codes: list[str]


class ThreeWayMatchOutcome(BaseModel):
    verdict: str
    risk_score: int
    qty_variance: Decimal
    price_variance: Decimal
    amount_variance: Decimal
    reason_codes: list[str]
    lines: list[MatchLineResult]


class RunThreeWayMatchResult(BaseModel):
    three_way_match_result_id: uuid.UUID
    outcome: ThreeWayMatchOutcome


def _pct_variance(actual: Decimal, expected: Decimal) -> Decimal | None:
    """Percent deviation of `actual` from `expected`. None when `expected`
    is zero and `actual` is also zero (nothing to compare — not a variance).
    """
    if expected == ZERO:
        return None if actual == ZERO else Decimal("100")
    return (abs(actual - expected) / expected) * Decimal("100")


def _is_round_number_amount(total: Decimal) -> bool:
    return total > ZERO and total % _ROUND_NUMBER_STEP == ZERO


def _is_weekend(submitted: date) -> bool:
    return submitted.weekday() >= 5  # Saturday=5, Sunday=6


def _evaluate_line(line: MatchLineInput, tolerances: MatchTolerances) -> MatchLineResult:
    reason_codes: list[str] = []

    po_qty = line.po_quantity
    po_price = line.po_unit_price
    grn_qty = line.grn_accepted_quantity
    inv_qty = line.invoice_quantity
    inv_price = line.invoice_unit_price
    inv_total = line.invoice_line_total

    po_present = po_qty is not None and po_price is not None
    grn_present = grn_qty is not None
    invoice_present = inv_qty is not None

    if not po_present:
        reason_codes.append(f"{PO_LINE_MISSING}: {line.product_sku} invoiced but not on the PO")
    if grn_qty is None:
        reason_codes.append(f"{GRN_LINE_MISSING}: {line.product_sku} invoiced but never received")
        grn_qty = ZERO
    if inv_qty is None:
        reason_codes.append(
            f"{INVOICE_LINE_MISSING}: {line.product_sku} received but never invoiced"
        )
        inv_qty = ZERO
        inv_price = None
        inv_total = None

    qty_variance = inv_qty - grn_qty
    qty_variance_pct = _pct_variance(inv_qty, grn_qty)
    qty_within = qty_variance_pct is None or qty_variance_pct <= tolerances.qty_variance_pct
    if not qty_within:
        reason_codes.append(
            f"{QTY_VARIANCE}: {line.product_sku} invoiced {inv_qty} vs accepted {grn_qty} "
            f"({qty_variance_pct:.1f}%, tolerance {tolerances.qty_variance_pct}%)"
        )

    price_variance_pct: Decimal | None = None
    price_variance = ZERO
    price_within = True
    if po_price is not None and inv_price is not None:
        price_variance = inv_price - po_price
        price_variance_pct = _pct_variance(inv_price, po_price)
        price_within = (
            price_variance_pct is None or price_variance_pct <= tolerances.price_variance_pct
        )
        if not price_within:
            reason_codes.append(
                f"{PRICE_VARIANCE}: {line.product_sku} invoiced Rs.{inv_price} vs PO Rs.{po_price} "
                f"({price_variance_pct:.1f}%, tolerance {tolerances.price_variance_pct}%)"
            )
            if price_variance_pct is not None and price_variance_pct > (
                tolerances.price_variance_pct * _PRICE_SPIKE_MULTIPLIER
            ):
                reason_codes.append(
                    f"{PRICE_SPIKE}: {line.product_sku} price deviation "
                    f"{price_variance_pct:.1f}% exceeds {_PRICE_SPIKE_MULTIPLIER}x tolerance"
                )

    expected_amount = grn_qty * (po_price or ZERO)
    actual_amount = inv_total or ZERO
    amount_variance = actual_amount - expected_amount
    amount_within = abs(amount_variance) <= tolerances.amount_variance_abs
    if not amount_within:
        reason_codes.append(
            f"{AMOUNT_VARIANCE}: {line.product_sku} invoiced Rs.{actual_amount} vs expected "
            f"Rs.{expected_amount} (variance Rs.{amount_variance}, "
            f"tolerance Rs.{tolerances.amount_variance_abs})"
        )

    structurally_ok = po_present and grn_present and invoice_present
    within_tolerance = qty_within and price_within and amount_within and structurally_ok

    return MatchLineResult(
        product_id=line.product_id,
        product_sku=line.product_sku,
        qty_variance=qty_variance,
        price_variance=price_variance,
        amount_variance=amount_variance,
        within_tolerance=within_tolerance,
        reason_codes=reason_codes,
    )


def evaluate_three_way_match(
    *,
    lines: list[MatchLineInput],
    tolerances: MatchTolerances = DEFAULT_TOLERANCES,
    unmatched_invoice_line_count: int = 0,
    is_new_supplier: bool = False,
    duplicate_invoice_found: bool = False,
    invoice_total: Decimal,
    invoice_date: date,
) -> ThreeWayMatchOutcome:
    """Pure function — no DB access. `unmatched_invoice_line_count` counts
    supplier_invoice_items with product_id=None (can't be product-matched
    at all); each one is a structural mismatch on its own.
    """
    line_results = [_evaluate_line(line, tolerances) for line in lines]

    qty_variance_total = sum((r.qty_variance for r in line_results), ZERO)
    amount_variance_total = sum((r.amount_variance for r in line_results), ZERO)
    price_variance_worst = max((r.price_variance for r in line_results), key=abs, default=ZERO)

    reason_codes: list[str] = []
    risk_score = 0

    structural_issue = unmatched_invoice_line_count > 0 or any(
        code.startswith((PO_LINE_MISSING, GRN_LINE_MISSING, INVOICE_LINE_MISSING))
        for line in line_results
        for code in line.reason_codes
    )
    if unmatched_invoice_line_count > 0:
        reason_codes.append(
            f"{UNMATCHED_INVOICE_LINE}: {unmatched_invoice_line_count} invoice line(s) "
            "have no product reference and cannot be matched"
        )
    for line in line_results:
        reason_codes.extend(line.reason_codes)
    if structural_issue:
        risk_score += _RISK_STRUCTURAL_MISMATCH

    if any(QTY_VARIANCE in code for line in line_results for code in line.reason_codes):
        risk_score += _RISK_QTY_VARIANCE
    if any(code.startswith(PRICE_VARIANCE) for line in line_results for code in line.reason_codes):
        risk_score += _RISK_PRICE_VARIANCE
    if any(PRICE_SPIKE in code for line in line_results for code in line.reason_codes):
        risk_score += _RISK_PRICE_SPIKE
    if any(AMOUNT_VARIANCE in code for line in line_results for code in line.reason_codes):
        risk_score += _RISK_AMOUNT_VARIANCE

    if duplicate_invoice_found:
        reason_codes.append(
            f"{DUPLICATE_INVOICE_SUSPECTED}: another invoice from this supplier "
            "with a similar amount and close date already exists"
        )
        risk_score += _RISK_DUPLICATE_INVOICE
    if is_new_supplier:
        reason_codes.append(f"{NEW_SUPPLIER}: this is the supplier's first purchase order")
        risk_score += _RISK_NEW_SUPPLIER
    if _is_round_number_amount(invoice_total):
        reason_codes.append(
            f"{ROUND_NUMBER_AMOUNT}: total Rs.{invoice_total} is an exact multiple of Rs.1000"
        )
        risk_score += _RISK_ROUND_NUMBER_AMOUNT
    if _is_weekend(invoice_date):
        reason_codes.append(
            f"{WEEKEND_SUBMISSION}: invoice dated {invoice_date} falls on a weekend"
        )
        risk_score += _RISK_WEEKEND_SUBMISSION

    risk_score = min(risk_score, _RISK_CAP)

    any_line_out_of_tolerance = any(not r.within_tolerance for r in line_results)

    if structural_issue or duplicate_invoice_found or risk_score >= _BLOCK_RISK_THRESHOLD:
        verdict = MatchVerdict.BLOCK
    elif risk_score >= _REVIEW_RISK_THRESHOLD or any_line_out_of_tolerance:
        verdict = MatchVerdict.REVIEW
    else:
        verdict = MatchVerdict.AUTO_APPROVE

    return ThreeWayMatchOutcome(
        verdict=verdict,
        risk_score=risk_score,
        qty_variance=qty_variance_total,
        price_variance=price_variance_worst,
        amount_variance=amount_variance_total,
        reason_codes=reason_codes,
        lines=line_results,
    )


_INVOICE_STATUS_BY_VERDICT = {
    MatchVerdict.AUTO_APPROVE: SupplierInvoiceStatus.MATCHED,
    MatchVerdict.BLOCK: SupplierInvoiceStatus.BLOCKED,
    # REVIEW: left as-is (SupplierInvoiceStatus has no dedicated "in
    # review" value) — a human needs to look at it before it moves.
}


async def match_three_way(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    purchase_order_id: uuid.UUID,
    goods_receipt_id: uuid.UUID,
    supplier_invoice_id: uuid.UUID,
    tolerances: MatchTolerances = DEFAULT_TOLERANCES,
) -> RunThreeWayMatchResult:
    purchase_order = await session.get(PurchaseOrder, purchase_order_id)
    if purchase_order is None or purchase_order.org_id != org_id:
        raise NotFoundError(f"purchase order {purchase_order_id} not found")

    goods_receipt = await session.get(GoodsReceipt, goods_receipt_id)
    if goods_receipt is None or goods_receipt.org_id != org_id:
        raise NotFoundError(f"goods receipt {goods_receipt_id} not found")
    if goods_receipt.purchase_order_id != purchase_order_id:
        raise ConflictError(
            f"goods receipt {goods_receipt_id} was not raised against purchase order "
            f"{purchase_order_id}"
        )

    supplier_invoice = await session.get(SupplierInvoice, supplier_invoice_id)
    if supplier_invoice is None or supplier_invoice.org_id != org_id:
        raise NotFoundError(f"supplier invoice {supplier_invoice_id} not found")
    if (
        supplier_invoice.purchase_order_id is not None
        and supplier_invoice.purchase_order_id != purchase_order_id
    ):
        raise ConflictError(
            f"supplier invoice {supplier_invoice_id} references a different purchase order"
        )
    if supplier_invoice.supplier_id != purchase_order.supplier_id:
        raise ConflictError(
            f"supplier invoice {supplier_invoice_id} supplier does not match "
            f"purchase order {purchase_order_id} supplier"
        )

    po_items = (
        (
            await session.execute(
                select(PurchaseOrderItem).where(
                    PurchaseOrderItem.purchase_order_id == purchase_order_id
                )
            )
        )
        .scalars()
        .all()
    )
    grn_items = (
        (
            await session.execute(
                select(GoodsReceiptItem).where(
                    GoodsReceiptItem.goods_receipt_id == goods_receipt_id
                )
            )
        )
        .scalars()
        .all()
    )
    invoice_items = (
        (
            await session.execute(
                select(SupplierInvoiceItem).where(
                    SupplierInvoiceItem.supplier_invoice_id == supplier_invoice_id
                )
            )
        )
        .scalars()
        .all()
    )

    unmatched_invoice_line_count = sum(1 for item in invoice_items if item.product_id is None)

    po_by_product = {item.product_id: item for item in po_items}
    grn_by_product = {item.product_id: item for item in grn_items}
    invoice_by_product: dict[uuid.UUID, SupplierInvoiceItem] = {}
    for item in invoice_items:
        if item.product_id is not None:
            invoice_by_product[item.product_id] = item

    product_ids = set(po_by_product) | set(grn_by_product) | set(invoice_by_product)
    products = (
        (await session.execute(select(Product).where(Product.id.in_(product_ids)))).scalars().all()
        if product_ids
        else []
    )
    sku_by_product = {p.id: p.sku for p in products}

    match_lines: list[MatchLineInput] = []
    for product_id in product_ids:
        po_item = po_by_product.get(product_id)
        grn_item = grn_by_product.get(product_id)
        invoice_item = invoice_by_product.get(product_id)
        match_lines.append(
            MatchLineInput(
                product_id=product_id,
                product_sku=sku_by_product.get(product_id, str(product_id)),
                po_quantity=Decimal(po_item.quantity) if po_item else None,
                po_unit_price=Decimal(po_item.unit_price) if po_item else None,
                grn_accepted_quantity=(Decimal(grn_item.accepted_quantity) if grn_item else None),
                invoice_quantity=Decimal(invoice_item.quantity) if invoice_item else None,
                invoice_unit_price=(Decimal(invoice_item.unit_price) if invoice_item else None),
                invoice_line_total=(Decimal(invoice_item.line_total) if invoice_item else None),
            )
        )

    prior_po_count = (
        await session.execute(
            select(PurchaseOrder.id).where(
                PurchaseOrder.supplier_id == purchase_order.supplier_id,
                PurchaseOrder.id != purchase_order.id,
            )
        )
    ).first()
    is_new_supplier = prior_po_count is None

    invoice_total = Decimal(supplier_invoice.total)
    duplicate_rows = (
        await session.execute(
            select(SupplierInvoice.id).where(
                SupplierInvoice.supplier_id == supplier_invoice.supplier_id,
                SupplierInvoice.id != supplier_invoice.id,
                SupplierInvoice.total == supplier_invoice.total,
            )
        )
    ).first()
    duplicate_invoice_found = duplicate_rows is not None

    outcome = evaluate_three_way_match(
        lines=match_lines,
        tolerances=tolerances,
        unmatched_invoice_line_count=unmatched_invoice_line_count,
        is_new_supplier=is_new_supplier,
        duplicate_invoice_found=duplicate_invoice_found,
        invoice_total=invoice_total,
        invoice_date=supplier_invoice.invoice_date,
    )

    match_result = ThreeWayMatchResult(
        org_id=org_id,
        purchase_order_id=purchase_order_id,
        goods_receipt_id=goods_receipt_id,
        supplier_invoice_id=supplier_invoice_id,
        qty_variance=outcome.qty_variance,
        price_variance=outcome.price_variance,
        amount_variance=outcome.amount_variance,
        risk_score=outcome.risk_score,
        verdict=outcome.verdict,
        reason_codes=outcome.reason_codes,
    )
    session.add(match_result)

    new_status = _INVOICE_STATUS_BY_VERDICT.get(MatchVerdict(outcome.verdict))
    if new_status is not None:
        supplier_invoice.status = new_status

    await session.flush()

    return RunThreeWayMatchResult(three_way_match_result_id=match_result.id, outcome=outcome)
