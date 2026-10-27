"""Шина событий: ядро генерирует, Telegram и плагины подписываются."""
from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from collections.abc import Callable
from enum import StrEnum
from typing import Any

logger = logging.getLogger("lootlyhub.events")


class Event(StrEnum):
    STARTED = "started"                    # (hub)
    NEW_ORDER = "new_order"                # (hub, order)
    ORDER_DELIVERED = "order_delivered"    # (hub, order, values, auto: bool)
    ORDER_CANCELLED = "order_cancelled"    # (hub, order, reason)
    NO_STOCK = "no_stock"                  # (hub, order, goods_file)
    DELIVERY_FAILED = "delivery_failed"    # (hub, order, error)
    NEW_MESSAGE = "new_message"            # (hub, order, message)
    COMMAND = "command"                    # (hub, order, message, autoresponse) — сработал автоответ
    PRODUCT_HIDDEN = "product_hidden"      # (hub, product_id, hidden: bool)


Handler = Callable[..., Any]


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event: Event | str, handler: Handler | None = None):
        """``bus.on(Event.NEW_ORDER, fn)`` или как декоратор ``@bus.on(Event.NEW_ORDER)``."""
        def register(fn: Handler) -> Handler:
            self._handlers[str(event)].append(fn)
            return fn
        return register(handler) if handler else register

    def has(self, event: Event | str) -> bool:
        return bool(self._handlers[str(event)])

    async def emit(self, event: Event | str, *args: Any) -> None:
        # Ошибка одного обработчика (например, кривого плагина) не ломает остальные.
        for handler in list(self._handlers[str(event)]):
            try:
                result = handler(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Ошибка в обработчике %s события %s",
                                 getattr(handler, "__qualname__", handler), event)
