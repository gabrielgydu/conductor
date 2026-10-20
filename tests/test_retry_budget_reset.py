"""Infra/transient retry budgets reset when the runner commits between failures."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import conductor.core.orchestrator as orch
from conductor.core.enums import StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import ConductorConfig

T0 = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _commit(wt, n):
    (wt / f"f{n}.txt").write_text(str(n))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"phase {n}")


@pytest.fixture
def wt(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _commit(wt, 0)
    return wt


@pytest.fixture
def storage(tmp_path):
    return SimpleNamespace(conductor_dir=lambda name: tmp_path / "home" / name)


def _state(wt):
    stage = StageState(name="s0", spec_mode="full", worktree=str(wt))
    run = RunState(index=0, name="feat", description="d", stages=[stage])
    return ConductorState(project_name="p", runs=[run])


async def _restart(state, run_idx, stage_idx, storage, log_path=None, audit_path=None):
    st = state.runs[run_idx].stages[stage_idx]
    st.last_exit_code = None
    st.status = StageStatus.GENERATED


class _Clock:
    now_value = T0

    class DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return _Clock.now_value


async def _fail(state, storage, exit_code, at=None):
    st = state.runs[0].stages[0]
    if at is not None:
        _Clock.now_value = at
    st.status = StageStatus.FAILED
    st.last_exit_code = exit_code
    st.backoff_until = None
    await orch.handle_failure(state, 0, 0, storage, ConductorConfig())
    return st


@pytest.fixture(autouse=True)
def _patches():
    _Clock.now_value = T0
    with patch.object(orch, "restart_stage", _restart), \
         patch.object(orch, "datetime", _Clock.DT):
        yield


@pytest.mark.asyncio
async def test_transient_window_blocks_without_progress(wt, storage):
    state = _state(wt)
    with patch.object(orch, "_is_transient_api_error", return_value=True):
        await _fail(state, storage, 1, at=T0)
        st = await _fail(state, storage, 1, at=T0 + timedelta(hours=8, minutes=30))
    assert st.status == StageStatus.BLOCKED


@pytest.mark.asyncio
async def test_transient_window_resets_after_commit(wt, storage):
    state = _state(wt)
    with patch.object(orch, "_is_transient_api_error", return_value=True):
        await _fail(state, storage, 1, at=T0)
        _commit(wt, 1)  # runner finished a phase after recovering
        later = T0 + timedelta(hours=8, minutes=30)
        st = await _fail(state, storage, 1, at=later)
    assert st.status == StageStatus.GENERATED
    assert st.first_transient_failure_ts == later
    assert st.transient_retries == 1
    assert st.backoff_until == later + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_transient_window_resets_after_hours_of_running(wt, storage):
    """No commit, but the runner worked for hours: the API was fine meanwhile."""
    state = _state(wt)
    with patch.object(orch, "_is_transient_api_error", return_value=True):
        await _fail(state, storage, 1, at=T0)
        state.runs[0].stages[0].started_at = T0 + timedelta(minutes=5)
        later = T0 + timedelta(hours=8, minutes=30)
        st = await _fail(state, storage, 1, at=later)
    assert st.status == StageStatus.GENERATED
    assert st.first_transient_failure_ts == later


@pytest.mark.asyncio
async def test_infra_budget_resets_after_commit(wt, storage):
    state = _state(wt)
    for n in range(1, 8):
        st = await _fail(state, storage, 143)
        assert st.status == StageStatus.GENERATED
        _commit(wt, n)
    assert st.infra_retries == 1


@pytest.mark.asyncio
async def test_infra_budget_still_caps_without_progress(wt, storage):
    state = _state(wt)
    for _ in range(5):
        st = await _fail(state, storage, 143)
        assert st.status == StageStatus.GENERATED
    st = await _fail(state, storage, 143)
    assert st.status == StageStatus.BLOCKED
