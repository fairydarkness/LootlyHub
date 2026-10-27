"""
Пример плагина LootlyHub. Файлы с "_" в начале имени не загружаются —
переименуйте в example.py, чтобы включить.
"""
import logging

from lootlyhub.events import Event

NAME = "Пример"
VERSION = "1.0"
AUTHOR = "@fairydarkns"
DESCRIPTION = "Логирует крупные заказы, выдаёт промокод для одного товара, проверяет никнейм."

logger = logging.getLogger("plugin.example")

PROMO_PRODUCT_ID = "000000000000000000000000"  # поменяйте на id своего товара


def setup(hub):
    @hub.events.on(Event.NEW_ORDER)
    async def big_order(hub, order):
        if order.amount >= 5000:
            logger.info("Крупный заказ %s на %g ₽", order.id, order.amount)

    # Своя выдача: вернуть строку/список значений или None, чтобы отдать заказ обычной автовыдаче.
    async def promo(order):
        import secrets
        return [f"PROMO-{secrets.token_hex(4).upper()}" for _ in range(order.quantity)]

    hub.add_deliverer(promo, product_id=PROMO_PRODUCT_ID)

    # Проверка поля «запрос данных» (нужен включённый [webhook]). None — «не моё поле».
    def nickname(product_id, field_id, label, value):
        if label != "Никнейм":
            return None
        return 3 <= len(value) <= 16 and value.isascii()

    hub.add_data_validator(nickname)
