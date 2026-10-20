"""conductor_run_loop resilience: one run's error must not stop the others."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import conductor.core.orchestrator as orch
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState
from conductor.core.orchestrator import ConductorConfig, conductor_run_loop
from conductor.core.storage import StorageResolver
from tests.helpers import make_conductor_state, make_run_state


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    """Storage under tmp_path, a no-op tmux, and no post-run Claude calls."""
    storage_base = tmp_path / "storage"

    def patched_init(self, repo_path: Path) -> None:
        self.repo_root = tmp_path / "repo"
        self._project_key = "test-project"
        self.base_dir = storage_base

    monkeypatch.setattr(StorageResolver, "__init__", patched_init)
    tmux = MagicMock()
    tmux._session_name = "conductor-test-proj"
    tmux.ensure_session = AsyncMock()
    tmux.is_window_alive = AsyncMock(return_value=False)
    tmux.is_runner_idle = AsyncMock(return_value=True)
    tmux.has_active_children = AsyncMock(return_value=False)
    tmux.get_pane_pid = AsyncMock(return_value=None)
    tmux.kill_window = AsyncMock()
    tmux.spawn_in_window_and_wait = AsyncMock(return_value=0)
    tmux.spawn_runner_in_window = AsyncMock()
    monkeypatch.setattr(orch._tmux_module, "TmuxManager", MagicMock(return_value=tmux))
    monkeypatch.setattr(orch, "conductor_post_run", AsyncMock())
    # The between-checks exit-file poll shells out to tmux directly.
    monkeypatch.setattr(orch, "_any_runner_exit_file_ready", lambda *a, **k: False)
    return tmux


def _state(*runs) -> ConductorState:
    return make_conductor_state("test-proj", list(runs))


def _active(index: int, name: str, status: StageStatus) -> object:
    run = make_run_state(index, name, status=RunStatus.ACTIVE)
    run.stages[0].status = status
    return run


@pytest.mark.asyncio
async def test_exception_in_one_run_fails_its_stage_and_loop_continues(loop_env):
    """A git error in run 0's advance must not kill the loop for run 1."""
    state = _state(
        _active(0, "feat-a", StageStatus.PENDING),
        _active(1, "feat-b", StageStatus.SPEC_COMPLETE),
    )
    config = ConductorConfig(check_interval_s=0.0, max_iterations=1, max_parallel=2)

    def boom(*args, **kwargs):
        raise RuntimeError("fatal: '/wt/feat-a' is a missing but already registered worktree")

    with (
        patch.object(orch, "create_worktree", side_effect=boom),
        patch.object(orch, "run_speccer_generate", new_callable=AsyncMock) as gen,
    ):
        result = await conductor_run_loop(state, config)

    stage_a = result.runs[0].stages[0]
    assert stage_a.status == StageStatus.FAILED
    assert "missing but already registered worktree" in stage_a.last_error
    gen.assert_awaited_once()  # run 1 was still advanced in the same iteration
    assert result.runs[1].stages[0].status == StageStatus.GENERATED


@pytest.mark.asyncio
async def test_error_inside_failure_handling_blocks_instead_of_spinning(loop_env):
    """handle_failure raising on a FAILED stage blocks it: no retry without budget."""
    state = _state(_active(0, "feat-a", StageStatus.FAILED))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=50)
    brain = AsyncMock(side_effect=FileNotFoundError("claude"))

    with patch.object(orch, "handle_failure", brain):
        result = await conductor_run_loop(state, config)

    assert brain.await_count == 1
    assert result.runs[0].stages[0].status == StageStatus.BLOCKED
    assert result.runs[0].status == RunStatus.BLOCKED
    assert "FileNotFoundError: claude" in result.runs[0].stages[0].last_error


