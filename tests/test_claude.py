"""Tests for conductor.core.claude — TDD Phase 2."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from conductor.core.claude import ClaudeResult, run_claude, run_claude_steerable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Real stream-json transcripts (Claude Code CLI 2.1.304, claude-haiku-5-5),
# captured with `claude -p - --output-format stream-json --verbose` and
# scrubbed of local paths and IDs.
STREAM_JSON_FIXTURES = Path(__file__).parent / "fixtures" / "stream_json"


def load_stream_json(name: str) -> bytes:
    return (STREAM_JSON_FIXTURES / f"{name}.jsonl").read_bytes()


# A Read tool call and a final answer: system init, assistant, user,
# rate_limit_event, assistant, result (subtype success).
SAMPLE_STREAM_JSON_OUTPUT = load_stream_json("success")
SAMPLE_STREAM_JSON_LINES = SAMPLE_STREAM_JSON_OUTPUT.splitlines(keepends=True)
SAMPLE_EVENT_TYPES = ["system", "assistant", "user", "rate_limit_event", "assistant", "result"]


def make_mock_proc(
    stdout_bytes=SAMPLE_STREAM_JSON_OUTPUT, returncode=0, readline_lines=None
):
    """Build a mock asyncio subprocess for run_claude: stdin write/drain/
    close/wait_closed, stdout.readline, and stderr.read drained to EOF.
    """
    proc = MagicMock()
    proc.returncode = returncode

    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()

    # stdin
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    stdin.wait_closed = AsyncMock()
    stdin.is_closing = MagicMock(return_value=False)
    proc.stdin = stdin

    # stdout — line by line
    if readline_lines is None:
        readline_lines = stdout_bytes.splitlines(keepends=True)
    stdout = MagicMock()
    lines_iter = iter(readline_lines + [b""])

    async def readline():
        return next(lines_iter)

    stdout.readline = readline
    proc.stdout = stdout

    # stderr — drained in the background
    stderr = MagicMock()
    stderr.read = AsyncMock(return_value=b"")
    proc.stderr = stderr

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
    assert result.output == SAMPLE_STREAM_JSON_OUTPUT.decode()
    assert result.tokens_used == {
        "input_tokens": 4,
        "output_tokens": 149,
        "cache_read_input_tokens": 28539,
        "cache_creation_input_tokens": 5815,
    }


@pytest.mark.asyncio
async def test_run_claude_without_streaming(capsys):
    """on_event=None reads the same output but prints no progress."""
    proc = make_mock_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("Say hello", on_event=None)
    proc.stdin.write.assert_called_once_with(b"Say hello")
    assert capsys.readouterr().err == ""
    assert result.exit_code == 0
    assert result.output == SAMPLE_STREAM_JSON_OUTPUT.decode()
    assert result.tokens_used["input_tokens"] == 4
    assert result.tokens_used["output_tokens"] == 149


@pytest.mark.asyncio
async def test_run_claude_streams_events_to_callback():
    lines = [
        b"not-json\n",
        b"\n",
        b'["not", "a", "dict"]\n',
        *SAMPLE_STREAM_JSON_LINES,
    ]
    proc = make_mock_proc(readline_lines=lines)
    events = []
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("hi", on_event=events.append)
    # only JSON objects reach the callback, in stream order
    assert [e["type"] for e in events] == SAMPLE_EVENT_TYPES
    # raw output keeps every line, including the ones that were not events
    assert result.output == b"".join(lines).decode()
    assert result.tokens_used["output_tokens"] == 149


@pytest.mark.asyncio
async def test_run_claude_default_prints_progress(capsys):
    lines = [
        b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls -la"}}]}}\n',
        *SAMPLE_STREAM_JSON_LINES,
    ]
    proc = make_mock_proc(readline_lines=lines)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        await run_claude("hi")
    assert "Bash: ls -la" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_claude_reports_cli_cost_and_result():
    proc = make_mock_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("hi", on_event=None)
    assert result.cost == 0.00152329  # total_cost_usd, not a table estimate
    assert result.result_event["type"] == "result"
    assert result.subtype == "success"
    assert not result.is_error and not result.failed
    assert result.num_turns == 2
    assert result.result_text == "first line of hello\nDONE"
    assert result.model_usage["claude-haiku-5-5"]["costUSD"] == 0.00152329


@pytest.mark.asyncio
async def test_run_claude_max_turns_is_an_error_result():
    proc = make_mock_proc(stdout_bytes=load_stream_json("error_max_turns"), returncode=1)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("hi", on_event=None)
    assert result.exit_code == 1
    assert result.is_error and result.failed
    assert result.subtype == "error_max_turns"
    assert result.result_text is None  # the CLI sends `errors` instead
    assert result.cost == 0.00024006
    assert result.tokens_used["output_tokens"] == 138


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [1, 0])
async def test_run_claude_error_result_fails_whatever_the_exit_code(returncode):
    """Not logged in / unknown model arrive as an is_error result with the
    message as result text; treat it as a failure even with exit code 0."""
    proc = make_mock_proc(stdout_bytes=load_stream_json("model_not_found"), returncode=returncode)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("hi", on_event=None)
    assert result.is_error and result.failed
    assert result.result_text.startswith("There's an issue with the selected model")
    assert result.cost == 0.0
    assert result.model_usage == {}


@pytest.mark.asyncio
async def test_run_claude_without_result_event():
    proc = make_mock_proc(stdout_bytes=b'{"type":"assistant","message":{"content":[]}}\n')
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("hi", on_event=None)
    assert result.result_event is None
    assert result.tokens_used is None and result.cost is None
    assert result.subtype is None and result.num_turns is None and result.result_text is None
    assert not result.failed


@pytest.mark.asyncio
async def test_run_claude_with_model():
    proc = make_mock_proc()
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_claude("hello", model="claude-opus-4-8")
    call_args = mock_exec.call_args[0]
    assert "--model" in call_args
    idx = list(call_args).index("--model")
    assert call_args[idx + 1] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_run_claude_prompt_via_stdin():
    proc = make_mock_proc()
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ) as mock_exec:
        await run_claude("my prompt text")
    # prompt must NOT appear in command args; claude reads it from stdin ("-p -")
    call_args = mock_exec.call_args[0]
    assert "my prompt text" not in call_args
    assert call_args[call_args.index("-p") + 1] == "-"
    # streaming path writes the prompt to stdin, then closes it so claude sees EOF
    proc.stdin.write.assert_called_once_with(b"my prompt text")
    proc.stdin.close.assert_called_once()
    proc.stdin.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_claude_nonzero_exit():
    proc = make_mock_proc(returncode=1)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("fail")
    assert result.exit_code == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("on_event", [None, lambda event: None], ids=["communicate", "streaming"])
async def test_run_claude_exits_before_reading_prompt(tmp_path, monkeypatch, on_event):
    """claude dying before it reads stdin (bad flag, not logged in) must surface
    as its exit code, not as BrokenPipeError — real pipes, prompt > pipe buffer."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    result = await asyncio.wait_for(run_claude("x" * 1_000_000, on_event=on_event), timeout=10)
    assert result.exit_code == 3
    assert result.output == ""
    assert result.tokens_used is None


