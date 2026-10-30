import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("shop")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
IMG_DIR = STATIC_DIR / "img"
IMG_DIR.mkdir(parents=True, exist_ok=True)
IMG_EXTENSIONS = (".webp", ".jpg", ".jpeg", ".png")

ONEC_BASE_URL = os.getenv("ONEC_BASE_URL", "http://127.0.0.1/ERP-Shop-demo/hs/shop")
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
ORDER_STEPS = ["Новый", "Собран", "Отправлен", "Выполнен"]

CART_COOKIE = "cart"
CART_MAX_QTY = 99
CATALOG_TTL = 5.0  # секунд

# Учебная версия 1С допускает одно подключение: запросы к 1С идут строго по одному
ONEC_LOCK = asyncio.Lock()
_catalog_cache: dict = {"time": 0.0, "data": []}

app = FastAPI(title="Coffee Shop", description="Витрина магазина поверх 1С")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["money"] = lambda value: "{:,.0f}".format(value).replace(",", " ")


# ---------- Обмен с 1С ----------

async def onec_request(method: str, path: str, **kwargs) -> httpx.Response:
    """Один запрос к 1С. При ответе про лимит подключений повторяет попытку."""
    url = f"{ONEC_BASE_URL}{path}"
    async with ONEC_LOCK:
        response = None
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=ONEC_TIMEOUT) as client:
                response = await client.request(method, url, **kwargs)
            if response.status_code == 500 and "подключений" in response.text and attempt < 2:
                await asyncio.sleep(1.5)
                continue
            break
        return response


def find_image(sku: str) -> str | None:
    """Ищет фото товара в static/img по артикулу."""
    for ext in IMG_EXTENSIONS:
        if (IMG_DIR / f"{sku}{ext}").exists():
            return f"/static/img/{sku}{ext}"
    return None


async def fetch_catalog(force: bool = False) -> list[dict]:
    """Каталог из 1С (кэш на несколько секунд)."""
    now = time.monotonic()
    if not force and _catalog_cache["data"] and now - _catalog_cache["time"] < CATALOG_TTL:
        return list(_catalog_cache["data"])
    response = await onec_request("GET", "/catalog")
    response.raise_for_status()
    products = response.json()
    for product in products:
        product["category"] = product["sku"].split("-")[0]
        product["name"] = product["name"].strip()
        product["image"] = find_image(product["sku"])
    _catalog_cache["time"] = now
    _catalog_cache["data"] = products
    return list(products)


# ---------- Вспомогательное ----------

def to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "on", "yes"}


def to_float(value: str) -> float | None:
    try:
        return float(value.replace(",", ".").strip())
    except ValueError:
        return None


def safe_next(url: str) -> str:
    """Редирект только на адреса этого же сайта."""
    return url if url.startswith("/") and not url.startswith("//") else "/"


def read_cart(request: Request) -> dict[str, int]:
    raw = request.cookies.get(CART_COOKIE, "")
    try:
        data = json.loads(unquote(raw))
    except ValueError:
        return {}
    cart: dict[str, int] = {}
    if isinstance(data, dict):
        for sku, qty in data.items():
            if isinstance(sku, str) and type(qty) is int and 0 < qty <= CART_MAX_QTY:
                cart[sku] = qty
    return cart


def write_cart(response: Response, cart: dict[str, int]) -> None:
    if cart:
        response.set_cookie(
            CART_COOKIE, quote(json.dumps(cart)),
            max_age=60 * 60 * 24 * 7, httponly=True, samesite="lax",
        )
    else:
        response.delete_cookie(CART_COOKIE)


