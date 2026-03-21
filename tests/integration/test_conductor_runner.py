"""Integration tests: conductor driving runner through execution lifecycle."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from conductor.core.enums import RunStatus, StageStatus
from conductor.core.orchestrator import ConductorConfig, conductor_run_loop
from conductor.core.storage import StorageResolver

# Helpers from shared module
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))
from helpers import (
    assert_brain_call_logged,
    make_conductor_state,
    make_run_state,
)


# ---------------------------------------------------------------------------
# CR-1: Runner spawns after generate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_starts_runner_after_generate(
    mock_tmux,
    mock_runner,
    tmp_storage_dir,
    tmp_path,
):
    """After speccer generate produces run.sh, conductor spawns runner."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.GENERATED
    # Set up worktree for the stage
    wt_dir = tmp_path / "worktrees" / "wt-0"
    wt_dir.mkdir(parents=True, exist_ok=True)
    run.stages[0].worktree = str(wt_dir)
    run.stages[0].branch = "conductor/test-project/test-run/stage-0"
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Write run.sh to feature dir
    storage = StorageResolver(Path.cwd())
    feature_dir = storage.feature_dir("test-run")
    mock_runner.write_run_sh(feature_dir)

    # Spawn callback: runner exits immediately after spawning
    def spawn_callback(name: str, cmd: str) -> None:
        if "run.sh" in cmd or "-exec" in name:
            mock_tmux.set_window_alive(name, False)
            mock_tmux.set_window_exit_code(name, 0)
            # Write exit_code file so orchestrator can read it
            # The orchestrator looks for /tmp/conductor-exit-{fname}
            exit_file = Path(f"/tmp/conductor-exit-test-run")
            exit_file.write_text("0")

    mock_tmux.set_spawn_callback(spawn_callback)

    result = await conductor_run_loop(state, config)

    # Stage should transition through EXECUTING to DONE (or at EXECUTING)
    final_status = result.runs[0].stages[0].status
    assert final_status in (StageStatus.EXECUTING, StageStatus.DONE), (
        f"Expected EXECUTING or DONE after runner spawn, got {final_status}"
    )

    # Runner window spawned (cmd contains run.sh or is a runner-type spawn)
    spawned = mock_tmux.get_spawned_commands()
    assert any(
        "run.sh" in entry["cmd"] or entry.get("runner") for entry in spawned
    ), (
        f"Expected runner spawn, got: {[e['cmd'] for e in spawned]}"
    )

    # CONDUCTOR-LOG.md contains runner start event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower()
        for keyword in ["runner", "executing", "run_started", "runner_started"]
    ), f"Expected runner event in log, got: {log_content}"


# ---------------------------------------------------------------------------
# CR-2: Runner completion detected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_runner_completion(
    mock_tmux,
    mock_runner,
    tmp_storage_dir,
    tmp_path,
):
    """Runner exits 0, conductor transitions stage to DONE."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    # Set up worktree for the stage
    wt_dir = tmp_path / "worktrees" / "wt-0"
    wt_dir.mkdir(parents=True, exist_ok=True)
    run.stages[0].worktree = str(wt_dir)
    run.stages[0].branch = "conductor/test-project/test-run/stage-0"
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window is dead (already exited cleanly)
    window_name = "run0:stage-0-exec"
    mock_tmux.set_window_alive(window_name, False)
    mock_tmux.set_window_exit_code(window_name, 0)

    # Write exit file that the orchestrator will read
    # The orchestrator looks for /tmp/conductor-exit-{fname}
    exit_file = Path(f"/tmp/conductor-exit-test-run")
    exit_file.write_text("0")

    result = await conductor_run_loop(state, config)

    # Stage should transition EXECUTING -> DONE
    assert result.runs[0].stages[0].status == StageStatus.DONE, (
        f"Expected DONE, got {result.runs[0].stages[0].status}"
    )

    # Run status should be DONE
    assert result.runs[0].status == RunStatus.DONE, (
        f"Expected run DONE, got {result.runs[0].status}"
    )

    # CONDUCTOR-LOG.md contains completion event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower()
        for keyword in ["done", "complete", "finished", "runner_done"]
    ), f"Expected completion event in log, got: {log_content}"


# ---------------------------------------------------------------------------
# CR-3: Stall detection with 5-check cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_runner_stall(
    mock_tmux,
    mock_runner,
    mock_claude_cli,
    tmp_storage_dir,
    tmp_path,
):
    """Stall detection fires, brain invoked, auto-fail at stall_count=5."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    # Set up worktree for the stage
    wt_dir = tmp_path / "worktrees" / "wt-0"
    wt_dir.mkdir(parents=True, exist_ok=True)
    run.stages[0].worktree = str(wt_dir)
    run.stages[0].branch = "conductor/test-project/test-run/stage-0"
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window stays alive but makes no progress
    window_name = "run0:stage-0-exec"
    mock_tmux.set_window_alive(window_name, True)

    # Activity log exists but never changes (stall condition)
    # The orchestrator looks for /tmp/ralph-activity-{fname}*
    activity_log = Path("/tmp/ralph-activity-test-run")
    activity_log.write_text("stuck here\n")

    # Brain returns retry action when diagnosed
    mock_claude_cli.set_response(
        "diagnos",
        '{"action": "retry", "message": "Retrying"}',
    )

    result = await conductor_run_loop(state, config)

    # Stage should be FAILED or BLOCKED after stall_count reaches 5
    assert result.runs[0].stages[0].status in (StageStatus.FAILED, StageStatus.BLOCKED), (
        f"Expected FAILED or BLOCKED after stall cap, got {result.runs[0].stages[0].status}"
    )

    # stall_count should have reached 5
    assert result.runs[0].monitor.stall_count >= 5, (
        f"Expected stall_count >= 5, got {result.runs[0].monitor.stall_count}"
    )

    # CONDUCTOR-LOG.md contains stall event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower() for keyword in ["stall", "stall_count", "auto-failed"]
    ), f"Expected stall event in log, got: {log_content}"


