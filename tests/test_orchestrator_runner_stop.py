"""A stage never leaves EXECUTING with its runner still working, and a
runner's late exit 0 is kept instead of being thrown away."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import conductor.core.orchestrator as orch
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import ConductorConfig

WINDOW = "run0:backend-exec"


def _env(tmp_path: Path, status: StageStatus = StageStatus.EXECUTING, minutes: int = 10):
    wt = tmp_path / "wt"
    (wt / "docs" / "auth").mkdir(parents=True)
    run_sh = wt / "docs" / "auth" / "run.sh"
    run_sh.write_text('PHASES[1]="impl"\n')
    stage = StageState(name="backend", spec_mode="backend", status=status)
    stage.worktree = str(wt)
    stage.started_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    run = RunState(
        index=0, name="auth", description="login", stages=[stage], status=RunStatus.ACTIVE
    )
    state = ConductorState(project_name="proj", runs=[run])
    home = tmp_path / "home"
    storage = SimpleNamespace(
        conductor_dir=lambda name: home / name,
        conductor_state=lambda name: home / name / "state.json",
    )
    return state, storage, wt


def _tmux(*, alive: bool = True, idle: bool = False, healthy: bool = True) -> MagicMock:
    tmux = MagicMock()
    tmux._session_name = "conductor-proj"
    tmux.is_window_alive = AsyncMock(return_value=alive)
    tmux.is_runner_idle = AsyncMock(return_value=idle)
    tmux.has_active_children = AsyncMock(return_value=healthy)
    tmux.get_pane_pid = AsyncMock(return_value=None)
    tmux.kill_window = AsyncMock()
    return tmux


def _exit_file(storage) -> Path:
    path = orch._runner_exit_file(storage, "proj", "auth")
    return path


# ---------------------------------------------------------------------------
# monitor_runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stalled_runner_is_stopped_when_its_stage_fails(tmp_path, monkeypatch):
    state, storage, _ = _env(tmp_path)
    tmux = _tmux()

    async def stall(state, run_idx, stage_idx, *a, **k):
        state.runs[run_idx].stages[stage_idx].status = StageStatus.FAILED

    monkeypatch.setattr(orch, "check_stall", stall)
    await orch.monitor_runner(state, 0, 0, tmux, storage, ConductorConfig())

    assert state.runs[0].stages[0].status == StageStatus.FAILED
    tmux.kill_window.assert_awaited_once_with(WINDOW)


@pytest.mark.asyncio
async def test_exit_written_while_declaring_the_runner_dead_wins(tmp_path):
    """The runner exits 0 between the exit-file check and the idle check."""
    state, storage, _ = _env(tmp_path)
    tmux = _tmux()

    async def idle_after_exit(window):
        exit_file = _exit_file(storage)
        exit_file.parent.mkdir(parents=True, exist_ok=True)
        exit_file.write_text("0")
        return True

    tmux.is_runner_idle = AsyncMock(side_effect=idle_after_exit)
    await orch.monitor_runner(state, 0, 0, tmux, storage, ConductorConfig())

    assert state.runs[0].stages[0].status == StageStatus.DONE


@pytest.mark.asyncio
async def test_runner_exit_code_file_counts_only_when_written_by_this_attempt(tmp_path):
    state, storage, wt = _env(tmp_path)
    durable = wt / "docs" / "auth" / "exit_code"
    durable.write_text("0")
    stale = state.runs[0].stages[0].started_at.timestamp() - 60
    os.utime(durable, (stale, stale))

    assert orch._runner_outcome(state, 0, 0, storage) is None

    durable.write_text("0")  # rewritten now, after the attempt started
    assert orch._runner_outcome(state, 0, 0, storage) == 0


# ---------------------------------------------------------------------------
# BLOCKED stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_file_caught_mid_write_is_not_a_failure(tmp_path, monkeypatch):
    """`echo 0 > file` truncates before writing; an empty file read in that
    instant must not turn a successful runner into exit 1 or a dead runner."""
    state, storage, _ = _env(tmp_path)
    exit_file = _exit_file(storage)
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text("")
    finished = MagicMock()
    monkeypatch.setattr(orch, "_finish_runner", finished)

    await orch.monitor_runner(state, 0, 0, _tmux(idle=True), storage, ConductorConfig())

    finished.assert_not_called()
    assert state.runs[0].stages[0].status == StageStatus.EXECUTING

    # A file that stays empty is a wrapper killed mid-write: a failure.
    old = time.time() - orch.EXIT_FILE_SETTLE_S - 1
    os.utime(exit_file, (old, old))
    await orch.monitor_runner(state, 0, 0, _tmux(), storage, ConductorConfig())

    assert finished.call_args.args[3] == 1


@pytest.mark.asyncio
async def test_blocking_stops_the_runner_before_container_teardown(tmp_path, monkeypatch):
    state, storage, _ = _env(tmp_path, StageStatus.BLOCKED)
    tmux = _tmux()
    order: list[str] = []
    tmux.kill_window = AsyncMock(side_effect=lambda w: order.append("kill"))
    monkeypatch.setattr(
        orch, "_teardown_run_containers", lambda *a, **k: order.append("teardown")
    )

    with patch.object(orch, "atomic_save"):
        await orch.advance_run(state, 0, tmux, storage, ConductorConfig())

    assert order == ["kill", "teardown"]
    assert state.runs[0].status == RunStatus.BLOCKED


@pytest.mark.asyncio
async def test_blocked_stage_whose_runner_exited_0_is_done(tmp_path):
    state, storage, _ = _env(tmp_path, StageStatus.BLOCKED)
    exit_file = _exit_file(storage)
    exit_file.write_text("0")

    with patch.object(orch, "atomic_save"):
        await orch.advance_run(state, 0, _tmux(idle=True), storage, ConductorConfig())

    assert state.runs[0].stages[0].status == StageStatus.DONE
    assert state.runs[0].status == RunStatus.ACTIVE


@pytest.mark.asyncio
async def test_auto_resume_keeps_a_successful_exit_after_the_block(tmp_path):
    state, storage, _ = _env(tmp_path, StageStatus.BLOCKED)
    state.runs[0].status = RunStatus.BLOCKED
    exit_file = _exit_file(storage)
    exit_file.write_text("0")

    with patch.object(orch, "atomic_save"):
        await orch._auto_resume_blocked_runs(state, storage, None, None, tmux=_tmux(idle=True))

    assert state.runs[0].stages[0].status == StageStatus.DONE
    assert state.runs[0].status == RunStatus.PENDING  # activated within max_parallel
    assert exit_file.read_text() == "0"


@pytest.mark.asyncio
async def test_auto_resume_watches_a_runner_that_is_still_running(tmp_path):
    state, storage, _ = _env(tmp_path, StageStatus.BLOCKED)
    state.runs[0].status = RunStatus.BLOCKED

    with (
        patch.object(orch, "atomic_save"),
        patch.object(orch, "restart_stage", new_callable=AsyncMock) as restart,
    ):
        await orch._auto_resume_blocked_runs(state, storage, None, None, tmux=_tmux())

    restart.assert_not_awaited()
    assert state.runs[0].stages[0].status == StageStatus.EXECUTING
    assert state.runs[0].status == RunStatus.ACTIVE


# ---------------------------------------------------------------------------
# _stop_runner against a real process group
# ---------------------------------------------------------------------------


needs_proc = pytest.mark.skipif(
    not Path("/proc/self/cmdline").exists(), reason="needs /proc"
)


def _spawn_group(argv: list[str]) -> subprocess.Popen:
    """Start *argv* as its own session/process group, like a tmux pane."""
    return subprocess.Popen(argv, start_new_session=True)


@pytest.fixture
def wrapper_group(tmp_path):
    exit_file = tmp_path / "ipc" / "conductor-exit-auth"
    # Same shape as tmux's runner wrapper: the exit file is in its command line.
    proc = _spawn_group(["sh", "-c", f"sleep 60 & sleep 60; echo $? > {exit_file}"])
    # tmux reaps its pane processes; here the test is the parent, so reap too.
    reaper = threading.Thread(target=proc.wait, daemon=True)
    reaper.start()
    yield proc, exit_file, reaper
    if proc.poll() is None:
        os.killpg(proc.pid, 9)
        proc.wait()


@pytest.fixture
def foreign_process():
    proc = _spawn_group(["sleep", "30"])
    yield proc
    proc.kill()
    proc.wait()


@needs_proc
@pytest.mark.asyncio
async def test_stop_runner_terminates_the_wrapper_process_group(wrapper_group):
    proc, exit_file, reaper = wrapper_group
    tmux = _tmux()
    tmux.get_pane_pid = AsyncMock(return_value=proc.pid)

    started = time.monotonic()
    await orch._stop_runner(tmux, WINDOW, exit_file, "test", None, None)

    reaper.join(timeout=5)
    assert proc.returncode is not None
    assert time.monotonic() - started < 5, "SIGTERM should be enough"
    assert not orch._group_alive(proc.pid)
    tmux.kill_window.assert_awaited_once_with(WINDOW)


@pytest.mark.asyncio
async def test_stop_runner_never_signals_a_foreign_process(tmp_path, foreign_process):
    """A reused pane pid whose command is not our wrapper is left alone."""
    tmux = _tmux()
    tmux.get_pane_pid = AsyncMock(return_value=foreign_process.pid)

    await orch._stop_runner(tmux, WINDOW, tmp_path / "conductor-exit-x", "t", None, None)

    assert foreign_process.poll() is None
