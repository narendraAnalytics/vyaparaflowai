import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.catalog import Product, Warehouse
from app.db.models.enums import MatchVerdict
from app.db.models.org import Organization
from app.db.models.partners import Supplier
from app.db.models.purchase import (
    GoodsReceipt,
    GoodsReceiptItem,
    PurchaseOrder,
    PurchaseOrderItem,
    SupplierInvoice,
    SupplierInvoiceItem,
    ThreeWayMatchResult,
)
from app.db.session import AsyncSessionLocal
from app.services.matching import (
    AMOUNT_VARIANCE,
    DEFAULT_TOLERANCES,
    DUPLICATE_INVOICE_SUSPECTED,
    GRN_LINE_MISSING,
    INVOICE_LINE_MISSING,
    NEW_SUPPLIER,
    PO_LINE_MISSING,
    PRICE_SPIKE,
    PRICE_VARIANCE,
    QTY_VARIANCE,
    ROUND_NUMBER_AMOUNT,
    UNMATCHED_INVOICE_LINE,
    WEEKEND_SUBMISSION,
    MatchLineInput,
    MatchTolerances,
    evaluate_three_way_match,
    match_three_way,
)

TELANGANA = "36"
# 2026-08-24 is a Monday; +5/+6 land on the following weekend.
A_WEEKDAY = date(2026, 8, 24)
A_SATURDAY = date(2026, 8, 29)


def _line(**overrides: object) -> MatchLineInput:
    defaults: dict[str, object] = dict(
        product_id=uuid.uuid4(),
        product_sku="SKU-1",
        po_quantity=Decimal("10"),
        po_unit_price=Decimal("100"),
        grn_accepted_quantity=Decimal("10"),
        invoice_quantity=Decimal("10"),
        invoice_unit_price=Decimal("100"),
        invoice_line_total=Decimal("1000"),
    )
    defaults.update(overrides)
    return MatchLineInput.model_validate(defaults)


def _evaluate(
    *lines: MatchLineInput,
    tolerances: MatchTolerances = DEFAULT_TOLERANCES,
    unmatched_invoice_line_count: int = 0,
    is_new_supplier: bool = False,
    duplicate_invoice_found: bool = False,
    invoice_total: Decimal = Decimal("1234"),  # deliberately not a round-thousand amount
    invoice_date: date = A_WEEKDAY,
):
    return evaluate_three_way_match(
        lines=list(lines),
        tolerances=tolerances,
        unmatched_invoice_line_count=unmatched_invoice_line_count,
        is_new_supplier=is_new_supplier,
        duplicate_invoice_found=duplicate_invoice_found,
        invoice_total=invoice_total,
        invoice_date=invoice_date,
    )


# ---------------------------------------------------------------------------
# Table-driven pure-function tests (no DB) — one row per variance scenario.
# ---------------------------------------------------------------------------

PERFECT_LINE = _line()

TABLE_CASES = [
    # (label, line, expect_within_tolerance, expected_reason_substring_or_None)
    ("exact match", _line(), True, None),
    (
        "qty at exactly the tolerance boundary (2%) is within",
        _line(invoice_quantity=Decimal("10.2")),
        True,
        None,
    ),
    (
        "qty just over the tolerance boundary is a variance",
        _line(invoice_quantity=Decimal("10.21")),
        False,
        QTY_VARIANCE,
    ),
    (
        "qty under accepted (short-billing) is still a variance",
        _line(invoice_quantity=Decimal("9.5")),
        False,
        QTY_VARIANCE,
    ),
    (
        "price at exactly the tolerance boundary (1%) is within",
        _line(invoice_unit_price=Decimal("101"), invoice_line_total=Decimal("1010")),
        True,
        None,
    ),
    (
        "price just over the tolerance boundary is a variance",
        _line(invoice_unit_price=Decimal("101.01"), invoice_line_total=Decimal("1010.10")),
        False,
        PRICE_VARIANCE,
    ),
    (
        "price deviation over 3x tolerance is a spike on top of the variance",
        _line(invoice_unit_price=Decimal("104"), invoice_line_total=Decimal("1040")),
        False,
        PRICE_SPIKE,
    ),
    (
        "price deviation just under the spike multiplier is a variance but not a spike",
        _line(invoice_unit_price=Decimal("102"), invoice_line_total=Decimal("1020")),
        False,
        PRICE_VARIANCE,
    ),
    (
        "amount variance at exactly Rs.100 is within tolerance",
        _line(invoice_line_total=Decimal("1100")),
        True,
        None,
    ),
    (
        "amount variance just over Rs.100 is a variance",
        _line(invoice_line_total=Decimal("1100.01")),
        False,
        AMOUNT_VARIANCE,
    ),
    (
        "PO line missing (invoiced+received but never ordered)",
        _line(po_quantity=None, po_unit_price=None),
        False,
        PO_LINE_MISSING,
    ),
    (
        "GRN line missing (invoiced but never received)",
        _line(grn_accepted_quantity=None),
        False,
        GRN_LINE_MISSING,
    ),
    (
        "invoice line missing (received but never invoiced)",
        _line(invoice_quantity=None, invoice_unit_price=None, invoice_line_total=None),
        False,
        INVOICE_LINE_MISSING,
    ),
    (
        "zero accepted, zero invoiced qty is not a variance (nothing to compare)",
        _line(
            grn_accepted_quantity=Decimal("0"),
            invoice_quantity=Decimal("0"),
            invoice_line_total=Decimal("0"),
        ),
        True,
        None,
    ),
    (
        "zero accepted but something invoiced is a full variance",
        _line(grn_accepted_quantity=Decimal("0")),
        False,
        QTY_VARIANCE,
    ),
]


