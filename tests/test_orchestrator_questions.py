"""Speccer question rounds: auto-answers, their validation, and loop limits."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import conductor.core.brain as brain
import conductor.core.orchestrator as orch
from conductor.core.claude import ClaudeResult
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import ConductorConfig, advance_run

QUESTIONS = (
    "## Round 1 Questions\n\n"
    "### Q1: Scope\n**Question:** Which endpoints?\n**Answer:**\n\n---\n\n"
    "### Q2: Auth\n**Question:** Which auth method?\n**Answer:**\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _assistant(block: dict) -> dict:
    return {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [block]}}


class _FakeClaude:
    def __init__(self, output: str, exit_code: int = 0) -> None:
        self.output = output
        self.exit_code = exit_code
        self.calls: list[dict] = []

    async def __call__(self, prompt: str, **kwargs) -> ClaudeResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return ClaudeResult(
            exit_code=self.exit_code, output=self.output, tokens_used=None, cost=None,
            duration=1.0,
        )


def _answer(text: str) -> _FakeClaude:
    return _FakeClaude(
        _event_stream(
            _assistant({"type": "text", "text": text}),
            {"type": "result", "subtype": "success", "is_error": False, "result": text},
        )
    )


def _env(tmp_path: Path, *, questions: str | None = QUESTIONS, progress: str = "NEEDS_INPUT"):
    wt = tmp_path / "wt"
    spec = wt / "docs" / "auth" / "spec"
    spec.mkdir(parents=True)
    (spec / "PROGRESS.md").write_text(f"STATUS: {progress}\nITERATION: 2\n")
    if questions is not None:
        (spec / "QUESTIONS.md").write_text(questions)
    stage = StageState(name="backend", spec_mode="backend", status=StageStatus.SPEC_NEEDS_INPUT)
    stage.worktree = str(wt)
    run = RunState(
        index=0, name="auth", description="login", stages=[stage], status=RunStatus.ACTIVE
    )
    state = ConductorState(project_name="proj", runs=[run], overnight=True)
    home = tmp_path / "home"

    def brain_calls_dir(name: str) -> Path:
        d = home / name / "brain-calls"
        d.mkdir(parents=True, exist_ok=True)
        return d

    storage = SimpleNamespace(
        conductor_dir=lambda name: home / name,
        conductor_brief=lambda name: home / name / "BRIEF.md",
        conductor_state=lambda name: home / name / "state.json",
        tmux_log=lambda name, label: home / name / "logs" / f"{label}.log",
        brain_calls_dir=brain_calls_dir,
    )
    return state, storage, spec


def _tmux() -> MagicMock:
    """Fake tmux whose speccer windows exit 0 as soon as they are spawned."""
    tmux = MagicMock()

    def spawn(name, cmd, *, exit_file, **_):
        Path(exit_file).write_text("0")

    tmux.spawn_runner_in_window = AsyncMock(side_effect=spawn)
    return tmux


# ---------------------------------------------------------------------------
# answer_questions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answers_are_the_final_message_not_the_preamble(tmp_path, monkeypatch):
    state, storage, spec = _env(tmp_path)
    final = "### Q1\nAll /auth endpoints.\n### Q2\nJWT."
    fake = _FakeClaude(
        _event_stream(
            _assistant({"type": "text", "text": "Let me check the codebase first."}),
            _assistant({"type": "tool_use", "id": "t1", "name": "Grep", "input": {}}),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            _assistant({"type": "text", "text": final}),
            {"type": "result", "subtype": "success", "is_error": False, "result": final},
        )
    )
    monkeypatch.setattr(brain, "run_claude", fake)

    ok = await orch.answer_questions(state, 0, 0, storage)

    assert ok
    text = (spec / "QUESTIONS.md").read_text()
    assert "> ### Q1\n> All /auth endpoints.\n> ### Q2\n> JWT." in text
    assert "Let me check" not in text
    # Grounded in the project's code, read-only.
    assert fake.calls[0]["cwd"] == str(tmp_path / "wt")
    assert fake.calls[0]["append_args"] == ["--tools", "Read,Grep,Glob"]


@pytest.mark.asyncio
async def test_answers_missing_a_question_are_not_written(tmp_path, monkeypatch):
    state, storage, spec = _env(tmp_path)
    monkeypatch.setattr(brain, "run_claude", _answer("### Q1\nAll /auth endpoints."))

    ok = await orch.answer_questions(state, 0, 0, storage)

    assert not ok
    assert (spec / "QUESTIONS.md").read_text() == QUESTIONS
    assert state.runs[0].stages[0].spec_failures == 1


@pytest.mark.asyncio
async def test_failed_answer_call_backs_off_and_skips_continue(tmp_path, monkeypatch):
    state, storage, spec = _env(tmp_path)
    monkeypatch.setattr(brain, "run_claude", _FakeClaude("", exit_code=1))
    tmux = _tmux()

    with patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, ConductorConfig())

    stage = state.runs[0].stages[0]
    tmux.spawn_runner_in_window.assert_not_awaited()
    assert stage.status == StageStatus.SPEC_NEEDS_INPUT
    assert stage.backoff_until is not None and stage.backoff_until > datetime.now(timezone.utc)
    assert stage.spec_failures == 0
    assert (spec / "QUESTIONS.md").read_text() == QUESTIONS


# ---------------------------------------------------------------------------
# advance_run in SPEC_NEEDS_INPUT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_answered_questions_continue_without_a_brain_call(tmp_path, monkeypatch):
    """A human (or an earlier round) answered: don't append a second set of answers."""
    state, storage, _ = _env(tmp_path, questions=QUESTIONS + "\n> Use JWT\n")
    state.overnight = False
    fake = _answer("unused")
    monkeypatch.setattr(brain, "run_claude", fake)
    tmux = _tmux()

    with patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, ConductorConfig())

    assert fake.calls == []
    cmd = tmux.spawn_runner_in_window.await_args.args[1]
    assert "--continue" in cmd


