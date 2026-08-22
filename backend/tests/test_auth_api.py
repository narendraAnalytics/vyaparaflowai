import pytest

from app.db.seed import DEMO_USER_PASSWORD


@pytest.mark.asyncio
async def test_login_success(client):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password_is_problem_json(client):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": "wrong"},
    )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_current_user(client):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    access_token = login.json()["access_token"]
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "manager@srilakshmi.example.com"
    assert "Manager" in body["roles"]


@pytest.mark.asyncio
async def test_admin_ping_allows_manager_denies_sales(client):
    manager_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    manager_token = manager_login.json()["access_token"]
    ok = await client.get(
        "/api/v1/auth/admin-ping", headers={"Authorization": f"Bearer {manager_token}"}
    )
    assert ok.status_code == 200

    sales_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "sales@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    sales_token = sales_login.json()["access_token"]
    denied = await client.get(
        "/api/v1/auth/admin-ping", headers={"Authorization": f"Bearer {sales_token}"}
    )
    assert denied.status_code == 403
    assert denied.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_refresh_rotates_and_old_token_dies(client):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "accounts@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    old_refresh = login.json()["refresh_token"]

    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refreshed.status_code == 200
    new_refresh = refreshed.json()["refresh_token"]
    assert new_refresh != old_refresh

    reuse_old = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse_old.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(client):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "warehouse@srilakshmi.example.com", "password": DEMO_USER_PASSWORD},
    )
    refresh_token = login.json()["refresh_token"]

    logout = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 204

    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reuse.status_code == 401