@pytest.mark.parametrize(
    "label,line,expect_within,expect_reason", TABLE_CASES, ids=[c[0] for c in TABLE_CASES]
)
def test_line_variance_table(label, line, expect_within, expect_reason):
    outcome = _evaluate(line)
    [result] = outcome.lines
    assert result.within_tolerance is expect_within, label
    if expect_reason is not None:
        assert any(expect_reason in code for code in result.reason_codes), label


# ---------------------------------------------------------------------------
# Order-level verdict / risk-score tests
# ---------------------------------------------------------------------------


def test_perfect_match_auto_approves_with_zero_risk():
    outcome = _evaluate(PERFECT_LINE)
    assert outcome.verdict == MatchVerdict.AUTO_APPROVE
    assert outcome.risk_score == 0
    assert outcome.reason_codes == []


def test_qty_variance_alone_triggers_review_not_block():
    outcome = _evaluate(_line(invoice_quantity=Decimal("11")))
    assert outcome.verdict == MatchVerdict.REVIEW
    assert outcome.risk_score == 15


def test_structural_mismatch_forces_block():
    # a missing GRN line also drags in a qty variance (nothing accepted,
    # something invoiced) and an amount variance (expected amount is 0) —
    # structural(20) + qty(15) + amount(10) = 45, still under the 60
    # score-based BLOCK threshold, but BLOCK is forced anyway.
    outcome = _evaluate(_line(grn_accepted_quantity=None))
    assert outcome.verdict == MatchVerdict.BLOCK
    assert outcome.risk_score == 45


def test_duplicate_invoice_forces_block_regardless_of_score():
    outcome = _evaluate(PERFECT_LINE, duplicate_invoice_found=True)
    assert outcome.verdict == MatchVerdict.BLOCK
    assert any(DUPLICATE_INVOICE_SUSPECTED in c for c in outcome.reason_codes)


def test_unmatched_invoice_line_count_is_structural():
    outcome = _evaluate(PERFECT_LINE, unmatched_invoice_line_count=2)
    assert outcome.verdict == MatchVerdict.BLOCK
    assert any(UNMATCHED_INVOICE_LINE in c for c in outcome.reason_codes)


def test_new_supplier_alone_stays_under_review_threshold():
    outcome = _evaluate(PERFECT_LINE, is_new_supplier=True)
    assert outcome.risk_score == 10
    assert outcome.verdict == MatchVerdict.AUTO_APPROVE
    assert any(NEW_SUPPLIER in c for c in outcome.reason_codes)


def test_round_number_total_adds_risk_and_reason():
    outcome = _evaluate(PERFECT_LINE, invoice_total=Decimal("5000"))
    assert outcome.risk_score == 5
    assert any(ROUND_NUMBER_AMOUNT in c for c in outcome.reason_codes)


def test_non_round_total_does_not_add_risk():
    outcome = _evaluate(PERFECT_LINE, invoice_total=Decimal("5001"))
    assert outcome.risk_score == 0


def test_weekend_submission_adds_risk_and_reason():
    outcome = _evaluate(PERFECT_LINE, invoice_date=A_SATURDAY)
    assert outcome.risk_score == 5
    assert any(WEEKEND_SUBMISSION in c for c in outcome.reason_codes)


def test_weekday_submission_does_not_add_risk():
    outcome = _evaluate(PERFECT_LINE, invoice_date=A_WEEKDAY)
    assert outcome.risk_score == 0


