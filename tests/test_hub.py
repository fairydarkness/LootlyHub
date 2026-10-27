import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from lootlyhub.config import load_main
from lootlyhub.events import Event
from lootlyhub.hub import Hub
from lootlyhub.tg.auth import check_password, hash_password

from .fake_lootly import FakeLootly

MAIN = """
[lootly]
api_key = "lootly_sk_test"
[features]
greeting = true
[messages]
greeting = "Привет, заказ $order_id"
after_delivery = "Спасибо! $value"
[webhook]
default_verdict = false
[[webhook.rules]]
field = "Логин"
pattern = "^[a-z0-9_]{3,16}$"
"""

DELIVERY = """
[[product]]
id = "p1"
goods = "keys.txt"

[[product]]
title = "Гайд"
value = "https://example.com/guide"
"""

AUTORESPONSE = """
[[command]]
triggers = ["!help"]
response = "Заказ $order_id: $product"
"""


@pytest.fixture
async def env(tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/main.toml").write_text(MAIN, encoding="utf-8")
    (tmp_path / "configs/delivery.toml").write_text(DELIVERY, encoding="utf-8")
    (tmp_path / "configs/autoresponse.toml").write_text(AUTORESPONSE, encoding="utf-8")
    (tmp_path / "goods").mkdir()
    (tmp_path / "goods/keys.txt").write_text("K1\nK2\nK3\n", encoding="utf-8")

    fake = FakeLootly()
    fake.add_product("p1", "Steam ключ")
    fake.add_product("p2", "Гайд")
    fake.add_product("p3", "Ручной товар")
    hub = Hub(load_main())
    hub.api._http = httpx.AsyncClient(base_url="https://api.test/v1", transport=fake.transport(),
                                      headers=dict(hub.api._http.headers))
    events = []
    for ev in Event:
        hub.events.on(ev, lambda *a, ev=ev: events.append((ev, a[1:])))
    try:
        yield hub, fake, events, tmp_path
    finally:
        await hub.api.close()
        hub.storage.close()
        os.chdir(cwd)


def keys(tmp_path):
    return (tmp_path / "goods/keys.txt").read_text().split()


async def test_auto_delivery_single(env):
    hub, fake, events, tmp = env
    fake.add_order("o1", "p1")
    await hub.poll_orders()

    o = fake.orders["o1"]
    assert o["status"] == "awaiting_confirmation" and o["deliveredValue"] == "K1"
    assert keys(tmp) == ["K2", "K3"]
    assert fake.sent("o1") == ["Привет, заказ o1", "Спасибо! K1"]
    assert hub.storage.order("o1")["state"] == "delivered"
    assert [e for e, _ in events] == [Event.NEW_ORDER, Event.ORDER_DELIVERED]

    await hub.poll_orders()  # повторный опрос ничего не делает
    assert fake.count("deliver") == 1


async def test_quantity_uses_delivered_values(env):
    hub, fake, _, tmp = env
    fake.add_order("o1", "p1", quantity=2)
    await hub.poll_orders()
    assert fake.orders["o1"]["deliveredValues"] == ["K1", "K2"]
    assert keys(tmp) == ["K3"]


async def test_static_value_and_unmapped(env):
    hub, fake, _, _ = env
    fake.add_order("o1", "p2", quantity=3)
    fake.add_order("o2", "p3")
    await hub.poll_orders()
    assert fake.orders["o1"]["deliveredValue"] == "https://example.com/guide"
    assert fake.orders["o2"]["status"] == "paid"
    assert hub.storage.order("o2")["state"] == "manual"


async def test_no_stock_then_restock(env):
    hub, fake, events, tmp = env
    fake.add_order("o1", "p1", quantity=5)
    await hub.poll_orders()
    assert fake.orders["o1"]["status"] == "paid"
    assert keys(tmp) == ["K1", "K2", "K3"]
    assert hub.storage.order("o1")["state"] == "no_stock"
    assert Event.NO_STOCK in [e for e, _ in events]

    await hub.goods.add("keys.txt", ["K4", "K5"])
    await hub.poll_orders()
    assert fake.orders["o1"]["deliveredValues"] == ["K1", "K2", "K3", "K4", "K5"]
    assert sum(e == Event.NO_STOCK for e, _ in events) == 1


async def test_taken_by_human_returns_keys(env):
    hub, fake, _, tmp = env
    o = fake.add_order("o1", "p1")
    hub.storage.add_order("o1", "p1", "Steam ключ")
    o.update(status="processing", assigneeType="user")  # человек успел взять
    order = await hub.api.get_order("o1")
    order.status = "paid"  # бот видел заказ ещё оплаченным
    await hub.auto_deliver(order)
    assert keys(tmp) == ["K1", "K2", "K3"]
    assert hub.storage.order("o1")["state"] == "manual"
    assert fake.count("deliver") == 0


async def test_deliver_outage_recovers_with_same_values(env):
    hub, fake, _, tmp = env
    hub.api.max_retries = 0
    fake.add_order("o1", "p1")
    fake.fail_next["deliver"] = [503]
    await hub.poll_orders()
    assert fake.orders["o1"]["status"] == "processing"
    assert hub.storage.order("o1")["state"] == "reserved"
    assert keys(tmp) == ["K2", "K3"]

    await hub.poll_orders()  # восстановление
    assert fake.orders["o1"]["deliveredValue"] == "K1"
    assert hub.storage.order("o1")["state"] == "delivered"
    assert keys(tmp) == ["K2", "K3"]


async def test_cancel_returns_reserved(env):
    hub, fake, _, tmp = env
    hub.api.max_retries = 0
    fake.add_order("o1", "p1")
    fake.fail_next["deliver"] = [503]
    await hub.poll_orders()
    await hub.cancel("o1", "тест")
    assert fake.orders["o1"]["status"] == "cancelled"
    assert keys(tmp) == ["K1", "K2", "K3"]
    assert hub.storage.order("o1")["state"] == "cancelled"


async def test_order_cancelled_during_outage(env):
    hub, fake, _, tmp = env
    hub.api.max_retries = 0
    fake.add_order("o1", "p1")
    fake.fail_next["deliver"] = [503]
    await hub.poll_orders()
    fake.orders["o1"]["status"] = "cancelled"  # покупатель/поддержка отменили
    await hub.poll_orders()
    assert keys(tmp) == ["K1", "K2", "K3"]


async def test_rate_limit_retry(env):
    hub, fake, _, _ = env
    fake.fail_next["GET /orders"] = [429]
    orders, _ = await hub.api.list_orders()
    assert orders == []
    assert fake.count("GET /orders") == 2


async def test_auto_hide_and_unhide(env):
    hub, fake, events, tmp = env
    hub.set_feature("auto_hide", True)
    fake.add_order("o1", "p1", quantity=3)
    await hub.poll_orders()
    assert fake.products["p1"]["archived"] is True
    await hub.goods.add("keys.txt", ["K9"])
    await hub.poll_orders()
    assert fake.products["p1"]["archived"] is False
    assert [a[1] for e, a in events if e == Event.PRODUCT_HIDDEN] == [True, False]


async def test_autoresponse_ignores_old_history(env):
    hub, fake, events, _ = env
    fake.add_order("o1", "p3")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    fake.add_message("o1", "!help", created_at=old)
    await hub.poll_orders()
    await hub.poll_messages()
    assert fake.sent("o1") == []

    fake.add_message("o1", "!HELP")
    fake.add_message("o1", "просто текст")
    await hub.poll_messages()
    assert fake.sent("o1") == ["Заказ o1: Ручной товар"]
    assert [a[1].text for e, a in events if e == Event.NEW_MESSAGE] == ["!HELP", "просто текст"]

    await hub.poll_messages()
    assert len(fake.sent("o1")) == 1


async def test_manual_deliver(env):
    hub, fake, _, _ = env
    fake.add_order("o1", "p3", quantity=2)
    with pytest.raises(ValueError):
        await hub.deliver("o1", ["a", "b", "c"])
    order = await hub.deliver("o1", ["a", "b"])
    assert order.delivered_values == ["a", "b"]


async def test_data_request(env):
    hub, *_ = env
    assert await hub.check_data_request("p1", "f1", "Логин", "player_one") is True
    assert await hub.check_data_request("p1", "f1", "Логин", "Bad Name!") is False
    assert await hub.check_data_request("p1", "f2", "Другое", "x") is False
    hub.add_data_validator(lambda pid, fid, label, value: True if label == "Другое" else None)
    assert await hub.check_data_request("p1", "f2", "Другое", "x") is True


def test_password_hash():
    h = hash_password("correct horse")
    assert check_password("correct horse", h)
    assert not check_password("wrong", h)
    assert not check_password("x", "garbage")
