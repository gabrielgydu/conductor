"""conductor_run_loop resilience: one run's error must not stop the others."""

from __future__ import annotations

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

    assert state.runs[0].status == RunStatus.ACTIVE