def test_benign_flags_stacking_to_review_threshold():
    # new_supplier(10) + round_number(5) + weekend(5) = 20 -> hits the
    # review threshold even though every line is a perfect match.
    outcome = _evaluate(
        PERFECT_LINE,
        is_new_supplier=True,
        invoice_total=Decimal("5000"),
        invoice_date=A_SATURDAY,
    )
    assert outcome.risk_score == 20
    assert outcome.verdict == MatchVerdict.REVIEW


def test_risk_score_caps_at_100():
    # stack every single risk factor at once: structural (missing GRN),
    # qty variance, price variance + spike, amount variance, duplicate
    # invoice, new supplier, round-number total, weekend submission —
    # raw sum is 20+15+15+15+10+25+10+5+5=120, capped at 100.
    outcome = _evaluate(
        _line(
            grn_accepted_quantity=None,
            invoice_quantity=Decimal("999"),
            invoice_unit_price=Decimal("1000"),
            invoice_line_total=Decimal("999000"),
        ),
        unmatched_invoice_line_count=1,
        duplicate_invoice_found=True,
        is_new_supplier=True,
        invoice_total=Decimal("100000"),
        invoice_date=A_SATURDAY,
    )
    assert outcome.risk_score == 100
    assert outcome.verdict == MatchVerdict.BLOCK


def test_severe_price_deviation_crosses_block_threshold():
    # price variance (15) + spike (15) + amount variance (10) = 40, still
    # below the 60 block threshold on its own...
    line = _line(invoice_unit_price=Decimal("140"), invoice_line_total=Decimal("1400"))
    outcome = _evaluate(line)
    assert outcome.verdict == MatchVerdict.REVIEW
    assert outcome.risk_score == 40
    # ...but stacked with a duplicate-invoice suspicion it's an easy BLOCK.
    outcome_dup = _evaluate(line, duplicate_invoice_found=True)
    assert outcome_dup.verdict == MatchVerdict.BLOCK
    assert outcome_dup.risk_score == 65


def test_multi_line_variances_aggregate_across_lines():
    line_a = _line(product_sku="A", invoice_quantity=Decimal("11"))
    line_b = _line(
        product_sku="B", invoice_unit_price=Decimal("110"), invoice_line_total=Decimal("1100")
    )
    outcome = _evaluate(line_a, line_b)
    assert outcome.qty_variance == Decimal("1")  # only line A drifted
    assert outcome.price_variance == Decimal("10")  # only line B drifted
    assert len(outcome.lines) == 2
    assert outcome.verdict == MatchVerdict.REVIEW


def test_custom_tolerances_are_respected():
    loose = MatchTolerances(
        qty_variance_pct=Decimal("50"),
        price_variance_pct=Decimal("50"),
        amount_variance_abs=Decimal("10000"),
    )
    outcome = _evaluate(_line(invoice_quantity=Decimal("14")), tolerances=loose)
    assert outcome.verdict == MatchVerdict.AUTO_APPROVE
    assert outcome.risk_score == 0


# ---------------------------------------------------------------------------
# DB-backed integration tests for the async wrapper
# ---------------------------------------------------------------------------


