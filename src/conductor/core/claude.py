"""Claude CLI integration for conductor."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Callable

# Re-exported: callers resolve aliases through conductor.core.claude.
from conductor.core.model_catalog import resolve_model as resolve_model  # noqa: PLC0414
from conductor.core.stats import (
    result_cost_usd,
    result_events,
    session_num_turns,
    session_token_stats,
)

# Sentinel: when on_event is _USE_DEFAULT, use the built-in progress printer.
_USE_DEFAULT = object()

# ANSI codes for progress output
_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_GRAY = "\033[90m"
_GREEN_BOLD = "\033[1;32m"
_RED_BOLD = "\033[1;31m"
_RESET = "\033[0m"


def _isatty() -> bool:
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _ts() -> str:
    """Return a dim HH:MM:SS timestamp prefix."""
    t = datetime.now().astimezone().strftime("%H:%M:%S")  # local: no zone is shown
    if _isatty():
        return f"{_DIM}{t}{_RESET} "
    return f"{t} "

# Track context window size (updated from init events)
_ctx_max = 200_000


def progress_on_event(event: dict) -> None:
    """Default on_event callback — prints live progress to stderr."""
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

        # Context usage line
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

    # Result event — subtype in bold green, or bold red with the CLI's message on an error
    if t == "result":
        # Imported here, not at module scope: loop mode and manual-test run
        # `python3 -m conductor.core.stream_filter`, and runpy warns when
        # importing the conductor.core package has already loaded it.
        from conductor.core.stream_filter import describe_result

        ts = _ts()
        label, summary, error_text = describe_result(event)
        if color:
            c = _RED_BOLD if event.get("is_error") else _GREEN_BOLD
            sys.stderr.write(f"{ts}{c}{label}{_RESET}{_DIM}{summary}{_RESET}\n")
        else:
            sys.stderr.write(f"{ts}{label}{summary}\n")
        if error_text:
            sys.stderr.write(f"{ts}  {error_text}\n")
        sys.stderr.flush()


@dataclass
class ClaudeResult:
    exit_code: int
    output: str  # raw stdout: the stream-json lines
    tokens_used: dict[str, int] | None
    cost: float | None  # the CLI's own total_cost_usd; None if it reported none
    duration: float
    # The stream-json `result` event (the last one), or None if none arrived.
    result_event: dict | None = None
    # The last few KB of the CLI's stderr. Worth logging on a failure, though
    # the CLI reports most errors (auth, unknown model) in the result event.
    stderr: str = ""
    # True when run_claude's timeout_s expired and the CLI was stopped.
    timed_out: bool = False
    # Turns over the whole session (each result event counts only its own);
    # None if no result event reported any.
    num_turns: int | None = None

    @property
    def is_error(self) -> bool:
        """True when the CLI flagged its result as an error.

        Not logged in, an unknown model and error_max_turns all set it. The CLI
        exits 1 for those too, but `failed` checks both rather than rely on that.
        """
        return bool(self.result_event and self.result_event.get("is_error"))

    @property
    def failed(self) -> bool:
        """True when the run did not succeed: non-zero exit, error result or timeout."""
        return self.exit_code != 0 or self.is_error or self.timed_out

    @property
    def subtype(self) -> str | None:
        """The result subtype: "success", "error_max_turns", ..."""
        return self.result_event.get("subtype") if self.result_event else None

    @property
    def result_text(self) -> str | None:
        """The session's final text; for an error result, the CLI's error message."""
        text = self.result_event.get("result") if self.result_event else None
        return text if isinstance(text, str) else None

    @property
    def model_usage(self) -> dict[str, dict]:
        """Per-model token counts and costUSD (`modelUsage`), keyed by model ID."""
        usage = self.result_event.get("modelUsage") if self.result_event else None
        return usage if isinstance(usage, dict) else {}


def _tokens_used(results: list[dict]) -> dict[str, int] | None:
    """ClaudeResult.tokens_used: the session's totals; None if no result reports usage."""
    if not any(isinstance(e.get("usage"), dict) or e.get("modelUsage") for e in results):
        return None
    tokens = session_token_stats(results)
    return {
        "input_tokens": tokens.input,
        "output_tokens": tokens.output,
        "cache_read_input_tokens": tokens.cache_read,
        "cache_creation_input_tokens": tokens.cache_write,
    }


