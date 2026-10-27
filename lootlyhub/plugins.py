"""
Плагины — .py файлы в папке plugins/. Минимальный плагин::

    NAME = "Мой плагин"
    VERSION = "1.0"
    AUTHOR = "@me"
    DESCRIPTION = "Что делает"

    def setup(hub):
        @hub.events.on("new_order")
        async def on_order(hub, order):
            ...

Плагин выполняется с правами бота и видит API-ключ, поэтому ставьте только код,
который прочитали. Файлы, имя которых начинается с "_", не загружаются.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hub import Hub

logger = logging.getLogger("lootlyhub.plugins")

PLUGINS_DIR = Path("plugins")


@dataclass(slots=True)
class PluginInfo:
    file: str
    name: str
    version: str
    author: str
    description: str
    module: ModuleType


async def load_plugins(hub: Hub, directory: Path = PLUGINS_DIR) -> list[PluginInfo]:
    directory.mkdir(exist_ok=True)
    loaded = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"lootlyhub_plugins.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise AttributeError("нет функции setup(hub)")
            result = setup(hub)
            if inspect.isawaitable(result):
                await result
        except Exception:
            sys.modules.pop(module_name, None)
            logger.exception("Не удалось загрузить плагин %s", path.name)
            continue
        info = PluginInfo(
            file=path.name,
            name=str(getattr(module, "NAME", path.stem)),
            version=str(getattr(module, "VERSION", "?")),
            author=str(getattr(module, "AUTHOR", "?")),
            description=str(getattr(module, "DESCRIPTION", "")),
            module=module,
        )
        loaded.append(info)
        logger.info("Плагин загружен: %s %s (%s)", info.name, info.version, info.author)
    return loaded