@pytest.fixture
async def rig():
    async with AsyncSessionLocal() as session:
        org = Organization(name=f"test-matching-{uuid.uuid4()}", state_code=TELANGANA)
        session.add(org)
        await session.flush()

        warehouse = Warehouse(
            org_id=org.id, code=f"WH-{uuid.uuid4().hex[:8]}", name="Test Warehouse"
        )
        product = Product(
            org_id=org.id,
            sku=f"TEST-MATCH-{uuid.uuid4().hex[:8]}",
            name="Test Product",
            hsn_code="8544",
            uom="PCS",
            gst_rate=Decimal("18"),
        )
        supplier = Supplier(org_id=org.id, name="Test Supplier", state_code=TELANGANA)
        other_supplier = Supplier(org_id=org.id, name="Other Supplier", state_code=TELANGANA)
        session.add_all([warehouse, product, supplier, other_supplier])
        await session.flush()

        po = PurchaseOrder(
            org_id=org.id,
            supplier_id=supplier.id,
            po_number=f"PO-TEST-{uuid.uuid4().hex[:8]}",
            order_date=A_WEEKDAY,
            status="approved",
            subtotal=Decimal("1005.00"),
            tax_total=Decimal("0"),
            total=Decimal("1005.00"),
        )
        session.add(po)
        await session.flush()
        po_item = PurchaseOrderItem(
            purchase_order_id=po.id,
            product_id=product.id,
            quantity=Decimal("10"),
            unit_price=Decimal("100.50"),
            gst_rate=Decimal("0"),
            line_subtotal=Decimal("1005.00"),
            line_tax=Decimal("0"),
            line_total=Decimal("1005.00"),
        )
        session.add(po_item)

        grn = GoodsReceipt(
            org_id=org.id,
            purchase_order_id=po.id,
            warehouse_id=warehouse.id,
            grn_number=f"GRN-TEST-{uuid.uuid4().hex[:8]}",
            received_date=A_WEEKDAY,
        )
        session.add(grn)
        await session.flush()
        grn_item = GoodsReceiptItem(
            goods_receipt_id=grn.id,
            purchase_order_item_id=po_item.id,
            product_id=product.id,
            ordered_quantity=Decimal("10"),
            received_quantity=Decimal("10"),
            accepted_quantity=Decimal("10"),
        )
        session.add(grn_item)

        invoice = SupplierInvoice(
            org_id=org.id,
            supplier_id=supplier.id,
            purchase_order_id=po.id,
            invoice_number=f"INV-TEST-{uuid.uuid4().hex[:8]}",
            invoice_date=A_WEEKDAY,
            due_date=A_WEEKDAY,
            status="received",
            subtotal=Decimal("1005.00"),
            tax_total=Decimal("0"),
            total=Decimal("1005.00"),
        )
        session.add(invoice)
        await session.flush()
        invoice_item = SupplierInvoiceItem(
            supplier_invoice_id=invoice.id,
            product_id=product.id,
            quantity=Decimal("10"),
            unit_price=Decimal("100.50"),
            gst_rate=Decimal("0"),
            line_total=Decimal("1005.00"),
        )
        session.add(invoice_item)
        await session.commit()

        ids = {
            "org_id": org.id,
            "warehouse_id": warehouse.id,
            "product_id": product.id,
            "supplier_id": supplier.id,
            "other_supplier_id": other_supplier.id,
            "po_id": po.id,
            "grn_id": grn.id,
            "invoice_id": invoice.id,
        }

    yield ids

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ThreeWayMatchResult).where(ThreeWayMatchResult.org_id == ids["org_id"])
        )
        await session.execute(
            delete(SupplierInvoiceItem).where(
                SupplierInvoiceItem.supplier_invoice_id == ids["invoice_id"]
            )
        )
        await session.execute(
            delete(SupplierInvoice).where(SupplierInvoice.id == ids["invoice_id"])
        )
        await session.execute(
            delete(GoodsReceiptItem).where(GoodsReceiptItem.goods_receipt_id == ids["grn_id"])
        )
        await session.execute(delete(GoodsReceipt).where(GoodsReceipt.id == ids["grn_id"]))
        await session.execute(
            delete(PurchaseOrderItem).where(PurchaseOrderItem.purchase_order_id == ids["po_id"])
        )
        await session.execute(delete(PurchaseOrder).where(PurchaseOrder.id == ids["po_id"]))
        await session.execute(
            delete(Supplier).where(Supplier.id.in_([ids["supplier_id"], ids["other_supplier_id"]]))
        )
        await session.execute(delete(Product).where(Product.id == ids["product_id"]))
        await session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse_id"]))
        await session.execute(delete(Organization).where(Organization.id == ids["org_id"]))
        await session.commit()


@pytest.mark.asyncio
async def test_match_three_way_perfect_match_auto_approves_and_updates_status(rig):
    async with AsyncSessionLocal() as session:
        result = await match_three_way(
            session,
            org_id=rig["org_id"],
            purchase_order_id=rig["po_id"],
            goods_receipt_id=rig["grn_id"],
            supplier_invoice_id=rig["invoice_id"],
        )
        await session.commit()

    assert result.outcome.verdict == MatchVerdict.AUTO_APPROVE
    # this is the supplier's first-ever PO in this rig, so +10 new-supplier risk
    assert result.outcome.risk_score == 10

    async with AsyncSessionLocal() as session:
        row = await session.get(ThreeWayMatchResult, result.three_way_match_result_id)
        assert row is not None
        assert row.verdict == "auto_approve"

        invoice = await session.get(SupplierInvoice, rig["invoice_id"])
        assert invoice.status == "matched"


