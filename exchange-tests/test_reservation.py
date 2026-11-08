import pytest

pytestmark = pytest.mark.api


def stock_of(onec, sku):
    """Доступное количество товара по данным каталога (остаток минус резерв)."""
    return next(p for p in onec.get("/catalog").json() if p["sku"] == sku)["stock"]


def make_order(onec, order_payload, sku, quantity):
    return onec.post("/orders", json=order_payload([{"sku": sku, "quantity": quantity}]))


def test_order_reserves_stock(onec, in_stock_product, order_payload):
    sku = in_stock_product["sku"]
    before = stock_of(onec, sku)
    assert make_order(onec, order_payload, sku, 1).status_code == 201
    assert stock_of(onec, sku) == before - 1


def test_cancel_returns_stock(onec, in_stock_product, order_payload):
    sku = in_stock_product["sku"]
    before = stock_of(onec, sku)
    number = make_order(onec, order_payload, sku, 1).json()["order_number"]

    response = onec.cancel(number, "autotest@example.com")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "Отменён"
    assert stock_of(onec, sku) == before


def test_cancelled_order_status_is_visible(onec, in_stock_product, order_payload):
    number = make_order(onec, order_payload, in_stock_product["sku"], 1).json()["order_number"]
    onec.cancel(number, "autotest@example.com")
    response = onec.get(f"/orders/{number}", params={"email": "autotest@example.com"})
    assert response.status_code == 200
    assert response.json()["status"] == "Отменён"


def test_cancel_is_idempotent(onec, in_stock_product, order_payload):
    sku = in_stock_product["sku"]
    before = stock_of(onec, sku)
    number = make_order(onec, order_payload, sku, 1).json()["order_number"]
    assert onec.cancel(number, "autotest@example.com").status_code == 200
    assert onec.cancel(number, "autotest@example.com").status_code == 200
    assert stock_of(onec, sku) == before


def test_cancel_with_foreign_email_gets_404(onec, in_stock_product, order_payload):
    sku = in_stock_product["sku"]
    before = stock_of(onec, sku)
    number = make_order(onec, order_payload, sku, 1).json()["order_number"]
    assert onec.cancel(number, "someone-else@example.com").status_code == 404
    assert stock_of(onec, sku) == before - 1  # заказ остался и держит резерв


def test_cancel_unknown_order_gets_404(onec):
    assert onec.cancel("999999999", "autotest@example.com").status_code == 404


def test_cancel_requires_email(onec, in_stock_product, order_payload):
    number = make_order(onec, order_payload, in_stock_product["sku"], 1).json()["order_number"]
    response = onec.post(f"/orders/{number}/cancel")
    assert response.status_code == 400


def test_last_unit_goes_to_first_buyer(onec, catalog, order_payload):
    """Два покупателя на последнюю единицу: второй получает 409."""
    candidates = [p for p in catalog if 1 <= p["stock"] <= 10]
    if not candidates:
        pytest.skip("В каталоге нет товара с небольшим остатком")
    product = min(candidates, key=lambda p: p["stock"])
    sku, available = product["sku"], int(product["stock"])

    first = make_order(onec, order_payload, sku, available)
    assert first.status_code == 201, first.text
    assert stock_of(onec, sku) == 0

    second = make_order(onec, order_payload, sku, 1)
    assert second.status_code == 409
    assert second.json()["items"][0]["available"] == 0
