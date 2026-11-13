"""Contract tests against an isolated, migrated PostgreSQL test database.

Set TEST_DATABASE_URL before running pytest. The tests create unique records and
never drop tables or truncate data. Do not point this variable at production.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest


@pytest.fixture(scope="session")
def app():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated, migrated PostgreSQL database")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("Integration tests require PostgreSQL")
    os.environ["DATABASE_URL"] = url.replace("postgresql://", "postgresql+psycopg://", 1)
    os.environ.setdefault("SECRET_KEY", "integration-test-secret-key-not-for-production")
    from app.main import app as application
    return application


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


def checked(response, expected):
    assert response.status_code == expected, f"{response.status_code}: {response.text}"
    return response.json() if response.content else None


def account(client, prefix):
    email = f"{prefix}-{uuid4().hex}@example.com"
    payload = {"email": email, "password": "test-password-1234"}
    user = checked(client.post("/api/v1/auth/register", json=payload), 201)
    token = checked(client.post("/api/v1/auth/login", json=payload), 200)["access_token"]
    return user, {"Authorization": f"Bearer {token}"}


def workspace(client, headers):
    org = checked(client.post("/api/v1/organizations", headers=headers, json={
        "name": "Test workspace", "slug": f"test-{uuid4().hex}", "default_currency": "INR"
    }), 201)
    return f"/api/v1/organizations/{org['id']}"


def stock_setup(client, headers, base, quantity=10):
    warehouse = checked(client.post(f"{base}/warehouses", headers=headers, json={
        "code": "MAIN", "name": "Main warehouse"
    }), 201)
    product = checked(client.post(f"{base}/products", headers=headers, json={
        "sku": "SKU-1", "name": "Test product", "unit_price_minor": 2500,
        "low_stock_threshold": 2
    }), 201)
    position = checked(client.post(f"{base}/inventory/adjustments", headers=headers, json={
        "warehouse_id": warehouse["id"], "product_id": product["id"],
        "delta_on_hand": quantity, "reason": "Initial receipt"
    }), 200)
    assert (position["on_hand_qty"], position["reserved_qty"], position["available_qty"]) == (quantity, 0, quantity)
    return warehouse, product


def order_body(warehouse, product, quantity=1):
    return {"warehouse_id": warehouse["id"], "customer_name": "Test customer",
            "customer_email": "customer@example.com",
            "items": [{"product_id": product["id"], "quantity": quantity}]}


def inventory(client, headers, base, product_id):
    page = checked(client.get(f"{base}/inventory", headers=headers), 200)
    return next(item for item in page["items"] if item["product_id"] == product_id)


def test_reserve_idempotency_and_fulfillment(client):
    _, headers = account(client, "owner")
    base = workspace(client, headers)
    warehouse, product = stock_setup(client, headers, base)
    key_headers = {**headers, "Idempotency-Key": uuid4().hex}
    body = order_body(warehouse, product, 3)
    first = checked(client.post(f"{base}/orders", headers=key_headers, json=body), 201)
    replay = client.post(f"{base}/orders", headers=key_headers, json=body)
    assert checked(replay, 201) == first
    assert replay.headers.get("Idempotency-Replayed", "").lower() == "true"
    changed = order_body(warehouse, product, 4)
    assert client.post(f"{base}/orders", headers=key_headers, json=changed).status_code == 409
    position = inventory(client, headers, base, product["id"])
    assert (position["on_hand_qty"], position["reserved_qty"], position["available_qty"]) == (10, 3, 7)
    checked(client.post(f"{base}/orders/{first['id']}/fulfill", headers=headers), 200)
    checked(client.post(f"{base}/orders/{first['id']}/fulfill", headers=headers), 200)
    assert client.post(f"{base}/orders/{first['id']}/cancel", headers=headers).status_code == 409
    position = inventory(client, headers, base, product["id"])
    assert (position["on_hand_qty"], position["reserved_qty"], position["available_qty"]) == (7, 0, 7)


def test_multiline_failure_rolls_back_and_cancel_releases(client):
    _, headers = account(client, "owner")
    base = workspace(client, headers)
    warehouse, product = stock_setup(client, headers, base, quantity=5)
    second = checked(client.post(f"{base}/products", headers=headers, json={
        "sku": "SKU-2", "name": "Out of stock", "unit_price_minor": 100
    }), 201)
    body = order_body(warehouse, product, 2)
    body["items"].append({"product_id": second["id"], "quantity": 1})
    assert client.post(f"{base}/orders", headers={**headers, "Idempotency-Key": uuid4().hex}, json=body).status_code == 409
    position = inventory(client, headers, base, product["id"])
    assert (position["on_hand_qty"], position["reserved_qty"]) == (5, 0)
    orders = checked(client.get(f"{base}/orders", headers=headers), 200)
    assert not orders["items"]
    body["items"].pop()
    order = checked(client.post(f"{base}/orders", headers={**headers, "Idempotency-Key": uuid4().hex}, json=body), 201)
    assert client.post(f"{base}/inventory/adjustments", headers=headers, json={
        "warehouse_id": warehouse["id"], "product_id": product["id"], "delta_on_hand": -4, "reason": "Correction"
    }).status_code == 409
    checked(client.post(f"{base}/orders/{order['id']}/cancel", headers=headers), 200)
    checked(client.post(f"{base}/orders/{order['id']}/cancel", headers=headers), 200)
    position = inventory(client, headers, base, product["id"])
    assert (position["on_hand_qty"], position["reserved_qty"]) == (5, 0)


def test_tenant_isolation_roles_and_viewer_redaction(client):
    _, admin = account(client, "admin")
    viewer, viewer_headers = account(client, "viewer")
    _, outsider = account(client, "outsider")
    base = workspace(client, admin)
    warehouse, product = stock_setup(client, admin, base)
    order = checked(client.post(f"{base}/orders", headers={**admin, "Idempotency-Key": uuid4().hex}, json=order_body(warehouse, product)), 201)
    assert client.get(f"{base}/products", headers=outsider).status_code == 404
    checked(client.post(f"{base}/members", headers=admin, json={"email": viewer["email"], "role": "viewer"}), 201)
    assert client.post(f"{base}/products", headers=viewer_headers, json={"sku": "NO", "name": "Denied", "unit_price_minor": 1}).status_code == 403
    assert client.post(f"{base}/orders/{order['id']}/fulfill", headers=viewer_headers).status_code == 403
    visible = checked(client.get(f"{base}/orders/{order['id']}", headers=viewer_headers), 200)
    assert visible["customer_email"] is None
    other_base = workspace(client, outsider)
    assert client.get(f"{other_base}/products/{product['id']}", headers=outsider).status_code == 404


def test_two_concurrent_orders_cannot_oversell(app, client):
    from fastapi.testclient import TestClient
    _, headers = account(client, "owner")
    base = workspace(client, headers)
    warehouse, product = stock_setup(client, headers, base, quantity=1)
    body = order_body(warehouse, product)
    start = Barrier(2)

    def create(_):
        with TestClient(app) as concurrent_client:
            start.wait(timeout=10)
            response = concurrent_client.post(f"{base}/orders", headers={**headers, "Idempotency-Key": uuid4().hex}, json=body)
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(create, range(2)))
    assert sorted(statuses) == [201, 409]
    position = inventory(client, headers, base, product["id"])
    assert (position["on_hand_qty"], position["reserved_qty"], position["available_qty"]) == (1, 1, 0)


def test_refresh_csrf_rotation_and_reuse_revokes_family(client):
    email = f"session-{uuid4().hex}@example.com"
    credentials = {"email": email, "password": "test-password-1234"}
    checked(client.post('/api/v1/auth/register', json=credentials), 201)
    checked(client.post('/api/v1/auth/login', json=credentials), 200)
    old_refresh = client.cookies.get('sp_refresh')
    old_csrf = client.cookies.get('sp_csrf')
    origin = 'http://localhost:5173'
    assert client.post('/api/v1/auth/refresh', headers={'Origin': origin}).status_code == 403
    assert client.post('/api/v1/auth/refresh', headers={'Origin': 'https://attacker.example', 'X-CSRF-Token': old_csrf}).status_code == 403
    checked(client.post('/api/v1/auth/refresh', headers={'Origin': origin, 'X-CSRF-Token': old_csrf}), 200)
    new_refresh = client.cookies.get('sp_refresh')
    new_csrf = client.cookies.get('sp_csrf')
    assert new_refresh != old_refresh
    old_cookie = f'sp_refresh={old_refresh}; sp_csrf={old_csrf}'
    replay = client.post('/api/v1/auth/refresh', headers={'Origin': origin, 'X-CSRF-Token': old_csrf, 'Cookie': old_cookie})
    assert replay.status_code == 401
    new_cookie = f'sp_refresh={new_refresh}; sp_csrf={new_csrf}'
    revoked = client.post('/api/v1/auth/refresh', headers={'Origin': origin, 'X-CSRF-Token': new_csrf, 'Cookie': new_cookie})
    assert revoked.status_code == 401


def test_login_rate_limit_is_shared_and_survives_failed_requests(client):
    credentials = {'email': f'unknown-{uuid4().hex}@example.com', 'password': 'incorrect-password'}
    for _ in range(5):
        assert client.post('/api/v1/auth/login', json=credentials).status_code == 401
    limited = client.post('/api/v1/auth/login', json=credentials)
    assert limited.status_code == 429
    assert limited.headers['Retry-After'] == '60'


def test_invalid_credentials_are_not_echoed_in_validation_errors(client):
    secret = 'secret-' + 'x' * 130
    for path in ('register', 'login'):
        response = client.post(f'/api/v1/auth/{path}', json={'email': 'invalid', 'password': secret})
        assert response.status_code == 422
        assert secret not in response.text
        assert 'input' not in response.text


def test_idempotency_replays_reordered_lines_and_simultaneous_requests(app, client):
    from fastapi.testclient import TestClient
    _, headers = account(client, 'order-owner')
    base = workspace(client, headers)
    warehouse, product = stock_setup(client, headers, base, quantity=5)
    second = checked(client.post(f'{base}/products', headers=headers, json={
        'sku': 'SKU-2', 'name': 'Second', 'unit_price_minor': 100
    }), 201)
    checked(client.post(f'{base}/inventory/adjustments', headers=headers, json={
        'warehouse_id': warehouse['id'], 'product_id': second['id'], 'delta_on_hand': 5, 'reason': 'Initial receipt'
    }), 200)
    body = order_body(warehouse, product, 1)
    body['items'].append({'product_id': second['id'], 'quantity': 1})
    key = uuid4().hex
    start = Barrier(2)

    def create(_):
        with TestClient(app) as concurrent_client:
            start.wait(timeout=10)
            response = concurrent_client.post(f'{base}/orders', headers={**headers, 'Idempotency-Key': key}, json=body)
            return response.status_code, response.json(), response.headers.get('Idempotency-Replayed')

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert [r[0] for r in results] == [201, 201]
    assert results[0][1] == results[1][1]
    assert sorted(r[2] for r in results) == ['false', 'true']
    body['items'].reverse()
    replay = client.post(f'{base}/orders', headers={**headers, 'Idempotency-Key': key}, json=body)
    assert checked(replay, 201) == results[0][1]
    assert inventory(client, headers, base, product['id'])['reserved_qty'] == 1
    assert inventory(client, headers, base, second['id'])['reserved_qty'] == 1


def test_warehouse_activity_ledger_and_tenant_foreign_keys(app, client):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.main import engine
    _, headers = account(client, 'ledger-owner')
    base = workspace(client, headers)
    warehouse, product = stock_setup(client, headers, base, quantity=4)
    order = checked(client.post(f'{base}/orders', headers={**headers, 'Idempotency-Key': uuid4().hex}, json=order_body(warehouse, product, 2)), 201)
    checked(client.post(f"{base}/orders/{order['id']}/fulfill", headers=headers), 200)
    movements = checked(client.get(f'{base}/inventory/movements', headers=headers), 200)['items']
    position = inventory(client, headers, base, product['id'])
    assert sum(m['delta_on_hand'] for m in movements) == position['on_hand_qty']
    assert sum(m['delta_reserved'] for m in movements) == position['reserved_qty']
    checked(client.patch(f"{base}/warehouses/{warehouse['id']}", headers=headers, json={'is_active': False}), 200)
    assert client.post(f'{base}/inventory/adjustments', headers=headers, json={
        'warehouse_id': warehouse['id'], 'product_id': product['id'], 'delta_on_hand': 1, 'reason': 'Receipt'
    }).status_code == 409

    _, other_headers = account(client, 'other-owner')
    other_base = workspace(client, other_headers)
    foreign_product = checked(client.post(f'{other_base}/products', headers=other_headers, json={
        'sku': 'FOREIGN', 'name': 'Other tenant product', 'unit_price_minor': 1
    }), 201)
    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text('INSERT INTO inventory (organization_id,warehouse_id,product_id) VALUES (:org,:warehouse,:product)'), {
                'org': base.rsplit('/', 1)[-1], 'warehouse': warehouse['id'], 'product': foreign_product['id']
            })
        connection.rollback()