def install_fake_claude(tmp_path, monkeypatch, script: str) -> Path:
    """Put a shell-script `claude` first on PATH; returns its pid file.

    The script runs with $PIDFILE set; scripts that block `exec` into the
    blocking command so the CLI's pid is the one that has to be stopped.
    """
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n" + script)
    fake.chmod(0o755)
    pidfile = tmp_path / "claude.pid"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return pidfile


def assert_reaped(pidfile: Path) -> None:
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # killed *and* reaped: not even a zombie is left


# The pid file is written first: once the event is out, run_claude may stop
# the script at any moment (test_run_claude_error_stops_the_cli aborts on it).
_HANGING_CLAUDE = """\
echo $$ > "$PIDFILE"
cat >/dev/null
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}'
exec sleep 30
"""


@pytest.mark.asyncio
async def test_run_claude_timeout_stops_the_cli(tmp_path, monkeypatch):
    pidfile = install_fake_claude(tmp_path, monkeypatch, _HANGING_CLAUDE)
    result = await asyncio.wait_for(run_claude("hi", on_event=None, timeout_s=0.5), timeout=10)
    assert result.timed_out and result.failed
    assert result.exit_code != 0
    assert '"text":"working"' in result.output  # output up to the timeout is kept
    assert result.result_event is None
    assert_reaped(pidfile)


@pytest.mark.asyncio
async def test_run_claude_cancel_stops_the_cli(tmp_path, monkeypatch):
    """Cancelling the coroutine used to leave claude running, orphaned."""
    pidfile = install_fake_claude(tmp_path, monkeypatch, _HANGING_CLAUDE)
    task = asyncio.create_task(run_claude("hi", on_event=None))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert_reaped(pidfile)


class _Abort(BaseException):
    pass


@pytest.mark.asyncio
async def test_run_claude_error_stops_the_cli(tmp_path, monkeypatch):
    pidfile = install_fake_claude(tmp_path, monkeypatch, _HANGING_CLAUDE)

    def abort(event):
        raise _Abort()

    with pytest.raises(_Abort):
        await asyncio.wait_for(run_claude("hi", on_event=abort), timeout=10)
    assert_reaped(pidfile)


@pytest.mark.asyncio
async def test_run_claude_survives_a_failing_callback(capsys):
    """A bug in a progress printer must not cost the session's result."""
    calls = []

    def flaky(event):
        calls.append(event["type"])
        raise AttributeError("'NoneType' object has no attribute 'get'")

    proc = make_mock_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_claude("hi", on_event=flaky)
    assert calls == SAMPLE_EVENT_TYPES  # still called for every event
    assert result.subtype == "success" and result.cost == 0.00152329
    assert capsys.readouterr().err.count("on_event callback failed") == 1


@pytest.mark.asyncio
async def test_run_claude_keeps_stderr_tail(tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, """\
cat >/dev/null
head -c 10000 /dev/zero | tr '\\0' x >&2
echo '[claude-code:unrecognized_model] {"model":"nope"}' >&2
exit 1
""")
    result = await asyncio.wait_for(run_claude("hi", on_event=None), timeout=10)
    assert result.exit_code == 1 and result.failed
    assert len(result.stderr) == 4096
    assert result.stderr.endswith('[claude-code:unrecognized_model] {"model":"nope"}\n')
    assert result.stderr.startswith("xxx")


@pytest.mark.asyncio
async def test_run_claude_skips_a_line_over_the_stream_limit(tmp_path, monkeypatch):
    """An oversized line used to raise ValueError and abandon the session."""
    import conductor.core.claude as claude_mod

    monkeypatch.setattr(claude_mod, "_STREAM_LIMIT", 4096)
    fixture = STREAM_JSON_FIXTURES / "success.jsonl"
    install_fake_claude(tmp_path, monkeypatch, f"""\
cat >/dev/null
printf '{{"type":"user","message":{{"content":"%s"}}}}\\n' "$(head -c 20000 /dev/zero | tr '\\0' y)"
cat '{fixture}'
""")
    result = await asyncio.wait_for(run_claude("hi", on_event=None), timeout=10)
    assert result.exit_code == 0
    assert result.subtype == "success" and result.cost == 0.00152329
    # The oversized line is dropped; at most a non-JSON tail fragment of it remains.
    assert "y" * 4096 not in result.output


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
