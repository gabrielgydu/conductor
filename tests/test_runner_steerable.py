"""runner.steerable.SteerableSession against a fake stream-json `claude` on PATH."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

import runner.steerable as steerable
from runner.steerable import SteerableSession

PROMISE = "<promise>P1_DONE</promise>"

# Emits the stream-json shape of CLI 2.1.x: init, a Read tool_use, the tool
# result (echoed twice, as the real CLI does), the promise text and a result
# event. FAKE_CLAUDE_MODE picks the variant; afterwards it waits for stdin EOF
# like the real CLI in --input-format stream-json mode.
_FAKE_CLAUDE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys

    mode = os.environ.get("FAKE_CLAUDE_MODE", "normal")
    body_size = int(os.environ.get("FAKE_CLAUDE_BODY", "1000"))
    sys.stdin.readline()  # initial prompt

    def emit(event):
        sys.stdout.write(json.dumps(event) + "\\n")
        sys.stdout.flush()

    emit({{"type": "system", "subtype": "init", "model": "claude-haiku-5-5"}})
    if mode == "silent":
        sys.stdin.read()
        sys.exit(0)
    emit({{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "name": "Read", "input": {{"file_path": "big.txt"}}}}]}}}})
    body = "x" * body_size
    emit({{"type": "user",
          "message": {{"content": [{{"type": "tool_result", "content": body}}]}},
          "tool_use_result": {{"file": {{"content": body}}}}}})
    emit({{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "All steps done. {promise}"}}]}}}})
    emit({{"type": "result", "subtype": "success", "total_cost_usd": 0.01,
          "usage": {{"input_tokens": 10, "output_tokens": 5}}}})
    if mode == "chatty":
        # Output nobody reads any more: more than the reader's pause threshold.
        for _ in range(64):
            sys.stdout.write("y" * 16384 + "\\n")
        sys.stdout.flush()
    sys.stdin.read()
    """
)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(_FAKE_CLAUDE.format(python=sys.executable, promise=PROMISE))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def configure(mode: str = "normal", body: int = 1000) -> None:
        monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
        monkeypatch.setenv("FAKE_CLAUDE_BODY", str(body))

    return configure


async def _run_session(tmp_path: Path, **stream_kwargs):
    session = await SteerableSession.launch("do it", activity_log=tmp_path / "activity.log")
    try:
        return await asyncio.wait_for(session.stream_events(**stream_kwargs), timeout=10)
    finally:
        await session.close()


async def test_small_tool_result_round_trip(fake_claude, tmp_path):
    fake_claude()
    text, result = await _run_session(tmp_path, idle_timeout=60)
    assert PROMISE in text
    assert result is not None and result["total_cost_usd"] == 0.01


async def test_tool_result_line_over_64k_is_read(fake_claude, tmp_path):
    # ~400 KB line: asyncio's default 64 KiB limit used to kill the reader, and
    # the promise and result after it were lost until the idle timeout.
    fake_claude(body=200_000)
    text, result = await _run_session(tmp_path, idle_timeout=60)
    assert PROMISE in text
    assert result is not None
    assert "Read: big.txt" in (tmp_path / "activity.log").read_text()


async def test_line_over_stream_limit_is_skipped_not_fatal(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setattr(steerable, "_STREAM_LIMIT", 64 * 1024)
    fake_claude(body=200_000)
    text, result = await _run_session(tmp_path, idle_timeout=60)
    assert PROMISE in text
    assert result is not None


async def test_dead_reader_ends_the_wait(fake_claude, tmp_path):
    fake_claude(mode="silent")

    async def boom(_event):
        raise RuntimeError("callback failed")

    start = time.monotonic()
    text, result = await _run_session(tmp_path, on_event=boom, idle_timeout=60)
    assert (text, result) == ("", None)
    assert time.monotonic() - start < 5


async def test_close_drains_unread_output(fake_claude, tmp_path, monkeypatch):
    # Claude keeps writing after the result event. Unread, that output paused
    # the stdout transport and close() never returned on Python 3.14.
    monkeypatch.setattr(steerable, "_STREAM_LIMIT", 64 * 1024)
    fake_claude(mode="chatty")
    session = await SteerableSession.launch("do it", activity_log=tmp_path / "activity.log")
    _text, result = await asyncio.wait_for(session.stream_events(idle_timeout=60), timeout=10)
    assert result is not None
    start = time.monotonic()
    rc = await asyncio.wait_for(session.close(), timeout=20)
    assert rc == 0
    assert time.monotonic() - start < 5