@pytest.mark.asyncio
async def test_executing_stage_survives_a_single_monitor_error(loop_env):
    """A runner is not abandoned because one monitoring pass raised."""
    state = _state(_active(0, "feat-a", StageStatus.EXECUTING))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=1)

    with patch.object(
        orch, "monitor_runner", AsyncMock(side_effect=OSError("pstree vanished"))
    ):
        result = await conductor_run_loop(state, config)

    assert result.runs[0].stages[0].status == StageStatus.EXECUTING


@pytest.mark.asyncio
async def test_auto_resume_reactivates_run_blocked_after_its_last_stage(loop_env, tmp_path):
    """A run blocked in its post-run step (current_stage past the end) resumes."""
    run = make_run_state(0, "feat-a", status=RunStatus.BLOCKED)
    run.stages[0].status = StageStatus.DONE
    run.current_stage = 1
    state = _state(run)
    storage = StorageResolver(tmp_path)

    await orch._auto_resume_blocked_runs(state, storage, None, None)

    assert state.runs[0].status == RunStatus.PENDING
    assert orch.activate_ready_runs(state) == [0]


@pytest.mark.asyncio
async def test_resumed_runs_respect_max_parallel(loop_env, tmp_path):
    """Two runs blocked at different times must not both resume at once."""
    runs = []
    for i, name in enumerate(("feat-a", "feat-b")):
        run = make_run_state(i, name, status=RunStatus.BLOCKED)
        run.stages[0].status = StageStatus.DONE
        run.current_stage = 1
        runs.append(run)
    state = _state(*runs)

    await orch._auto_resume_blocked_runs(state, StorageResolver(tmp_path), None, None)

    assert orch.activate_ready_runs(state, max_parallel=1) == [0]
    assert state.runs[1].status == RunStatus.PENDING


def test_pending_run_listed_before_an_active_one_waits_for_its_slot():
    first = make_run_state(0, "feat-a", status=RunStatus.PENDING)
    second = make_run_state(1, "feat-b", status=RunStatus.ACTIVE)
    state = _state(first, second)

    assert orch.activate_ready_runs(state, max_parallel=1) == [1]
    assert first.status == RunStatus.PENDING


# ---------------------------------------------------------------------------
# Loop pacing: sleep when nothing can act, never burn max_iterations spinning
# ---------------------------------------------------------------------------


class _FakeSleep:
    """Records asyncio.sleep calls; runs *on_call(n)* after each one."""

    def __init__(self, on_call=None) -> None:
        self.calls: list[float] = []
        self._on_call = on_call

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._on_call is not None:
            self._on_call(len(self.calls))


def _write_questions(wt: Path, fname: str, text: str = "### Q1: Scope\n**Answer:**\n") -> Path:
    questions = wt / "docs" / fname / "spec" / "QUESTIONS.md"
    questions.parent.mkdir(parents=True, exist_ok=True)
    questions.write_text(text)
    return questions


def _block_run(state: ConductorState, run_idx: int = 0):
    def on_call(n: int) -> None:
        state.runs[run_idx].status = RunStatus.BLOCKED

    return on_call


@pytest.mark.asyncio
async def test_loop_sleeps_until_backoff_expires_instead_of_spinning(loop_env, monkeypatch):
    """A stage in transient backoff makes the loop wait ~until expiry, not spin."""
    from datetime import datetime, timedelta, timezone

    run = _active(0, "feat-a", StageStatus.GENERATED)
    run.stages[0].backoff_until = datetime.now(timezone.utc) + timedelta(seconds=30)
    state = _state(run)
    sleep = _FakeSleep(_block_run(state))
    monkeypatch.setattr(orch.asyncio, "sleep", sleep)
    config = ConductorConfig(check_interval_s=120.0, max_iterations=5)

    with patch.object(orch, "start_runner", new_callable=AsyncMock) as start:
        await conductor_run_loop(state, config)

    start.assert_not_awaited()
    assert 28 <= sum(sleep.calls) <= 31, sleep.calls
    assert orch.conductor_post_run.await_args.args[2] == "all_done"


