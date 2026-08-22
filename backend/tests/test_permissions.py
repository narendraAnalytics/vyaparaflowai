from app.core.permissions import ROLE_PERMISSIONS, has_permission


def test_owner_has_every_permission():
    assert has_permission({"Owner"}, {"anything.at.all"})


def test_manager_has_po_approve():
    assert has_permission({"Manager"}, {"po.approve"})


def test_sales_lacks_po_approve():
    assert not has_permission({"Sales"}, {"po.approve"})


def test_unknown_role_grants_nothing():
    assert not has_permission({"NotARealRole"}, {"po.approve"})


def test_all_seeded_roles_have_an_entry():
    for role in ["Owner", "Manager", "Sales", "Warehouse", "Accounts"]:
        assert role in ROLE_PERMISSIONS
