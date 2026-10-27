"""Ядро LootlyHub: опрос заказов и чатов, автовыдача, автоответ, автоскрытие товаров."""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from . import config as cfg
from .api import LootlyAPI, LootlyError, Message, Order, OrderStatus
from .events import Event, EventBus
from .goods import Goods
from .plugins import PluginInfo, load_plugins
from .storage import Storage
from .templates import render

logger = logging.getLogger("lootlyhub")

# fn(order) -> строка / список значений для выдачи, или None, если плагин этот заказ не выдаёт.
Deliverer = Callable[[Order], "list[str] | str | None | Awaitable[list[str] | str | None]"]
# fn(product_id, field_id, label, value) -> True/False, или None — «не моё поле».
DataValidator = Callable[[str, str, str, str], "bool | None | Awaitable[bool | None]"]


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


class Hub:
    def __init__(self, main: cfg.MainConfig, storage: Storage | None = None, goods: Goods | None = None):
        self.cfg = main
        self.api = LootlyAPI(main.api_key, proxy=main.api_proxy)
        self.storage = storage or Storage()
        self.goods = goods or Goods()
        self.events = EventBus()
        self.delivery_rules: list[cfg.DeliveryRule] = []
        self.autoresponses: list[cfg.AutoResponse] = []
        self.plugins: list[PluginInfo] = []
        self.deliverers: list[tuple[str | None, Deliverer]] = []
        self.data_validators: list[DataValidator] = []
        self.started_at = time.time()
        self.running = False
        self._order_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: list[asyncio.Task] = []
        self.reload()

    # ------------------------------------------------------------ settings

    def reload(self) -> None:
        """Перечитывает delivery.toml и autoresponse.toml без перезапуска."""
        self.delivery_rules = cfg.load_delivery()
        self.autoresponses = cfg.load_autoresponse()
        logger.info("Правил автовыдачи: %d, команд автоответа: %d",
                    len(self.delivery_rules), len(self.autoresponses))

    @property
    def features(self) -> dict[str, bool]:
        return self.cfg.features | self.storage.get("features", {})

    def feature(self, name: str) -> bool:
        return self.features.get(name, False)

    def set_feature(self, name: str, value: bool) -> None:
        if name not in cfg.DEFAULT_FEATURES:
            raise KeyError(name)
        overrides = self.storage.get("features", {})
        overrides[name] = value
        self.storage.set("features", overrides)

    # ------------------------------------------------------- plugin hooks

    def add_deliverer(self, fn: Deliverer, product_id: str | None = None) -> None:
        """Своя логика выдачи (например, генерация кода через API поставщика)."""
        self.deliverers.append((product_id, fn))

    def add_data_validator(self, fn: DataValidator) -> None:
        """Проверка полей «запрос данных» для вебхука."""
        self.data_validators.append(fn)

    def rule_for(self, order: Order) -> cfg.DeliveryRule | None:
        for rule in self.delivery_rules:
            if rule.enabled and rule.matches(order.product_id, order.product_title):
                return rule
        return None

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.plugins = await load_plugins(self)
        products = await self.api.all_products()  # заодно проверяем ключ
        external = sum(p.external for p in products)
        logger.info("Ключ рабочий. Товаров: %d, под автоматизацией: %d", len(products), external)
        if not external:
            logger.warning("Нет товаров с включённой внешней автоматизацией (external) — "
                           "заказы через API приходить не будут.")
        self.running = True
        await self.events.emit(Event.STARTED, self)
        self._tasks = [
            asyncio.create_task(self._loop(self.poll_orders, self.cfg.orders_interval), name="orders"),
            asyncio.create_task(self._loop(self.poll_messages, self.cfg.messages_interval), name="messages"),
        ]

    async def stop(self) -> None:
        self.running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.api.close()
        self.storage.close()

    async def _loop(self, fn: Callable[[], Awaitable[None]], interval: float) -> None:
        while self.running:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except LootlyError as e:
                logger.error("%s: %s", fn.__name__, e)
                if e.code in ("API_KEY_INVALID", "API_KEY_REQUIRED"):
                    logger.critical("API-ключ отозван или неверен — проверьте configs/main.toml")
                    interval_ = max(interval, 60)
                    await asyncio.sleep(interval_)
                    continue
            except Exception:
                logger.exception("Ошибка в цикле %s", fn.__name__)
            await asyncio.sleep(interval)

    # -------------------------------------------------------------- orders

    async def poll_orders(self) -> None:
        await self._recover_reserved()
        for order in await self.api.all_orders("new"):
            if self.storage.add_order(order.id, order.product_id, order.product_title):
                logger.info("Новый заказ %s: %s x%d, %g ₽", order.id, order.product_title,
                            order.quantity, order.amount)
                await self.events.emit(Event.NEW_ORDER, self, order)
                await self.auto_deliver(order)
            elif self.storage.order(order.id)["state"] == "no_stock":
                # Товар мог появиться — пробуем снова, без повторных уведомлений.
                await self.auto_deliver(order, quiet=True)
        await self._sync_hidden_products()

    async def auto_deliver(self, order: Order, quiet: bool = False) -> None:
        if not self.feature("auto_delivery"):
            self.storage.set_order_state(order.id, "manual")
            return
        async with self._order_locks[order.id]:
            row = self.storage.order(order.id)
            if row and row["state"] not in ("new", "no_stock"):
                return

            for product_id, deliverer in self.deliverers:
                if product_id and product_id != order.product_id:
                    continue
                values = await _maybe_await(deliverer(order))
                if values:
                    values = [values] if isinstance(values, str) else list(values)
                    self.storage.set_order_state(order.id, "reserved", reserved=values)
                    await self._fulfill(order, values, goods=None, message="")
                    return

            rule = self.rule_for(order)
            if rule is None:
                self.storage.set_order_state(order.id, "manual")
                return

            if rule.value:
                values = [rule.value]
            else:
                values = await self.goods.take(rule.goods, order.quantity)
                if values is None:
                    await self._no_stock(order, rule, quiet)
                    return
            self.storage.set_order_state(order.id, "reserved", goods=rule.goods or "", reserved=values)
            await self._fulfill(order, values, goods=rule.goods or None, message=rule.message)
            if rule.goods:
                await self._check_hide(rule.goods)

    async def _no_stock(self, order: Order, rule: cfg.DeliveryRule, quiet: bool) -> None:
        self.storage.set_order_state(order.id, "no_stock")
        if quiet:
            return
        left = self.goods.count(rule.goods)
        logger.warning("Заказ %s: в goods/%s %d шт., нужно %d", order.id, rule.goods, left, order.quantity)
        await self.events.emit(Event.NO_STOCK, self, order, rule.goods)
        if self.feature("auto_cancel_no_stock"):
            await self._cancel(order.id, self.cfg.cancel_reason)
        elif self.cfg.no_stock:
            await self.send_message(order.id, render(self.cfg.no_stock, order))
        await self._check_hide(rule.goods)

    async def _fulfill(self, order: Order, values: list[str], goods: str | None, message: str) -> bool:
        """take → приветствие → deliver → сообщение. Значения уже записаны в БД как reserved."""
        try:
            if order.status == OrderStatus.PAID:
                taken = await self._take(order)
                if taken is None:
                    await self._release(order.id, goods, values, "manual")
                    return False
                order = taken
                if self.feature("greeting") and self.cfg.greeting:
                    await self.send_message(order.id, render(self.cfg.greeting, order))
            order = await self.api.deliver_order(order.id, values)
        except LootlyError as e:
            if e.retryable or e.code == "NETWORK_ERROR":
                logger.warning("Заказ %s: выдача отложена (%s), повторю позже", order.id, e)
                return False
            current = await self._safe_get(order.id)
            if current and current.status in (OrderStatus.AWAITING_CONFIRMATION, OrderStatus.COMPLETED):
                order = current  # выдача уже прошла (например, ответ потерялся)
            else:
                logger.error("Заказ %s: не удалось выдать: %s", order.id, e)
                state = "cancelled" if current and current.status == OrderStatus.CANCELLED else "failed"
                await self._release(order.id, goods, values, state)
                await self.events.emit(Event.DELIVERY_FAILED, self, current or order, e)
                return False

        self.storage.set_order_state(order.id, "delivered")
        logger.info("Заказ %s выдан (%d шт.)", order.id, len(values))
        text = message or self.cfg.after_delivery
        if self.feature("after_delivery") and text:
            await self.send_message(order.id, render(text, order, values))
        await self.events.emit(Event.ORDER_DELIVERED, self, order, values, True)
        return True

    async def _take(self, order: Order) -> Order | None:
        """Берёт заказ. None — если его уже взял человек или он отменён."""
        try:
            return await self.api.take_order(order.id)
        except LootlyError as e:
            if e.code != "ORDER_INVALID_STATUS":
                raise
        current = await self.api.get_order(order.id)
        if current.status == OrderStatus.PROCESSING and current.assignee_type == "api":
            return current  # мы же и взяли в прошлый раз
        logger.info("Заказ %s уже не наш (статус %s, исполнитель %s)", order.id,
                    current.status, current.assignee_type)
        return None

    async def _release(self, order_id: str, goods: str | None, values: list[str], state: str) -> None:
        """Возвращает зарезервированные значения в файл товаров."""
        if goods:
            await self.goods.put_back(goods, values)
            logger.info("Заказ %s: %d шт. возвращено в goods/%s", order_id, len(values), goods)
        self.storage.set_order_state(order_id, state, clear_reserved=True)

    async def _safe_get(self, order_id: str) -> Order | None:
        try:
            return await self.api.get_order(order_id)
        except LootlyError:
            return None

    async def _recover_reserved(self) -> None:
        """Доводит до конца выдачи, прерванные падением или сетью."""
        for row in self.storage.reserved_orders():
            async with self._order_locks[row["id"]]:
                values = self.storage.reserved_values(row)
                order = await self._safe_get(row["id"])
                if order is None:
                    continue
                if order.status in (OrderStatus.AWAITING_CONFIRMATION, OrderStatus.COMPLETED):
                    self.storage.set_order_state(order.id, "delivered")
                elif order.status == OrderStatus.CANCELLED:
                    await self._release(order.id, row["goods"], values, "cancelled")
                else:
                    rule = self.rule_for(order)
                    await self._fulfill(order, values, row["goods"] or None, rule.message if rule else "")

    # ----------------------------------------------------- manual actions

    async def deliver(self, order_id: str, values: list[str]) -> Order:
        """Ручная выдача (из Telegram)."""
        async with self._order_locks[order_id]:
            order = await self.api.get_order(order_id)
            if order.status == OrderStatus.PAID:
                order = await self.api.take_order(order_id)
            if order.quantity > 1 and len(values) not in (1, order.quantity):
                raise ValueError(f"Нужно 1 или {order.quantity} значений, а передано {len(values)}")
            order = await self.api.deliver_order(order_id, values, idempotency_key=f"manual-deliver-{order_id}")
            self.storage.add_order(order.id, order.product_id, order.product_title)
            self.storage.set_order_state(order_id, "delivered")
        await self.events.emit(Event.ORDER_DELIVERED, self, order, values, False)
        return order

    async def cancel(self, order_id: str, reason: str | None = None) -> Order:
        """Отмена с возвратом денег. Зарезервированные ключи возвращаются в файл."""
        async with self._order_locks[order_id]:
            return await self._cancel(order_id, reason)

    async def _cancel(self, order_id: str, reason: str | None) -> Order:
        order = await self.api.cancel_order(order_id, reason)
        row = self.storage.order(order_id)
        if row and row["state"] == "reserved":
            await self._release(order_id, row["goods"], self.storage.reserved_values(row), "cancelled")
        elif row:
            self.storage.set_order_state(order_id, "cancelled")
        logger.info("Заказ %s отменён: %s", order_id, reason or "без причины")
        await self.events.emit(Event.ORDER_CANCELLED, self, order, reason)
        return order

    async def send_message(self, order_id: str, text: str) -> Message | None:
        if not text.strip():
            return None
        try:
            msg = await self.api.send_message(order_id, text[:4000])
        except LootlyError as e:
            logger.error("Заказ %s: не удалось отправить сообщение: %s", order_id, e)
            return None
        self.storage.mark_messages_seen(order_id, [msg.id])
        return msg

    # ------------------------------------------------------------ auto-hide

    async def _check_hide(self, goods: str) -> None:
        """Архивирует товары, у которых закончился файл goods (если включено auto_hide)."""
        if not self.feature("auto_hide") or self.goods.count(goods) > 0:
            return
        hidden = set(self.storage.get("hidden_products", []))
        for rule in self.delivery_rules:
            if rule.goods != goods or not rule.product_id or rule.product_id in hidden:
                continue
            try:
                await self.api.archive_product(rule.product_id, True)
            except LootlyError as e:
                logger.error("Не удалось скрыть товар %s: %s", rule.product_id, e)
                continue
            hidden.add(rule.product_id)
            logger.info("Товар %s скрыт: закончился goods/%s", rule.product_id, goods)
            await self.events.emit(Event.PRODUCT_HIDDEN, self, rule.product_id, True)
        self.storage.set("hidden_products", sorted(hidden))

    async def _sync_hidden_products(self) -> None:
        """Возвращает из архива скрытые нами товары, когда их пополнили."""
        hidden = set(self.storage.get("hidden_products", []))
        if not hidden:
            return
        for rule in self.delivery_rules:
            if rule.product_id in hidden and rule.goods and self.goods.count(rule.goods) > 0:
                try:
                    await self.api.archive_product(rule.product_id, False)
                except LootlyError as e:
                    logger.error("Не удалось вернуть товар %s: %s", rule.product_id, e)
                    continue
                hidden.discard(rule.product_id)
                logger.info("Товар %s возвращён из архива: goods/%s пополнен", rule.product_id, rule.goods)
                await self.events.emit(Event.PRODUCT_HIDDEN, self, rule.product_id, False)
        self.storage.set("hidden_products", sorted(hidden))

    # ------------------------------------------------------------ messages

    async def poll_messages(self) -> None:
        if not (self.feature("auto_response") or self.feature("notify_messages")
                or self.events.has(Event.NEW_MESSAGE)):
            return
        since = datetime.now(timezone.utc) - timedelta(hours=self.cfg.track_chats_hours)
        orders: list[Order] = []
        for status in ("new", "processing", "awaiting"):
            orders += await self.api.all_orders(status, max_pages=2)
        for order in orders:
            if order.created_at and order.created_at < since:
                continue
            try:
                await self._check_chat(order)
            except LootlyError as e:
                if e.code in ("CHAT_NOT_FOUND", "ORDER_NOT_FOUND"):
                    continue
                raise

    async def _check_chat(self, order: Order) -> None:
        messages, _ = await self.api.list_messages(order.id, limit=50)
        if not messages:
            return
        row = self.storage.order(order.id)
        first_time = not self.storage.has_messages(order.id)
        # Для чата, который видим впервые, новыми считаем только сообщения после того,
        # как бот узнал о заказе (или запустился) — чтобы не отвечать на старую переписку.
        border = row["first_seen"] if row else self.started_at
        new = []
        for m in sorted(messages, key=lambda m: m.created_at or datetime.min.replace(tzinfo=timezone.utc)):
            if self.storage.is_message_seen(m.id):
                continue
            if first_time and m.created_at and m.created_at.timestamp() < border:
                continue
            new.append(m)
        self.storage.mark_messages_seen(order.id, [m.id for m in messages])

        for m in new:
            if m.sender_role != "customer":
                continue
            logger.info("Сообщение в заказе %s: %s", order.id, m.text[:200])
            await self.events.emit(Event.NEW_MESSAGE, self, order, m)
            if self.feature("auto_response"):
                for cmd in self.autoresponses:
                    if cmd.matches(m.text):
                        await self.send_message(order.id, render(cmd.response, order))
                        await self.events.emit(Event.COMMAND, self, order, m, cmd)
                        break

    # ------------------------------------------------------------- webhook

    async def check_data_request(self, product_id: str, field_id: str, label: str, value: str) -> bool:
        for validator in self.data_validators:
            verdict = await _maybe_await(validator(product_id, field_id, label, value))
            if verdict is not None:
                return bool(verdict)
        for rule in self.cfg.webhook_rules:
            if rule.field not in (field_id, label) or (rule.product_id and rule.product_id != product_id):
                continue
            return rule.pattern.fullmatch(value) is not None if rule.pattern else bool(value.strip())
        return self.cfg.webhook_default