# Max bytes per stdout line: stream-json lines carry whole tool results.
_STREAM_LIMIT = 10 * 1024 * 1024
# How much of the CLI's stderr a ClaudeResult keeps.
_STDERR_TAIL_BYTES = 4096
# How long a terminated CLI gets to exit before it is killed.
_TERMINATE_GRACE_S = 5.0


async def _write_prompt(proc: asyncio.subprocess.Process, prompt: str) -> None:
    """Write the prompt to stdin and close it.

    Like communicate(), ignore a broken pipe: if claude exits before reading
    its whole prompt (bad flag, auth failure), its exit code reports that.
    """
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    proc.stdin.close()
    try:
        await proc.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _drain_stderr(proc: asyncio.subprocess.Process, tail: bytearray) -> None:
    """Read stderr to EOF so the CLI never blocks on it, keeping the last bytes."""
    while chunk := await proc.stderr.read(4096):
        tail += chunk
        del tail[:-_STDERR_TAIL_BYTES]


async def _read_stdout(
    proc: asyncio.subprocess.Process,
    lines: list[str],
    on_event: Callable[[dict], None] | None,
) -> None:
    """Collect stdout lines until EOF, handing each JSON object to on_event."""
    callback_failed = False
    while True:
        try:
            raw = await proc.stdout.readline()
        except ValueError:
            # A line over _STREAM_LIMIT. readline() has discarded what it had
            # buffered; the rest of that line may come back as a non-JSON
            # fragment, which every consumer skips. Carry on rather than lose
            # the session.
            sys.stderr.write(f"run_claude: skipped a stdout line over {_STREAM_LIMIT} bytes\n")
            continue
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace")
        lines.append(line)
        stripped = line.strip()
        if on_event is None or not stripped:
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        try:
            on_event(event)
        except Exception as exc:  # noqa: BLE001
            # Progress output is best effort; it must not cost the session.
            if not callback_failed:
                callback_failed = True
                sys.stderr.write(f"run_claude: on_event callback failed, continuing: {exc!r}\n")


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Stop the CLI if it is still running (SIGTERM, then SIGKILL) and reap it."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), _TERMINATE_GRACE_S)
            return
        except TimeoutError:
            proc.kill()
    except ProcessLookupError:
        pass  # exited on its own in the meantime
    await proc.wait()


async def run_claude(
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int = 10,
    output_format: str = "stream-json",
    append_args: list[str] | None = None,
    cwd: str | None = None,
    on_event: Callable[[dict], None] | object = _USE_DEFAULT,
    timeout_s: float | None = None,
) -> ClaudeResult:
    """Run claude CLI non-interactively, passing prompt via stdin.

    By default, streams live progress to stderr (tool calls, text, context usage).
    Pass on_event=None to suppress output, or a custom callback; an exception in
    the callback is reported once and does not stop the run.

    timeout_s bounds the whole run. When it expires the CLI is stopped and the
    result has timed_out=True, a non-zero exit code and the output so far.
    However run_claude ends (returns, raises or is cancelled), the CLI process
    has been stopped and reaped by then.
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
        limit=_STREAM_LIMIT,
    )

    lines: list[str] = []
    stderr_tail = bytearray()
    timed_out = False
    # Feed stdin while reading stdout and stderr, so no pipe can fill up and
    # deadlock whatever order the CLI reads and writes in.
    io_tasks = [
        asyncio.create_task(_write_prompt(proc, prompt)),
        asyncio.create_task(_drain_stderr(proc, stderr_tail)),
        asyncio.create_task(_read_stdout(proc, lines, on_event)),
    ]
    try:
        async with asyncio.timeout(timeout_s) as deadline:
            await asyncio.gather(*io_tasks)
            await proc.wait()
    except TimeoutError:
        if not deadline.expired():
            raise
        timed_out = True
    finally:
        try:
            await _stop(proc)
        finally:
            for task in io_tasks:
                task.cancel()
            await asyncio.gather(*io_tasks, return_exceptions=True)
    duration = time.monotonic() - start

    stdout_text = "".join(lines)
    results = result_events(stdout_text)
    result_event = results[-1] if results else None

    return ClaudeResult(
        exit_code=proc.returncode or 0,
        output=stdout_text,
        tokens_used=_tokens_used(results),
        cost=result_cost_usd(result_event),  # cumulative: the last result's is the session's
        duration=duration,
        result_event=result_event,
        stderr=stderr_tail.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        num_turns=session_num_turns(results),
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
