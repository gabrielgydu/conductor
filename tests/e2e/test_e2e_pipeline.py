"""E2E pipeline tests — full lifecycle via conductor_run_loop with composed mocks."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))

from conductor.core.enums import RunStatus, StageStatus
from conductor.core.orchestrator import ConductorConfig, conductor_run_loop
from helpers import (
    assert_brain_call_logged,
    assert_log_contains_events,
    make_run_state,
    make_conductor_state,
)


def _make_spawn_callback(mock_speccer, mock_tmux):
    """Return a spawn callback that drives MockSpeccer and marks window dead on each spawn."""
    def callback(name: str, cmd: str) -> None:
        mock_speccer._handle_spawn(name, cmd)
        mock_tmux.set_window_alive(name, False)
    return callback


# ---------------------------------------------------------------------------
# E2E-1: Single run full pipeline
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_single_run_full_pipeline(e2e_env, tmp_storage_dir):
    """Full lifecycle: PENDING -> SPEC_RUNNING -> SPEC_COMPLETE -> GENERATED -> run DONE."""
    run = make_run_state(0, "impl")
    state = make_conductor_state("pipeline-test", [run])
    config = ConductorConfig(
        check_interval_s=0.0,
        max_iterations=50,
        project_root=e2e_env.repo_path,
    )

    e2e_env.speccer.set_lifecycle(final_status="COMPLETE")
    e2e_env.tmux.set_spawn_callback(_make_spawn_callback(e2e_env.speccer, e2e_env.tmux))

    result = await conductor_run_loop(state, config)

    # All runs reach DONE
    assert result.runs[0].status == RunStatus.DONE, (
        f"Expected run DONE, got {result.runs[0].status}"
    )
    # Stage reaches terminal state (GENERATED or DONE)
    final_stage_status = result.runs[0].stages[0].status
    assert final_stage_status in (StageStatus.GENERATED, StageStatus.DONE), (
        f"Expected stage GENERATED or DONE, got {final_stage_status}"
    )

    # CONDUCTOR-LOG.md contains lifecycle events
    conductor_log = tmp_storage_dir / "conductor" / "pipeline-test" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    assert_log_contains_events(conductor_log, ["SPEC_INIT", "SPEC_COMPLETE", "GENERATED"])

    # MockTmux shows at least one speccer spawn
    spawned = e2e_env.tmux.get_spawned_commands()
    assert len(spawned) >= 1, f"Expected at least 1 spawn, got {len(spawned)}"
    assert any("speccer" in entry["cmd"] for entry in spawned), (
        "Expected speccer command in spawns"
    )


# ---------------------------------------------------------------------------
# E2E-2: Multi-run with deps
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_multi_run_with_deps(e2e_env, tmp_storage_dir):
    """DAG: A independent, B depends A, C depends A — all reach DONE."""
    runs = [
        make_run_state(0, "run-a"),
        make_run_state(1, "run-b", depends_on=[0]),
        make_run_state(2, "run-c", depends_on=[0]),
    ]
    state = make_conductor_state("multi-run-test", runs)
    config = ConductorConfig(
        check_interval_s=0.0,
        max_iterations=50,
        project_root=e2e_env.repo_path,
    )

    e2e_env.speccer.set_lifecycle(final_status="COMPLETE")
    e2e_env.tmux.set_spawn_callback(_make_spawn_callback(e2e_env.speccer, e2e_env.tmux))

    result = await conductor_run_loop(state, config)

    # All three runs reach DONE
    for i, run in enumerate(result.runs):
        assert run.status == RunStatus.DONE, (
            f"run[{i}] ({run.name}) expected DONE, got {run.status}"
        )

    # CONDUCTOR-LOG.md contains events for all runs
    conductor_log = tmp_storage_dir / "conductor" / "multi-run-test" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    for run_name in ("run-a", "run-b", "run-c"):
        assert run_name in log_content, f"Expected {run_name} events in log"

    # run-a's SPEC_COMPLETE appears before run-b's (processed in order)
    idx_a = log_content.find("SPEC_COMPLETE: run-a")
    idx_b = log_content.find("SPEC_COMPLETE: run-b")
    assert idx_a != -1 and idx_b != -1, "Expected SPEC_COMPLETE for run-a and run-b"
    assert idx_a < idx_b, "Expected run-a to complete before run-b in the log"

    # All 3 speccer spawns occurred
    spawned = e2e_env.tmux.get_spawned_commands()
    assert len(spawned) >= 3, f"Expected at least 3 speccer spawns, got {len(spawned)}"


# ---------------------------------------------------------------------------
# E2E-3: Failure recovery
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_failure_recovery(e2e_env, tmp_storage_dir):
    """Speccer fails first attempt, conductor retries, succeeds on second."""
    run = make_run_state(0, "retry-run")
    state = make_conductor_state("retry-test", [run])
    config = ConductorConfig(
        check_interval_s=0.0,
        max_iterations=50,
        project_root=e2e_env.repo_path,
    )

    # fail_count=1: first invocation writes FAILED, second writes COMPLETE
    e2e_env.speccer.set_lifecycle(fail_count=1, final_status="COMPLETE")
    e2e_env.tmux.set_spawn_callback(_make_spawn_callback(e2e_env.speccer, e2e_env.tmux))

    result = await conductor_run_loop(state, config)

    # retry_count == 1
    assert result.runs[0].monitor.retry_count == 1, (
        f"Expected retry_count=1, got {result.runs[0].monitor.retry_count}"
    )

    # Run reaches DONE
    assert result.runs[0].status == RunStatus.DONE, (
        f"Expected run DONE, got {result.runs[0].status}"
    )

    # CONDUCTOR-LOG.md has retry event
    conductor_log = tmp_storage_dir / "conductor" / "retry-test" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"
    log_content = conductor_log.read_text()
    assert "RETRY" in log_content.upper(), f"Expected RETRY in log, got: {log_content}"

    # MockTmux shows 2 speccer spawns (initial + 1 retry)
    spawned = e2e_env.tmux.get_spawned_commands()
    assert len(spawned) == 2, f"Expected 2 spawns, got {len(spawned)}"


# ---------------------------------------------------------------------------
# E2E-4: Overnight mode (NEEDS_INPUT auto-answered)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_overnight_mode(e2e_env, tmp_storage_dir):
    """With NEEDS_INPUT, conductor auto-answers via brain and continues speccer."""
    run = make_run_state(0, "overnight-run")
    state = make_conductor_state("overnight-test", [run])
    config = ConductorConfig(
        check_interval_s=0.0,
        max_iterations=50,
        project_root=e2e_env.repo_path,
    )

    # First invocation -> NEEDS_INPUT, second -> COMPLETE
    e2e_env.speccer.set_lifecycle(needs_input=True, final_status="COMPLETE")
    e2e_env.claude.set_response(
        "answer-questions",
        "## QUESTIONS.md\n### Q1: Scope\n> Full scope\n### Q2: Auth\n> JWT tokens",
    )
    e2e_env.tmux.set_spawn_callback(_make_spawn_callback(e2e_env.speccer, e2e_env.tmux))

    result = await conductor_run_loop(state, config)

    # MockClaudeCLI was called for brain answer-questions
    assert e2e_env.claude.call_count >= 1, (
        f"Expected at least 1 brain call, got {e2e_env.claude.call_count}"
    )

    # Brain call logged to brain_calls_dir
    brain_calls_dir = tmp_storage_dir / "conductor" / "overnight-test" / "brain-calls"
    assert_brain_call_logged(brain_calls_dir)

    # Second speccer spawn has --continue
    spawned = e2e_env.tmux.get_spawned_commands()
    assert len(spawned) >= 2, f"Expected at least 2 spawns, got {len(spawned)}"
    assert any("--continue" in entry["cmd"] for entry in spawned), (
        "Expected a speccer spawn with --continue flag"
    )

    # Run reaches DONE
    assert result.runs[0].status == RunStatus.DONE, (
        f"Expected run DONE, got {result.runs[0].status}"
    )
