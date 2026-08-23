"""GST pricing engine — computes CGST/SGST/IGST, cess, discounts, rounding
and the HSN-wise summary for a set of order lines. This is the sole place
tax math happens; sales.py (2.7) and procurement.py (2.8) call `price_order`
rather than reimplementing any of this.

Tax type (Sections 7-9, IGST Act 2017): intra-state (CGST + SGST, each =
rate/2) when the supply's origin state and place of supply are the same
state/UT; inter-state (IGST = full rate) otherwise. Place of supply for
goods is generally where movement of goods terminates for delivery to the
recipient, NOT the billing address. UTGST (union territories without their
own legislature — Chandigarh, Lakshadweep, A&N Islands, Dadra & Nagar
Haveli and Daman & Diu, Ladakh) is mechanically identical to SGST — same
rate/2 split — so `sgst_amount` below doubles as the UTGST amount for those
cases; callers relabel it on the invoice PDF, the arithmetic doesn't change.

Discounts (Section 15(3), CGST Act): a discount given before or at the time
of supply, recorded on the invoice, is excluded from taxable value directly
— no credit note needed. Line discounts reduce that line's own taxable
value; a header (invoice-level) discount is apportioned pro-rata across
lines by each line's post-line-discount share, so the HSN-wise summary's
taxable values still sum to the invoice subtotal exactly. Post-supply
discounts not agreed up front are out of scope here — those need a credit
note (services/matching.py / credit_debit_notes, not this module).

Money math uses Decimal throughout, never float (repo convention). Note:
the `Mapped[float]` type hints on ORM money columns (sales.py, etc.) are
cosmetic — SQLAlchemy's Numeric column type defaults to asdecimal=True, so
those columns actually round-trip as Decimal at runtime. This module's
Decimal contract is what actually flows through the system.

Rounding: every per-line tax amount (CGST/SGST/IGST/cess) is rounded
independently to paise (2dp, ROUND_HALF_UP — ">=50 paise rounds up", the
standard commercial convention in Indian invoicing, not banker's rounding).
The invoice grand total is then separately rounded to the nearest rupee per
Rule 46(r) of the CGST Rules; the gap is reported as `round_off` rather than
silently folded back into a line — this mirrors the e-invoice schema's
dedicated `RndOffAmt` field, where line/tax amounts are never adjusted just
to make the total land on a whole rupee.
"""

import uuid
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

_PAISE = Decimal("0.01")
_RUPEE = Decimal("1")
ZERO = Decimal("0")


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


class PricingLineInput(BaseModel):
    product_id: uuid.UUID
    hsn_code: str
    uom: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    gst_rate: Decimal = Field(ge=0, le=100)
    cess_rate: Decimal = Field(default=ZERO, ge=0, le=100)
    discount_percent: Decimal = Field(default=ZERO, ge=0, le=100)
    discount_amount: Decimal = Field(default=ZERO, ge=0)


class PricingLineResult(BaseModel):
    product_id: uuid.UUID
    hsn_code: str
    uom: str
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    line_discount: Decimal
    header_discount_share: Decimal
    taxable_value: Decimal
    gst_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    cess_rate: Decimal
    cess_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal


class HsnSummaryLine(BaseModel):
    hsn_code: str
    uom: str
    gst_rate: Decimal
    total_quantity: Decimal
    taxable_value: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    cess_amount: Decimal
    total_amount: Decimal


class PricingResult(BaseModel):
    tax_type: str  # "intra_state" | "inter_state"
    lines: list[PricingLineResult]
    hsn_summary: list[HsnSummaryLine]
    gross_total: Decimal
    discount_total: Decimal
    subtotal: Decimal
    cgst_total: Decimal
    sgst_total: Decimal
    igst_total: Decimal
    cess_total: Decimal
    tax_total: Decimal
    round_off: Decimal
    grand_total: Decimal


