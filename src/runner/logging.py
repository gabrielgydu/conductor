"""Logging helpers for the runner — mirrors conductor.core.logging style."""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_CYAN_BOLD = "\033[1;36m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _isatty() -> bool:
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _c(text: str, code: str) -> str:
    if not _isatty():
        return text
    return f"{code}{text}{_RESET}"


def log(msg: str) -> None:
    ts = _c(_ts(), _DIM)
    sys.stderr.write(f"{ts} {msg}\n")
    sys.stderr.flush()


def info(msg: str) -> None:
    log(msg)


def success(msg: str) -> None:
    log(_c(msg, _GREEN))


def warn(msg: str) -> None:
    log(_c(msg, _YELLOW))


def error(msg: str) -> None:
    log(_c(msg, _RED))


def dim(msg: str) -> str:
    return _c(msg, _DIM)


def bold(msg: str) -> str:
    return _c(msg, _BOLD)


def header(text: str) -> None:
    sep = "─" * 60
    if _isatty():
        sys.stderr.write(f"\n{_CYAN_BOLD}{sep}\n{text}\n{sep}{_RESET}\n")
    else:
        sys.stderr.write(f"\n{sep}\n{text}\n{sep}\n")
    sys.stderr.flush()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)
