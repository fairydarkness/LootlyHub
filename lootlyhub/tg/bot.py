"""Панель управления и уведомления в Telegram (aiogram 3)."""
from __future__ import annotations

import asyncio
import logging
import time
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import __author__, __version__
from ..api import LootlyError, Message as ChatMessage, Order, OrderStatus
from ..config import ConfigError
from ..events import Event
from ..goods import GoodsError
from ..hub import Hub
from ..templates import VARIABLES
from .auth import check_password

logger = logging.getLogger("lootlyhub.tg")

MAX_LOGIN_ATTEMPTS = 5

FEATURE_NAMES = {
    "auto_delivery": "Автовыдача",
    "auto_response": "Автоответ",
    "greeting": "Приветствие после взятия",
    "after_delivery": "Сообщение после выдачи",
    "auto_hide": "Скрывать товар без остатков",
    "auto_cancel_no_stock": "Отмена, если нет товара",
    "notify_orders": "🔔 Новые заказы",
    "notify_messages": "🔔 Сообщения",
    "notify_delivery": "🔔 Выдачи",
}

BTN_ORDERS, BTN_GOODS, BTN_STATUS, BTN_SETTINGS = "📦 Заказы", "🗃 Товары", "📊 Статус", "⚙️ Настройки"
MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_GOODS)],
    [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_SETTINGS)],
])


class Form(StatesGroup):
    reply = State()
    deliver = State()
    add_goods = State()


def e(text: object) -> str:
    return escape(str(text), quote=False)


def order_card(order: Order, title: str = "") -> str:
    lines = [title] if title else []
    lines += [
        f"<b>{e(order.product_title)}</b> × {order.quantity}",
        f"Заказ: <code>{e(order.id)}</code>",
        f"Сумма: <b>{order.amount:g} ₽</b> · {e(order.status_name)}",
    ]
    if order.created_at:
        lines.append(f"Создан: {order.created_at.astimezone():%d.%m.%Y %H:%M}")
    if order.field_values:
        lines.append("")
        lines += [f"• {e(f.label)}: <code>{e(f.value)}</code>" for f in order.field_values]
    if order.delivered_values or order.delivered_value:
        lines.append("")
        lines.append("Выдано:")
        values = order.delivered_values or [order.delivered_value]
        lines += [f"<code>{e(v)}</code>" for v in values[:20]]
    return "\n".join(lines)


def order_kb(order: Order) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    oid = order.id
    kb.button(text="💬 Ответить", callback_data=f"o:reply:{oid}")
    if order.status in (OrderStatus.PAID, OrderStatus.PROCESSING):
        kb.button(text="📤 Выдать", callback_data=f"o:deliver:{oid}")
    if order.status in (OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.AWAITING_CONFIRMATION):
        kb.button(text="❌ Отменить", callback_data=f"o:cancel:{oid}")
    kb.button(text="🔄", callback_data=f"o:show:{oid}")
    kb.adjust(2)
    return kb.as_markup()


