"""Blocking speccer steps are polled with liveness and inactivity checks,
so a dead or silent window cannot freeze the orchestrator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import conductor.core.orchestrator as orch
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState


def _tmux(*, alive: bool = True, idle: bool = False) -> MagicMock:
    tmux = MagicMock()
    tmux.spawn_runner_in_window = AsyncMock()
    tmux.is_window_alive = AsyncMock(return_value=alive)
    tmux.is_runner_idle = AsyncMock(return_value=idle)
    tmux.get_pane_pid = AsyncMock(return_value=None)
    tmux.kill_window = AsyncMock()
    return tmux


@pytest.fixture
def fast_polling(monkeypatch):
    monkeypatch.setattr(orch, "STEP_POLL_S", 0.01, raising=False)
    monkeypatch.setattr(orch, "STEP_STARTUP_GRACE_S", 0.0, raising=False)
    monkeypatch.setattr(orch, "STEP_INACTIVITY_S", 0.3, raising=False)


def _step(tmux, tmp_path: Path, stage: StageState | None = None):
    return orch._run_step_in_window(
        tmux,
        "run0:backend",
        "speccer run",
        exit_file=tmp_path / "exit",
        cwd=str(tmp_path),
        log_file=tmp_path / "pane.log",
        activity=[tmp_path / "spec"],
        stage_state=stage or StageState(name="backend", spec_mode="backend"),
        log_path=None,
        audit_path=None,
    )


@pytest.mark.asyncio
async def test_closed_window_without_exit_code_does_not_hang(tmp_path, fast_polling):
    rc = await asyncio.wait_for(_step(_tmux(alive=False), tmp_path), 2)
    assert rc is None


@pytest.mark.asyncio
async def test_silent_window_is_stopped_after_the_inactivity_limit(tmp_path, fast_polling):
    tmux = _tmux()
    stage = StageState(name="backend", spec_mode="backend")

    rc = await asyncio.wait_for(_step(tmux, tmp_path, stage), 5)

    assert rc == orch.EXIT_TIMED_OUT
    assert (tmp_path / "exit").read_text() == str(orch.EXIT_TIMED_OUT)
    tmux.kill_window.assert_awaited_once_with("run0:backend")
    assert "no output" in stage.last_error


@pytest.mark.asyncio
async def test_output_keeps_a_long_step_alive(tmp_path, fast_polling):
    tmux = _tmux()
    log = tmp_path / "pane.log"

    async def busy_then_exit():
        for _ in range(8):  # 0.8 s of output, well past the 0.3 s limit
            log.write_text("thinking...\n")
            await asyncio.sleep(0.1)
        (tmp_path / "exit").write_text("0")

    worker = asyncio.create_task(busy_then_exit())
    rc = await asyncio.wait_for(_step(tmux, tmp_path), 5)
    await worker

    assert rc == 0
    tmux.kill_window.assert_not_awaited()


@pytest.mark.asyncio
async def test_speccer_init_fails_the_stage_when_its_window_dies(tmp_path, fast_polling):
    wt = tmp_path / "wt"
    stage = StageState(name="backend", spec_mode="backend", status=StageStatus.PENDING)
    stage.worktree = str(wt)
    run = RunState(index=0, name="auth", description="x", stages=[stage], status=RunStatus.ACTIVE)
    state = ConductorState(project_name="proj", runs=[run])
    home = tmp_path / "home"
    storage = SimpleNamespace(
        conductor_dir=lambda name: home / name,
        tmux_log=lambda name, label: home / name / "logs" / f"{label}.log",
    )

    await asyncio.wait_for(
        orch.run_speccer_init(state, 0, 0, _tmux(alive=False), storage), 2
    )

    assert stage.status == StageStatus.FAILED
