"""Tests for advance_run state machine transitions."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import ConductorConfig, advance_run
import conductor.core.orchestrator as orch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(stage_status: StageStatus, overnight: bool = False, num_stages: int = 1) -> ConductorState:
    stages = [StageState(name=f"stage-{i}", spec_mode="full") for i in range(num_stages)]
    stages[0].status = stage_status
    stages[0].worktree = "/tmp/fake-wt"
    stages[0].branch = "conductor/proj/run-0/stage-0"
    run = RunState(
        index=0,
        name="run-0",
        description="test run",
        stages=stages,
        status=RunStatus.ACTIVE,
    )
    return ConductorState(
        project_name="proj",
        base_branch="main",
        runs=[run],
        overnight=overnight,
    )


def _make_tmux() -> MagicMock:
    tmux = MagicMock()
    tmux.ensure_session = AsyncMock()
    tmux.is_window_alive = AsyncMock(return_value=True)
    tmux.is_runner_idle = AsyncMock(return_value=False)
    tmux.spawn_in_window = AsyncMock()
    tmux.spawn_in_window_and_wait = AsyncMock(return_value=0)
    tmux.spawn_runner_in_window = AsyncMock()
    return tmux


def _make_storage(tmp_path: Path) -> MagicMock:
    storage = MagicMock()
    storage.repo_root = tmp_path / "repo"
    storage.conductor_state = MagicMock(return_value=tmp_path / "state.json")
    storage.conductor_dir = MagicMock(return_value=tmp_path / "conductor")
    storage.conductor_brief = MagicMock(return_value=tmp_path / "brief.md")
    storage.brain_calls_dir = MagicMock(return_value=tmp_path / "brain-calls")
    (tmp_path / "brain-calls").mkdir(parents=True, exist_ok=True)
    return storage


def _make_config(overnight: bool = False) -> ConductorConfig:
    return ConductorConfig(check_interval_s=0.0, max_iterations=1, overnight=overnight)


# ---------------------------------------------------------------------------
# pending -> spec_init (mocks create_worktree, run_speccer_init)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_pending_creates_worktree_and_calls_speccer_init(tmp_path):
    state = _make_state(StageStatus.PENDING)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "create_worktree") as mock_cw, \
         patch.object(orch, "run_speccer_init", new_callable=AsyncMock) as mock_init, \
         patch.object(orch, "run_speccer_run", new_callable=AsyncMock) as mock_run, \
         patch.object(orch, "speccer_exit_code_handler") as mock_exit, \
         patch.object(orch, "write_feature_description") as mock_desc, \
         patch.object(orch, "write_constitution") as mock_const, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_cw.assert_called_once()
    mock_init.assert_called_once()


@pytest.mark.asyncio
async def test_advance_run_pending_sets_spec_init_then_falls_through(tmp_path):
    """pending -> spec_init -> immediately calls run_speccer_run (fall-through)."""
    state = _make_state(StageStatus.PENDING)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "create_worktree"), \
         patch.object(orch, "run_speccer_init", new_callable=AsyncMock), \
         patch.object(orch, "run_speccer_run", new_callable=AsyncMock) as mock_run, \
         patch.object(orch, "speccer_exit_code_handler") as mock_exit, \
         patch.object(orch, "write_feature_description"), \
         patch.object(orch, "write_constitution"), \
         patch.object(orch, "pre_reset_speccer_status"), \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    # run_speccer_run must be called (fall-through to spec_init branch)
    mock_run.assert_called_once()
    mock_exit.assert_called_once()


# ---------------------------------------------------------------------------
# spec_init -> calls run_speccer_run + exit_code_handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_spec_init_calls_run_and_handler(tmp_path):
    state = _make_state(StageStatus.SPEC_INIT)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "pre_reset_speccer_status") as mock_reset, \
         patch.object(orch, "run_speccer_run", new_callable=AsyncMock) as mock_run, \
         patch.object(orch, "speccer_exit_code_handler") as mock_exit, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_reset.assert_called_once()
    mock_run.assert_called_once()
    mock_exit.assert_called_once()


@pytest.mark.asyncio
async def test_advance_run_spec_running_also_calls_run(tmp_path):
    state = _make_state(StageStatus.SPEC_RUNNING)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "pre_reset_speccer_status"), \
         patch.object(orch, "run_speccer_run", new_callable=AsyncMock) as mock_run, \
         patch.object(orch, "speccer_exit_code_handler"), \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# spec_needs_input with overnight=True -> calls answer_questions + continue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_spec_needs_input_overnight_answers_and_continues(tmp_path):
    state = _make_state(StageStatus.SPEC_NEEDS_INPUT, overnight=True)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config(overnight=True)

    with patch.object(orch, "answer_questions", new_callable=AsyncMock) as mock_aq, \
         patch.object(orch, "run_speccer_continue", new_callable=AsyncMock) as mock_cont, \
         patch.object(orch, "speccer_exit_code_handler") as mock_exit, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_aq.assert_called_once()
    mock_cont.assert_called_once()
    mock_exit.assert_called_once()


# ---------------------------------------------------------------------------
# spec_needs_input with overnight=False -> returns (skips)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_spec_needs_input_not_overnight_skips(tmp_path):
    state = _make_state(StageStatus.SPEC_NEEDS_INPUT, overnight=False)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config(overnight=False)

    with patch.object(orch, "answer_questions", new_callable=AsyncMock) as mock_aq, \
         patch.object(orch, "run_speccer_continue", new_callable=AsyncMock) as mock_cont, \
         patch.object(orch, "atomic_save") as mock_save:
        await advance_run(state, 0, tmux, storage, config)

    mock_aq.assert_not_called()
    mock_cont.assert_not_called()
    # Stage stays at spec_needs_input
    assert state.runs[0].stages[0].status == StageStatus.SPEC_NEEDS_INPUT


# ---------------------------------------------------------------------------
# spec_complete -> calls run_speccer_generate, sets generated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_spec_complete_generates_and_sets_generated(tmp_path):
    state = _make_state(StageStatus.SPEC_COMPLETE)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "run_speccer_generate", new_callable=AsyncMock) as mock_gen, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_gen.assert_called_once()
    assert state.runs[0].stages[0].status == StageStatus.GENERATED


# ---------------------------------------------------------------------------
# generated -> calls start_runner, sets executing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_generated_starts_runner_and_sets_executing(tmp_path):
    state = _make_state(StageStatus.GENERATED)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "start_runner", new_callable=AsyncMock) as mock_start, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_start.assert_called_once()
    assert state.runs[0].stages[0].status == StageStatus.EXECUTING


# ---------------------------------------------------------------------------
# executing -> calls monitor_runner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_executing_calls_monitor_runner(tmp_path):
    state = _make_state(StageStatus.EXECUTING)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "monitor_runner", new_callable=AsyncMock) as mock_monitor, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_monitor.assert_called_once()


# ---------------------------------------------------------------------------
# done -> increments current_stage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_done_increments_current_stage_single_stage(tmp_path):
    """Single stage done -> run marked DONE and PR created."""
    state = _make_state(StageStatus.DONE)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "atomic_save"), \
         patch.object(orch, "push_and_create_pr") as mock_pr:
        await advance_run(state, 0, tmux, storage, config)

    # current_stage advanced to 1 (past end of stages list)
    assert state.runs[0].current_stage == 1


@pytest.mark.asyncio
async def test_advance_run_done_advances_to_next_stage(tmp_path):
    """Two-stage run: first stage done -> current_stage becomes 1."""
    state = _make_state(StageStatus.DONE, num_stages=2)
    state.runs[0].stages[1].status = StageStatus.PENDING
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    assert state.runs[0].current_stage == 1


# ---------------------------------------------------------------------------
# failed -> calls handle_failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_failed_calls_handle_failure(tmp_path):
    state = _make_state(StageStatus.FAILED)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "handle_failure", new_callable=AsyncMock) as mock_fail, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_fail.assert_called_once()


@pytest.mark.asyncio
async def test_advance_run_stalled_calls_handle_failure(tmp_path):
    state = _make_state(StageStatus.STALLED)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "handle_failure", new_callable=AsyncMock) as mock_fail, \
         patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    mock_fail.assert_called_once()


# ---------------------------------------------------------------------------
# blocked -> sets run status to blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_advance_run_blocked_sets_run_status(tmp_path):
    state = _make_state(StageStatus.BLOCKED)
    tmux = _make_tmux()
    storage = _make_storage(tmp_path)
    config = _make_config()

    with patch.object(orch, "atomic_save"):
        await advance_run(state, 0, tmux, storage, config)

    assert state.runs[0].status == RunStatus.BLOCKED
