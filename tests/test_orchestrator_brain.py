"""Brain calls: prompt templates, reply extraction, lenient ACTION parsing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import conductor.core.brain as brain
import conductor.core.orchestrator as orch
from conductor.core.claude import ClaudeResult
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import ConductorConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _text(text: str, parent: str | None = None) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool_use() -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
    }


def _result(text: str | None = None, subtype: str = "success", is_error: bool = False) -> dict:
    event = {"type": "result", "subtype": subtype, "is_error": is_error}
    if text is not None:
        event["result"] = text
    return event


def _claude(output: str, exit_code: int = 0) -> ClaudeResult:
    return ClaudeResult(
        exit_code=exit_code, output=output, tokens_used=None, cost=None, duration=0.1
    )


class _FakeClaude:
    """Stands in for run_claude: records prompts and kwargs, replies with *text*."""

    def __init__(self, text: str = "", exit_code: int = 0) -> None:
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []
        self.text = text
        self.exit_code = exit_code

    async def __call__(self, prompt: str, **kwargs) -> ClaudeResult:
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        return _claude(_stream(_text(self.text), _result(self.text)), self.exit_code)


def _storage(tmp_path: Path) -> SimpleNamespace:
    base = tmp_path / "conductor-home"

    def brain_calls_dir(name: str) -> Path:
        d = base / name / "brain-calls"
        d.mkdir(parents=True, exist_ok=True)
        return d

    return SimpleNamespace(
        conductor_dir=lambda name: base / name,
        brain_calls_dir=brain_calls_dir,
    )


def _failed_spec_state(tmp_path: Path) -> ConductorState:
    wt = tmp_path / "wt"
    stage = StageState(name="backend", spec_mode="backend", status=StageStatus.FAILED)
    stage.worktree = str(wt)
    progress = wt / "docs" / "auth" / "spec" / "PROGRESS.md"
    progress.parent.mkdir(parents=True)
    progress.write_text("STATUS: INIT\nITERATION: 1\n")
    run = RunState(
        index=0, name="auth", description="login", stages=[stage], status=RunStatus.ACTIVE
    )
    return ConductorState(project_name="proj", runs=[run])


# ---------------------------------------------------------------------------
# read_reply
# ---------------------------------------------------------------------------


def test_read_reply_prefers_result_event_over_preamble():
    out = _stream(
        _text("Let me check the code first."),
        _tool_use(),
        _text("### Q1\nUse soft delete."),
        _result("### Q1\nUse soft delete."),
    )
    reply = brain.read_reply(_claude(out))
    assert reply.ok
    assert reply.text == "### Q1\nUse soft delete."


def test_read_reply_without_result_uses_text_after_last_tool_call():
    out = _stream(
        _text("Let me look around."),
        _tool_use(),
        _text("Answer part one."),
        _text("subagent chatter", parent="toolu_1"),
        _text("Answer part two."),
    )
    reply = brain.read_reply(_claude(out))
    assert reply.text == "Answer part one.\n\nAnswer part two."


@pytest.mark.parametrize(
    "out,exit_code",
    [
        (_stream(_text("Not logged in · Please run /login"), _result("x", is_error=True)), 1),
        (_stream(_text("partial"), _result(subtype="error_max_turns")), 0),
        ("", 0),
    ],
)
def test_read_reply_flags_failed_calls(out, exit_code):
    assert not brain.read_reply(_claude(out, exit_code)).ok


@pytest.mark.asyncio
async def test_ask_turns_missing_cli_and_timeout_into_failed_reply(monkeypatch):
    async def missing(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(brain, "run_claude", missing)
    assert not (await brain.ask("x", model="m", timeout_s=5)).ok

    async def hang(*a, **k):
        import asyncio

        await asyncio.sleep(10)

    monkeypatch.setattr(brain, "run_claude", hang)
    reply = await brain.ask("x", model="m", timeout_s=0.01)
    assert not reply.ok and "timed out" in reply.detail


# ---------------------------------------------------------------------------
# parse_action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ACTION: RETRY\nflaky network", "RETRY"),
        ("**Action:** retry\nflaky", "RETRY"),
        ("```\nACTION: BLOCK\nneeds a human\n```", "BLOCK"),
        ("Diagnosis: transient.\n\nI recommend ACTION: RETRY.", "RETRY"),
        ("Diagnosis: a retry should succeed", None),
        ("ACTION: DELETE_EVERYTHING", None),
    ],
)
def test_parse_action_is_lenient_but_only_accepts_allowed_verdicts(text, expected):
    action, _ = brain.parse_action(text, ("RETRY", "BLOCK"))
    assert action == expected


def test_parse_action_returns_the_text_after_the_verdict():
    action, rest = brain.parse_action(
        "```\nACTION: STEER\nStop re-reading the file.\nWrite the output.\n```",
        ("STEER", "RESET", "IGNORE"),
    )
    assert action == "STEER"
    assert rest == "Stop re-reading the file.\nWrite the output."


def test_diagnose_templates_carry_the_action_contract():
    failure = brain.render_prompt("diagnose-failure", "CTX")
    stall = brain.render_prompt("diagnose-stall", "CTX")
    assert "CTX" in failure and "{CONTEXT}" not in failure
    assert "ACTION: RETRY" in failure and "ACTION: BLOCK" in failure
    assert all(f"ACTION: {a}" in stall for a in ("STEER", "RESET", "IGNORE"))


# ---------------------------------------------------------------------------
# handle_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_diagnosis_sends_contract_and_speccer_evidence(tmp_path, monkeypatch):
    state = _failed_spec_state(tmp_path)
    storage = _storage(tmp_path)
    logs = storage.conductor_dir("proj") / "logs"
    logs.mkdir(parents=True)
    (logs / "speccer-run_auth_20261020_101500.log").write_text(
        "\x1b[31mError: No promise token detected\x1b[0m\n"
    )
    fake = _FakeClaude("ACTION: RETRY\nThe agent stopped early.")
    monkeypatch.setattr(brain, "run_claude", fake)
    monkeypatch.setattr(orch, "restart_stage", AsyncMock())

    await orch.handle_failure(state, 0, 0, storage, ConductorConfig())

    prompt = fake.prompts[0]
    assert "ACTION: RETRY" in prompt and "ACTION: BLOCK" in prompt
    assert "Error: No promise token detected" in prompt
    assert "\x1b[" not in prompt
    assert "STATUS: INIT" in prompt
    assert fake.kwargs[0]["append_args"] == ["--tools", ""]
    assert state.runs[0].stages[0].retries == 1


@pytest.mark.parametrize(
    ("last_error", "raised"),
    [
        ("speccer-run produced no output for 30 min; conductor stopped it", False),
        ("conductor itself raised while advancing the stage: OSError: disk full", True),
    ],
)
def test_failure_context_shows_what_conductor_recorded_as_is(tmp_path, last_error, raised):
    """An inactivity stop or a bad run.sh is not presented as a conductor crash."""
    state = _failed_spec_state(tmp_path)
    state.runs[0].stages[0].last_error = last_error

    context = orch._failure_context(state, 0, 0, _storage(tmp_path), 0, 3)

    assert f"## Recorded by Conductor\n{last_error}" in context
    assert ("raised" in context) is raised


@pytest.mark.asyncio
async def test_failure_diagnosis_without_verdict_retries_while_budget_remains(
    tmp_path, monkeypatch
):
    state = _failed_spec_state(tmp_path)
    monkeypatch.setattr(brain, "run_claude", _FakeClaude("Diagnosis: a retry should succeed"))
    restart = AsyncMock()
    monkeypatch.setattr(orch, "restart_stage", restart)

    await orch.handle_failure(state, 0, 0, _storage(tmp_path), ConductorConfig())

    restart.assert_awaited_once()
    assert state.runs[0].stages[0].retries == 1
    assert state.runs[0].stages[0].status != StageStatus.BLOCKED


@pytest.mark.asyncio
async def test_failure_diagnosis_block_verdict_in_markdown_blocks(tmp_path, monkeypatch):
    state = _failed_spec_state(tmp_path)
    monkeypatch.setattr(brain, "run_claude", _FakeClaude("**Action:** BLOCK\nMissing API key."))

    await orch.handle_failure(state, 0, 0, _storage(tmp_path), ConductorConfig())

    assert state.runs[0].stages[0].status == StageStatus.BLOCKED


@pytest.mark.asyncio
async def test_failed_diagnosis_call_backs_off_without_spending_a_retry(tmp_path, monkeypatch):
    state = _failed_spec_state(tmp_path)
    monkeypatch.setattr(brain, "run_claude", _FakeClaude("API Error: 529 overloaded", exit_code=1))
    restart = AsyncMock()
    monkeypatch.setattr(orch, "restart_stage", restart)

    await orch.handle_failure(state, 0, 0, _storage(tmp_path), ConductorConfig())

    stage = state.runs[0].stages[0]
    assert stage.retries == 0
    assert stage.backoff_until is not None
    assert stage.backoff_until > datetime.now(timezone.utc) + timedelta(seconds=20)
    restart.assert_awaited_once()


# ---------------------------------------------------------------------------
# check_stall
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stall_diagnosis_uses_template_and_steers(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    activity = wt / "docs" / "auth" / "activity.log"
    activity.parent.mkdir(parents=True)
    activity.write_text("reading foo.py\n")
    stage = StageState(name="backend", spec_mode="backend", status=StageStatus.EXECUTING)
    stage.worktree = str(wt)
    stage.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    run = RunState(
        index=0, name="auth", description="login", stages=[stage], status=RunStatus.ACTIVE
    )
    state = ConductorState(project_name="proj", runs=[run])
    run.monitor.last_progress_hash = orch.compute_progress_hash(state, 0, 0, None)
    run.monitor.stall_count = 1
    tmux = MagicMock()
    tmux.has_active_children = AsyncMock(return_value=False)
    fake = _FakeClaude("**Action:** steer\nStop re-reading foo.py and write the test.")
    monkeypatch.setattr(brain, "run_claude", fake)

    await orch.check_stall(state, 0, 0, tmux, _storage(tmp_path), ConductorConfig())

    assert "ACTION: STEER" in fake.prompts[0]
    msgs = [m.read_text() for m in (wt / "docs" / "auth" / "steer_inbox").glob("*.msg")]
    assert msgs == ["Stop re-reading foo.py and write the test."]
