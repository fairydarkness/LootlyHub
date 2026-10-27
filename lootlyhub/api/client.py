"""Асинхронный клиент Lootly Seller API (https://docs.seller.lootly.net)."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from .. import __version__
from .errors import LootlyError, RateLimited
from .types import Category, CurrencyRate, Message, Order, Product

logger = logging.getLogger("lootlyhub.api")

BASE_URL = "https://api.seller.lootly.net/v1"


def _retry_after(response: httpx.Response) -> float:
    """Сколько ждать после 429. RateLimit-Reset — секунды до сброса окна (или unix-время)."""
    for header in ("RateLimit-Reset", "Retry-After"):
        value = response.headers.get(header)
        if not value:
            continue
        try:
            n = float(value)
        except ValueError:
            continue
        if n > 1_000_000_000:  # пришло абсолютное unix-время
            n -= time.time()
        return max(1.0, min(n, 300.0))
    return 5.0


class LootlyAPI:
    """
    Тонкая обёртка над REST API. Все методы возвращают разобранные объекты
    из :mod:`lootlyhub.api.types` или кидают :class:`LootlyError`.

    Мутации take / deliver / cancel принимают ``idempotency_key`` —
    повтор с тем же ключом не выполнит действие дважды, поэтому их безопасно ретраить.
    """

    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 15.0,
                 max_retries: int = 3, proxy: str | None = None):
        if not api_key.startswith("lootly_sk_"):
            logger.warning("API-ключ не похож на ключ Lootly (ожидается префикс lootly_sk_).")
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            proxy=proxy,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": f"LootlyHub/{__version__}",
            },
        )
        self.rate_limit_remaining: int | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> LootlyAPI:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---------------------------------------------------------------- transport

    async def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                      files: dict | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        # Сетевые ошибки и 5xx повторяем только там, где повтор не задвоит действие.
        safe = method == "GET" or idempotency_key is not None
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._http.request(method, path, params=params, json=json,
                                                    files=files, headers=headers)
            except httpx.TransportError as e:
                if safe and attempt <= self.max_retries:
                    delay = 2 ** attempt
                    logger.warning("%s %s: сетевая ошибка (%s), повтор через %ss", method, path, e, delay)
                    await asyncio.sleep(delay)
                    continue
                raise LootlyError("NETWORK_ERROR", 0, path) from e

            if (remaining := response.headers.get("RateLimit-Remaining")) is not None:
                try:
                    self.rate_limit_remaining = int(remaining)
                except ValueError:
                    pass

            if response.status_code == 429:
                delay = _retry_after(response)
                # 429 значит, что запрос не выполнялся — повторять безопасно всегда.
                if attempt <= self.max_retries:
                    logger.warning("%s %s: rate limit, жду %.0fс", method, path, delay)
                    await asyncio.sleep(delay)
                    continue
                raise RateLimited(delay, path)

            if response.status_code >= 500 and safe and attempt <= self.max_retries:
                delay = 2 ** attempt
                logger.warning("%s %s: HTTP %s, повтор через %ss", method, path, response.status_code, delay)
                await asyncio.sleep(delay)
                continue

            try:
                body = response.json()
            except ValueError:
                raise LootlyError("BAD_RESPONSE", response.status_code, path) from None

            if response.is_success and body.get("success"):
                return body.get("data") or {}
            code = (body.get("error") or {}).get("code") or "UNKNOWN_ERROR"
            raise LootlyError(code, response.status_code, path)

    # ----------------------------------------------------------------- catalog

    async def list_products(self, page: int = 1, limit: int = 100) -> tuple[list[Product], int]:
        data = await self.request("GET", "/products", params={"page": page, "limit": limit})
        return [Product.from_dict(p) for p in data.get("products", [])], int(data.get("total") or 0)

    async def all_products(self) -> list[Product]:
        result: list[Product] = []
        page = 1
        while True:
            products, total = await self.list_products(page=page, limit=100)
            result.extend(products)
            if not products or len(result) >= total:
                return result
            page += 1

    async def get_product(self, product_id: str) -> Product:
        data = await self.request("GET", f"/products/{product_id}")
        return Product.from_dict(data["product"])

    async def create_product(self, body: dict[str, Any]) -> Product:
        """Все поля ProductInput обязательны — см. документацию."""
        data = await self.request("POST", "/products", json=body)
        return Product.from_dict(data["product"])

    async def update_product(self, product_id: str, body: dict[str, Any]) -> Product:
        """Полная замена товара (PUT): передавайте все поля, как при создании."""
        data = await self.request("PUT", f"/products/{product_id}", json=body)
        return Product.from_dict(data["product"])

    async def archive_product(self, product_id: str, archived: bool = True) -> Product:
        data = await self.request("POST", f"/products/{product_id}/archive", json={"archived": archived})
        return Product.from_dict(data["product"])

    async def list_categories(self) -> list[Category]:
        data = await self.request("GET", "/categories")
        return [Category.from_dict(c) for c in data.get("categories", [])]

    async def upload_media(self, file: str | Path | BinaryIO, filename: str | None = None) -> str:
        """Загружает картинку (до 10 МБ) и возвращает id медиа для imageId."""
        if isinstance(file, (str, Path)):
            path = Path(file)
            with path.open("rb") as f:
                content = f.read()
            filename = filename or path.name
        else:
            content = file.read()
            filename = filename or "image"
        data = await self.request("POST", "/media", files={"file": (filename, content)})
        return data["id"]

    async def list_currency_rates(self) -> list[CurrencyRate]:
        data = await self.request("GET", "/currency-rates")
        return [CurrencyRate.from_dict(r) for r in data.get("rates", [])]

    # ------------------------------------------------------------- fulfillment

    async def list_orders(self, status: str = "new", product_id: str | None = None,
                          page: int = 1, limit: int = 100) -> tuple[list[Order], int]:
        """status: new | processing | awaiting | completed | cancelled | all."""
        data = await self.request("GET", "/orders", params={
            "status": status, "productId": product_id, "page": page, "limit": limit,
        })
        return [Order.from_dict(o) for o in data.get("orders", [])], int(data.get("total") or 0)

    async def all_orders(self, status: str = "new", product_id: str | None = None,
                         max_pages: int = 10) -> list[Order]:
        result: list[Order] = []
        for page in range(1, max_pages + 1):
            orders, total = await self.list_orders(status, product_id, page=page, limit=100)
            result.extend(orders)
            if not orders or len(result) >= total:
                break
        return result

    async def get_order(self, order_id: str) -> Order:
        data = await self.request("GET", f"/orders/{order_id}")
        return Order.from_dict(data["order"])

    async def take_order(self, order_id: str, idempotency_key: str | None = None) -> Order:
        """paid → processing, исполнитель — api."""
        data = await self.request("POST", f"/orders/{order_id}/take",
                                  idempotency_key=idempotency_key or f"take-{order_id}")
        return Order.from_dict(data["order"])

    async def deliver_order(self, order_id: str, values: list[str] | str | None = None,
                            idempotency_key: str | None = None) -> Order:
        """
        processing → awaiting_confirmation. ``values`` — один ключ (строка) или список
        ровно из ``quantity`` значений для заказа с quantity > 1.
        """
        body: dict[str, Any] = {}
        if isinstance(values, str):
            body["deliveredValue"] = values
        elif values:
            if len(values) == 1:
                body["deliveredValue"] = values[0]
            else:
                body["deliveredValues"] = list(values)
        data = await self.request("POST", f"/orders/{order_id}/deliver", json=body,
                                  idempotency_key=idempotency_key or f"deliver-{order_id}")
        return Order.from_dict(data["order"])

    async def cancel_order(self, order_id: str, reason: str | None = None,
                           idempotency_key: str | None = None) -> Order:
        """Возврат денег покупателю, статус → cancelled."""
        data = await self.request("POST", f"/orders/{order_id}/cancel",
                                  json={"reason": reason} if reason else {},
                                  idempotency_key=idempotency_key or f"cancel-{order_id}")
        return Order.from_dict(data["order"])

    async def list_messages(self, order_id: str, cursor: str | None = None,
                            limit: int = 50) -> tuple[list[Message], str | None]:
        data = await self.request("GET", f"/orders/{order_id}/messages",
                                  params={"cursor": cursor, "limit": limit})
        return [Message.from_dict(m) for m in data.get("messages", [])], data.get("nextCursor") or None

    async def send_message(self, order_id: str, text: str) -> Message:
        data = await self.request("POST", f"/orders/{order_id}/messages", json={"text": text})
        return Message.from_dict(data["message"])