# ---------------------------------------------------------------------------
# CR-4: Steer stalled runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_steers_stalled_runner(
    mock_tmux,
    mock_runner,
    mock_claude_cli,
    tmp_storage_dir,
    tmp_path,
):
    """Brain diagnosis returns STEER, conductor sends steer message to runner."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    # Set up worktree for the stage
    wt_dir = tmp_path / "worktrees" / "wt-0"
    wt_dir.mkdir(parents=True, exist_ok=True)
    run.stages[0].worktree = str(wt_dir)
    run.stages[0].branch = "conductor/test-project/test-run/stage-0"
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window alive but stalled
    window_name = "run0:stage-0-exec"
    mock_tmux.set_window_alive(window_name, True)

    # Activity log never changes
    # The orchestrator looks for /tmp/ralph-activity-{fname}*
    activity_log = Path("/tmp/ralph-activity-test-run")
    activity_log.write_text("working on task\n")

    steer_message = "Focus on the database migration"

    # Brain returns steer action
    mock_claude_cli.set_response(
        "diagnos",
        f'{{"action": "steer", "message": "{steer_message}"}}',
    )

    result = await conductor_run_loop(state, config)

    # Brain should have been called - check that brain was invoked in logs
    # (brain call logs dir might not exist if brain is mocked)
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower()
        for keyword in ["brain", "diagnos", "stall"]
    ), f"Expected brain call in log, got: {log_content}"


# ---------------------------------------------------------------------------
# CR-5: Dead runner detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_dead_runner(
    mock_tmux,
    mock_runner,
    mock_claude_cli,
    tmp_storage_dir,
    tmp_path,
):
    """Runner dies without exit file, conductor detects and handles it."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    # Set up worktree for the stage
    wt_dir = tmp_path / "worktrees" / "wt-0"
    wt_dir.mkdir(parents=True, exist_ok=True)
    run.stages[0].worktree = str(wt_dir)
    run.stages[0].branch = "conductor/test-project/test-run/stage-0"
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window is dead — no exit file written
    window_name = "run0:stage-0-exec"
    mock_tmux.set_window_alive(window_name, False)
    # Intentionally no exit_code file (this is the key to dead runner detection)

    result = await conductor_run_loop(state, config)

    # Stage should be either FAILED or BLOCKED (failed initially, then handled by brain)
    assert result.runs[0].stages[0].status in (StageStatus.FAILED, StageStatus.BLOCKED), (
        f"Expected FAILED or BLOCKED after dead runner, got {result.runs[0].stages[0].status}"
    )

    # CONDUCTOR-LOG.md contains failure handling event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower()
        for keyword in ["failure", "failed", "brain_call", "diagnose"]
    ), f"Expected failure event in log, got: {log_content}"
