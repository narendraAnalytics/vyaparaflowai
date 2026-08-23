import uuid

import pytest

from app.db.seed import DEMO_USER_PASSWORD


async def _login(client, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": DEMO_USER_PASSWORD}
    )
    assert response.status_code == 200
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_customers_requires_auth(client):
    response = await client.get("/api/v1/customers")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_customers_returns_seeded_data(client):
    token = await _login(client, "sales@srilakshmi.example.com")
    response = await client.get("/api/v1/customers", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 10
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) >= 10
    assert all(item["is_active"] for item in body["items"])


@pytest.mark.asyncio
async def test_list_customers_pagination_and_search(client):
    token = await _login(client, "sales@srilakshmi.example.com")
    page = await client.get(
        "/api/v1/customers", headers=_auth(token), params={"limit": 2, "offset": 0}
    )
    assert page.status_code == 200
    assert len(page.json()["items"]) == 2

    name = page.json()["items"][0]["name"]
    search = await client.get("/api/v1/customers", headers=_auth(token), params={"q": name[:4]})
    assert search.status_code == 200
    assert any(item["name"] == name for item in search.json()["items"])


@pytest.mark.asyncio
async def test_get_customer_by_id_and_404(client):
    token = await _login(client, "sales@srilakshmi.example.com")
    listed = await client.get("/api/v1/customers", headers=_auth(token), params={"limit": 1})
    customer_id = listed.json()["items"][0]["id"]

    got = await client.get(f"/api/v1/customers/{customer_id}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["id"] == customer_id

    missing = await client.get(
        "/api/v1/customers/00000000-0000-0000-0000-000000000000", headers=_auth(token)
    )
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_create_customer_denied_for_sales_allowed_for_manager(client):
    sales_token = await _login(client, "sales@srilakshmi.example.com")
    denied = await client.post(
        "/api/v1/customers",
        headers=_auth(sales_token),
        json={"name": "Test Customer Co"},
    )
    assert denied.status_code == 403

    manager_token = await _login(client, "manager@srilakshmi.example.com")
    created = await client.post(
        "/api/v1/customers",
        headers=_auth(manager_token),
        json={"name": "Test Customer Co", "credit_limit": 50000, "payment_terms_days": 15},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Test Customer Co"
    assert body["is_active"] is True
    assert body["credit_limit"] == 50000

    # invalid GSTIN format is rejected before it reaches the DB
    bad = await client.post(
        "/api/v1/customers",
        headers=_auth(manager_token),
        json={"name": "Bad GSTIN Co", "gstin": "not-a-gstin"},
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_update_and_deactivate_customer(client):
    manager_token = await _login(client, "manager@srilakshmi.example.com")
    created = await client.post(
        "/api/v1/customers", headers=_auth(manager_token), json={"name": "Lifecycle Customer"}
    )
    customer_id = created.json()["id"]

    updated = await client.patch(
        f"/api/v1/customers/{customer_id}",
        headers=_auth(manager_token),
        json={"credit_limit": 12345.50},
    )
    assert updated.status_code == 200
    assert updated.json()["credit_limit"] == 12345.50
    assert updated.json()["name"] == "Lifecycle Customer"

    deactivated = await client.delete(
        f"/api/v1/customers/{customer_id}", headers=_auth(manager_token)
    )
    assert deactivated.status_code == 204

    got = await client.get(f"/api/v1/customers/{customer_id}", headers=_auth(manager_token))
    assert got.json()["is_active"] is False


@pytest.mark.asyncio
async def test_supplier_crud_and_rbac(client):
    sales_token = await _login(client, "sales@srilakshmi.example.com")
    denied = await client.post(
        "/api/v1/suppliers", headers=_auth(sales_token), json={"name": "New Supplier"}
    )
    assert denied.status_code == 403

    manager_token = await _login(client, "manager@srilakshmi.example.com")
    created = await client.post(
        "/api/v1/suppliers",
        headers=_auth(manager_token),
        json={"name": "New Supplier", "lead_time_days": 3},
    )
    assert created.status_code == 201
    assert created.json()["reliability_score"] == 100

    listed = await client.get("/api/v1/suppliers", headers=_auth(manager_token))
    assert listed.status_code == 200
    assert listed.json()["total"] >= 6


@pytest.mark.asyncio
async def test_product_create_rejects_duplicate_sku(client):
    manager_token = await _login(client, "manager@srilakshmi.example.com")
    payload = {
        "sku": f"TEST-DUP-{uuid.uuid4().hex[:8]}",
        "name": "Duplicate Test Product",
        "hsn_code": "7318",
        "uom": "PCS",
        "gst_rate": 18,
    }
    first = await client.post("/api/v1/products", headers=_auth(manager_token), json=payload)
    assert first.status_code == 201

    second = await client.post("/api/v1/products", headers=_auth(manager_token), json=payload)
    assert second.status_code == 409
    assert second.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_warehouse_crud_and_rbac(client):
    code = f"WH-{uuid.uuid4().hex[:8]}"
    warehouse_token = await _login(client, "warehouse@srilakshmi.example.com")
    denied = await client.post(
        "/api/v1/warehouses",
        headers=_auth(warehouse_token),
        json={"code": code, "name": "New Warehouse"},
    )
    assert denied.status_code == 403

    manager_token = await _login(client, "manager@srilakshmi.example.com")
    created = await client.post(
        "/api/v1/warehouses",
        headers=_auth(manager_token),
        json={"code": code, "name": "New Warehouse"},
    )
    assert created.status_code == 201

    dup = await client.post(
        "/api/v1/warehouses",
        headers=_auth(manager_token),
        json={"code": code, "name": "Another Warehouse"},
    )
    assert dup.status_code == 409
