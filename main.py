"""
LootlyHub — бот автоматизации магазина Lootly.
Автор: @fairydarkns

  python main.py                  запуск
  python main.py hash-password    хэш пароля для telegram.password_hash
  python main.py check            проверить конфиги и API-ключ
"""
import argparse
import asyncio
import getpass
import logging
import signal
import sys

from lootlyhub import __author__, __version__
from lootlyhub.api import LootlyError
from lootlyhub.config import ConfigError, load_main
from lootlyhub.logs import setup_logging

logger = logging.getLogger("lootlyhub")

BANNER = f"""
  _                 _   _       _   _       _
 | |    ___   ___ | |_| |_   _| | | |_   _| |__
 | |   / _ \\ / _ \\| __| | | | | |_| | | | | '_ \\
 | |__| (_) | (_) | |_| | |_| |  _  | |_| | |_) |
 |_____\\___/ \\___/ \\__|_|\\__, |_| |_|\\__,_|_.__/
                         |___/   v{__version__} · {__author__}
"""


async def run() -> None:
    from lootlyhub.hub import Hub

    hub = Hub(load_main())
    services = []
    if hub.cfg.tg_enabled:
        from lootlyhub.tg import TelegramPanel
        services.append(TelegramPanel(hub))
    if hub.cfg.webhook_enabled:
        from lootlyhub.webhook import WebhookServer
        services.append(WebhookServer(hub))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    try:
        for service in services:
            await service.start()
        await hub.start()
        logger.info("LootlyHub работает. Ctrl+C — остановить.")
        await stop.wait()
    finally:
        logger.info("Останавливаюсь…")
        for service in reversed(services):
            await service.stop()
        await hub.stop()


async def check() -> None:
    from lootlyhub.api import LootlyAPI
    from lootlyhub.config import load_autoresponse, load_delivery

    cfg = load_main()
    rules, cmds = load_delivery(), load_autoresponse()
    async with LootlyAPI(cfg.api_key, proxy=cfg.api_proxy) as api:
        products = await api.all_products()
        orders, total = await api.list_orders("new", limit=1)
    ids = {p.id for p in products}
    print(f"Ключ OK. Товаров: {len(products)}, под автоматизацией: {sum(p.external for p in products)}, "
          f"новых заказов: {total}")
    print(f"Правил выдачи: {len(rules)}, команд автоответа: {len(cmds)}")
    for r in rules:
        if r.product_id and r.product_id not in ids:
            print(f"  ! товар {r.product_id} из delivery.toml не найден в магазине")
        p = next((p for p in products if r.matches(p.id, p.title)), None)
        if p and not p.external:
            print(f"  ! «{p.title}» без внешней автоматизации — его заказы в API не попадут")


def main() -> None:
    parser = argparse.ArgumentParser(description="LootlyHub — бот для магазина Lootly")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "hash-password", "check"])
    parser.add_argument("--debug", action="store_true", help="подробные логи в консоль")
    args = parser.parse_args()

    if args.command == "hash-password":
        from lootlyhub.tg.auth import hash_password
        password = getpass.getpass("Пароль для ПУ Telegram: ")
        if len(password) < 8:
            sys.exit("Слишком короткий пароль (нужно от 8 символов).")
        if password != getpass.getpass("Ещё раз: "):
            sys.exit("Пароли не совпали.")
        print(f'\npassword_hash = "{hash_password(password)}"')
        return

    setup_logging(args.debug)
    print(BANNER)
    try:
        asyncio.run(check() if args.command == "check" else run())
    except ConfigError as e:
        logger.critical("%s", e)
        sys.exit(1)
    except LootlyError as e:
        hint = " — проверьте lootly.api_key" if e.status == 401 else ""
        logger.critical("Lootly API: %s%s", e, hint)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