def page(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("cart_count", sum(read_cart(request).values()))
    return templates.TemplateResponse(request, name, context, status_code=status_code)


# ---------- Служебные адреса ----------

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    """Проверка связи с 1С (пригодится для автотестов)."""
    try:
        await fetch_catalog(force=True)
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("1С недоступна: %r", exc)
        return Response(status_code=503, content="1С недоступна")
    return {"status": "ok"}


@app.get("/api/catalog")
async def api_catalog():
    """Каталог в виде JSON (данные берутся из 1С)."""
    return await fetch_catalog()


# ---------- Каталог ----------

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
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")

    return page(
        request,
        "index.html",
        products=products,
        total=len(all_products),
        categories=CATEGORIES,
        sorts=SORTS,
        current=category,
        q=q,
        stock_only=stock_only,
        sort=sort,
        price_min=price_min,
        price_max=price_max,
        filters_active=filters_active,
        make_url=make_url,
        error=error,
        cart=read_cart(request),
        here=here,
    )


# ---------- Корзина ----------

async def build_cart(cart: dict[str, int]):
    catalog = {p["sku"]: p for p in await fetch_catalog()}
    lines, total = [], 0.0
    for sku, qty in cart.items():
        product = catalog.get(sku)
        if product is None:
            continue
        line_sum = product["price"] * qty
        lines.append({
            "sku": sku,
            "name": product["name"],
            "unit": product["unit"],
            "price": product["price"],
            "qty": qty,
            "sum": line_sum,
            "stock": product["stock"],
            "ok": qty <= product["stock"],
        })
        total += line_sum
    return lines, total


async def render_cart(request: Request, error: str | None = None,
                      form: dict | None = None, status_code: int = 200):
    cart = read_cart(request)
    lines, total = [], 0.0
    if cart:
        try:
            lines, total = await build_cart(cart)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("Ошибка обращения к 1С: %r", exc)
            error = error or "Не удалось получить цены из 1С. Попробуйте позже."
    can_checkout = bool(lines) and all(line["ok"] for line in lines)
    return page(
        request, "cart.html", status_code=status_code,
        lines=lines, total=total, error=error, form=form or {}, can_checkout=can_checkout,
    )


@app.get("/cart", response_class=HTMLResponse)
async def cart_view(request: Request):
    return await render_cart(request)


@app.post("/cart/add")
async def cart_add(
    request: Request,
    sku: str = Form(...),
    qty: int = Form(1),
    next: str = Form("/"),
):
    cart = read_cart(request)
    try:
        catalog = {p["sku"]: p for p in await fetch_catalog()}
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Ошибка обращения к 1С: %r", exc)
        return RedirectResponse(safe_next(next), status_code=303)
    product = catalog.get(sku)
    if product and product["stock"] > 0 and qty > 0:
        limit = min(int(product["stock"]), CART_MAX_QTY)
        cart[sku] = min(cart.get(sku, 0) + qty, limit)
    response = RedirectResponse(safe_next(next), status_code=303)
    write_cart(response, cart)
    return response


@app.post("/cart/update")
async def cart_update(request: Request, sku: str = Form(...), qty: int = Form(0)):
    cart = read_cart(request)
    if sku in cart:
        if qty <= 0:
            del cart[sku]
        else:
            cart[sku] = min(qty, CART_MAX_QTY)
    response = RedirectResponse("/cart", status_code=303)
    write_cart(response, cart)
    return response


@app.post("/checkout")
async def checkout(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
):
    cart = read_cart(request)
    if not cart:
        return RedirectResponse("/cart", status_code=303)

    form = {"name": name.strip(), "email": email.strip(),
            "phone": phone.strip(), "address": address.strip()}
    if not form["name"] or "@" not in form["email"] or not form["address"]:
        return await render_cart(
            request, error="Заполните имя, корректный email и адрес доставки.",
            form=form, status_code=400,
        )

    payload = {
        "customer": form,
        "items": [{"sku": sku, "quantity": qty} for sku, qty in cart.items()],
    }
    try:
        response = await onec_request("POST", "/orders", json=payload)
    except httpx.HTTPError as exc:
        logger.error("Не удалось отправить заказ в 1С: %r", exc)
        return await render_cart(
            request, error="Не удалось связаться с 1С. Заказ не отправлен, попробуйте ещё раз.",
            form=form, status_code=503,
        )

    if response.status_code == 201:
        number = response.json()["order_number"]
        _catalog_cache["time"] = 0.0  # остатки изменились
        redirect = RedirectResponse(
            f"/order/{number}?email={quote(form['email'])}", status_code=303)
        write_cart(redirect, {})
        return redirect

    try:
        data = response.json()
    except ValueError:
        data = {}
    error = "Не удалось оформить заказ. Попробуйте позже."
    if response.status_code == 409:
        parts = [
            f"{item['sku']}: нужно {item['requested']}, доступно {item['available']}"
            for item in data.get("items", [])
        ]
        error = "Недостаточно товара на складе. " + "; ".join(parts)
    elif response.status_code == 400:
        error = data.get("error", error)
    else:
        logger.error("1С ответила %s: %s", response.status_code, response.text[:300])
    return await render_cart(
        request, error=error, form=form,
        status_code=409 if response.status_code == 409 else 400,
    )


# ---------- Заказ и его статус ----------

@app.get("/order", response_class=HTMLResponse)
async def order_lookup(request: Request, number: str = "", email: str = ""):
    if number.strip() and email.strip():
        return RedirectResponse(
            f"/order/{quote(number.strip())}?email={quote(email.strip())}", status_code=303)
    return page(request, "order.html", number=number, email=email)


@app.get("/order/{number}", response_class=HTMLResponse)
async def order_view(request: Request, number: str, email: str = ""):
    if not number.isdigit() or not email.strip():
        return page(request, "order.html", status_code=404, number=number, email=email,
                    error="Укажите номер заказа и email покупателя.")
    try:
        response = await onec_request("GET", f"/orders/{number}", params={"email": email.strip()})
    except httpx.HTTPError as exc:
        logger.error("Ошибка обращения к 1С: %r", exc)
        return page(request, "order.html", status_code=503, number=number, email=email,
                    error="Не удалось связаться с 1С. Попробуйте позже.")

    if response.status_code == 200:
        order = response.json()
        status = order["status"]
        step_index = ORDER_STEPS.index(status) if status in ORDER_STEPS else -1
        return page(request, "order.html", order=order, steps=ORDER_STEPS,
                    step_index=step_index, number=number, email=email)
    return page(request, "order.html", status_code=404, number=number, email=email,
                error="Заказ не найден. Проверьте номер и email.")