"""
HTTP-эндпоинт для полей «запрос данных»: Lootly присылает POST с введённым
покупателем значением и ждёт булев вердикт ({"ok": true/false}) за 5 секунд.

Lootly ходит только на публичный HTTPS, поэтому поставьте перед ботом
reverse-proxy с TLS (nginx, Caddy) или туннель. Запросы не подписаны —
вставьте в webhook.path случайную строку, чтобы адрес нельзя было угадать.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .hub import Hub

logger = logging.getLogger("lootlyhub.webhook")


class WebhookServer:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.app = web.Application(client_max_size=64 * 1024)
        self.app.router.add_post(hub.cfg.webhook_path, self.data_request)
        self.app.router.add_get("/health", self.health)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.hub.cfg.webhook_host, self.hub.cfg.webhook_port)
        await site.start()
        logger.info("Вебхук «запрос данных» слушает %s:%s%s", self.hub.cfg.webhook_host,
                    self.hub.cfg.webhook_port, self.hub.cfg.webhook_path)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def data_request(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            product_id, field_id = str(body["productId"]), str(body["fieldId"])
            label, value = str(body.get("label", "")), str(body["value"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"ok": False}, status=400)
        try:
            verdict = await self.hub.check_data_request(product_id, field_id, label, value)
        except Exception:
            logger.exception("Ошибка проверки поля %s", label or field_id)
            verdict = False
        logger.info("Проверка «%s» = %r для товара %s: %s", label or field_id, value[:64], product_id,
                    "ok" if verdict else "отказ")
        return web.json_response({"ok": verdict})
