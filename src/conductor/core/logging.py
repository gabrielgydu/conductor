"""Structured logging for conductor — terminal, markdown, and audit."""
from __future__ import annotations

import json
import sys
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Event registry
# ---------------------------------------------------------------------------

EventConfig = namedtuple("EventConfig", ["icon", "log_to_markdown"])

EVENT_REGISTRY: dict[str, EventConfig] = {
    "CONDUCTOR_START":   EventConfig("●",  True),
    "CONDUCTOR_EXIT":    EventConfig("■",  True),
    "RUN_ACTIVATE":      EventConfig("▶",  True),
    "RUN_COMPLETE":      EventConfig("✓",  True),
    "STAGE_TRANSITION":  EventConfig("→",  True),
    "WORKTREE_CREATE":   EventConfig("⚙",  False),
    "WORKTREE_REMOVE":   EventConfig("⚙",  False),
    "SPECCER_INIT":      EventConfig("⚙",  False),
    "SPECCER_RUN":       EventConfig("⚙",  False),
    "SPECCER_EXIT":      EventConfig("⚙",  False),
    "BRAIN_CALL":        EventConfig("🧠", True),
    "RUNNER_START":      EventConfig("▶",  False),
    "RUNNER_EXIT":       EventConfig("✓",  False),
    "RUNNER_STEER":      EventConfig("↪",  False),
    "STALL_CHECK":       EventConfig("○",  False),
    "FAILURE":           EventConfig("✗",  True),
    "BLOCKED":           EventConfig("⊘",  True),
    "PLAN":              EventConfig("◆",  True),
    "RETRY":             EventConfig("↻",  False),
}

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_RED = "\033[31m"
_CYAN = "\033[36m"
_CYAN_BOLD = "\033[1;36m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_COLOR_MAP: dict[str, str] = {
    "FAILURE": _RED,
    "BLOCKED": _RED,
    "RUN_ACTIVATE": _CYAN,
    "RUNNER_START": _CYAN,
    "CONDUCTOR_START": _CYAN_BOLD,
    "PLAN": _YELLOW,
    "BRAIN_CALL": _YELLOW,
}


def _isatty() -> bool:
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _colorize(text: str, color: str) -> str:
    if not _isatty():
        return text
    return f"{color}{text}{_RESET}"


def dim(text: str) -> str:
    return _colorize(text, _DIM)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_dim() -> str:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return dim(ts)


# ---------------------------------------------------------------------------
# live_log
# ---------------------------------------------------------------------------

def live_log(
    event: str,
    message: str,
    *,
    audit_data: dict[str, Any] | None = None,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Write event to terminal, optional markdown log, and optional audit NDJSON."""
    cfg = EVENT_REGISTRY.get(event)
    if cfg is None:
        icon = "?"
        log_to_markdown = False
        _write_stderr(f"[conductor] WARNING: unknown event type: {event}\n")
    else:
        icon = cfg.icon
        log_to_markdown = cfg.log_to_markdown

    # --- Terminal ---
    color = _COLOR_MAP.get(event, "")
    if color and _isatty():
        colored_msg = f"{color}{message}{_RESET}"
    else:
        colored_msg = message

    ts_dim = _now_dim()
    line = f"{ts_dim} {icon} {colored_msg}\n"
    _write_stderr(line)

    # --- Markdown ---
    if log_path is not None and log_to_markdown:
        timestamp = _now_iso()
        md_line = f"### {icon} {event} — {timestamp}\n\n{message}\n\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(md_line)

    # --- Audit NDJSON ---
    if audit_path is not None:
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "event": event,
            "message": message,
        }
        if audit_data:
            record.update(audit_data)
        ndjson_line = json.dumps(record, ensure_ascii=False) + "\n"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(ndjson_line)


def _write_stderr(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def header(text: str) -> None:
    """Print a cyan bold header with separator to stderr."""
    separator = "─" * 60
    if _isatty():
        sys.stderr.write(f"{_CYAN_BOLD}{separator}\n{text}\n{separator}{_RESET}\n")
    else:
        sys.stderr.write(f"{separator}\n{text}\n{separator}\n")
    sys.stderr.flush()


def error(text: str) -> None:
    """Print red error text to stderr."""
    if _isatty():
        sys.stderr.write(f"{_RED}{text}{_RESET}\n")
    else:
        sys.stderr.write(f"{text}\n")
    sys.stderr.flush()


def die(message: str, exit_code: int = 1) -> None:
    """Print error and exit."""
    error(message)
    sys.exit(exit_code)
