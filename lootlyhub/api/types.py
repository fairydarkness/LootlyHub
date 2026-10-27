from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class OrderStatus(StrEnum):
    PAID = "paid"
    PROCESSING = "processing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# Значения фильтра status у GET /orders отличаются от статусов заказа.
ORDER_FILTERS = ("new", "processing", "awaiting", "completed", "cancelled", "all")

STATUS_NAMES = {
    OrderStatus.PAID: "🆕 оплачен",
    OrderStatus.PROCESSING: "⚙️ в работе",
    OrderStatus.AWAITING_CONFIRMATION: "⏳ ждёт подтверждения",
    OrderStatus.COMPLETED: "✅ завершён",
    OrderStatus.CANCELLED: "❌ отменён",
}


@dataclass(slots=True)
class FieldValue:
    field_id: str
    label: str
    value: str


@dataclass(slots=True)
class Order:
    id: str
    status: str
    product_id: str
    product_title: str
    quantity: int
    amount: float
    field_values: list[FieldValue]
    assignee_type: str | None
    delivered_value: str | None
    delivered_values: list[str]
    created_at: datetime | None
    taken_at: datetime | None
    delivered_at: datetime | None
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Order:
        return cls(
            id=d["id"],
            status=d.get("status", ""),
            product_id=d.get("productId", ""),
            product_title=d.get("productTitle", ""),
            quantity=int(d.get("quantity") or 1),
            amount=float(d.get("amount") or 0),
            field_values=[FieldValue(f.get("fieldId", ""), f.get("label", ""), f.get("value", ""))
                          for f in d.get("fieldValues") or []],
            assignee_type=d.get("assigneeType"),
            delivered_value=d.get("deliveredValue"),
            delivered_values=list(d.get("deliveredValues") or []),
            created_at=parse_dt(d.get("createdAt")),
            taken_at=parse_dt(d.get("takenAt")),
            delivered_at=parse_dt(d.get("deliveredAt")),
            raw=d,
        )

    @property
    def status_name(self) -> str:
        try:
            return STATUS_NAMES[OrderStatus(self.status)]
        except ValueError:
            return self.status

    def field(self, label_or_id: str) -> str | None:
        for f in self.field_values:
            if label_or_id in (f.field_id, f.label):
                return f.value
        return None


@dataclass(slots=True)
class Message:
    id: str
    sender_role: str  # seller | customer | system
    text: str
    created_at: datetime | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(d["id"], d.get("senderRole", ""), d.get("text", ""), parse_dt(d.get("createdAt")))


@dataclass(slots=True)
class Product:
    id: str
    title: str
    category_id: str
    active: bool
    archived: bool
    external: bool
    price: float
    price_rub: float
    currency_rate_id: str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Product:
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            category_id=d.get("categoryId", ""),
            active=bool(d.get("active")),
            archived=bool(d.get("archived")),
            external=bool(d.get("external")),
            price=float(d.get("price") or 0),
            price_rub=float(d.get("priceRub") or 0),
            currency_rate_id=d.get("currencyRateId") or "",
            raw=d,
        )


@dataclass(slots=True)
class Category:
    id: str
    path_ru: str
    path_en: str
    filters: list[dict[str, Any]]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Category:
        return cls(d["id"], d.get("pathRu", ""), d.get("pathEn", ""), list(d.get("filters") or []))


@dataclass(slots=True)
class CurrencyRate:
    id: str
    code: str
    rate: float
    updated_at: datetime | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CurrencyRate:
        return cls(d["id"], d.get("code", ""), float(d.get("rate") or 0), parse_dt(d.get("updatedAt")))