class TelegramPanel:
    def __init__(self, hub: Hub):
        self.hub = hub
        cfg = hub.cfg
        session = AiohttpSession(proxy=cfg.tg_proxy) if cfg.tg_proxy else None
        self.bot = Bot(cfg.tg_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher()
        self._task: asyncio.Task | None = None

        auth, public = Router(name="auth"), Router(name="public")
        auth.message.filter(self.IsAdmin(self))
        auth.callback_query.filter(self.IsAdmin(self))
        self._register(auth)
        public.message.register(self.login, F.chat.type == "private", F.text)
        self.dp.include_routers(auth, public)
        self._subscribe()

    # ---------------------------------------------------------------- access

    class IsAdmin(Filter):
        def __init__(self, panel: TelegramPanel):
            self.panel = panel

        async def __call__(self, event: Message | CallbackQuery) -> bool:
            user = event.from_user
            return user is not None and user.id in self.panel.admins

    @property
    def admins(self) -> set[int]:
        return set(self.hub.cfg.tg_admins) | set(self.hub.storage.get("tg_authorized", []))

    async def login(self, m: Message) -> None:
        storage, uid = self.hub.storage, m.from_user.id
        if not self.hub.cfg.tg_password_hash:
            await m.answer("⛔ Доступ закрыт.")
            return
        attempts = storage.get("tg_attempts", {})
        if attempts.get(str(uid), 0) >= MAX_LOGIN_ATTEMPTS:
            return
        if m.text.startswith("/"):
            await m.answer("🔑 Введите пароль от панели LootlyHub.")
            return
        if check_password(m.text, self.hub.cfg.tg_password_hash):
            storage.set("tg_authorized", sorted(set(storage.get("tg_authorized", [])) | {uid}))
            attempts.pop(str(uid), None)
            storage.set("tg_attempts", attempts)
            logger.warning("Вход в ПУ Telegram: %s (id %s)", m.from_user.username, uid)
            await self.notify(f"🔐 В ПУ вошёл @{e(m.from_user.username)} (<code>{uid}</code>)")
            try:
                await m.delete()  # не держим пароль в истории чата
            except TelegramAPIError:
                pass
            await m.answer("✅ Доступ открыт.", reply_markup=MENU)
            await self.send_status(m)
        else:
            attempts[str(uid)] = attempts.get(str(uid), 0) + 1
            storage.set("tg_attempts", attempts)
            logger.warning("Неудачная попытка входа в ПУ: %s (id %s)", m.from_user.username, uid)
            left = MAX_LOGIN_ATTEMPTS - attempts[str(uid)]
            await m.answer(f"❌ Неверный пароль. Осталось попыток: {left}.")

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.bot.set_my_commands([
            BotCommand(command="start", description="Меню"),
            BotCommand(command="status", description="Статус бота"),
            BotCommand(command="orders", description="Заказы: new / processing / awaiting / all"),
            BotCommand(command="order", description="Заказ по ID"),
            BotCommand(command="goods", description="Остатки в goods/"),
            BotCommand(command="addgoods", description="Пополнить файл: /addgoods имя.txt"),
            BotCommand(command="products", description="Товары магазина"),
            BotCommand(command="settings", description="Переключатели"),
            BotCommand(command="reload", description="Перечитать delivery/autoresponse"),
            BotCommand(command="plugins", description="Плагины"),
            BotCommand(command="vars", description="Переменные для текстов"),
            BotCommand(command="cancel", description="Отменить ввод"),
        ])
        me = await self.bot.get_me()
        logger.info("Telegram-бот @%s запущен", me.username)
        self._task = asyncio.create_task(self.dp.start_polling(self.bot, handle_signals=False))

    async def stop(self) -> None:
        if self._task:
            await self.dp.stop_polling()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.bot.session.close()

    async def notify(self, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
        for uid in self.admins:
            try:
                await self.bot.send_message(uid, text, reply_markup=kb)
            except TelegramAPIError as err:
                logger.debug("Не удалось отправить уведомление %s: %s", uid, err)

    async def notify_message(self, order: Order, msg: ChatMessage) -> None:
        kb = InlineKeyboardBuilder()
        kb.button(text="💬 Ответить", callback_data=f"o:reply:{order.id}")
        kb.button(text="📦 Заказ", callback_data=f"o:show:{order.id}")
        await self.notify(f"💬 <b>{e(order.product_title)}</b> · <code>{e(order.id)}</code>\n\n"
                          f"{e(msg.text)}", kb.as_markup())

    # ---------------------------------------------------------------- events

    def _subscribe(self) -> None:
        ev, hub = self.hub.events, self.hub

        @ev.on(Event.STARTED)
        async def started(_hub):
            await self.notify(f"✅ <b>LootlyHub {__version__}</b> запущен.")

        @ev.on(Event.NEW_ORDER)
        async def new_order(_hub, order: Order):
            if not hub.feature("notify_orders"):
                return
            auto = hub.feature("auto_delivery") and hub.rule_for(order) is not None
            title = "🛒 <b>Новый заказ</b>" + (" · будет выдан автоматически" if auto else "")
            await self.notify(order_card(order, title), order_kb(order))

        @ev.on(Event.ORDER_DELIVERED)
        async def delivered(_hub, order: Order, values: list[str], auto: bool):
            if hub.feature("notify_delivery"):
                how = "автоматически" if auto else "вручную"
                await self.notify(order_card(order, f"📤 <b>Выдано {how}</b>"), order_kb(order))

        @ev.on(Event.NO_STOCK)
        async def no_stock(_hub, order: Order, goods: str):
            await self.notify(order_card(order, f"⚠️ <b>Нет товара</b> в goods/{e(goods)} "
                                                f"(осталось {hub.goods.count(goods)}). "
                                                f"Пополните: /addgoods {e(goods)}"), order_kb(order))

        @ev.on(Event.DELIVERY_FAILED)
        async def failed(_hub, order: Order, error: Exception):
            await self.notify(order_card(order, f"🚫 <b>Ошибка выдачи:</b> <code>{e(error)}</code>"),
                              order_kb(order))

        @ev.on(Event.ORDER_CANCELLED)
        async def cancelled(_hub, order: Order, reason: str | None):
            await self.notify(f"❌ Заказ <code>{e(order.id)}</code> отменён"
                              + (f": {e(reason)}" if reason else "."))

        @ev.on(Event.NEW_MESSAGE)
        async def new_message(_hub, order: Order, msg: ChatMessage):
            if hub.feature("notify_messages"):
                await self.notify_message(order, msg)

        @ev.on(Event.COMMAND)
        async def command(_hub, order: Order, msg: ChatMessage, cmd):
            # notify = true у команды — уведомить, даже если уведомления о сообщениях выключены
            if cmd.notify and not hub.feature("notify_messages"):
                await self.notify_message(order, msg)

        @ev.on(Event.PRODUCT_HIDDEN)
        async def hidden(_hub, product_id: str, is_hidden: bool):
            what = "скрыт (кончился товар)" if is_hidden else "возвращён в продажу"
            await self.notify(f"🗃 Товар <code>{e(product_id)}</code> {what}.")

    # -------------------------------------------------------------- handlers

    def _register(self, r: Router) -> None:
        r.message.register(self.cmd_start, CommandStart())
        r.message.register(self.cmd_cancel_input, Command("cancel"))
        r.message.register(self.send_status, Command("status"))
        r.message.register(self.send_status, F.text == BTN_STATUS)
        r.message.register(self.cmd_orders, Command("orders"))
        r.message.register(self.cmd_orders, F.text == BTN_ORDERS)
        r.message.register(self.cmd_order, Command("order"))
        r.message.register(self.cmd_goods, Command("goods"))
        r.message.register(self.cmd_goods, F.text == BTN_GOODS)
        r.message.register(self.cmd_addgoods, Command("addgoods"))
        r.message.register(self.cmd_products, Command("products"))
        r.message.register(self.cmd_settings, Command("settings"))
        r.message.register(self.cmd_settings, F.text == BTN_SETTINGS)
        r.message.register(self.cmd_reload, Command("reload"))
        r.message.register(self.cmd_plugins, Command("plugins"))
        r.message.register(self.cmd_vars, Command("vars"))

        r.message.register(self.input_reply, Form.reply, F.text)
        r.message.register(self.input_deliver, Form.deliver, F.text | F.document)
        r.message.register(self.input_goods, Form.add_goods, F.text | F.document)

        r.callback_query.register(self.cb_order, F.data.startswith("o:"))
        r.callback_query.register(self.cb_orders_list, F.data.startswith("ol:"))
        r.callback_query.register(self.cb_settings, F.data.startswith("s:"))
        r.callback_query.register(self.cb_goods, F.data.startswith("g:"))

    async def cmd_start(self, m: Message, state: FSMContext) -> None:
        await state.clear()
        await m.answer(f"<b>LootlyHub {__version__}</b> · автор {e(__author__)}\n"
                       f"Бот автоматизации магазина Lootly.", reply_markup=MENU)

    async def cmd_cancel_input(self, m: Message, state: FSMContext) -> None:
        await state.clear()
        await m.answer("Ок, отменил.", reply_markup=MENU)

    async def send_status(self, m: Message) -> None:
        hub = self.hub
        uptime = int(time.time() - hub.started_at)
        stats = hub.storage.stats()
        f = hub.features
        goods = hub.goods.files()
        text = [
            f"📊 <b>LootlyHub {__version__}</b>",
            f"Аптайм: {uptime // 86400}д {uptime % 86400 // 3600}ч {uptime % 3600 // 60}м",
            "",
            f"Автовыдача: {'🟢' if f['auto_delivery'] else '🔴'}  Автоответ: {'🟢' if f['auto_response'] else '🔴'}",
            f"Правил выдачи: {len(hub.delivery_rules)} · команд: {len(hub.autoresponses)} · "
            f"плагинов: {len(hub.plugins)}",
            "",
            f"Выдано: {stats.get('delivered', 0)} · вручную/ждут: {stats.get('manual', 0)} · "
            f"нет товара: {stats.get('no_stock', 0)} · ошибок: {stats.get('failed', 0)}",
            f"Товара в goods/: {sum(goods.values())} шт. в {len(goods)} файлах",
        ]
        if hub.api.rate_limit_remaining is not None:
            text.append(f"Остаток лимита API: {hub.api.rate_limit_remaining}")
        await m.answer("\n".join(text), reply_markup=MENU)

    # ---- orders

    async def cmd_orders(self, m: Message, command: CommandObject | None = None) -> None:
        status = (command.args or "new").strip() if command else "new"
        await self._send_orders(m, status)

    async def _send_orders(self, m: Message, status: str, edit: bool = False) -> None:
        try:
            orders, total = await self.hub.api.list_orders(status=status, limit=15)
        except LootlyError as err:
            await m.answer(f"Ошибка API: <code>{e(err)}</code>")
            return
        kb = InlineKeyboardBuilder()
        for o in orders:
            kb.button(text=f"{o.status_name.split()[0]} {o.product_title[:28]} · {o.amount:g}₽",
                      callback_data=f"o:show:{o.id}")
        for s, name in (("new", "Новые"), ("processing", "В работе"), ("awaiting", "Ждут"), ("all", "Все")):
            kb.button(text=("• " if s == status else "") + name, callback_data=f"ol:{s}")
        kb.adjust(*[1] * len(orders), 4)
        text = f"📦 Заказы <b>{e(status)}</b>: {total}" + ("" if orders else "\nПусто.")
        if edit:
            await m.edit_text(text, reply_markup=kb.as_markup())
        else:
            await m.answer(text, reply_markup=kb.as_markup())

    async def cb_orders_list(self, c: CallbackQuery) -> None:
        await self._send_orders(c.message, c.data.split(":", 1)[1], edit=True)
        await c.answer()

    async def cmd_order(self, m: Message, command: CommandObject) -> None:
        if not command.args:
            await m.answer("Использование: /order &lt;id&gt;")
            return
        try:
            order = await self.hub.api.get_order(command.args.strip())
        except LootlyError as err:
            await m.answer(f"Ошибка: <code>{e(err)}</code>")
            return
        await m.answer(order_card(order), reply_markup=order_kb(order))

    async def cb_order(self, c: CallbackQuery, state: FSMContext) -> None:
        _, action, oid = c.data.split(":", 2)
        hub = self.hub
        try:
            if action == "show":
                order = await hub.api.get_order(oid)
                await c.message.answer(order_card(order), reply_markup=order_kb(order))
            elif action == "reply":
                await state.set_state(Form.reply)
                await state.update_data(order_id=oid)
                await c.message.answer(f"✍️ Сообщение покупателю в заказ <code>{e(oid)}</code>. /cancel — отмена.")
            elif action == "deliver":
                order = await hub.api.get_order(oid)
                await state.set_state(Form.deliver)
                await state.update_data(order_id=oid)
                hint = (f"по одному на строку, ровно {order.quantity} шт. (или одно значение на весь заказ)"
                        if order.quantity > 1 else "одним сообщением")
                await c.message.answer(f"📤 Что выдать по заказу <code>{e(oid)}</code>? "
                                       f"Пришлите значения {hint} или .txt файл. /cancel — отмена.")
            elif action == "cancel":
                kb = InlineKeyboardBuilder()
                kb.button(text="Да, вернуть деньги", callback_data=f"o:cancelok:{oid}")
                kb.button(text="Нет", callback_data=f"o:show:{oid}")
                await c.message.answer(f"Отменить заказ <code>{e(oid)}</code> с возвратом средств?",
                                       reply_markup=kb.as_markup())
            elif action == "cancelok":
                await hub.cancel(oid, hub.cfg.cancel_reason)
                await c.message.edit_text(f"❌ Заказ <code>{e(oid)}</code> отменён.")
        except LootlyError as err:
            await c.message.answer(f"Ошибка API: <code>{e(err)}</code>")
        await c.answer()

    async def input_reply(self, m: Message, state: FSMContext) -> None:
        oid = (await state.get_data())["order_id"]
        await state.clear()
        msg = await self.hub.send_message(oid, m.text)
        await m.answer("✅ Отправлено." if msg else "❌ Не удалось отправить, см. логи.")

    async def _read_lines(self, m: Message) -> list[str]:
        if m.document:
            if m.document.file_size and m.document.file_size > 5 * 1024 * 1024:
                await m.answer("Файл больше 5 МБ.")
                return []
            data = await self.bot.download(m.document)
            text = data.read().decode("utf-8-sig", errors="replace")
        else:
            text = m.text or ""
        return [line.strip() for line in text.splitlines() if line.strip()]

    async def input_deliver(self, m: Message, state: FSMContext) -> None:
        oid = (await state.get_data())["order_id"]
        values = await self._read_lines(m)
        if not values:
            return
        await state.clear()
        try:
            order = await self.hub.deliver(oid, values)
        except (LootlyError, ValueError) as err:
            await m.answer(f"❌ <code>{e(err)}</code>")
            return
        await m.answer(order_card(order, "✅ <b>Выдано</b>"), reply_markup=order_kb(order))

    # ---- goods / products

    async def cmd_goods(self, m: Message) -> None:
        files = self.hub.goods.files()
        used = {r.goods for r in self.hub.delivery_rules if r.goods}
        kb = InlineKeyboardBuilder()
        lines = ["🗃 <b>Остатки goods/</b>"]
        for name in sorted(set(files) | used):
            n = files.get(name, 0)
            mark = "🔴" if n == 0 else "🟡" if n < 5 else "🟢"
            lines.append(f"{mark} <code>{e(name)}</code> — {n} шт." + ("" if name in used else " (не в delivery.toml)"))
            kb.button(text=f"➕ {name}", callback_data=f"g:add:{name}"[:64])
        if len(lines) == 1:
            lines.append("Пусто. Создайте файл: /addgoods имя.txt")
        kb.adjust(2)
        await m.answer("\n".join(lines), reply_markup=kb.as_markup())

    async def _ask_goods(self, m: Message, state: FSMContext, name: str) -> None:
        try:
            self.hub.goods.path(name)
        except GoodsError as err:
            await m.answer(str(err))
            return
        await state.set_state(Form.add_goods)
        await state.update_data(goods=name)
        await m.answer(f"➕ Пришлите товары для <code>{e(name)}</code>: по одному на строку или .txt файлом. "
                       f"/cancel — отмена.")

    async def cmd_addgoods(self, m: Message, command: CommandObject, state: FSMContext) -> None:
        name = (command.args or "").strip()
        if not name:
            await m.answer("Использование: /addgoods имя.txt")
            return
        if not name.endswith(".txt"):
            name += ".txt"
        await self._ask_goods(m, state, name)

    async def cb_goods(self, c: CallbackQuery, state: FSMContext) -> None:
        await self._ask_goods(c.message, state, c.data.split(":", 2)[2])
        await c.answer()

    async def input_goods(self, m: Message, state: FSMContext) -> None:
        name = (await state.get_data())["goods"]
        values = await self._read_lines(m)
        if not values:
            return
        await state.clear()
        added = await self.hub.goods.add(name, values)
        await m.answer(f"✅ Добавлено {added} шт. в <code>{e(name)}</code>, теперь {self.hub.goods.count(name)}.")

    async def cmd_products(self, m: Message) -> None:
        try:
            products = await self.hub.api.all_products()
        except LootlyError as err:
            await m.answer(f"Ошибка API: <code>{e(err)}</code>")
            return
        lines = [f"🛍 <b>Товары</b> ({len(products)})"]
        for p in products[:50]:
            flags = ("🤖" if p.external else "👤") + ("📦" if p.archived else "🟢" if p.active else "⚪️")
            rule = next((r for r in self.hub.delivery_rules if r.matches(p.id, p.title)), None)
            src = f" → {e(rule.goods or 'value')}" if rule else ""
            lines.append(f"{flags} {e(p.title)} · {p.price_rub:g}₽{src}\n<code>{e(p.id)}</code>")
        lines.append("\n🤖 под API · 👤 вручную · 📦 архив · ⚪️ выключен")
        await m.answer("\n".join(lines))

    # ---- settings

    def _settings_kb(self) -> InlineKeyboardMarkup:
        kb = InlineKeyboardBuilder()
        for key, value in self.hub.features.items():
            kb.button(text=f"{'🟢' if value else '🔴'} {FEATURE_NAMES.get(key, key)}", callback_data=f"s:{key}")
        kb.adjust(1)
        return kb.as_markup()

    async def cmd_settings(self, m: Message) -> None:
        await m.answer("⚙️ <b>Настройки</b> (сохраняются, перекрывают configs/main.toml)",
                       reply_markup=self._settings_kb())

    async def cb_settings(self, c: CallbackQuery) -> None:
        key = c.data.split(":", 1)[1]
        try:
            self.hub.set_feature(key, not self.hub.feature(key))
        except KeyError:
            await c.answer("Неизвестная опция")
            return
        await c.message.edit_reply_markup(reply_markup=self._settings_kb())
        await c.answer(f"{FEATURE_NAMES.get(key, key)}: {'вкл' if self.hub.feature(key) else 'выкл'}")

    async def cmd_reload(self, m: Message) -> None:
        try:
            self.hub.reload()
        except ConfigError as err:
            await m.answer(f"❌ {e(err)}")
            return
        await m.answer(f"✅ Перечитано: {len(self.hub.delivery_rules)} правил выдачи, "
                       f"{len(self.hub.autoresponses)} команд.")

    async def cmd_plugins(self, m: Message) -> None:
        if not self.hub.plugins:
            await m.answer("Плагинов нет. Положите .py в папку plugins/ и перезапустите бота.")
            return
        await m.answer("\n\n".join(f"🧩 <b>{e(p.name)}</b> {e(p.version)} · {e(p.author)}\n"
                                   f"{e(p.description)}\n<code>plugins/{e(p.file)}</code>"
                                   for p in self.hub.plugins))

    async def cmd_vars(self, m: Message) -> None:
        await m.answer("Переменные для текстов:\n" +
                       "\n".join(f"<code>{k}</code> — {v}" for k, v in VARIABLES.items()))