@pytest.mark.asyncio
async def test_spec_finished_outside_conductor_is_picked_up(tmp_path):
    state, storage, _ = _env(tmp_path, progress="COMPLETE")
    state.overnight = False

    with patch.object(orch, "atomic_save"):
        await advance_run(state, 0, _tmux(), storage, ConductorConfig())

    assert state.runs[0].stages[0].status == StageStatus.SPEC_COMPLETE


@pytest.mark.asyncio
async def test_auto_answer_rounds_are_capped(tmp_path, monkeypatch):
    state, storage, _ = _env(tmp_path)
    state.runs[0].stages[0].question_rounds = orch.MAX_AUTO_QUESTION_ROUNDS
    fake = _answer("### Q1\na\n### Q2\nb")
    monkeypatch.setattr(brain, "run_claude", fake)
    tmux = _tmux()

    with patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, ConductorConfig())

    assert fake.calls == []
    tmux.spawn_runner_in_window.assert_not_awaited()
    stage = state.runs[0].stages[0]
    assert orch._waiting_reason(state, state.runs[0], True, datetime.now(timezone.utc)) == "human"
    assert stage.status == StageStatus.SPEC_NEEDS_INPUT


# ---------------------------------------------------------------------------
# speccer exits that leave the spec waiting for input
# ---------------------------------------------------------------------------


def _speccer_exits(state, storage, code: int) -> None:
    exit_file = orch._speccer_exit_file(storage, "proj", "auth")
    exit_file.write_text(str(code))
    orch.speccer_exit_code_handler(state, 0, 0, "auth", storage)


def test_repeated_failing_continues_fail_the_stage(tmp_path):
    """speccer exits 1 but leaves STATUS=NEEDS_INPUT: count it, FAIL after the limit."""
    state, storage, _ = _env(tmp_path)
    stage = state.runs[0].stages[0]

    for _ in range(orch.MAX_SPEC_FAILURES - 1):
        _speccer_exits(state, storage, 1)
        assert stage.status == StageStatus.SPEC_NEEDS_INPUT
    _speccer_exits(state, storage, 1)

    assert stage.status == StageStatus.FAILED
    assert stage.last_exit_code == 1


def test_clean_speccer_exit_resets_the_failure_count(tmp_path):
    state, storage, _ = _env(tmp_path)
    stage = state.runs[0].stages[0]
    for _ in range(orch.MAX_SPEC_FAILURES - 1):
        _speccer_exits(state, storage, 1)

    _speccer_exits(state, storage, 0)  # new round of questions, cleanly
    _speccer_exits(state, storage, 1)

    assert stage.spec_failures == 1
    assert stage.status == StageStatus.SPEC_NEEDS_INPUT
