"""Загрузка конфигов из configs/*.toml."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path("configs")


class ConfigError(Exception):
    pass


def _load(path: Path, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Нет файла {path}. Скопируйте {path.with_suffix('.example.toml').name} "
                              f"в {path.name} и заполните.")
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


@dataclass(slots=True)
class DataRequestRule:
    field: str            # id или подпись поля
    pattern: re.Pattern | None
    product_id: str = ""  # пусто = для любого товара


@dataclass(slots=True)
class MainConfig:
    api_key: str
    api_proxy: str | None
    orders_interval: float
    messages_interval: float
    track_chats_hours: float

    features: dict[str, bool]

    greeting: str
    after_delivery: str
    no_stock: str
    cancel_reason: str

    tg_enabled: bool
    tg_token: str
    tg_admins: list[int]
    tg_password_hash: str
    tg_proxy: str | None

    webhook_enabled: bool
    webhook_host: str
    webhook_port: int
    webhook_path: str
    webhook_default: bool
    webhook_rules: list[DataRequestRule] = field(default_factory=list)


DEFAULT_FEATURES = {
    "auto_delivery": True,      # автовыдача из goods/ и статичных значений
    "auto_response": True,      # автоответ на команды в чате заказа
    "greeting": True,           # приветствие после взятия заказа
    "after_delivery": True,     # сообщение после выдачи
    "auto_hide": False,         # архивировать товар, когда кончился товар в файле, и возвращать при пополнении
    "auto_cancel_no_stock": False,  # отменять заказ (возврат), если товара нет
    "notify_orders": True,
    "notify_messages": True,
    "notify_delivery": True,
}


def load_main(path: Path | None = None) -> MainConfig:
    path = path or CONFIG_DIR / "main.toml"
    d = _load(path)
    lootly = d.get("lootly", {})
    polling = d.get("polling", {})
    msgs = d.get("messages", {})
    tg = d.get("telegram", {})
    wh = d.get("webhook", {})

    api_key = str(lootly.get("api_key", "")).strip()
    if not api_key:
        raise ConfigError(f"{path}: заполните lootly.api_key (Магазин → Настройки → API ключи).")

    features = dict(DEFAULT_FEATURES)
    for k, v in d.get("features", {}).items():
        if k not in features:
            raise ConfigError(f"{path}: неизвестная опция features.{k}")
        features[k] = bool(v)

    rules = []
    for r in wh.get("rules", []):
        if "field" not in r:
            raise ConfigError(f"{path}: у webhook.rules нужен field")
        try:
            pattern = re.compile(r["pattern"]) if r.get("pattern") else None
        except re.error as e:
            raise ConfigError(f"{path}: плохая регулярка у поля {r['field']!r}: {e}") from e
        rules.append(DataRequestRule(str(r["field"]), pattern, str(r.get("product_id", ""))))

    tg_enabled = bool(tg.get("enabled", False))
    if tg_enabled and not tg.get("token"):
        raise ConfigError(f"{path}: telegram.enabled = true, но telegram.token пустой.")
    if tg_enabled and not tg.get("admins") and not tg.get("password_hash"):
        raise ConfigError(f"{path}: задайте telegram.admins или telegram.password_hash "
                          f"(python main.py hash-password), иначе в ПУ никто не войдёт.")

    return MainConfig(
        api_key=api_key,
        api_proxy=lootly.get("proxy") or None,
        orders_interval=max(3.0, float(polling.get("orders_interval", 10))),
        messages_interval=max(5.0, float(polling.get("messages_interval", 20))),
        track_chats_hours=float(polling.get("track_chats_hours", 72)),
        features=features,
        greeting=msgs.get("greeting", ""),
        after_delivery=msgs.get("after_delivery", ""),
        no_stock=msgs.get("no_stock", ""),
        cancel_reason=msgs.get("cancel_reason", "Нет в наличии"),
        tg_enabled=tg_enabled,
        tg_token=tg.get("token", ""),
        tg_admins=[int(x) for x in tg.get("admins", [])],
        tg_password_hash=tg.get("password_hash", ""),
        tg_proxy=tg.get("proxy") or None,
        webhook_enabled=bool(wh.get("enabled", False)),
        webhook_host=wh.get("host", "0.0.0.0"),
        webhook_port=int(wh.get("port", 8080)),
        webhook_path=wh.get("path", "/lootly/data-request"),
        webhook_default=bool(wh.get("default_verdict", False)),
        webhook_rules=rules,
    )


@dataclass(slots=True)
class DeliveryRule:
    product_id: str = ""
    title: str = ""
    goods: str = ""         # файл в goods/, по строке на единицу товара
    value: str = ""         # статичная выдача (ссылка, инструкция) — не расходуется
    message: str = ""       # своё сообщение после выдачи (иначе messages.after_delivery)
    enabled: bool = True

    def matches(self, product_id: str, title: str) -> bool:
        if self.product_id:
            return self.product_id == product_id
        return bool(self.title) and self.title.casefold() == title.casefold()


def load_delivery(path: Path | None = None) -> list[DeliveryRule]:
    path = path or CONFIG_DIR / "delivery.toml"
    rules = []
    for i, p in enumerate(_load(path, required=False).get("product", []), 1):
        rule = DeliveryRule(
            product_id=str(p.get("id", "")), title=str(p.get("title", "")),
            goods=str(p.get("goods", "")), value=str(p.get("value", "")),
            message=str(p.get("message", "")), enabled=bool(p.get("enabled", True)),
        )
        if not (rule.product_id or rule.title):
            raise ConfigError(f"{path}: у [[product]] №{i} нужен id или title")
        if bool(rule.goods) == bool(rule.value):
            raise ConfigError(f"{path}: у [[product]] №{i} нужно ровно одно из goods / value")
        if rule.goods and ("/" in rule.goods or "\\" in rule.goods or rule.goods.startswith(".")):
            raise ConfigError(f"{path}: goods у [[product]] №{i} — имя файла в goods/, без путей")
        rules.append(rule)
    return rules


@dataclass(slots=True)
class AutoResponse:
    triggers: list[str]
    response: str
    notify: bool = False

    def matches(self, text: str) -> bool:
        t = text.strip().casefold()
        return any(t == trig.casefold() for trig in self.triggers)


def load_autoresponse(path: Path | None = None) -> list[AutoResponse]:
    path = path or CONFIG_DIR / "autoresponse.toml"
    result = []
    for i, c in enumerate(_load(path, required=False).get("command", []), 1):
        triggers = c.get("triggers") or ([c["trigger"]] if c.get("trigger") else [])
        if not triggers or not c.get("response"):
            raise ConfigError(f"{path}: у [[command]] №{i} нужны triggers и response")
        result.append(AutoResponse([str(t) for t in triggers], str(c["response"]), bool(c.get("notify", False))))
    return result
