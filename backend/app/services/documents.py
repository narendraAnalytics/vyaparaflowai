"""Rendering business documents to PDF — pure functions, no DB/HTTP I/O
(same convention as pricing.py/matching.py: the money/layout math is
testable without a database). The async wrapper that loads a PO, calls
this, uploads to object storage, and persists a `documents` row lives in
services/procurement.py (mark_purchase_order_sent... no — see
generate_purchase_order_pdf() there), not here, to keep this module
free of DB access entirely.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


@dataclass
class PurchaseOrderPdfLine:
    sku: str
    product_name: str
    quantity: Decimal
    unit_price: Decimal
    gst_rate: Decimal
    line_total: Decimal


@dataclass
class PurchaseOrderPdfInput:
    po_number: str
    order_date: date
    org_name: str
    org_gstin: str | None
    org_address: str | None
    supplier_name: str
    supplier_gstin: str | None
    supplier_address: str | None
    lines: list[PurchaseOrderPdfLine]
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal


def render_purchase_order_pdf(data: PurchaseOrderPdfInput) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "POTitle", parent=styles["Heading1"], fontSize=18, spaceAfter=2 * mm
    )

    story = [
        Paragraph(f"Purchase Order {data.po_number}", title_style),
        Paragraph(f"Date: {data.order_date.isoformat()}", styles["Normal"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>From:</b> {data.org_name}", styles["Normal"]),
        Paragraph(f"GSTIN: {data.org_gstin or '-'}", styles["Normal"]),
        Paragraph(data.org_address or "", styles["Normal"]),
        Spacer(1, 4 * mm),
        Paragraph(f"<b>To (Supplier):</b> {data.supplier_name}", styles["Normal"]),
        Paragraph(f"GSTIN: {data.supplier_gstin or '-'}", styles["Normal"]),
        Paragraph(data.supplier_address or "", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]

    table_data = [["SKU", "Product", "Qty", "Unit Price", "GST %", "Line Total"]]
    for line in data.lines:
        table_data.append(
            [
                line.sku,
                line.product_name,
                f"{line.quantity:.3f}",
                f"Rs.{line.unit_price:.2f}",
                f"{line.gst_rate:.2f}%",
                f"Rs.{line.line_total:.2f}",
            ]
        )
    table_data.append(["", "", "", "", "Subtotal", f"Rs.{data.subtotal:.2f}"])
    table_data.append(["", "", "", "", "Tax", f"Rs.{data.tax_total:.2f}"])
    table_data.append(["", "", "", "", "Total", f"Rs.{data.total:.2f}"])

    table = Table(table_data, colWidths=[25 * mm, 55 * mm, 18 * mm, 28 * mm, 18 * mm, 28 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d3748")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, len(data.lines)), 0.5, colors.grey),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (4, -3), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story.append(table)

    doc.build(story)
    return buffer.getvalue()


@dataclass
class CustomerInvoicePdfLine:
    sku: str
    product_name: str
    quantity: Decimal
    unit_price: Decimal
    gst_rate: Decimal
    line_total: Decimal


@dataclass
class CustomerInvoicePdfInput:
    invoice_number: str
    invoice_date: date
    due_date: date
    org_name: str
    org_gstin: str | None
    org_address: str | None
    customer_name: str
    customer_gstin: str | None
    customer_address: str | None
    lines: list[CustomerInvoicePdfLine]
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal


def render_customer_invoice_pdf(data: CustomerInvoicePdfInput) -> bytes:
    """Mirrors render_purchase_order_pdf() above - same reportlab layout,
    the O2C-side document (WF-06, roadmap.txt 3.9).
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "InvoiceTitle", parent=styles["Heading1"], fontSize=18, spaceAfter=2 * mm
    )

    story = [
        Paragraph(f"Tax Invoice {data.invoice_number}", title_style),
        Paragraph(f"Invoice Date: {data.invoice_date.isoformat()}", styles["Normal"]),
        Paragraph(f"Due Date: {data.due_date.isoformat()}", styles["Normal"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>From:</b> {data.org_name}", styles["Normal"]),
        Paragraph(f"GSTIN: {data.org_gstin or '-'}", styles["Normal"]),
        Paragraph(data.org_address or "", styles["Normal"]),
        Spacer(1, 4 * mm),
        Paragraph(f"<b>To (Customer):</b> {data.customer_name}", styles["Normal"]),
        Paragraph(f"GSTIN: {data.customer_gstin or '-'}", styles["Normal"]),
        Paragraph(data.customer_address or "", styles["Normal"]),
        Spacer(1, 8 * mm),
    ]

    table_data = [["SKU", "Product", "Qty", "Unit Price", "GST %", "Line Total"]]
    for line in data.lines:
        table_data.append(
            [
                line.sku,
                line.product_name,
                f"{line.quantity:.3f}",
                f"Rs.{line.unit_price:.2f}",
                f"{line.gst_rate:.2f}%",
                f"Rs.{line.line_total:.2f}",
            ]
        )
    table_data.append(["", "", "", "", "Subtotal", f"Rs.{data.subtotal:.2f}"])
    table_data.append(["", "", "", "", "Tax", f"Rs.{data.tax_total:.2f}"])
    table_data.append(["", "", "", "", "Total", f"Rs.{data.total:.2f}"])

    table = Table(table_data, colWidths=[25 * mm, 55 * mm, 18 * mm, 28 * mm, 18 * mm, 28 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d3748")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, len(data.lines)), 0.5, colors.grey),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (4, -3), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story.append(table)

    doc.build(story)
    return buffer.getvalue()
