"""Additional tests for conductor.core.claude — resolve_model, calculate_cost, NDJSON."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from conductor.core.claude import (
    ClaudeResult,
    resolve_model,
    calculate_cost,
    run_claude_steerable,
)


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


def test_resolve_model_opus():
    assert resolve_model("opus") == "claude-opus-4-6[1m]"


def test_resolve_model_opus_200k():
    assert resolve_model("opus-200k") == "claude-opus-4-6"


def test_resolve_model_sonnet():
    assert resolve_model("sonnet") == "claude-sonnet-4-6"


def test_resolve_model_haiku():
    assert resolve_model("haiku") == "claude-haiku-4-5"


def test_resolve_model_unknown_passthrough():
    assert resolve_model("claude-custom-model") == "claude-custom-model"


def test_resolve_model_empty_string_passthrough():
    assert resolve_model("") == ""


# ---------------------------------------------------------------------------
# calculate_cost
# ---------------------------------------------------------------------------


def test_calculate_cost_none_input():
    assert calculate_cost(None) is None


def test_calculate_cost_empty_dict():
    assert calculate_cost({}) is None


def test_calculate_cost_known_values():
    tokens = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    cost = calculate_cost(tokens)
    # input: 1M * 15.0 / 1M = 15.0
    # output: 1M * 75.0 / 1M = 75.0
    assert cost == pytest.approx(90.0)


def test_calculate_cost_cache_read():
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 0,
    }
    cost = calculate_cost(tokens)
    assert cost == pytest.approx(1.5)


def test_calculate_cost_cache_write():
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
    }
    cost = calculate_cost(tokens)
    assert cost == pytest.approx(18.75)


def test_calculate_cost_all_zeros():
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    cost = calculate_cost(tokens)
    assert cost == pytest.approx(0.0)


def test_calculate_cost_small_values():
    tokens = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    cost = calculate_cost(tokens)
    expected = 100 * 15.0 / 1_000_000 + 50 * 75.0 / 1_000_000
    assert cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# SteerableSession.send() produces correct NDJSON
# ---------------------------------------------------------------------------


def _make_proc_with_lines(lines: list[bytes]) -> MagicMock:
    proc = MagicMock()
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()

    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    stdin.is_closing = MagicMock(return_value=False)
    proc.stdin = stdin

    stdout = MagicMock()
    lines_iter = iter(lines + [b""])

    async def readline():
        return next(lines_iter)

    stdout.readline = readline
    proc.stdout = stdout
    return proc


@pytest.mark.asyncio
async def test_steerable_send_produces_correct_ndjson():
    """send() must produce {type, message, session_id, parent_tool_use_id} on one line."""
    proc = _make_proc_with_lines(
        [
            b'{"type":"result","result":{"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n',
        ]
    )

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("initial prompt")

    await session.send("test message")

    # First write is the initial prompt; second is our send()
    writes = proc.stdin.write.call_args_list
    assert len(writes) >= 2

    last_write = writes[-1][0][0].decode("utf-8")
    assert last_write.endswith("\n"), "NDJSON must end with newline"

    data = json.loads(last_write.strip())
    assert data == {
        "type": "user",
        "message": {"role": "user", "content": "test message"},
        "session_id": "default",
        "parent_tool_use_id": None,
    }


@pytest.mark.asyncio
async def test_steerable_initial_write_is_correct_ndjson():
    """The initial prompt write must also be correct NDJSON."""
    proc = _make_proc_with_lines(
        [
            b'{"type":"result","result":{"usage":{"input_tokens":1,"output_tokens":1,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n',
        ]
    )

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        await run_claude_steerable("hello world")

    first_write = proc.stdin.write.call_args_list[0][0][0].decode("utf-8")
    data = json.loads(first_write.strip())
    assert data["type"] == "user"
    assert data["message"]["role"] == "user"
    assert data["message"]["content"] == "hello world"
    assert data["session_id"] == "default"
    assert data["parent_tool_use_id"] is None


@pytest.mark.asyncio
async def test_steerable_send_with_special_chars():
    """send() must correctly JSON-encode messages with quotes and newlines."""
    proc = _make_proc_with_lines([b""])

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        session = await run_claude_steerable("init")

    msg = 'Message with "quotes" and\nnewlines'
    await session.send(msg)

    last_write = proc.stdin.write.call_args_list[-1][0][0].decode("utf-8")
    data = json.loads(last_write.strip())
    assert data["message"]["content"] == msg
