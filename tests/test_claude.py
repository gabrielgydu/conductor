"""Tests for conductor.core.claude — TDD Phase 2."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from conductor.core.claude import ClaudeResult, run_claude, run_claude_steerable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_STREAM_JSON_LINES = [
    b'{"type":"assistant","content":"Hello"}\n',
    b'{"type":"result","result":{"usage":{"input_tokens":1000,"output_tokens":500,"cache_read_input_tokens":200,"cache_creation_input_tokens":100}}}\n',
]

SAMPLE_STREAM_JSON_OUTPUT = b"".join(SAMPLE_STREAM_JSON_LINES)


def make_mock_proc(
    stdout_bytes=SAMPLE_STREAM_JSON_OUTPUT, returncode=0, readline_lines=None
):
    """Build a mock asyncio subprocess."""
    proc = MagicMock()
    proc.returncode = returncode

    # communicate() returns (stdout, stderr)
    proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()

    # stdin
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    stdin.is_closing = MagicMock(return_value=False)
    proc.stdin = stdin

    # stdout — for streaming use
    if readline_lines is not None:
        stdout = MagicMock()
        lines_iter = iter(readline_lines + [b""])

        async def readline():
            return next(lines_iter)

        stdout.readline = readline
        proc.stdout = stdout

    return proc


# ---------------------------------------------------------------------------
# run_claude tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_claude_basic():
    proc = make_mock_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("Say hello")
    assert isinstance(result, ClaudeResult)
    assert result.exit_code == 0
    assert result.tokens_used is not None
    assert result.tokens_used["input_tokens"] == 1000
    assert result.tokens_used["output_tokens"] == 500


@pytest.mark.asyncio
async def test_run_claude_with_model():
    proc = make_mock_proc()
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_claude("hello", model="claude-opus-4-6")
    call_args = mock_exec.call_args[0]
    assert "--model" in call_args
    idx = list(call_args).index("--model")
    assert call_args[idx + 1] == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_run_claude_prompt_via_stdin():
    proc = make_mock_proc()
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_claude("my prompt text")
    # prompt must NOT appear in command args
    call_args = mock_exec.call_args[0]
    assert "my prompt text" not in call_args
    # must be passed via communicate stdin
    communicate_call = proc.communicate.call_args
    assert communicate_call[0][0] == b"my prompt text"


@pytest.mark.asyncio
async def test_run_claude_nonzero_exit():
    proc = make_mock_proc(returncode=1)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("fail")
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_run_claude_append_args():
    proc = make_mock_proc()
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_claude("hello", append_args=["--flag", "value"])
    call_args = list(mock_exec.call_args[0])
    assert "--flag" in call_args
    assert "value" in call_args


# ---------------------------------------------------------------------------
# SteerableSession tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steerable_session_send():
    lines = [
        b'{"type":"assistant","content":"Hi"}\n',
        b'{"type":"result","result":{"usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n',
        b"",
    ]
    proc = make_mock_proc(readline_lines=lines)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("initial")

    await session.send("follow-up")

    written = proc.stdin.write.call_args_list
    # first call is initial prompt, second is follow-up
    assert len(written) >= 2
    last_write = written[-1][0][0]
    data = json.loads(last_write.decode())
    assert data["type"] == "user"
    assert data["message"]["role"] == "user"
    assert data["message"]["content"] == "follow-up"
    assert data["session_id"] == "default"
    assert data["parent_tool_use_id"] is None


@pytest.mark.asyncio
async def test_steerable_session_poll():
    lines = [
        b'{"type":"assistant","content":"Hello"}\n',
        b"not-json\n",
        b'{"type":"ping"}\n',
        b"",
    ]
    proc = make_mock_proc(readline_lines=lines)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("hi")

    events = []
    async for event in session.poll():
        events.append(event)

    # non-JSON line skipped, 2 valid events
    assert len(events) == 2
    assert events[0]["type"] == "assistant"
    assert events[1]["type"] == "ping"


@pytest.mark.asyncio
async def test_steerable_session_wait_result():
    lines = [
        b'{"type":"assistant","content":"Hi"}\n',
        b'{"type":"result","result":{"usage":{"input_tokens":100,"output_tokens":50,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n',
        b"",
    ]
    proc = make_mock_proc(readline_lines=lines)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("test")

    result = await session.wait(timeout=30)
    assert isinstance(result, ClaudeResult)
    assert result.tokens_used["input_tokens"] == 100
    assert result.tokens_used["output_tokens"] == 50


@pytest.mark.asyncio
async def test_steerable_session_idle_timeout():
    # readline never returns data (simulates hanging)
    proc = MagicMock()
    proc.returncode = None
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=-9)

    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    stdin.is_closing = MagicMock(return_value=False)
    proc.stdin = stdin

    stdout = MagicMock()

    async def slow_readline():
        await asyncio.sleep(100)  # never returns in time
        return b""

    stdout.readline = slow_readline
    proc.stdout = stdout

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("test")

    result = await session.wait(timeout=0.05)
    assert proc.kill.called
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_steerable_session_close_idempotent():
    lines = [b""]
    proc = make_mock_proc(readline_lines=lines)
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("hi")

    # close twice — no error
    await session.close()
    await session.close()


@pytest.mark.asyncio
async def test_steerable_send_after_close():
    lines = [b""]
    proc = make_mock_proc(readline_lines=lines)
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("hi")

    await session.close()
    with pytest.raises(RuntimeError, match="Session closed"):
        await session.send("after close")


@pytest.mark.asyncio
async def test_ndjson_format():
    lines = [
        b'{"type":"result","result":{"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n',
        b"",
    ]
    proc = make_mock_proc(readline_lines=lines)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        await run_claude_steerable("hello world")

    # The initial prompt write
    first_write = proc.stdin.write.call_args_list[0][0][0]
    line = first_write.decode("utf-8")
    assert line.endswith("\n")
    data = json.loads(line.strip())
    assert data == {
        "type": "user",
        "message": {"role": "user", "content": "hello world"},
        "session_id": "default",
        "parent_tool_use_id": None,
    }


@pytest.mark.asyncio
async def test_invalid_json_in_stdout():
    lines = [
        b"not-json\n",
        b"also not json!\n",
        b'{"type":"result","result":{"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n',
        b"",
    ]
    proc = make_mock_proc(readline_lines=lines)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("test")

    events = []
    async for event in session.poll():
        events.append(event)
    # only the valid JSON line
    assert len(events) == 1
    assert events[0]["type"] == "result"
