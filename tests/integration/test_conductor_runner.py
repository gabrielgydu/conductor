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
    assert_log_contains_events,
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
):
    """After speccer generate produces run.sh, conductor spawns runner."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.GENERATED
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Write run.sh to feature dir
    storage = StorageResolver(Path.cwd())
    feature_dir = storage.feature_dir("test-run")
    mock_runner.write_run_sh(feature_dir)

    # Spawn callback: runner exits immediately after spawning
    def spawn_callback(name: str, cmd: str) -> None:
        if "run.sh" in cmd or "runner" in name:
            mock_tmux.set_window_alive(name, False)
            mock_tmux.set_window_exit_code(name, 0)
            # Write exit_code file so orchestrator can read it
            (feature_dir / "exit_code").write_text("0")

    mock_tmux.set_spawn_callback(spawn_callback)

    result = await conductor_run_loop(state, config)

    # Stage should transition through EXECUTING to DONE (or at EXECUTING)
    final_status = result.runs[0].stages[0].status
    assert final_status in (StageStatus.EXECUTING, StageStatus.DONE), (
        f"Expected EXECUTING or DONE after runner spawn, got {final_status}"
    )

    # Runner window spawned with run.sh command
    spawned = mock_tmux.get_spawned_commands()
    assert any("run.sh" in entry["cmd"] for entry in spawned), (
        f"Expected runner spawn with run.sh, got: {[e['cmd'] for e in spawned]}"
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
):
    """Runner exits 0, conductor transitions stage to DONE."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window is dead (already exited cleanly)
    window_name = "runner-0-0"
    mock_tmux.set_window_alive(window_name, False)
    mock_tmux.set_window_exit_code(window_name, 0)

    # Write completion markers in feature dir
    storage = StorageResolver(Path.cwd())
    feature_dir = storage.feature_dir("test-run")
    feature_dir.mkdir(parents=True, exist_ok=True)
    (feature_dir / "activity.log").write_text("COMPLETE\n")
    (feature_dir / "exit_code").write_text("0")

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
):
    """Stall detection fires, brain invoked, auto-fail at stall_count=5."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window stays alive but makes no progress
    window_name = "runner-0-0"
    mock_tmux.set_window_alive(window_name, True)

    # Activity log exists but never changes (stall condition)
    storage = StorageResolver(Path.cwd())
    feature_dir = storage.feature_dir("test-run")
    feature_dir.mkdir(parents=True, exist_ok=True)
    (feature_dir / "activity.log").write_text("stuck here\n")

    # Brain returns retry action when diagnosed
    mock_claude_cli.set_response(
        "diagnos",
        '{"action": "retry", "message": "Retrying"}',
    )

    result = await conductor_run_loop(state, config)

    # Stage should be FAILED after stall_count reaches 5
    assert result.runs[0].stages[0].status == StageStatus.FAILED, (
        f"Expected FAILED after stall cap, got {result.runs[0].stages[0].status}"
    )

    # stall_count should have reached 5
    assert result.runs[0].monitor.stall_count >= 5, (
        f"Expected stall_count >= 5, got {result.runs[0].monitor.stall_count}"
    )

    # CONDUCTOR-LOG.md contains stall-fail event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower()
        for keyword in ["stall", "failed", "stalled"]
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
):
    """Brain diagnosis returns STEER, conductor sends steer message to runner."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window alive but stalled
    window_name = "runner-0-0"
    mock_tmux.set_window_alive(window_name, True)

    # Activity log never changes
    storage = StorageResolver(Path.cwd())
    feature_dir = storage.feature_dir("test-run")
    feature_dir.mkdir(parents=True, exist_ok=True)
    (feature_dir / "activity.log").write_text("working on task\n")

    steer_message = "Focus on the database migration"

    # Brain returns steer action
    mock_claude_cli.set_response(
        "diagnos",
        f'{{"action": "steer", "message": "{steer_message}"}}',
    )

    result = await conductor_run_loop(state, config)

    # Brain should have been called (steer action logged to brain_calls_dir)
    brain_calls_dir = tmp_storage_dir / "conductor" / "test-project" / "brain-calls"
    assert_brain_call_logged(brain_calls_dir)

    # CONDUCTOR-LOG.md contains steer event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower()
        for keyword in ["steer", "brain", "diagnosis", "diagnos"]
    ), f"Expected steer event in log, got: {log_content}"


# ---------------------------------------------------------------------------
# CR-5: Dead runner detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_dead_runner(
    mock_tmux,
    mock_runner,
    tmp_storage_dir,
):
    """Runner dies without exit file, conductor marks stage FAILED."""
    run = make_run_state(0, "test-run")
    run.stages[0].status = StageStatus.EXECUTING
    run.stages[0].pid = 12345
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window is dead — no exit file written
    window_name = "runner-0-0"
    mock_tmux.set_window_alive(window_name, False)
    # Intentionally no exit_code file

    result = await conductor_run_loop(state, config)

    # Stage should be FAILED (runner died unexpectedly)
    assert result.runs[0].stages[0].status == StageStatus.FAILED, (
        f"Expected FAILED after dead runner, got {result.runs[0].stages[0].status}"
    )

    # CONDUCTOR-LOG.md contains runner died event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(
        keyword in log_content.lower()
        for keyword in ["died", "dead", "failed", "crash", "runner_failed"]
    ), f"Expected runner died event in log, got: {log_content}"
