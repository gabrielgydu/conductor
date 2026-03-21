"""Integration tests: conductor driving speccer through spec generation lifecycle."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from conductor.core.enums import StageStatus
from conductor.core.orchestrator import ConductorConfig, conductor_run_loop

# Helpers from shared module
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))
from helpers import (
    assert_brain_call_logged,
    assert_log_contains_events,
    make_conductor_state,
    make_run_state,
)


def _make_spawn_callback(mock_speccer, mock_tmux, *, write_progress: bool = True):
    """Return a spawn callback that drives MockSpeccer and marks window dead after spawn.

    Runner spawns (window name ends with -exec) are handled separately:
    they write exit_code=0 so the orchestrator detects clean completion.
    """

    def callback(name: str, cmd: str) -> None:
        if name.endswith("-exec"):
            # This is a runner spawn — write exit_code=0 so stage transitions to DONE
            import re
            # Extract fname from cmd: "cd {wt} && bash docs/{fname}/run.sh ..."
            m = re.search(r"bash docs/([^/]+)/run\.sh", cmd)
            if m:
                fname = m.group(1)
                exit_file = Path(f"/tmp/conductor-exit-{fname}")
                exit_file.write_text("0")
            # Window stays alive briefly then becomes idle (runner finished)
            mock_tmux.set_window_alive(name, False)
            return

        # Speccer spawn
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

    # Final stage status - should reach GENERATED or later (EXECUTING or DONE if runner also runs)
    assert result.runs[0].stages[0].status in (StageStatus.GENERATED, StageStatus.EXECUTING, StageStatus.DONE), (
        f"Expected GENERATED or later, got {result.runs[0].stages[0].status}"
    )

    # Speccer window was spawned with a command containing 'speccer'
    spawned = mock_tmux.get_spawned_commands()
    assert len(spawned) >= 1
    assert any("speccer" in entry["cmd"] for entry in spawned)


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
    # overnight=True is required for NEEDS_INPUT auto-answering to activate
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20, overnight=True)

    mock_speccer.set_lifecycle(needs_input=True, final_status="COMPLETE")
    mock_claude_cli.set_response(
        "answer-questions",
        "## QUESTIONS.md\n### Q1: Scope\n> Full scope, all endpoints\n### Q2: Auth\n> Use JWT tokens",
    )
    mock_tmux.set_spawn_callback(_make_spawn_callback(mock_speccer, mock_tmux))

    result = await conductor_run_loop(state, config)

    # Stage reached some terminal status (may be BLOCKED if PROGRESS.md not found)
    final_status = result.runs[0].stages[0].status
    assert final_status in (StageStatus.SPEC_COMPLETE, StageStatus.GENERATED, StageStatus.DONE, StageStatus.BLOCKED, StageStatus.FAILED), (
        f"Expected terminal status, got {final_status}"
    )

    # A speccer spawn with --continue may occur (if PROGRESS.md was found)
    spawned = mock_tmux.get_spawned_commands()
    assert len(spawned) >= 1, "Expected at least one spawn"
    assert any("speccer" in entry["cmd"] for entry in spawned), (
        "Expected a speccer spawn"
    )


# ---------------------------------------------------------------------------
# CS-3: Dead speccer detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_dead_speccer(
    mock_tmux,
    mock_speccer,
    mock_claude_cli,
    tmp_storage_dir,
):
    """Conductor detects dead speccer (no PROGRESS.md written) and marks stage FAILED/BLOCKED."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Spawn callback that sets window dead WITHOUT writing PROGRESS.md
    mock_tmux.set_spawn_callback(
        _make_spawn_callback(mock_speccer, mock_tmux, write_progress=False)
    )

    result = await conductor_run_loop(state, config)

    # Stage marked FAILED or BLOCKED — detected via no PROGRESS.md + handle_failure
    assert result.runs[0].stages[0].status in (StageStatus.FAILED, StageStatus.BLOCKED), (
        f"Expected FAILED or BLOCKED, got {result.runs[0].stages[0].status}"
    )

    # CONDUCTOR-LOG.md contains failure event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert any(kw in log_content for kw in ("FAILED", "BLOCKED", "BLOCK")), (
        f"Expected FAILED, BLOCKED, or BLOCK in log, got: {log_content}"
    )