def price_order(
    *,
    origin_state_code: str,
    place_of_supply_state_code: str,
    lines: list[PricingLineInput],
    header_discount_percent: Decimal = ZERO,
    header_discount_amount: Decimal = ZERO,
) -> PricingResult:
    """Price a full order/invoice: per-line tax split, discounts applied
    before tax, rupee-rounded grand total, and an HSN-wise summary grouped
    by (hsn_code, gst_rate) — matching GSTR-1 Table 12 / the e-invoice
    schema's HSN summary section.
    """
    if not lines:
        raise ValueError("price_order requires at least one line")

    is_intra_state = origin_state_code == place_of_supply_state_code
    tax_type = "intra_state" if is_intra_state else "inter_state"

    raw_lines = []
    net_subtotal = ZERO
    for line in lines:
        gross = line.quantity * line.unit_price
        line_discount = gross * (line.discount_percent / Decimal(100)) + line.discount_amount
        line_discount = min(line_discount, gross)
        net_after_line_discount = gross - line_discount
        raw_lines.append((line, gross, line_discount, net_after_line_discount))
        net_subtotal += net_after_line_discount

    header_discount_total = (
        net_subtotal * (header_discount_percent / Decimal(100)) + header_discount_amount
    )
    header_discount_total = min(header_discount_total, net_subtotal)

    result_lines: list[PricingLineResult] = []
    cgst_total = sgst_total = igst_total = cess_total = ZERO
    gross_total = discount_total = subtotal = ZERO
    allocated_header_discount = ZERO

    for idx, (line, gross, line_discount, net_after_line_discount) in enumerate(raw_lines):
        if idx == len(raw_lines) - 1:
            # last line absorbs the remainder so allocated shares sum to
            # header_discount_total exactly, no matter how the pro-rata
            # division rounds.
            header_share = header_discount_total - allocated_header_discount
        elif net_subtotal > 0:
            header_share = header_discount_total * (net_after_line_discount / net_subtotal)
        else:
            header_share = ZERO
        allocated_header_discount += header_share

        taxable_value = max(net_after_line_discount - header_share, ZERO)

        cgst = sgst = igst = ZERO
        if is_intra_state:
            half_rate = line.gst_rate / Decimal(2)
            cgst = _round_money(taxable_value * half_rate / Decimal(100))
            sgst = _round_money(taxable_value * half_rate / Decimal(100))
        else:
            igst = _round_money(taxable_value * line.gst_rate / Decimal(100))
        cess = _round_money(taxable_value * line.cess_rate / Decimal(100))

        tax_amount = cgst + sgst + igst + cess
        taxable_value_rounded = _round_money(taxable_value)
        line_total = taxable_value_rounded + tax_amount
        gross_rounded = _round_money(gross)
        line_discount_rounded = _round_money(line_discount)
        header_share_rounded = _round_money(header_share)

        result_lines.append(
            PricingLineResult(
                product_id=line.product_id,
                hsn_code=line.hsn_code,
                uom=line.uom,
                quantity=line.quantity,
                unit_price=line.unit_price,
                gross_amount=gross_rounded,
                line_discount=line_discount_rounded,
                header_discount_share=header_share_rounded,
                taxable_value=taxable_value_rounded,
                gst_rate=line.gst_rate,
                cgst_amount=cgst,
                sgst_amount=sgst,
                igst_amount=igst,
                cess_rate=line.cess_rate,
                cess_amount=cess,
                tax_amount=tax_amount,
                line_total=line_total,
            )
        )

        gross_total += gross_rounded
        discount_total += line_discount_rounded + header_share_rounded
        subtotal += taxable_value_rounded
        cgst_total += cgst
        sgst_total += sgst
        igst_total += igst
        cess_total += cess

    tax_total = cgst_total + sgst_total + igst_total + cess_total
    pre_round_total = subtotal + tax_total
    grand_total = pre_round_total.quantize(_RUPEE, rounding=ROUND_HALF_UP)
    round_off = grand_total - pre_round_total

    return PricingResult(
        tax_type=tax_type,
        lines=result_lines,
        hsn_summary=_build_hsn_summary(result_lines),
        gross_total=gross_total,
        discount_total=discount_total,
        subtotal=subtotal,
        cgst_total=cgst_total,
        sgst_total=sgst_total,
        igst_total=igst_total,
        cess_total=cess_total,
        tax_total=tax_total,
        round_off=round_off,
        grand_total=grand_total,
    )


def _build_hsn_summary(lines: list[PricingLineResult]) -> list[HsnSummaryLine]:
    groups: dict[tuple[str, Decimal], HsnSummaryLine] = {}
    order: list[tuple[str, Decimal]] = []
    for line in lines:
        key = (line.hsn_code, line.gst_rate)
        summary = groups.get(key)
        if summary is None:
            summary = HsnSummaryLine(
                hsn_code=line.hsn_code,
                uom=line.uom,
                gst_rate=line.gst_rate,
                total_quantity=ZERO,
                taxable_value=ZERO,
                cgst_amount=ZERO,
                sgst_amount=ZERO,
                igst_amount=ZERO,
                cess_amount=ZERO,
                total_amount=ZERO,
            )
            groups[key] = summary
            order.append(key)
        summary.total_quantity += line.quantity
        summary.taxable_value += line.taxable_value
        summary.cgst_amount += line.cgst_amount
        summary.sgst_amount += line.sgst_amount
        summary.igst_amount += line.igst_amount
        summary.cess_amount += line.cess_amount
        summary.total_amount += line.line_total
    return [groups[key] for key in order]
