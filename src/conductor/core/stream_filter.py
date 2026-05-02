"""Stream filter: reads stream-json from stdin, pretty-prints to stdout, saves raw json to file.

Usage: claude -p - ... | python3 -m conductor.core.stream_filter OUTPUT_FILE
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

# ANSI codes
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_GRAY = "\033[90m"
_GREEN_BOLD = "\033[1;32m"
_RESET = "\033[0m"

_ctx_max = 200_000


def _ts() -> str:
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return f"{_DIM}{t}{_RESET} "


def _format_event(event: dict) -> None:
    """Print formatted event to stdout (mirrors progress_on_event from claude.py)."""
    global _ctx_max
    t = event.get("type")

    if t == "system" and event.get("subtype") == "init":
        model = event.get("model", "")
        _ctx_max = 1_000_000 if "[1m]" in model else 200_000
        return

    if t == "assistant":
        ts = _ts()
        msg = event.get("message", {})
        usage = msg.get("usage", {})

        inp = usage.get("input_tokens", 0)
        cc = usage.get("cache_creation_input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        if inp or cc or cr or out:
            ctx_k = (inp + cc + cr) // 1000
            ctx_max_k = _ctx_max // 1000
            pct = (inp + cc + cr) * 100 // _ctx_max if _ctx_max else 0
            stats = f"[{ctx_k}k/{ctx_max_k}k {pct}% | in:{inp} cache_r:{cr} cache_w:{cc} out:{out}]"
            sys.stdout.write(f"{ts}{_DIM}{stats}{_RESET}\n")

        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            bt = block.get("type")

            if bt == "tool_use":
                name = block.get("name", "")
                inp_data = block.get("input", {})
                if name in ("Edit", "Write", "Read"):
                    detail = inp_data.get("file_path", "")
                    c = _DIM + _CYAN
                elif name == "Bash":
                    detail = inp_data.get("command", "")[:150]
                    c = _DIM + _YELLOW
                elif name == "Grep":
                    pat = inp_data.get("pattern", "")
                    path = inp_data.get("path", "")
                    glb = inp_data.get("glob", "")
                    detail = pat + (f" in {path}" if path else "") + (f" ({glb})" if glb else "")
                    c = _DIM + _MAGENTA
                elif name == "Glob":
                    pat = inp_data.get("pattern", "")
                    path = inp_data.get("path", "")
                    detail = pat + (f" in {path}" if path else "")
                    c = _DIM + _MAGENTA
                elif name in ("TaskRead", "TaskWrite", "TodoRead", "TodoWrite"):
                    detail = ""
                    c = _DIM
                else:
                    detail = str(inp_data)[:120]
                    c = _DIM
                sys.stdout.write(f"{ts}{c}{name}: {detail}{_RESET}\n")

            elif bt == "text":
                text = block.get("text", "").strip()
                if text:
                    sys.stdout.write(f"{ts}{text}\n")

            elif bt == "thinking":
                thinking = block.get("thinking", "").strip()
                if thinking:
                    sys.stdout.write(f"{ts}{_DIM}{_CYAN}{thinking}{_RESET}\n")

        sys.stdout.flush()
        return

    if t == "user":
        ts = _ts()
        msg = event.get("message", {})
        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                c = str(block.get("content", ""))
                if len(c) > 200:
                    c = c[:200] + "…"
                if c:
                    sys.stdout.write(f"{ts}{_DIM}  {_GRAY}→ {c}{_RESET}\n")
        sys.stdout.flush()
        return

    if t == "result":
        ts = _ts()
        subtype = event.get("subtype", "")
        sys.stdout.write(f"{ts}{_GREEN_BOLD}{subtype}{_RESET}\n")
        sys.stdout.flush()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 -m conductor.core.stream_filter OUTPUT_FILE", file=sys.stderr)
        sys.exit(1)

    output_path = sys.argv[1]

    with open(output_path, "w", encoding="utf-8") as out_f:
        for line in sys.stdin:
            # Save raw json to file
            out_f.write(line)
            out_f.flush()

            # Parse and pretty-print
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
                if isinstance(event, dict):
                    _format_event(event)
            except (json.JSONDecodeError, ValueError):
                pass


if __name__ == "__main__":
    main()
