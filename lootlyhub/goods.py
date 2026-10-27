"""Файлы товаров goods/*.txt: одна строка — одна единица товара (ключ, аккаунт, код)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

GOODS_DIR = Path("goods")


class GoodsError(Exception):
    pass


class Goods:
    def __init__(self, directory: Path = GOODS_DIR):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def path(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise GoodsError(f"Недопустимое имя файла товаров: {name!r}")
        return self.dir / name

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    def _read(self, name: str) -> list[str]:
        path = self.path(name)
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _write(self, name: str, lines: list[str]) -> None:
        path = self.path(name)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        os.replace(tmp, path)  # атомарно: при падении файл не обрежется наполовину

    def count(self, name: str) -> int:
        return len(self._read(name))

    def files(self) -> dict[str, int]:
        return {p.name: self.count(p.name) for p in sorted(self.dir.glob("*.txt"))}

    async def take(self, name: str, amount: int) -> list[str] | None:
        """Забирает ``amount`` первых строк. None — если столько нет (файл не меняется)."""
        async with self._lock(name):
            lines = self._read(name)
            if len(lines) < amount:
                return None
            self._write(name, lines[amount:])
            return lines[:amount]

    async def put_back(self, name: str, values: list[str]) -> None:
        """Возвращает значения в начало файла (откат резерва)."""
        async with self._lock(name):
            self._write(name, values + self._read(name))

    async def add(self, name: str, values: list[str]) -> int:
        values = [v.strip() for v in values if v.strip()]
        async with self._lock(name):
            self._write(name, self._read(name) + values)
        return len(values)
