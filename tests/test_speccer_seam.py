"""The real speccer CLI driven by the real orchestrator state machine.

The fake tmux runs each speccer command in-process (speccer.cli._cmd_run_async
/ _cmd_generate_async) and turns its SystemExit into the exit file. The
speccer's Claude sessions and the brain calls (answer_questions, diagnosis)
are scripted. Nothing real is spawned.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import conductor.core.claude as claude_mod
import conductor.core.orchestrator as orch
import speccer.cli as sp
from conductor.core import brain
from conductor.core.claude import ClaudeResult
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import ConductorConfig, advance_run

MARKER = "[NEEDS CLARIFICATION: which database?]"
Q1 = "## Round 1 Questions\n\n### Q1: Database\n**Question:** Which database?\n**Answer:**\n"
COMPLETE = "<promise>SPEC_COMPLETE</promise>"
NEEDS_INPUT = "<promise>SPEC_NEEDS_INPUT</promise>"


def _result(text: str) -> ClaudeResult:
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text})
    return ClaudeResult(exit_code=0, output=out + "\n", tokens_used=None, cost=None, duration=1.0)


class SpeccerSessions:
    """Scripted speccer Claude sessions; each step edits the spec dir and returns a verdict."""

    def __init__(self, step):
        self.step = step
        self.calls = 0

    async def __call__(self, prompt, **kw):
        cwd = Path(kw["cwd"])
        if cwd.name != "spec":  # speccer generate runs in the docs dir
            for name in ("PRD.md", "TECHNICAL-DESIGN.md", "API-CONTRACTS.md",
                         "IMPLEMENTATION-PLAN.md"):
                (cwd / name).write_text("x\n")
            (cwd / "prompts").mkdir(exist_ok=True)
            (cwd / "prompts" / "01-build.md").write_text("Build it.\n")
            (cwd / "run.sh").write_text('PHASES=("01-build")\n')
            return _result("Generated.")
        self.calls += 1
        return _result(self.step(cwd, self.calls))


class Brain:
    """Scripted brain: answers questions, and says RETRY to every diagnosis."""

    def __init__(self, answer="### Q1\nPostgres."):
        self.answer = answer
        self.answer_calls = 0
        self.diagnose_calls = 0

    async def __call__(self, prompt, **kw):
        if "on behalf of a product owner" in prompt:
            self.answer_calls += 1
            return _result(self.answer)
        self.diagnose_calls += 1
        return _result("ACTION: RETRY\nTry again.")


def _tmux(log: list):
    """Fake tmux that runs speccer in-process; logs (subcommand and flags, exit code)."""
    tmux = MagicMock()

    async def spawn(window, cmd, *, exit_file, **_):
        argv = shlex.split(cmd)
        start = next(
            i for i, tok in enumerate(argv)
            if i > 0 and argv[i - 1].endswith("speccer") and tok in ("run", "generate")
        )
        args = sp._parse_args(argv[start:])
        code = 0
        try:
            if args.subcommand == "run":
                await sp._cmd_run_async(sp._resolve_spec_dir(args), args.cont)
            else:
                await sp._cmd_generate_async(sp._resolve_spec_dir(args), False)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        log.append((args.subcommand + (" --continue" if args.cont else ""), code))
        Path(exit_file).write_text(str(code))

    tmux.spawn_runner_in_window = AsyncMock(side_effect=spawn)
    tmux.is_window_alive = AsyncMock(return_value=True)
    tmux.is_runner_idle = AsyncMock(return_value=False)
    return tmux


def _env(tmp_path: Path, *, overnight: bool):
    wt = tmp_path / "wt"
    spec = wt / "docs" / "auth" / "spec"
    sp.cmd_init("auth", spec, mode="backend", preset="", constitution=False)
    (spec / "FEATURE-DESCRIPTION.md").write_text("Login with email.\n")
    stage = StageState(name="backend", spec_mode="backend", status=StageStatus.SPEC_INIT)
    stage.worktree = str(wt)
    run = RunState(index=0, name="auth", description="login", stages=[stage],
                   status=RunStatus.ACTIVE)
    state = ConductorState(project_name="proj", runs=[run], overnight=overnight)
    home = tmp_path / "home"

    def brain_calls_dir(name):
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


async def _drive(state, storage, tmux, overnight, steps=40) -> str:
    """advance_run like the main loop does, until the stage blocks, waits for a human or leaves the spec."""
    cfg = ConductorConfig(check_interval_s=0, overnight=overnight)
    run = state.runs[0]
    for _ in range(steps):
        stage = run.stages[run.current_stage]
        if stage.status == StageStatus.BLOCKED:
            return "blocked"
        if stage.status in (StageStatus.GENERATED, StageStatus.EXECUTING):
            return "generated"
        if orch._waiting_reason(state, run, overnight, orch.datetime.now(orch.timezone.utc)) == "human":
            return "human"
        await advance_run(state, 0, tmux, storage, cfg)
    return "steps exhausted"


def _rejected_every_time(spec_dir: Path, n: int) -> str:
    # Every session leaves the same marker and claims completion.
    (spec_dir / "domains" / "api.md").write_text(f"# API\n\nStore users in {MARKER}\n")
    if n == 1:
        (spec_dir / "QUESTIONS.md").write_text(Q1)
    return COMPLETE


# ---------------------------------------------------------------------------
# Rejected SPEC_COMPLETE claims: speccer stops for a human (EXIT_NEEDS_HUMAN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("overnight", [True, False])
async def test_rejected_completions_wait_for_a_human(tmp_path, monkeypatch, overnight):
    state, storage, spec = _env(tmp_path, overnight=overnight)
    sessions = SpeccerSessions(_rejected_every_time)
    monkeypatch.setattr(claude_mod, "run_claude", sessions)
    fake_brain = Brain()
    monkeypatch.setattr(brain, "run_claude", fake_brain)
    calls: list = []

    outcome = await _drive(state, storage, _tmux(calls), overnight)

    stage = state.runs[0].stages[0]
    assert outcome == "human", calls
    assert sessions.calls == sp.MAX_REJECTED_COMPLETIONS
    assert [code for _, code in calls] == [1, 1, sp.EXIT_NEEDS_HUMAN]
    assert fake_brain.diagnose_calls == 0
    assert stage.status == StageStatus.SPEC_NEEDS_INPUT
    assert stage.spec_failures == 0
    message = orch._questions_waiting_message(state, state.runs[0], overnight)
    assert f"{spec}/HANDOFF.md" in message
    assert f"set REJECTED_COMPLETIONS: 0 in {spec}/PROGRESS.md" in message


@pytest.mark.asyncio
async def test_waiting_for_a_human_survives_a_conductor_restart(tmp_path, monkeypatch):
    state, storage, _ = _env(tmp_path, overnight=True)
    monkeypatch.setattr(claude_mod, "run_claude", SpeccerSessions(_rejected_every_time))
    fake_brain = Brain()
    monkeypatch.setattr(brain, "run_claude", fake_brain)
    calls: list = []
    tmux = _tmux(calls)
    assert await _drive(state, storage, tmux, True) == "human"
    first = len(calls)

    # The next `conductor run`, also from a stage an older conductor had blocked.
    for blocked in (False, True):
        if blocked:
            state.runs[0].stages[0].status = StageStatus.BLOCKED
            state.runs[0].status = RunStatus.BLOCKED
        await orch._auto_resume_blocked_runs(state, storage, None, None, tmux=None)
        orch.activate_ready_runs(state)
        assert await _drive(state, storage, tmux, True) == "human"

    assert len(calls) == first, "speccer was started again while it waits for a human"
    assert fake_brain.diagnose_calls == 0


@pytest.mark.asyncio
async def test_resetting_the_rejection_count_resumes_the_spec(tmp_path, monkeypatch):
    state, storage, spec = _env(tmp_path, overnight=False)

    def step(spec_dir, n):
        if n <= sp.MAX_REJECTED_COMPLETIONS:
            return _rejected_every_time(spec_dir, n)
        return COMPLETE

    sessions = SpeccerSessions(step)
    monkeypatch.setattr(claude_mod, "run_claude", sessions)
    monkeypatch.setattr(brain, "run_claude", Brain())
    tmux = _tmux([])
    assert await _drive(state, storage, tmux, False) == "human"

    # What the waiting message asks for (Q1 is answered too).
    (spec / "domains" / "api.md").write_text("# API\n\nStore users in Postgres.\n")
    (spec / "QUESTIONS.md").write_text(Q1 + "> Postgres.\n")
    sp.update_rejected_completions(spec, 0)

    assert await _drive(state, storage, tmux, False) == "generated"
    assert sessions.calls == sp.MAX_REJECTED_COMPLETIONS + 1


# ---------------------------------------------------------------------------
# A rejection that left no questions: speccer goes on without answers
# ---------------------------------------------------------------------------

# What spec agents really leave in QUESTIONS.md when they have no questions.
NO_OPEN_QUESTIONS = (
    "# Questions\n\n"
    "All Round 1 questions (Q1-Q8) have been answered and incorporated into domain specs.\n"
    "No open questions. Spec is complete.\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("overnight", [True, False])
@pytest.mark.parametrize("questions", ["", NO_OPEN_QUESTIONS], ids=["blank", "no-open-questions"])
async def test_rejection_without_questions_continues(tmp_path, monkeypatch, overnight, questions):
    state, storage, spec = _env(tmp_path, overnight=overnight)

    def step(spec_dir, n):
        if n == 1:
            (spec_dir / "domains" / "api.md").write_text(f"# API\n\n{MARKER}\n")
            (spec_dir / "QUESTIONS.md").write_text(questions)
        else:
            (spec_dir / "domains" / "api.md").write_text("# API\n\nPostgres.\n")
        return COMPLETE

    sessions = SpeccerSessions(step)
    monkeypatch.setattr(claude_mod, "run_claude", sessions)
    fake_brain = Brain(answer="There are no open questions to answer.")
    monkeypatch.setattr(brain, "run_claude", fake_brain)

    outcome = await _drive(state, storage, _tmux([]), overnight)

    assert outcome == "generated"
    assert sessions.calls == 2
    assert fake_brain.answer_calls == 0
    assert (spec / "QUESTIONS.md").read_text() == questions
