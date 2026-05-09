"""
Structured logging setup with dual handlers (console + rotating file).
Console output uses ANSI colors; file output stays plain text.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class ColoredFormatter(logging.Formatter):
    """ANSI color formatter for terminal output."""

    # Color map: levelname -> ANSI escape code
    COLORS = {
        "DEBUG": "\033[36m",      # cyan
        "INFO": "\033[32m",       # green  (success)
        "WARNING": "\033[33m",    # yellow (caution)
        "ERROR": "\033[31m",      # red    (failure)
        "CRITICAL": "\033[35m",   # magenta (critical)
        "RESET": "\033[0m",
    }

    def __init__(self, fmt: str | None = None, datefmt: str | None = None) -> None:
        super().__init__(fmt, datefmt)

    def format(self, record: logging.LogRecord) -> str:
        raw = super().format(record)
        color = self.COLORS.get(record.levelname, self.COLORS["RESET"])
        reset = self.COLORS["RESET"]
        # Color only the levelname portion for readability
        return raw.replace(record.levelname, f"{color}{record.levelname}{reset}")


def _enable_windows_ansi() -> None:
    """Enable ANSI escape codes in legacy Windows CMD."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def setup_logging(level: str = "INFO", log_file: str = "engagement.log") -> None:
    _enable_windows_ansi()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    plain_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    colored_fmt = ColoredFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console (colored)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(colored_fmt)
    root.addHandler(console)

    # Rotating file (max 5 MB, keep 3 backups) — plain text, no ANSI codes
    file_path = Path(log_file)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_hdl = RotatingFileHandler(
        file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_hdl.setFormatter(plain_fmt)
    root.addHandler(file_hdl)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
