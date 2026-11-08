import html

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.site


@pytest.fixture
def site(onec):
    from main import app
    with TestClient(app) as client:
        yield client


def cards(page_text: str) -> int:
    """Каждая карточка товара содержит один заголовок <h3>."""
    return page_text.count("<h3>")


def test_health(site):
    response = site.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_page(site):
    response = site.get("/")
    assert response.status_code == 200
    assert "Coffee Shop" in response.text


def test_site_catalog_matches_1c(site, catalog):
    on_site = {p["sku"] for p in site.get("/api/catalog").json()}
    assert on_site == {p["sku"] for p in catalog}


def test_all_products_on_index(site, catalog):
    assert cards(site.get("/").text) == len(catalog)


def test_in_stock_filter(site, catalog):
    page = site.get("/", params={"in_stock": "1"}).text
    assert cards(page) == sum(1 for p in catalog if p["stock"] > 0)
    assert "Нет в наличии" not in page


def test_price_filter(site, catalog):
    page = site.get("/", params={"price_min": "500", "price_max": "1000"}).text
    expected = sum(1 for p in catalog if 500 <= p["price"] <= 1000)
    assert cards(page) == expected


def test_search_by_name(site, catalog):
    page = site.get("/", params={"q": "чай"}).text
    expected = sum(1 for p in catalog if "чай" in p["name"].lower())
    assert cards(page) == expected


def test_category_filter(site, catalog):
    page = site.get("/", params={"category": "TEA"}).text
    assert cards(page) == sum(1 for p in catalog if p["sku"].startswith("TEA-"))


def test_empty_cart(site):
    response = site.get("/cart")
    assert response.status_code == 200
    assert "Корзина пуста" in response.text


def test_add_to_cart(site, in_stock_product):
    response = site.post("/cart/add", data={"sku": in_stock_product["sku"], "qty": 1, "next": "/"},
                         follow_redirects=False)
    assert response.status_code == 303
    cart_page = site.get("/cart").text
    assert html.escape(in_stock_product["name"], quote=False) in cart_page


def test_out_of_stock_cannot_be_added(site, out_of_stock_product):
    if out_of_stock_product is None:
        pytest.skip("В каталоге нет товара с нулевым остатком")
    site.post("/cart/add", data={"sku": out_of_stock_product["sku"], "qty": 1, "next": "/"},
              follow_redirects=False)
    assert "Корзина пуста" in site.get("/cart").text


def test_checkout_validation(site, in_stock_product):
    site.post("/cart/add", data={"sku": in_stock_product["sku"], "qty": 1, "next": "/"},
              follow_redirects=False)
    response = site.post("/checkout", data={"name": "", "email": "x@example.com",
                                            "phone": "", "address": ""})
    assert response.status_code == 400
    assert "Заполните" in response.text


def test_order_page_wrong_email(site, created_order):
    response = site.get(f"/order/{created_order['number']}", params={"email": "nobody@example.com"})
    assert response.status_code == 404
    assert "Заказ не найден" in response.text


def test_order_page_shows_status(site, created_order):
    response = site.get(f"/order/{created_order['number']}", params={"email": created_order["email"]})
    assert response.status_code == 200
    assert created_order["number"] in response.text


def test_checkout_creates_order_in_1c(site, onec, in_stock_product):
    """Сквозной сценарий: корзина на сайте, заказ в 1С, страница заказа."""
    site.post("/cart/add", data={"sku": in_stock_product["sku"], "qty": 1, "next": "/"},
              follow_redirects=False)
    response = site.post(
        "/checkout",
        data={"name": "Автотест Сайт", "email": "autotest@example.com",
              "phone": "+7 900 000-00-00", "address": "г. Тест, ул. Тестовая, 1"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/order/")
    # заказ создан на сайте: запоминаем, чтобы отменить после теста и освободить резерв
    onec.track(location.split("?")[0].rsplit("/", 1)[1], "autotest@example.com")
    order_page = site.get(location)
    assert order_page.status_code == 200
    assert "Новый" in order_page.text
    assert "Корзина пуста" in site.get("/cart").text
