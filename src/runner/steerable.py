"""Steerable session management for the runner.

Uses subprocess.PIPE (NOT FIFOs) to communicate with Claude in
--input-format stream-json mode.

Key design (from bug report ralph-bugs-report.md):
- NO named FIFOs — use asyncio subprocess pipes directly
- NO fd inheritance tricks — close_fds=True everywhere
- Idle timeout for completion detection (not polling for "result" which
  requires EOF to trigger — circular deadlock)
- stdin=DEVNULL for any background subprocesses spawned in callbacks
"""
from __future__ import annotations

import asyncio
import json
import time
from asyncio.subprocess import PIPE
from pathlib import Path
from typing import AsyncIterator, Callable, Awaitable

from runner.activity import append_event_to_activity_log


# Seconds of output silence after which we consider the session idle-complete.
# Claude emits a "result" event only after getting EOF, so we can't wait for
# it — instead we watch for end_turn stop_reason or idle silence.
_DEFAULT_IDLE_TIMEOUT = 600.0


class SteerableSession:
    """Interactive Claude session using subprocess stdin/stdout pipes.

    Wraps conductor.core.claude.SteerableSession with runner-specific
    concerns: activity log writing, event streaming for token detection.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        activity_log: Path,
        start_time: float,
    ) -> None:
        self._proc = proc
        self._activity_log = activity_log
        self._start_time = start_time
        self._last_event_time = time.monotonic()
        self._closed = False

    @classmethod
    async def launch(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int = 100,
        cwd: str | None = None,
        append_args: list[str] | None = None,
        activity_log: Path,
    ) -> "SteerableSession":
        """Start Claude with --input-format stream-json, send initial prompt."""
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

        # Write the initial prompt as NDJSON
        initial = json.dumps({"type": "user", "role": "user", "content": prompt}) + "\n"
        proc.stdin.write(initial.encode("utf-8"))
        await proc.stdin.drain()

        return cls(proc, activity_log, start)

    async def send(self, message: str) -> None:
        """Send a follow-up message to the running Claude process."""
        if self._closed or self._proc.stdin is None or self._proc.stdin.is_closing():
            raise RuntimeError("SteerableSession: cannot send to closed session")
        formatted = json.dumps({"type": "user", "role": "user", "content": message}) + "\n"
        self._proc.stdin.write(formatted.encode("utf-8"))
        await self._proc.stdin.drain()

    async def stream_events(
        self,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    ) -> tuple[str, dict | None]:
        """Consume stdout events until Claude signals completion or we time out.

        Returns (accumulated_text, result_event_or_None).

        Completion is detected by:
          1. A "result" event (Claude sent EOF first — rare)
          2. stop_reason == "end_turn" in an assistant message
          3. Idle timeout (no stdout bytes for idle_timeout seconds)

        Activity log is written for each event.
        """
        text_parts: list[str] = []
        result_event: dict | None = None
        done = asyncio.Event()

        self._last_event_time = time.monotonic()

        async def _read_loop() -> None:
            nonlocal result_event
            assert self._proc.stdout is not None
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    # EOF from Claude
                    done.set()
                    return
                self._last_event_time = time.monotonic()
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Write to activity log
                append_event_to_activity_log(self._activity_log, event)

                # Collect assistant text
                if event.get("type") == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text", "")
                            if t:
                                text_parts.append(t)

                # Explicit result event — Claude finished
                if event.get("type") == "result":
                    result_event = event
                    done.set()
                    return

                # end_turn stop reason is a reliable completion signal
                if event.get("type") == "assistant":
                    msg = event.get("message", {})
                    if msg.get("stop_reason") == "end_turn":
                        # Give Claude a moment to emit result before we stop
                        await asyncio.sleep(0.5)
                        done.set()
                        return

                if on_event is not None:
                    await on_event(event)

        async def _idle_watchdog() -> None:
            while not done.is_set():
                await asyncio.sleep(min(idle_timeout, 10.0))
                if done.is_set():
                    return
                idle = time.monotonic() - self._last_event_time
                if idle >= idle_timeout:
                    done.set()
                    return

        read_task = asyncio.ensure_future(_read_loop())
        watchdog_task = asyncio.ensure_future(_idle_watchdog())

        await done.wait()

        read_task.cancel()
        watchdog_task.cancel()
        # Absorb cancellation exceptions
        for t in (read_task, watchdog_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        return "\n".join(text_parts), result_event

    def get_exit_code(self) -> int | None:
        """Return process exit code if finished, else None."""
        return self._proc.returncode

    async def close(self) -> int:
        """Gracefully close the session; returns the process exit code."""
        if self._closed:
            return self._proc.returncode or 0
        self._closed = True

        if self._proc.returncode is None:
            try:
                if self._proc.stdin and not self._proc.stdin.is_closing():
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

        return self._proc.returncode or 0

    @property
    def duration(self) -> float:
        return time.monotonic() - self._start_time
