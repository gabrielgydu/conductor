"""Integration tests: conductor driving speccer through spec generation lifecycle."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from conductor.core.enums import StageStatus
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


def _make_spawn_callback(mock_speccer, mock_tmux, *, write_progress: bool = True):
    """Return a spawn callback that drives MockSpeccer and marks window dead after spawn."""

    def callback(name: str, cmd: str) -> None:
        if write_progress:
            mock_speccer._handle_spawn(name, cmd)
        # Mark process as dead (simulates process exiting after writing status)
        mock_tmux.set_window_alive(name, False)

    return callback


# ---------------------------------------------------------------------------
# CS-1: Full spec lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_advances_through_spec_stages(
    mock_tmux,
    mock_speccer,
    tmp_storage_dir,
):
    """Conductor drives stage through PENDING -> SPEC_RUNNING -> SPEC_COMPLETE -> GENERATED."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    mock_speccer.set_lifecycle(final_status="COMPLETE")
    mock_tmux.set_spawn_callback(_make_spawn_callback(mock_speccer, mock_tmux))

    result = await conductor_run_loop(state, config)

    # Final stage status
    assert result.runs[0].stages[0].status == StageStatus.GENERATED

    # Speccer window was spawned with a command containing 'speccer'
    spawned = mock_tmux.get_spawned_commands()
    assert len(spawned) >= 1
    assert any("speccer" in entry["cmd"] for entry in spawned)

    # CONDUCTOR-LOG.md contains transition events
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    assert_log_contains_events(conductor_log, ["SPEC_INIT", "SPEC_COMPLETE", "GENERATED"])


# ---------------------------------------------------------------------------
# CS-2: NEEDS_INPUT handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_handles_spec_needs_input(
    mock_tmux,
    mock_speccer,
    mock_claude_cli,
    tmp_storage_dir,
):
    """Conductor detects NEEDS_INPUT, calls brain, restarts speccer with --continue."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    mock_speccer.set_lifecycle(needs_input=True, final_status="COMPLETE")
    mock_claude_cli.set_response(
        "answer-questions",
        "## QUESTIONS.md\n### Q1: Scope\n> Full scope, all endpoints\n### Q2: Auth\n> Use JWT tokens",
    )
    mock_tmux.set_spawn_callback(_make_spawn_callback(mock_speccer, mock_tmux))

    result = await conductor_run_loop(state, config)

    # Stage reached SPEC_COMPLETE or GENERATED
    final_status = result.runs[0].stages[0].status
    assert final_status in (StageStatus.SPEC_COMPLETE, StageStatus.GENERATED), (
        f"Expected SPEC_COMPLETE or GENERATED, got {final_status}"
    )

    # MockClaudeCLI called once for brain answer-questions
    assert mock_claude_cli.call_count == 1, (
        f"Expected 1 brain call, got {mock_claude_cli.call_count}"
    )

    # Brain call logged to brain_calls_dir
    brain_calls_dir = tmp_storage_dir / "conductor" / "test-project" / "brain-calls"
    assert_brain_call_logged(brain_calls_dir)

    # Second speccer spawn has --continue
    spawned = mock_tmux.get_spawned_commands()
    assert len(spawned) >= 2, f"Expected at least 2 spawns, got {len(spawned)}"
    assert any("--continue" in entry["cmd"] for entry in spawned), (
        "Expected a speccer spawn with --continue flag"
    )


# ---------------------------------------------------------------------------
# CS-3: Dead speccer detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_dead_speccer(
    mock_tmux,
    mock_speccer,
    tmp_storage_dir,
):
    """Conductor detects dead speccer (no PROGRESS.md written) and marks stage FAILED."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Spawn callback that sets window dead WITHOUT writing PROGRESS.md
    mock_tmux.set_spawn_callback(
        _make_spawn_callback(mock_speccer, mock_tmux, write_progress=False)
    )

    result = await conductor_run_loop(state, config)

    # Stage marked FAILED — detected via is_window_alive() returning False with no status
    assert result.runs[0].stages[0].status == StageStatus.FAILED

    # CONDUCTOR-LOG.md contains failure event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert "FAILED" in log_content, f"Expected FAILED in log, got: {log_content}"


# ---------------------------------------------------------------------------
# CS-4: Retry on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_retries_failed_spec(
    mock_tmux,
    mock_speccer,
    tmp_storage_dir,
):
    """Speccer fails once, conductor retries, second attempt succeeds."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    mock_speccer.set_lifecycle(fail_count=1, final_status="COMPLETE")
    mock_tmux.set_spawn_callback(_make_spawn_callback(mock_speccer, mock_tmux))

    result = await conductor_run_loop(state, config)

    # retry_count incremented to 1
    assert result.runs[0].monitor.retry_count == 1

    # Two speccer spawns (initial + 1 retry)
    spawned = mock_tmux.get_spawned_commands()
    assert len(spawned) == 2, f"Expected 2 speccer spawns, got {len(spawned)}"

    # Stage eventually reaches SPEC_COMPLETE or GENERATED
    final_status = result.runs[0].stages[0].status
    assert final_status in (StageStatus.SPEC_COMPLETE, StageStatus.GENERATED), (
        f"Expected SPEC_COMPLETE or GENERATED, got {final_status}"
    )


# ---------------------------------------------------------------------------
# CS-5: Max retries exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_fails_after_max_retries(
    mock_tmux,
    mock_speccer,
    tmp_storage_dir,
):
    """Speccer fails 3 times (exceeds retry limit of 2), conductor marks permanently FAILED."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    mock_speccer.set_lifecycle(fail_count=3)
    mock_tmux.set_spawn_callback(_make_spawn_callback(mock_speccer, mock_tmux))

    result = await conductor_run_loop(state, config)

    # retry_count capped at max_retries (2)
    assert result.runs[0].monitor.retry_count == 2

    # Stage permanently FAILED
    assert result.runs[0].stages[0].status == StageStatus.FAILED

    # Exactly 3 speccer spawns (initial + 2 retries)
    spawned = mock_tmux.get_spawned_commands()
    assert len(spawned) == 3, f"Expected 3 speccer spawns, got {len(spawned)}"

    # CONDUCTOR-LOG.md contains failure event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert "max retries" in log_content.lower(), (
        f"Expected 'max retries' in log, got: {log_content}"
    )
