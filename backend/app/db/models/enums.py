"""Status values as plain Python string enums.

Stored as VARCHAR + an explicit CHECK constraint (not native Postgres ENUM)
so adding a new status later is a plain migration, not an ALTER TYPE dance.
"""

from enum import StrEnum


class SalesOrderStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    PARTIALLY_RESERVED = "partially_reserved"
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    INVOICED = "invoiced"
    CANCELLED = "cancelled"


class PurchaseRequisitionStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONVERTED = "converted"
    CANCELLED = "cancelled"


class PurchaseOrderStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    INVOICED = "invoiced"
    PAID = "paid"
    CANCELLED = "cancelled"


class SupplierInvoiceStatus(StrEnum):
    RECEIVED = "received"
    MATCHED = "matched"
    APPROVED = "approved"
    BLOCKED = "blocked"
    PAID = "paid"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    OVERDUE = "overdue"
    VOID = "void"


class MatchVerdict(StrEnum):
    AUTO_APPROVE = "auto_approve"
    REVIEW = "review"
    BLOCK = "block"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELEGATED = "delegated"
    ESCALATED = "escalated"


class StockMovementType(StrEnum):
    RECEIPT = "receipt"
    ISSUE = "issue"
    RESERVE = "reserve"
    RELEASE = "release"
    ADJUSTMENT = "adjustment"
    RETURN_IN = "return_in"
    RETURN_OUT = "return_out"


class LedgerAccount(StrEnum):
    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    ACCOUNTS_PAYABLE = "accounts_payable"
    INVENTORY = "inventory"
    SALES_REVENUE = "sales_revenue"
    COGS = "cogs"
    GST_OUTPUT = "gst_output"
    GST_INPUT = "gst_input"
    CASH_BANK = "cash_bank"


class CreditDebitNoteType(StrEnum):
    CREDIT_TO_CUSTOMER = "credit_to_customer"
    DEBIT_TO_SUPPLIER = "debit_to_supplier"


class NotificationChannel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    SLACK = "slack"
    SMS = "sms"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


def check_values(*members: StrEnum) -> str:
    """Render a StrEnum's members as a SQL CHECK(... IN (...)) value list."""
    return ", ".join(f"'{m.value}'" for m in members)