@pytest.mark.asyncio
async def test_match_three_way_qty_variance_leaves_invoice_status_untouched(rig):
    async with AsyncSessionLocal() as session:
        item = (
            await session.execute(
                select(SupplierInvoiceItem).where(
                    SupplierInvoiceItem.supplier_invoice_id == rig["invoice_id"]
                )
            )
        ).scalar_one()
        item.quantity = Decimal("11")
        item.line_total = Decimal("1105.50")  # 11 * 100.50
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await match_three_way(
            session,
            org_id=rig["org_id"],
            purchase_order_id=rig["po_id"],
            goods_receipt_id=rig["grn_id"],
            supplier_invoice_id=rig["invoice_id"],
        )
        await session.commit()

    assert result.outcome.verdict == MatchVerdict.REVIEW

    async with AsyncSessionLocal() as session:
        invoice = await session.get(SupplierInvoice, rig["invoice_id"])
        assert invoice.status == "received"  # unchanged — REVIEW doesn't move status


@pytest.mark.asyncio
async def test_match_three_way_unreceived_product_blocks(rig):
    async with AsyncSessionLocal() as session:
        extra_product = Product(
            org_id=rig["org_id"],
            sku=f"TEST-EXTRA-{uuid.uuid4().hex[:8]}",
            name="Never Received",
            hsn_code="7318",
            uom="PCS",
            gst_rate=Decimal("18"),
        )
        session.add(extra_product)
        await session.flush()
        session.add(
            SupplierInvoiceItem(
                supplier_invoice_id=rig["invoice_id"],
                product_id=extra_product.id,
                quantity=Decimal("5"),
                unit_price=Decimal("50"),
                gst_rate=Decimal("0"),
                line_total=Decimal("250"),
            )
        )
        await session.commit()
        extra_product_id = extra_product.id

    async with AsyncSessionLocal() as session:
        result = await match_three_way(
            session,
            org_id=rig["org_id"],
            purchase_order_id=rig["po_id"],
            goods_receipt_id=rig["grn_id"],
            supplier_invoice_id=rig["invoice_id"],
        )
        await session.commit()

    assert result.outcome.verdict == MatchVerdict.BLOCK
    assert any(GRN_LINE_MISSING in c for c in result.outcome.reason_codes)
    assert any(PO_LINE_MISSING in c for c in result.outcome.reason_codes)

    async with AsyncSessionLocal() as session:
        invoice = await session.get(SupplierInvoice, rig["invoice_id"])
        assert invoice.status == "blocked"
        await session.execute(
            delete(SupplierInvoiceItem).where(SupplierInvoiceItem.product_id == extra_product_id)
        )
        await session.execute(delete(Product).where(Product.id == extra_product_id))
        await session.commit()


@pytest.mark.asyncio
async def test_match_three_way_grn_from_different_po_rejected(rig):
    async with AsyncSessionLocal() as session:
        other_po = PurchaseOrder(
            org_id=rig["org_id"],
            supplier_id=rig["supplier_id"],
            po_number=f"PO-OTHER-{uuid.uuid4().hex[:8]}",
            order_date=A_WEEKDAY,
            status="approved",
            subtotal=Decimal("0"),
            tax_total=Decimal("0"),
            total=Decimal("0"),
        )
        session.add(other_po)
        await session.commit()
        other_po_id = other_po.id

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await match_three_way(
                session,
                org_id=rig["org_id"],
                purchase_order_id=other_po_id,
                goods_receipt_id=rig["grn_id"],
                supplier_invoice_id=rig["invoice_id"],
            )

    async with AsyncSessionLocal() as session:
        await session.execute(delete(PurchaseOrder).where(PurchaseOrder.id == other_po_id))
        await session.commit()


@pytest.mark.asyncio
async def test_match_three_way_supplier_mismatch_rejected(rig):
    async with AsyncSessionLocal() as session:
        invoice = await session.get(SupplierInvoice, rig["invoice_id"])
        invoice.supplier_id = rig["other_supplier_id"]
        invoice.purchase_order_id = None
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(ConflictError):
            await match_three_way(
                session,
                org_id=rig["org_id"],
                purchase_order_id=rig["po_id"],
                goods_receipt_id=rig["grn_id"],
                supplier_invoice_id=rig["invoice_id"],
            )


@pytest.mark.asyncio
async def test_match_three_way_unknown_purchase_order_rejected(rig):
    async with AsyncSessionLocal() as session:
        with pytest.raises(NotFoundError):
            await match_three_way(
                session,
                org_id=rig["org_id"],
                purchase_order_id=uuid.uuid4(),
                goods_receipt_id=rig["grn_id"],
                supplier_invoice_id=rig["invoice_id"],
            )
