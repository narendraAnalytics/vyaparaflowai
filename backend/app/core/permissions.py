"""Static role -> permission map. A DB-backed permissions table is
deliberately not built yet (YAGNI) — promote this to a table only when a
future phase needs runtime-editable permissions. See the Phase 2
foundation design doc for the decision.
"""

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "Owner": frozenset(),  # Owner bypasses the permission check entirely, see has_permission
    "Manager": frozenset(
        {
            "po.approve",
            "po.create",
            "pr.approve",
            "sales_order.create",
            "sales_order.approve",
            "customer.manage",
            "supplier.manage",
            "product.manage",
            "warehouse.manage",
            "approval.manage",
        }
    ),
    "Sales": frozenset({"sales_order.create", "customer.view"}),
    "Warehouse": frozenset({"goods_receipt.create", "delivery.create", "inventory.adjust"}),
    "Accounts": frozenset(
        {"payment.record", "invoice.create", "supplier_invoice.create", "supplier_invoice.match"}
    ),
    # n8n's fixed automation identity (Phase 3.2) — least-privilege: only
    # the writes the orchestration workflows actually call, never
    # customer/supplier/product management or approval-bypassing power.
    "Automation": frozenset(
        {
            "sales_order.create",
            "po.create",
            "goods_receipt.create",
            "supplier_invoice.create",
            "supplier_invoice.match",
            "payment.record",
            "approval.manage",
            "delivery.create",
            "invoice.create",
            "workflow_event.create",
        }
    ),
}


def has_permission(role_names: set[str], required: set[str]) -> bool:
    if "Owner" in role_names:
        return True
    granted: set[str] = set()
    for role_name in role_names:
        granted |= ROLE_PERMISSIONS.get(role_name, frozenset())
    return bool(granted & required)
