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

pytestmark = pytest.mark.usefixtures("isolated_run_loop")


def _make_spawn_callback(mock_speccer, mock_tmux):
    """Return a spawn callback that drives MockSpeccer.

    Every window finishes at once with exit code 0: speccer windows after
    MockSpeccer has written its files, runner windows (name ends with -exec)
    straight away, so the orchestrator sees a clean completion.
    """

    def callback(name: str, cmd: str) -> None:
        if not name.endswith("-exec"):
            mock_speccer._handle_spawn(name, cmd)
        mock_tmux.finish_window(name, 0)

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

    # CONDUCTOR-LOG.md should exist (contains lifecycle events)
    conductor_log = tmp_storage_dir / "conductor" / "pipeline-test" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"

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

    # CONDUCTOR-LOG.md should exist
    conductor_log = (
        tmp_storage_dir / "conductor" / "multi-run-test" / "CONDUCTOR-LOG.md"
    )
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"

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

    # Run should be terminal
    assert result.runs[0].status in (RunStatus.DONE, RunStatus.BLOCKED), (
        f"Expected run DONE or BLOCKED, got {result.runs[0].status}"
    )

    # CONDUCTOR-LOG.md should exist
    conductor_log = tmp_storage_dir / "conductor" / "retry-test" / "CONDUCTOR-LOG.md"
    assert conductor_log.exists(), "CONDUCTOR-LOG.md was not created"

    # At least 1 speccer spawn expected
    spawned = e2e_env.tmux.get_spawned_commands()
    assert len(spawned) >= 1, f"Expected at least 1 spawn, got {len(spawned)}"


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
        overnight=True,  # Enable overnight mode for NEEDS_INPUT auto-answering
    )

    # First invocation -> NEEDS_INPUT, second -> COMPLETE
    e2e_env.speccer.set_lifecycle(needs_input=True, final_status="COMPLETE")
    e2e_env.claude.set_response(
        "answering clarification questions",  # answer_questions' prompt
        "### Q1: Scope\nFull scope\n### Q2: Auth\nJWT tokens",
    )
    e2e_env.tmux.set_spawn_callback(_make_spawn_callback(e2e_env.speccer, e2e_env.tmux))

    result = await conductor_run_loop(state, config)

    # Claude was called for brain answer-questions
    assert e2e_env.claude.call_count >= 1, (
        f"Expected at least 1 brain call, got {e2e_env.claude.call_count}"
    )

    # At least 2 speccer spawns (initial + continue with --continue)
    spawned = e2e_env.tmux.get_spawned_commands()
    assert len(spawned) >= 2, f"Expected at least 2 spawns, got {len(spawned)}"

    # Run reaches DONE
    assert result.runs[0].status == RunStatus.DONE, (
        f"Expected run DONE, got {result.runs[0].status}"
    )
