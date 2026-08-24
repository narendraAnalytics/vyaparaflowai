"""Goods-receipt and supplier-invoice intake HTTP surface: wraps
services/receiving.py. Writes gated by goods_receipt.create (Warehouse) /
supplier_invoice.create (Accounts) — same per-entity ".create"/".manage"
convention as every other business-logic router.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_perm
from app.core.exceptions import ConflictError
from app.db.models.org import User
from app.db.session import get_db
from app.services.receiving import (
    CreateGoodsReceiptRequest,
    CreateGoodsReceiptResult,
    CreateSupplierInvoiceRequest,
    CreateSupplierInvoiceResult,
    create_goods_receipt,
    create_supplier_invoice,
)

router = APIRouter(tags=["receiving"])


@router.post(
    "/goods-receipts",
    response_model=CreateGoodsReceiptResult,
    status_code=201,
    operation_id="createGoodsReceipt",
    summary="Record a goods receipt against a purchase order",
    description=(
        "Records what actually arrived against a PO's lines (received/accepted/rejected/"
        "damaged quantities), increases on-hand stock for the accepted quantities via "
        "services/inventory.py, and advances the PO to partially_received or received. "
        "Over-receipt is rejected against each line's own outstanding quantity (ordered "
        "minus already-accepted across every prior GRN), not just this GRN's own numbers."
    ),
)
async def create_goods_receipt_endpoint(
    payload: CreateGoodsReceiptRequest,
    user: User = Depends(require_perm("goods_receipt.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateGoodsReceiptResult:
    try:
        result = await create_goods_receipt(db, org_id=user.org_id, request=payload)
    except IntegrityError as exc:
        raise ConflictError(
            "goods receipt violates a database constraint (check quantities)"
        ) from exc
    await db.commit()
    return result


@router.post(
    "/supplier-invoices",
    response_model=CreateSupplierInvoiceResult,
    status_code=201,
    operation_id="createSupplierInvoice",
    summary="Record an incoming supplier invoice",
    description=(
        "Captures a supplier's invoice as billed — line amounts are taken as given (this "
        "system doesn't recompute the supplier's own GST calculation), with header "
        "subtotal/tax_total derived by summing lines. Optionally linked to a purchase "
        "order (must be the same supplier). Status starts at received; run POST "
        "/matching/three-way next to move it to matched/blocked."
    ),
)
async def create_supplier_invoice_endpoint(
    payload: CreateSupplierInvoiceRequest,
    user: User = Depends(require_perm("supplier_invoice.create")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> CreateSupplierInvoiceResult:
    try:
        result = await create_supplier_invoice(db, org_id=user.org_id, request=payload)
    except IntegrityError as exc:
        raise ConflictError(
            f"supplier invoice {payload.invoice_number!r} already exists for this supplier, "
            "or violates a constraint"
        ) from exc
    await db.commit()
    return result
