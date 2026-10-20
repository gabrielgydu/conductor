"""Stream filter: reads stream-json from stdin, pretty-prints to stdout, saves raw json to file.

Usage: claude -p - ... | python3 -m conductor.core.stream_filter OUTPUT_FILE

OUTPUT_FILE receives every byte of stdin, whatever happens to the display:
callers read the session's events, the final result event above all, from it.
Uses only the standard library, so it can also run as a plain script.
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
_RED_BOLD = "\033[1;31m"
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
        label, summary, error_text = describe_result(event)
        color = _RED_BOLD if event.get("is_error") else _GREEN_BOLD
        sys.stdout.write(f"{ts}{color}{label}{_RESET}{_DIM}{summary}{_RESET}\n")
        if error_text:
            sys.stdout.write(f"{ts}  {error_text}\n")
        sys.stdout.flush()


def describe_result(event: dict) -> tuple[str, str, str]:
    """Display parts of a result event: (label, " (2 turns, $0.0015, 3.0s)", error text).

    The CLI reports some failures (not logged in, unknown model) with subtype
    "success" and is_error set; those are labelled "error". The error text is
    the result text or the `errors` list, and empty unless is_error is set.
    """
    is_error = bool(event.get("is_error"))
    label = str(event.get("subtype") or "")
    if is_error and label in ("", "success"):
        label = "error"

    details = []
    turns = event.get("num_turns")
    if isinstance(turns, int):
        details.append(f"{turns} turn{'' if turns == 1 else 's'}")
    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        details.append(f"${cost:.4f}")
    duration_ms = event.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        details.append(f"{duration_ms / 1000:.1f}s")
    summary = f" ({', '.join(details)})" if details else ""

    error_text = ""
    if is_error:
        result = event.get("result")
        errors = event.get("errors")
        if isinstance(result, str) and result.strip():
            error_text = result.strip()
        elif isinstance(errors, list):
            error_text = "; ".join(str(e) for e in errors)
        if len(error_text) > 500:
            error_text = error_text[:500] + "…"
    return label, summary, error_text


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 -m conductor.core.stream_filter OUTPUT_FILE", file=sys.stderr)
        sys.exit(1)

    output_path = sys.argv[1]
    display = True

    # Binary in and out: the file gets Claude's bytes unchanged, and an
    # undecodable byte cannot stop the copy.
    with open(output_path, "wb") as out_f:
        for raw in sys.stdin.buffer:
            # Save raw json to file before anything else can go wrong.
            out_f.write(raw)
            out_f.flush()

            if not display:
                continue
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            # The display is best effort. An event shape the printer does not
            # expect, or a terminal that went away, must not end the filter:
            # Claude would get a broken pipe and the rest of the stream,
            # result event included, would never reach OUTPUT_FILE.
            try:
                _format_event(event)
            except OSError:
                display = False  # the terminal went away; keep copying
            except Exception as exc:  # noqa: BLE001
                _warn(f"cannot display a {event.get('type')!r} event: {exc!r}")


def _warn(message: str) -> None:
    try:
        print(f"stream_filter: {message}", file=sys.stderr)
    except OSError:
        pass


if __name__ == "__main__":
    main()
