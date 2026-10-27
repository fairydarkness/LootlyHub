import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

COLORS = {"DEBUG": "\033[90m", "INFO": "\033[36m", "WARNING": "\033[33m", "ERROR": "\033[31m",
          "CRITICAL": "\033[41m"}
RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = COLORS.get(record.levelname)
        return f"{color}{text}{RESET}" if color else text


def setup_logging(debug: bool = False) -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColorFormatter(fmt, "%H:%M:%S") if sys.stdout.isatty() else logging.Formatter(fmt))
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    file = RotatingFileHandler("logs/lootlyhub.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file.setFormatter(logging.Formatter(fmt))
    file.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers[:] = [console, file]
    for noisy in ("httpx", "httpcore", "aiogram.event", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