@pytest.mark.asyncio
async def test_waiting_iterations_do_not_count_against_max_iterations(loop_env, monkeypatch):
    """Monitoring an executing runner for longer than max_iterations checks is fine."""
    state = _state(_active(0, "feat-a", StageStatus.EXECUTING))

    def on_call(n: int) -> None:
        if n == 50:
            state.runs[0].status = RunStatus.BLOCKED

    monkeypatch.setattr(orch.asyncio, "sleep", _FakeSleep(on_call))
    config = ConductorConfig(check_interval_s=10.0, max_iterations=2)

    with patch.object(orch, "monitor_runner", new_callable=AsyncMock) as monitor:
        await conductor_run_loop(state, config)

    assert monitor.await_count == 50
    assert orch.conductor_post_run.await_args.args[2] != "max_iterations"


@pytest.mark.asyncio
async def test_questions_for_a_human_end_the_loop_with_needs_input(loop_env, monkeypatch, tmp_path):
    """Without overnight, a lone stage waiting on QUESTIONS.md exits instead of spinning."""
    state = _state(_active(0, "feat-a", StageStatus.SPEC_NEEDS_INPUT))
    state.overnight = False
    state.runs[0].stages[0].worktree = str(tmp_path / "wt")
    _write_questions(tmp_path / "wt", "feat-a")
    sleep = _FakeSleep()
    monkeypatch.setattr(orch.asyncio, "sleep", sleep)
    config = ConductorConfig(check_interval_s=120.0)  # overnight comes from state

    with patch.object(orch, "answer_questions", new_callable=AsyncMock) as answer:
        result = await conductor_run_loop(state, config)

    answer.assert_not_awaited()
    assert sleep.calls == []
    orch.conductor_post_run.assert_not_awaited()
    assert result.runs[0].stages[0].status == StageStatus.SPEC_NEEDS_INPUT
    log = (tmp_path / "storage" / "conductor" / "test-proj" / "CONDUCTOR-LOG.md").read_text()
    assert str(tmp_path / "wt" / "docs" / "feat-a" / "spec" / "QUESTIONS.md") in log


@pytest.mark.asyncio
async def test_human_wait_does_not_stop_an_executing_run(loop_env, monkeypatch, tmp_path):
    """One run waits for answers, another executes: keep monitoring, don't exit."""
    state = _state(
        _active(0, "feat-a", StageStatus.SPEC_NEEDS_INPUT),
        _active(1, "feat-b", StageStatus.EXECUTING),
    )
    state.overnight = False
    state.runs[0].stages[0].worktree = str(tmp_path / "wt")
    _write_questions(tmp_path / "wt", "feat-a")
    monkeypatch.setattr(orch.asyncio, "sleep", _FakeSleep(_block_run(state, 1)))
    config = ConductorConfig(check_interval_s=30.0, max_parallel=2)

    with patch.object(orch, "monitor_runner", new_callable=AsyncMock) as monitor:
        await conductor_run_loop(state, config)

    assert monitor.await_count == 1
    assert state.runs[0].stages[0].status == StageStatus.SPEC_NEEDS_INPUT


@pytest.mark.asyncio
async def test_second_conductor_on_the_same_project_is_refused(loop_env, tmp_path):
    """A second loop must not start while one runs: it would kill the first
    one's tmux windows and overwrite its state file."""
    state = _state(_active(0, "feat-a", StageStatus.EXECUTING))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=1, project_root=tmp_path)
    storage = StorageResolver(tmp_path)
    holder = orch._lock_project(storage, state.project_name)
    try:
        with patch.object(orch, "_conductor_run_loop", AsyncMock()) as loop:
            with pytest.raises(orch.ProjectLocked, match=f"pid {os.getpid()}"):
                await conductor_run_loop(state, config)
        loop.assert_not_awaited()
    finally:
        holder.close()

    # Released: the next start runs.
    with patch.object(orch, "_conductor_run_loop", AsyncMock(return_value=state)) as loop:
        assert await conductor_run_loop(state, config) is state
