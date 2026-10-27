"""Переменные в текстах сообщений: $order_id, $product, $quantity, $amount, $value, $fields, $date."""
from __future__ import annotations

from datetime import datetime
from string import Template

from .api import Order

VARIABLES = {
    "$order_id": "ID заказа",
    "$product": "название товара",
    "$quantity": "количество",
    "$amount": "сумма заказа, ₽",
    "$value": "выданное значение (ключи через перевод строки)",
    "$fields": "поля, заполненные покупателем",
    "$date": "текущая дата и время",
}


def render(text: str, order: Order | None = None, values: list[str] | None = None) -> str:
    if not text:
        return ""
    data = {"date": datetime.now().strftime("%d.%m.%Y %H:%M")}
    if order is not None:
        data |= {
            "order_id": order.id,
            "product": order.product_title,
            "quantity": str(order.quantity),
            "amount": f"{order.amount:g}",
            "fields": "\n".join(f"{f.label}: {f.value}" for f in order.field_values),
        }
    if values is not None:
        data["value"] = "\n".join(values)
    return Template(text).safe_substitute(data)
