"""Activity log writing — strip ANSI and append stream-json events to activity.log."""
from __future__ import annotations

import json
from pathlib import Path

from runner.logging import strip_ansi


_MAX_ACTIVITY_LINE = 300


def _extract_text_from_event(event: dict) -> str | None:
    """Extract a human-readable text snippet from a stream-json event."""
    t = event.get("type")

    if t == "assistant":
        msg = event.get("message", {})
        content = msg.get("content", [])
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text[:_MAX_ACTIVITY_LINE])
            elif bt == "thinking":
                thinking = block.get("thinking", "").strip()
                if thinking:
                    parts.append(f"[thinking] {thinking[:100]}")
            elif bt == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if name in ("Edit", "Write", "Read"):
                    fp = inp.get("file_path", "")
                    parts.append(f"{name}: {fp}")
                elif name == "Bash":
                    cmd = inp.get("command", "")[:150]
                    parts.append(f"Bash: {cmd}")
                elif name == "Grep":
                    pat = inp.get("pattern", "")
                    parts.append(f"Grep: {pat}")
                else:
                    parts.append(f"{name}: {str(inp)[:120]}")
        return "\n".join(parts) if parts else None

    if t == "result":
        subtype = event.get("subtype", "")
        return f"[result: {subtype}]"

    return None


def append_event_to_activity_log(activity_log: Path, event: dict) -> None:
    """Extract text from a stream-json event and append it to activity.log."""
    text = _extract_text_from_event(event)
    if not text:
        return

    clean = strip_ansi(text)
    if not clean.strip():
        return

    activity_log.parent.mkdir(parents=True, exist_ok=True)
    with open(activity_log, "a", encoding="utf-8") as f:
        f.write(clean + "\n")


def append_raw_to_activity_log(activity_log: Path, raw_line: str) -> None:
    """Append a raw (already-text) line to activity.log after stripping ANSI."""
    clean = strip_ansi(raw_line)
    if not clean.strip():
        return
    activity_log.parent.mkdir(parents=True, exist_ok=True)
    with open(activity_log, "a", encoding="utf-8") as f:
        f.write(clean + "\n")


def parse_stream_json_text(stream_output: str) -> str:
    """Extract all assistant text from stream-json output string."""
    parts: list[str] = []
    for line in stream_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(text)
    return "\n".join(parts)