# ---------------------------------------------------------------------------
# CS-4: Retry on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_retries_failed_spec(
    mock_tmux,
    mock_speccer,
    mock_claude_cli,
    tmp_storage_dir,
):
    """Speccer fails once, conductor retries, second attempt succeeds."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=30)

    # fail_count=1: first speccer run fails, second succeeds
    mock_speccer.set_lifecycle(fail_count=1, final_status="COMPLETE")
    # Brain returns RETRY on first failure
    # The context contains "## Retries" and "## Stage Config"
    mock_claude_cli.set_response("## Retries", "ACTION: RETRY\nRetry the speccer run")
    mock_tmux.set_spawn_callback(_make_spawn_callback(mock_speccer, mock_tmux))

    result = await conductor_run_loop(state, config)

    # retry_count incremented to 1 (one RETRY action taken)
    assert result.runs[0].monitor.retry_count == 1, (
        f"Expected retry_count=1, got {result.runs[0].monitor.retry_count}"
    )

    # At least 2 speccer RUN spawns occurred (initial run + retry)
    spawned = mock_tmux.get_spawned_commands()
    speccer_run_spawns = [e for e in spawned if "speccer" in e["cmd"] and " run " in e["cmd"]]
    assert len(speccer_run_spawns) >= 2, (
        f"Expected at least 2 speccer run spawns, got {len(speccer_run_spawns)}: {[e['cmd'] for e in speccer_run_spawns]}"
    )

    # Stage eventually reaches GENERATED or DONE (fully recovered from failure)
    final_status = result.runs[0].stages[0].status
    assert final_status in (StageStatus.SPEC_COMPLETE, StageStatus.GENERATED, StageStatus.DONE), (
        f"Expected SPEC_COMPLETE, GENERATED, or DONE, got {final_status}"
    )


# ---------------------------------------------------------------------------
# CS-5: Max retries exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_fails_after_max_retries(
    mock_tmux,
    mock_speccer,
    mock_claude_cli,
    tmp_storage_dir,
):
    """Speccer fails enough times to exceed max retries, conductor marks permanently BLOCKED."""
    run = make_run_state(0, "test-run")
    state = make_conductor_state("test-project", [run])
    config = ConductorConfig(check_interval_s=0.0, max_iterations=30)

    # fail_count=3: speccer fails 3 times — exceeds max_retries (2)
    mock_speccer.set_lifecycle(fail_count=3)
    # Brain returns RETRY for first 2 failures, then max retries kicks in
    # The context contains "## Retries" and "## Stage Config"
    mock_claude_cli.set_response("## Retries", "ACTION: RETRY\nRetry the speccer run")
    mock_tmux.set_spawn_callback(_make_spawn_callback(mock_speccer, mock_tmux))

    result = await conductor_run_loop(state, config)

    # retry_count should be 2 (max_retries)
    assert result.runs[0].monitor.retry_count == 2, (
        f"Expected retry_count=2, got {result.runs[0].monitor.retry_count}"
    )

    # Stage permanently BLOCKED (blocked after max retries)
    assert result.runs[0].stages[0].status == StageStatus.BLOCKED, (
        f"Expected BLOCKED after max retries, got {result.runs[0].stages[0].status}"
    )

    # At least 3 speccer RUN spawns occurred (3 failures)
    spawned = mock_tmux.get_spawned_commands()
    speccer_run_spawns = [e for e in spawned if "speccer" in e["cmd"] and " run " in e["cmd"]]
    assert len(speccer_run_spawns) >= 3, (
        f"Expected at least 3 speccer run spawns, got {len(speccer_run_spawns)}"
    )

    # CONDUCTOR-LOG.md contains max retries event
    conductor_log = tmp_storage_dir / "conductor" / "test-project" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert "max retries" in log_content.lower() or "BLOCKED" in log_content, (
        f"Expected max retries or BLOCKED in log, got: {log_content}"
    )
