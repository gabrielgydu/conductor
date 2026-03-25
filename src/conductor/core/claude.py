"""Claude CLI integration for conductor."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from asyncio.subprocess import PIPE
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

# Sentinel: when on_event is _USE_DEFAULT, use the built-in progress printer.
_USE_DEFAULT = object()

# ANSI codes for progress output
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_GRAY = "\033[90m"
_GREEN_BOLD = "\033[1;32m"
_RESET = "\033[0m"


def _isatty() -> bool:
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _ts() -> str:
    """Return a dim HH:MM:SS timestamp prefix."""
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if _isatty():
        return f"{_DIM}{t}{_RESET} "
    return f"{t} "

# Track context window size (updated from init events)
_ctx_max = 200_000


def progress_on_event(event: dict) -> None:
    """Default on_event callback — prints live progress to stderr, matching ralph style."""
    global _ctx_max
    t = event.get("type")
    color = _isatty()

    # System init — detect context window size
    if t == "system" and event.get("subtype") == "init":
        model = event.get("model", "")
        if "[1m]" in model:
            _ctx_max = 1_000_000
        else:
            _ctx_max = 200_000
        return

    # Assistant turn — show context stats + content
    if t == "assistant":
        ts = _ts()
        msg = event.get("message", {})
        usage = msg.get("usage", {})

        # Context usage line (like ralph)
        inp = usage.get("input_tokens", 0)
        cc = usage.get("cache_creation_input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        if inp or cc or cr or out:
            ctx_k = (inp + cc + cr) // 1000
            ctx_max_k = _ctx_max // 1000
            pct = (inp + cc + cr) * 100 // _ctx_max if _ctx_max else 0
            stats = f"[{ctx_k}k/{ctx_max_k}k {pct}% | in:{inp} cache_r:{cr} cache_w:{cc} out:{out}]"
            if color:
                sys.stderr.write(f"{ts}{_DIM}{stats}{_RESET}\n")
            else:
                sys.stderr.write(f"{ts}{stats}\n")

        content = msg.get("content", [])
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")

            if bt == "tool_use":
                name = block.get("name", "")
                inp_data = block.get("input", {})
                if name in ("Edit", "Write", "Read"):
                    detail = inp_data.get("file_path", "")
                    c = _DIM + _CYAN if color else ""
                elif name == "Bash":
                    detail = inp_data.get("command", "")[:150]
                    c = _DIM + _YELLOW if color else ""
                elif name == "Grep":
                    pat = inp_data.get("pattern", "")
                    path = inp_data.get("path", "")
                    glb = inp_data.get("glob", "")
                    detail = pat + (f" in {path}" if path else "") + (f" ({glb})" if glb else "")
                    c = _DIM + _MAGENTA if color else ""
                elif name == "Glob":
                    pat = inp_data.get("pattern", "")
                    path = inp_data.get("path", "")
                    detail = pat + (f" in {path}" if path else "")
                    c = _DIM + _MAGENTA if color else ""
                elif name in ("TaskRead", "TaskWrite", "TodoRead", "TodoWrite"):
                    detail = ""
                    c = _DIM if color else ""
                else:
                    detail = str(inp_data)[:120]
                    c = _DIM if color else ""
                r = _RESET if color else ""
                sys.stderr.write(f"{ts}{c}{name}: {detail}{r}\n")

            elif bt == "text":
                text = block.get("text", "").strip()
                if text:
                    sys.stderr.write(f"{ts}{text}\n")

            elif bt == "thinking":
                thinking = block.get("thinking", "").strip()
                if thinking:
                    if color:
                        sys.stderr.write(f"{ts}{_DIM}{_CYAN}{thinking}{_RESET}\n")
                    else:
                        sys.stderr.write(f"{ts}{thinking}\n")

        sys.stderr.flush()
        return

    # Tool results (user events containing tool_result)
    if t == "user":
        ts = _ts()
        msg = event.get("message", {})
        content = msg.get("content", [])
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                c = str(block.get("content", ""))
                if len(c) > 200:
                    c = c[:200] + "…"
                if c:
                    if color:
                        sys.stderr.write(f"{ts}{_DIM}  {_GRAY}→ {c}{_RESET}\n")
                    else:
                        sys.stderr.write(f"{ts}  → {c}\n")
        sys.stderr.flush()
        return

    # Result event — show bold green subtype
    if t == "result":
        ts = _ts()
        subtype = event.get("subtype", "")
        if color:
            sys.stderr.write(f"{ts}{_GREEN_BOLD}{subtype}{_RESET}\n")
        else:
            sys.stderr.write(f"{ts}{subtype}\n")
        sys.stderr.flush()


@dataclass
class ClaudeResult:
    exit_code: int
    output: str
    tokens_used: dict[str, int] | None
    cost: float | None
    duration: float


def _extract_tokens_from_stdout(stdout_text: str) -> dict[str, int] | None:
    """Parse stream-json stdout and return token usage from the result event."""
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            # usage may be top-level on the result event or nested under result
            usage = event.get("usage")
            if not isinstance(usage, dict):
                result_val = event.get("result", {})
                if isinstance(result_val, dict):
                    usage = result_val.get("usage")
            if isinstance(usage, dict):
                return {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                }
    return None


async def run_claude(
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int = 10,
    output_format: str = "stream-json",
    append_args: list[str] | None = None,
    cwd: str | None = None,
    on_event: Callable[[dict], None] | object = _USE_DEFAULT,
) -> ClaudeResult:
    """Run claude CLI non-interactively, passing prompt via stdin.

    By default, streams live progress to stderr (tool calls, text, context usage).
    Pass on_event=None to suppress output, or a custom callback.
    """
    # Resolve sentinel to default progress printer
    if on_event is _USE_DEFAULT:
        on_event = progress_on_event
    cmd = [
        "claude",
        "-p", "-",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        "--verbose",
        "--output-format", output_format,
    ]
    if model:
        cmd += ["--model", model]
    if append_args:
        cmd.extend(append_args)

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
        cwd=cwd,
        close_fds=True,
        limit=10 * 1024 * 1024,  # 10MB line buffer (Claude JSON lines can be large)
    )

    if on_event is None:
        # Original non-streaming path
        stdout_bytes, _stderr_bytes = await proc.communicate(prompt.encode("utf-8"))
        duration = time.monotonic() - start
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    else:
        # Streaming path: write prompt, close stdin, then read stdout line by line
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

        # Drain stderr in background to avoid pipe deadlock
        async def _drain_stderr():
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break

        stderr_task = asyncio.create_task(_drain_stderr())

        lines: list[str] = []
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            lines.append(line)
            stripped = line.strip()
            if stripped:
                try:
                    event = json.loads(stripped)
                    if isinstance(event, dict):
                        on_event(event)
                except (json.JSONDecodeError, ValueError):
                    pass

        await stderr_task
        await proc.wait()
        duration = time.monotonic() - start
        stdout_text = "".join(lines)

    tokens = _extract_tokens_from_stdout(stdout_text)

    return ClaudeResult(
        exit_code=proc.returncode or 0,
        output=stdout_text,
        tokens_used=tokens,
        cost=None,
        duration=duration,
    )


class SteerableSession:
    """An interactive Claude session that accepts follow-up messages."""

    def __init__(self, proc: asyncio.subprocess.Process, start_time: float) -> None:
        self._proc = proc
        self._start_time = start_time
        self._closed = False
        self._last_event_time = time.monotonic()

    async def send(self, message: str) -> None:
        """Send a follow-up message to the running Claude process."""
        if self._closed or self._proc.stdin.is_closing():
            raise RuntimeError("Session closed")
        formatted = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": message},
            "session_id": "default",
            "parent_tool_use_id": None,
        }) + "\n"
        self._proc.stdin.write(formatted.encode("utf-8"))
        await self._proc.stdin.drain()

    async def poll(self) -> AsyncIterator[dict]:
        """Yield events from Claude stdout until EOF."""
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            self._last_event_time = time.monotonic()
            try:
                event = json.loads(line.decode("utf-8", errors="replace").strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            yield event

    async def wait(self, timeout: float = 300) -> ClaudeResult:
        """Consume all events, returning ClaudeResult when process ends or timeout fires."""
        output_parts: list[str] = []
        tokens: dict[str, int] | None = None
        timed_out = False

        self._last_event_time = time.monotonic()

        async def _consume():
            nonlocal tokens
            async for event in self.poll():
                if event.get("type") == "assistant":
                    content = event.get("content", "")
                    if isinstance(content, str):
                        output_parts.append(content)
                if event.get("type") == "result":
                    usage = event.get("usage")
                    if not isinstance(usage, dict):
                        result_val = event.get("result", {})
                        if isinstance(result_val, dict):
                            usage = result_val.get("usage")
                    if isinstance(usage, dict):
                        tokens = {
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                        }
                    break

        async def _idle_watchdog():
            nonlocal timed_out
            while True:
                await asyncio.sleep(min(timeout, 1.0))
                idle = time.monotonic() - self._last_event_time
                if idle >= timeout:
                    timed_out = True
                    self._proc.kill()
                    break

        consume_task = asyncio.ensure_future(_consume())
        watchdog_task = asyncio.ensure_future(_idle_watchdog())

        done, pending = await asyncio.wait(
            [consume_task, watchdog_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # propagate exceptions from consume
        for t in done:
            if not t.cancelled() and t.exception() and t is consume_task:
                raise t.exception()

        if not timed_out:
            try:
                self._proc.stdin.close()
            except Exception:
                pass

        exit_code = await self._proc.wait()
        duration = time.monotonic() - self._start_time

        return ClaudeResult(
            exit_code=exit_code or 0 if not timed_out else (exit_code or -9),
            output="".join(output_parts),
            tokens_used=tokens,
            cost=None,
            duration=duration,
        )

    async def close(self) -> None:
        """Close the session, terminating the subprocess if still running."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.returncode is None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
        except Exception:
            pass


