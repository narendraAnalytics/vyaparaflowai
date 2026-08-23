import uuid
from decimal import Decimal

import pytest

from app.services.pricing import PricingLineInput, price_order

# Telangana = 36, Karnataka = 29, Maharashtra = 27 (real GST state codes,
# matching the seed data's Sri Lakshmi Hardware tenant based in Telangana).
TELANGANA = "36"
KARNATAKA = "29"

WIRE_ID = uuid.uuid4()
PIPE_ID = uuid.uuid4()


def _line(**overrides) -> PricingLineInput:
    defaults = dict(
        product_id=WIRE_ID,
        hsn_code="8544",
        uom="MTR",
        quantity=Decimal("10"),
        unit_price=Decimal("100.00"),
        gst_rate=Decimal("18"),
    )
    defaults.update(overrides)
    return PricingLineInput(**defaults)


def test_intra_state_splits_cgst_sgst_evenly():
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=[_line()],
    )
    assert result.tax_type == "intra_state"
    line = result.lines[0]
    assert line.taxable_value == Decimal("1000.00")
    assert line.cgst_amount == Decimal("90.00")
    assert line.sgst_amount == Decimal("90.00")
    assert line.igst_amount == Decimal("0.00")
    assert line.tax_amount == Decimal("180.00")
    assert line.line_total == Decimal("1180.00")
    assert result.cgst_total == Decimal("90.00")
    assert result.sgst_total == Decimal("90.00")
    assert result.tax_total == Decimal("180.00")


def test_inter_state_charges_igst_only():
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=KARNATAKA,
        lines=[_line()],
    )
    assert result.tax_type == "inter_state"
    line = result.lines[0]
    assert line.cgst_amount == Decimal("0.00")
    assert line.sgst_amount == Decimal("0.00")
    assert line.igst_amount == Decimal("180.00")
    assert result.igst_total == Decimal("180.00")
    assert result.cgst_total == Decimal("0.00")
    assert result.sgst_total == Decimal("0.00")


def test_place_of_supply_not_billing_address_drives_tax_type():
    # Same origin, different place-of-supply state -> inter-state, even
    # though this simulates a bill-to in-state / ship-to out-of-state case
    # where a naive "billing address" rule would get it wrong.
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=KARNATAKA,
        lines=[_line()],
    )
    assert result.tax_type == "inter_state"


def test_line_discount_percent_reduces_taxable_value_before_tax():
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=[_line(discount_percent=Decimal("10"))],
    )
    line = result.lines[0]
    assert line.line_discount == Decimal("100.00")
    assert line.taxable_value == Decimal("900.00")
    # 18% of 900, split evenly
    assert line.cgst_amount == Decimal("81.00")
    assert line.sgst_amount == Decimal("81.00")


def test_line_discount_amount_is_flat_rupees():
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=[_line(discount_amount=Decimal("50.00"))],
    )
    line = result.lines[0]
    assert line.line_discount == Decimal("50.00")
    assert line.taxable_value == Decimal("950.00")


def test_line_discount_cannot_exceed_line_value():
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=[_line(discount_amount=Decimal("5000.00"))],
    )
    line = result.lines[0]
    assert line.taxable_value == Decimal("0.00")
    assert line.tax_amount == Decimal("0.00")


def test_header_discount_apportioned_pro_rata_across_lines():
    lines = [
        _line(
            product_id=WIRE_ID, hsn_code="8544", quantity=Decimal("10"), unit_price=Decimal("100")
        ),
        _line(
            product_id=PIPE_ID,
            hsn_code="3917",
            uom="PCS",
            quantity=Decimal("5"),
            unit_price=Decimal("400"),
            gst_rate=Decimal("18"),
        ),
    ]
    # line 1 net = 1000, line 2 net = 2000, total net = 3000
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=lines,
        header_discount_amount=Decimal("300.00"),
    )
    wire_line, pipe_line = result.lines
    # 1000/3000 share of 300 = 100; 2000/3000 share of 300 = 200
    assert wire_line.header_discount_share == Decimal("100.00")
    assert pipe_line.header_discount_share == Decimal("200.00")
    assert wire_line.taxable_value == Decimal("900.00")
    assert pipe_line.taxable_value == Decimal("1800.00")
    # HSN summary's taxable values reconcile exactly to the invoice subtotal
    assert sum((h.taxable_value for h in result.hsn_summary), Decimal("0")) == result.subtotal
    assert result.subtotal == Decimal("2700.00")


