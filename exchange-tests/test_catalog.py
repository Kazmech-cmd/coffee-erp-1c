import re

import pytest

pytestmark = pytest.mark.api

REQUIRED_FIELDS = {"sku", "name", "unit", "price", "description", "stock"}


def test_catalog_returns_list(onec):
    response = onec.get("/catalog")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    assert isinstance(response.json(), list)


def test_catalog_not_empty(catalog):
    assert len(catalog) > 0


def test_every_product_has_required_fields(catalog):
    for product in catalog:
        assert REQUIRED_FIELDS <= product.keys(), product.get("sku")


def test_prices_are_positive(catalog):
    for product in catalog:
        assert product["price"] > 0, product["sku"]


def test_stock_is_not_negative(catalog):
    for product in catalog:
        assert product["stock"] >= 0, product["sku"]


def test_sku_is_unique(catalog):
    skus = [p["sku"] for p in catalog]
    assert len(skus) == len(set(skus))


def test_sku_format(catalog):
    for product in catalog:
        assert re.fullmatch(r"[A-Z]{3}-\d{3}", product["sku"]), product["sku"]
