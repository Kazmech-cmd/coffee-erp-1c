import os
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shop-site"))

BASE_URL = os.getenv("ONEC_BASE_URL", "http://127.0.0.1/ERP-Shop-demo/hs/shop").rstrip("/")
os.environ["ONEC_BASE_URL"] = BASE_URL  # сайт в тестах ходит в ту же базу

TEST_EMAIL = "autotest@example.com"


class OneC:
    """Клиент HTTP-сервиса 1С. Повторяет запрос, если учебная версия отвечает про лимит подключений."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        # trust_env=False: не ходить к 127.0.0.1 через системный прокси.
        # Без keep-alive: IIS/1С иногда рвут «залежавшееся» соединение (WinError 10054).
        self.client = httpx.Client(
            timeout=30,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"Connection": "close"},
        )

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = None
        for attempt in range(4):
            try:
                response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                if attempt == 3:
                    raise
                time.sleep(1)
                continue
            if response.status_code == 500 and "подключений" in response.text and attempt < 3:
                time.sleep(2)
                continue
            break
        return response

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, **kwargs)


@pytest.fixture(scope="session")
def onec():
    api = OneC(BASE_URL)
    try:
        api.get("/catalog").raise_for_status()
    except httpx.HTTPError as exc:
        pytest.exit(
            f"1С недоступна по адресу {BASE_URL}: {exc!r}. "
            "Проверьте публикацию и закройте Конфигуратор и клиент демо-базы.",
            returncode=2,
        )
    yield api
    api.client.close()


@pytest.fixture(scope="session")
def catalog(onec):
    return onec.get("/catalog").json()


@pytest.fixture(scope="session")
def in_stock_product(catalog):
    for product in catalog:
        if product["stock"] >= 2:
            return product
    pytest.skip("В каталоге нет товара с остатком от 2: проведите поступление в демо-базе")


@pytest.fixture(scope="session")
def out_of_stock_product(catalog):
    for product in catalog:
        if product["stock"] <= 0:
            return product
    return None


@pytest.fixture(scope="session")
def order_payload():
    def build(items, email=TEST_EMAIL, **customer):
        data = {
            "name": "Автотест",
            "email": email,
            "phone": "+7 900 000-00-00",
            "address": "г. Тест, ул. Тестовая, 1",
        }
        data.update(customer)
        return {"customer": data, "items": items}
    return build


@pytest.fixture(scope="session")
def created_order(onec, in_stock_product, order_payload):
    """Один заказ на все тесты статуса, чтобы не плодить записи в учебной базе."""
    payload = order_payload([{"sku": in_stock_product["sku"], "quantity": 1}])
    response = onec.post("/orders", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "number": body["order_number"],
        "email": TEST_EMAIL,
        "product": in_stock_product,
        "total": body["total"],
    }
