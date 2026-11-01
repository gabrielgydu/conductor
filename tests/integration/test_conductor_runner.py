"""Integration tests: conductor driving runner through execution lifecycle."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

import conductor.core.orchestrator as orch
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.orchestrator import (
    ConductorConfig,
    compute_progress_hash,
    conductor_run_loop,
)

# Helpers from shared module
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))
from helpers import (
    assert_brain_call_logged,
    assert_log_contains_events,
    make_conductor_state,
    make_run_state,
)

pytestmark = pytest.mark.usefixtures("isolated_run_loop")

WINDOW = "run0:stage-0-exec"


def _audit_log(storage_dir: Path) -> Path:
    """CONDUCTOR-AUDIT.jsonl gets every event; CONDUCTOR-LOG.md only some."""
    return storage_dir / "conductor" / "test-project" / "CONDUCTOR-AUDIT.jsonl"


def _executing_state(tmp_path: Path, activity: str | None = None):
    """test-run stage-0 EXECUTING in a worktree, runner started 10 minutes ago.

    The start time puts the stage past monitor_runner's 30 s/60 s and
    check_stall's 5 min grace periods. *activity* becomes the runner's
    docs/test-run/activity.log, the file the stall check hashes.
    """
    run = make_run_state(0, "test-run")
    stage = run.stages[0]
    stage.status = StageStatus.EXECUTING
    stage.pid = 12345
    stage.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    wt_dir = tmp_path / "worktrees" / "wt-0"
    wt_dir.mkdir(parents=True, exist_ok=True)
    stage.worktree = str(wt_dir)
    stage.branch = "conductor/test-project/test-run/stage-0"
    if activity is not None:
        log = wt_dir / "docs" / "test-run" / "activity.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(activity)
    return make_conductor_state("test-project", [run])


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

    # speccer generate leaves run.sh (with PHASES) in the worktree's docs/<feature>/
    mock_runner.write_run_sh(wt_dir / "docs" / "test-run")

    # Spawn callback: runner exits 0 right after spawning
    def spawn_callback(name: str, cmd: str) -> None:
        if name.endswith("-exec"):
            mock_tmux.finish_window(name, 0)

    mock_tmux.set_spawn_callback(spawn_callback)

    result = await conductor_run_loop(state, config)

    # Stage should transition through EXECUTING to DONE (or at EXECUTING)
    final_status = result.runs[0].stages[0].status
    assert final_status in (StageStatus.EXECUTING, StageStatus.DONE), (
        f"Expected EXECUTING or DONE after runner spawn, got {final_status}"
    )

    # Runner window spawned via spawn_runner_in_window
    spawned = mock_tmux.get_spawned_commands()
    assert any(entry.get("runner") for entry in spawned), (
        f"Expected runner spawn, got: {[e['cmd'] for e in spawned]}"
    )

    # Audit log records the runner start and its clean exit
    assert_log_contains_events(
        _audit_log(tmp_storage_dir), ["RUNNER_START", "RUNNER_EXIT"]
    )


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
    # Started before its exit file was written: older exit files are not its result
    state = _executing_state(tmp_path)
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window is dead (already exited cleanly)
    window_name = "run0:stage-0-exec"
    mock_tmux.set_window_alive(window_name, False)
    mock_tmux.set_window_exit_code(window_name, 0)

    # The runner's tmux wrapper wrote its exit file into the project's ipc dir
    exit_file = tmp_storage_dir / "conductor" / "test-project" / "ipc" / "conductor-exit-test-run"
    exit_file.parent.mkdir(parents=True, exist_ok=True)
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
# CR-3: Stall detection with heartbeat suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_runner_stall(
    mock_tmux,
    mock_runner,
    mock_claude_cli,
    tmp_storage_dir,
    tmp_path,
):
    """Frozen activity log is counted as a stall, but a live Claude caps it.

    While the runner's process tree is healthy (has_active_children), the
    heartbeat keeps stall_count at 1, so no diagnose-stall brain call is made
    and the stage keeps EXECUTING: a long tool call is not a stall.
    """
    state = _executing_state(tmp_path, activity="stuck here\n")
    config = ConductorConfig(check_interval_s=0.0, max_iterations=6)
    mock_tmux.set_window_alive(WINDOW, True)
    mock_tmux.set_window_children(WINDOW, True)

    result = await conductor_run_loop(state, config)

    run = result.runs[0]
    assert run.stages[0].status == StageStatus.EXECUTING
    assert run.monitor.stall_count == 1, (
        f"Expected heartbeat to cap stall_count at 1, got {run.monitor.stall_count}"
    )
    assert run.monitor.last_heartbeat_ts is not None
    assert mock_claude_cli.call_count == 0, "No brain call while Claude is alive"

    assert_log_contains_events(_audit_log(tmp_storage_dir), ["STALL_CHECK"])


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
    """Brain diagnosis returns STEER, conductor writes it to the runner's steer_inbox.

    Escalation needs stall_count >= 2 without the heartbeat cap: the activity
    log already looked frozen on the previous check (stall_count=1) and the
    Claude process is gone now (shallow process tree).
    """
    state = _executing_state(tmp_path, activity="working on task\n")
    run = state.runs[0]
    run.monitor.last_progress_hash = compute_progress_hash(state, 0, 0, None)
    run.monitor.stall_count = 1
    config = ConductorConfig(check_interval_s=0.0, max_iterations=1)
    mock_tmux.set_window_alive(WINDOW, True)
    mock_tmux.set_window_children(WINDOW, False)

    steer_message = "Focus on the database migration"
    # Matches the diagnose-stall-prompt.md template the orchestrator renders
    mock_claude_cli.set_response(
        "diagnosing a stalled automation pipeline stage",
        f"ACTION: STEER\n{steer_message}",
    )

    result = await conductor_run_loop(state, config)

    assert result.runs[0].monitor.stall_count == 2
    assert mock_claude_cli.call_count == 1, "Expected one diagnose-stall brain call"

    inbox = Path(state.runs[0].stages[0].worktree) / "docs" / "test-run" / "steer_inbox"
    messages = [m.read_text() for m in sorted(inbox.glob("*.msg"))]
    assert messages == [steer_message], f"Expected steer message in inbox, got {messages}"

    assert_log_contains_events(
        _audit_log(tmp_storage_dir), ["diagnose-stall", "RUNNER_STEER"]
    )


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
    state = _executing_state(tmp_path)
    config = ConductorConfig(check_interval_s=0.0, max_iterations=20)

    # Runner window is dead — no exit file written
    mock_tmux.set_window_alive(WINDOW, False)
    # Intentionally no exit_code file (this is the key to dead runner detection)
    # Speccer steps of a restarted stage finish at once.
    mock_tmux.set_spawn_callback(
        lambda name, cmd: None if name.endswith("-exec") else mock_tmux.finish_window(name, 0)
    )

    result = await conductor_run_loop(state, config)

    # Stage should be either FAILED or BLOCKED (failed initially, then handled by brain)
    assert result.runs[0].stages[0].status in (
        StageStatus.FAILED,
        StageStatus.BLOCKED,
    ), (
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


# ---------------------------------------------------------------------------
# CR-6: Zombie runner (window open, Claude process gone)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conductor_detects_zombie_runner(
    mock_tmux,
    mock_runner,
    mock_claude_cli,
    tmp_storage_dir,
    tmp_path,
    monkeypatch,
):
    """Shallow process tree for a minute -> exit 143 -> infra retry from GENERATED."""
    state = _executing_state(tmp_path, activity="last words\n")
    mock_runner.write_run_sh(Path(state.runs[0].stages[0].worktree) / "docs" / "test-run")
    # Checks 30 s apart: shallow for 0 s, 30 s, then 60 s declares the zombie;
    # one more advance handles the failure.
    clock = iter(range(0, 3600, 30))
    shallow_for = orch._shallow_for
    monkeypatch.setattr(
        orch, "_shallow_for",
        lambda key, shallow, now=None: shallow_for(key, shallow, float(next(clock))),
    )
    config = ConductorConfig(check_interval_s=0.0, max_iterations=4)
    mock_tmux.set_window_alive(WINDOW, True)
    mock_tmux.set_window_children(WINDOW, False)

    result = await conductor_run_loop(state, config)

    stage = result.runs[0].stages[0]
    assert stage.infra_retries == 1, f"Expected one infra retry, got {stage.infra_retries}"
    assert stage.status == StageStatus.GENERATED, (
        f"Expected restart from GENERATED (valid run.sh), got {stage.status}"
    )
    assert mock_claude_cli.call_count == 0, "Infra failures retry without a brain call"
    assert_log_contains_events(
        _audit_log(tmp_storage_dir), ["RUNNER_SHALLOW_TREE", "RUNNER_ZOMBIE", "INFRA_RETRY"]
    )