def test_cess_applies_on_taxable_value_alongside_gst():
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=[_line(cess_rate=Decimal("5"))],
    )
    line = result.lines[0]
    assert line.cess_amount == Decimal("50.00")  # 5% of 1000
    assert line.tax_amount == Decimal("230.00")  # 90 + 90 + 50
    assert result.cess_total == Decimal("50.00")


def test_hsn_summary_groups_same_hsn_and_rate_across_lines():
    lines = [
        _line(quantity=Decimal("10"), unit_price=Decimal("100")),
        _line(quantity=Decimal("5"), unit_price=Decimal("100")),
    ]
    result = price_order(
        origin_state_code=TELANGANA, place_of_supply_state_code=TELANGANA, lines=lines
    )
    assert len(result.hsn_summary) == 1
    summary = result.hsn_summary[0]
    assert summary.total_quantity == Decimal("15")
    assert summary.taxable_value == Decimal("1500.00")


def test_hsn_summary_keeps_same_hsn_different_rate_separate():
    lines = [
        _line(gst_rate=Decimal("18")),
        _line(gst_rate=Decimal("5")),
    ]
    result = price_order(
        origin_state_code=TELANGANA, place_of_supply_state_code=TELANGANA, lines=lines
    )
    assert len(result.hsn_summary) == 2
    rates = {row.gst_rate for row in result.hsn_summary}
    assert rates == {Decimal("18"), Decimal("5")}


def test_grand_total_rounds_to_nearest_rupee_and_reports_round_off():
    # 1000 taxable + 18% tax = 1180.00 exactly -> no rounding needed here,
    # so force a fractional total via a rate that produces paise.
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=[_line(quantity=Decimal("3"), unit_price=Decimal("33.33"), gst_rate=Decimal("18"))],
    )
    pre_round = result.subtotal + result.tax_total
    assert result.grand_total == pre_round.quantize(Decimal("1"))
    assert result.round_off == result.grand_total - pre_round


def test_round_off_half_rupee_rounds_up():
    # taxable 100 + 0.5% tax = 100.50 -> nearest rupee rounds up to 101,
    # per the standard round-half-up convention (not banker's rounding).
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=[_line(quantity=Decimal("1"), unit_price=Decimal("100"), gst_rate=Decimal("0.5"))],
    )
    assert result.subtotal + result.tax_total == Decimal("100.50")
    assert result.grand_total == Decimal("101")
    assert result.round_off == Decimal("0.50")


def test_header_discount_with_zero_value_lines_does_not_divide_by_zero():
    # All lines net to zero (e.g. free promotional items) -> header discount
    # has nothing to apportion against; must not raise ZeroDivisionError.
    lines = [
        _line(unit_price=Decimal("0")),
        _line(product_id=PIPE_ID, hsn_code="3917", unit_price=Decimal("0")),
    ]
    result = price_order(
        origin_state_code=TELANGANA,
        place_of_supply_state_code=TELANGANA,
        lines=lines,
        header_discount_amount=Decimal("50.00"),
    )
    assert result.lines[0].header_discount_share == Decimal("0.00")
    assert result.subtotal == Decimal("0.00")


def test_empty_lines_rejected():
    with pytest.raises(ValueError, match="at least one line"):
        price_order(origin_state_code=TELANGANA, place_of_supply_state_code=TELANGANA, lines=[])


def test_negative_quantity_rejected_by_schema():
    with pytest.raises(ValueError):
        PricingLineInput(
            product_id=WIRE_ID,
            hsn_code="8544",
            uom="MTR",
            quantity=Decimal("-1"),
            unit_price=Decimal("100"),
            gst_rate=Decimal("18"),
        )


def test_discount_percent_over_100_rejected_by_schema():
    with pytest.raises(ValueError):
        PricingLineInput(
            product_id=WIRE_ID,
            hsn_code="8544",
            uom="MTR",
            quantity=Decimal("1"),
            unit_price=Decimal("100"),
            gst_rate=Decimal("18"),
            discount_percent=Decimal("150"),
        )