async def run_claude_steerable(
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int = 100,
    append_args: list[str] | None = None,
    cwd: str | None = None,
) -> SteerableSession:
    """Start an interactive Claude session with --input-format stream-json."""
    cmd = [
        "claude",
        "-p", "-",
        "--dangerously-skip-permissions",
        "--max-turns", str(max_turns),
        "--verbose",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
    ]
    if model:
        cmd += ["--model", model]
    if append_args:
        cmd.extend(append_args)

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
        cwd=cwd,
        close_fds=True,
        limit=10 * 1024 * 1024,  # 10MB line buffer (Claude JSON lines can be large)
    )

    initial = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "session_id": "default",
        "parent_tool_use_id": None,
    }) + "\n"
    proc.stdin.write(initial.encode("utf-8"))
    await proc.stdin.drain()

    return SteerableSession(proc, start)


def resolve_model(name: str) -> str:
    """Resolve short model names to full Claude model IDs."""
    _MODEL_MAP = {
        "opus": "claude-opus-4-6",
        "opus-200k": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
    }
    return _MODEL_MAP.get(name, name)


def calculate_cost(tokens: dict[str, int] | None) -> float | None:
    """Calculate cost from token usage. Returns None if tokens is None."""
    if not tokens:
        return None
    # Pricing per million tokens (as of 2025)
    input_cost = tokens.get("input_tokens", 0) * 15.0 / 1_000_000
    output_cost = tokens.get("output_tokens", 0) * 75.0 / 1_000_000
    cache_read_cost = tokens.get("cache_read_input_tokens", 0) * 1.5 / 1_000_000
    cache_write_cost = tokens.get("cache_creation_input_tokens", 0) * 18.75 / 1_000_000
    return input_cost + output_cost + cache_read_cost + cache_write_cost
