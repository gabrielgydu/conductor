"""Claude CLI integration for conductor."""
from __future__ import annotations

import asyncio
import json
import time
from asyncio.subprocess import PIPE
from dataclasses import dataclass, field
from typing import AsyncIterator


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
        if event.get("type") == "result":
            usage = event.get("result", {}).get("usage", {})
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
) -> ClaudeResult:
    """Run claude CLI non-interactively, passing prompt via stdin."""
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
    )
    stdout_bytes, _stderr_bytes = await proc.communicate(prompt.encode("utf-8"))
    duration = time.monotonic() - start

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
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
        formatted = json.dumps({"type": "user", "role": "user", "content": message}) + "\n"
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
                    usage = event.get("result", {}).get("usage", {})
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
    )

    initial = json.dumps({"type": "user", "role": "user", "content": prompt}) + "\n"
    proc.stdin.write(initial.encode("utf-8"))
    await proc.stdin.drain()

    return SteerableSession(proc, start)
