import logging
import os
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("shop")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
IMG_DIR = STATIC_DIR / "img"
IMG_DIR.mkdir(parents=True, exist_ok=True)
IMG_EXTENSIONS = (".webp", ".jpg", ".jpeg", ".png")

ONEC_BASE_URL = os.getenv("ONEC_BASE_URL", "http://127.0.0.1/ERP-Shop/hs/shop")
ONEC_TIMEOUT = float(os.getenv("ONEC_TIMEOUT", "15"))

CATEGORIES = {
    "COF": "Кофе",
    "TEA": "Чай",
    "ACC": "Аксессуары",
    "SYR": "Сиропы",
    "SWT": "Сладости",
}
SORTS = {
    "default": "Сначала в наличии",
    "price_asc": "Сначала дешёвые",
    "price_desc": "Сначала дорогие",
    "name": "По названию",
}

app = FastAPI(title="Coffee Shop", description="Витрина магазина поверх 1С")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def find_image(sku: str) -> str | None:
    """Ищет фото товара в static/img по артикулу."""
    for ext in IMG_EXTENSIONS:
        if (IMG_DIR / f"{sku}{ext}").exists():
            return f"/static/img/{sku}{ext}"
    return None


def to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "on", "yes"}


def to_float(value: str) -> float | None:
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return None


async def fetch_catalog() -> list[dict]:
    """Запрашивает каталог в 1С, добавляет категорию и фото."""
    async with httpx.AsyncClient(timeout=ONEC_TIMEOUT) as client:
        response = await client.get(f"{ONEC_BASE_URL}/catalog")
        response.raise_for_status()
        products = response.json()
    for product in products:
        product["category"] = product["sku"].split("-")[0]
        product["name"] = product["name"].strip()
        product["image"] = find_image(product["sku"])
    return products


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    """Проверка связи с 1С (пригодится для автотестов)."""
    try:
        await fetch_catalog()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("1С недоступна: %r", exc)
        return Response(status_code=503, content="1С недоступна")
    return {"status": "ok"}


@app.get("/api/catalog")
async def api_catalog():
    """Каталог в виде JSON (данные берутся из 1С)."""
    return await fetch_catalog()


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    category: str = "",
    q: str = "",
    in_stock: str = "",
    sort: str = "default",
    price_min: str = "",
    price_max: str = "",
):
    stock_only = to_bool(in_stock)
    if sort not in SORTS:
        sort = "default"

    error = None
    all_products: list[dict] = []
    try:
        all_products = await fetch_catalog()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Ошибка обращения к 1С: %r", exc)
        error = "Каталог временно недоступен: не удалось связаться с 1С."

    products = all_products
    if category:
        products = [p for p in products if p["category"] == category]
    if q.strip():
        needle = q.strip().lower()
        products = [p for p in products if needle in p["name"].lower()]
    if stock_only:
        products = [p for p in products if p["stock"] > 0]
    low = to_float(price_min)
    high = to_float(price_max)
    if low is not None:
        products = [p for p in products if p["price"] >= low]
    if high is not None:
        products = [p for p in products if p["price"] <= high]

    if sort == "price_asc":
        products.sort(key=lambda p: p["price"])
    elif sort == "price_desc":
        products.sort(key=lambda p: p["price"], reverse=True)
    elif sort == "name":
        products.sort(key=lambda p: p["name"].lower())
    else:
        products.sort(key=lambda p: (p["stock"] <= 0, p["sku"]))

    def make_url(**override) -> str:
        """Собирает ссылку, сохраняя текущие фильтры."""
        params = {
            "category": category,
            "q": q.strip(),
            "in_stock": "1" if stock_only else "",
            "sort": "" if sort == "default" else sort,
            "price_min": price_min.strip(),
            "price_max": price_max.strip(),
        }
        params.update(override)
        clean = {k: v for k, v in params.items() if v not in ("", None)}
        return "/?" + urlencode(clean) if clean else "/"

    filters_active = bool(
        category or q.strip() or stock_only or sort != "default"
        or price_min.strip() or price_max.strip()
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "products": products,
            "total": len(all_products),
            "categories": CATEGORIES,
            "sorts": SORTS,
            "current": category,
            "q": q,
            "stock_only": stock_only,
            "sort": sort,
            "price_min": price_min,
            "price_max": price_max,
            "filters_active": filters_active,
            "make_url": make_url,
            "error": error,
        },
    )