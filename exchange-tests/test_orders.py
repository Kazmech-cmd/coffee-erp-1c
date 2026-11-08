import pytest

pytestmark = pytest.mark.api

STATUSES = {"Новый", "Собран", "Отправлен", "Выполнен", "Отменён"}


# ---------- Создание заказа ----------

def test_create_order_success(onec, in_stock_product, order_payload):
    """Успешный заказ: 201, статус «Новый», сумма по ценам 1С."""
    p = in_stock_product
    response = onec.post("/orders", json=order_payload([{"sku": p["sku"], "quantity": 1}]))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "Новый"
    assert body["order_number"].isdigit()
    assert body["total"] == pytest.approx(p["price"])


def test_duplicate_lines_are_merged(onec, in_stock_product, order_payload):
    p = in_stock_product
    items = [{"sku": p["sku"], "quantity": 1}, {"sku": p["sku"], "quantity": 1}]
    response = onec.post("/orders", json=order_payload(items))
    assert response.status_code == 201, response.text
    assert response.json()["total"] == pytest.approx(p["price"] * 2)


def test_price_from_request_is_ignored(onec, in_stock_product, order_payload):
    """Цену нельзя подменить: она всегда берётся из справочника 1С."""
    p = in_stock_product
    items = [{"sku": p["sku"], "quantity": 1, "price": 1}]
    response = onec.post("/orders", json=order_payload(items))
    assert response.status_code == 201, response.text
    assert response.json()["total"] == pytest.approx(p["price"])


def test_not_enough_stock_returns_409(onec, catalog, order_payload):
    product = next(p for p in catalog if p["stock"] < 1000)
    wanted = int(product["stock"]) + 1
    response = onec.post("/orders", json=order_payload([{"sku": product["sku"], "quantity": wanted}]))
    assert response.status_code == 409
    body = response.json()
    assert "error" in body
    shortage = {item["sku"]: item for item in body["items"]}
    assert shortage[product["sku"]]["requested"] == wanted
    assert shortage[product["sku"]]["available"] < wanted


# ---------- Некорректные запросы ----------

@pytest.mark.parametrize("items", [
    [],
    [{"sku": "XXX-999", "quantity": 1}],
    [{"sku": "", "quantity": 1}],
    [{"sku": "TEA-001", "quantity": 0}],
    [{"sku": "TEA-001", "quantity": -1}],
    [{"sku": "TEA-001", "quantity": 1.5}],
    [{"sku": "TEA-001", "quantity": 1001}],
    [{"sku": "TEA-001", "quantity": "2"}],
    [{"sku": "TEA-001"}],
    ["TEA-001"],
], ids=["пустая корзина", "нет такого артикула", "пустой артикул", "количество 0",
        "количество минус 1", "дробное количество", "количество больше 1000",
        "количество строкой", "нет количества", "позиция не объект"])
def test_bad_items_return_400(onec, order_payload, items):
    response = onec.post("/orders", json=order_payload(items))
    assert response.status_code == 400, response.text
    assert response.json()["error"]


def test_too_many_positions_return_400(onec, order_payload):
    items = [{"sku": "TEA-001", "quantity": 1}] * 51
    response = onec.post("/orders", json=order_payload(items))
    assert response.status_code == 400


@pytest.mark.parametrize("overrides", [
    {"name": ""},
    {"email": "без-собаки"},
    {"email": ""},
    {"address": ""},
], ids=["нет имени", "email без @", "пустой email", "нет адреса"])
def test_bad_customer_returns_400(onec, in_stock_product, order_payload, overrides):
    payload = order_payload([{"sku": in_stock_product["sku"], "quantity": 1}], **overrides)
    response = onec.post("/orders", json=payload)
    assert response.status_code == 400, response.text


def test_customer_must_be_object(onec, in_stock_product):
    payload = {"customer": "Иван", "items": [{"sku": in_stock_product["sku"], "quantity": 1}]}
    assert onec.post("/orders", json=payload).status_code == 400


@pytest.mark.parametrize("body", [b"not a json", b"", b"[]"], ids=["не json", "пустое тело", "массив"])
def test_malformed_body_returns_400(onec, body):
    response = onec.post(
        "/orders", content=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 400


# ---------- Статус заказа ----------

def test_order_status_ok(onec, created_order):
    response = onec.get(f"/orders/{created_order['number']}", params={"email": created_order["email"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_number"] == created_order["number"]
    assert body["status"] in STATUSES
    assert body["total"] == pytest.approx(created_order["total"])
    assert [i["sku"] for i in body["items"]] == [created_order["product"]["sku"]]
    assert body["items"][0]["quantity"] == 1


def test_order_number_without_leading_zeros(onec, created_order):
    short = str(int(created_order["number"]))
    response = onec.get(f"/orders/{short}", params={"email": created_order["email"]})
    assert response.status_code == 200


def test_order_email_is_case_insensitive(onec, created_order):
    response = onec.get(f"/orders/{created_order['number']}", params={"email": created_order["email"].upper()})
    assert response.status_code == 200


def test_foreign_email_gets_404(onec, created_order):
    """Чужой email не должен раскрывать существование заказа."""
    response = onec.get(f"/orders/{created_order['number']}", params={"email": "someone-else@example.com"})
    assert response.status_code == 404
    assert response.json()["error"] == "Заказ не найден."


def test_unknown_number_gets_404(onec):
    response = onec.get("/orders/999999999", params={"email": "autotest@example.com"})
    assert response.status_code == 404


def test_status_requires_email(onec, created_order):
    response = onec.get(f"/orders/{created_order['number']}")
    assert response.status_code == 400
