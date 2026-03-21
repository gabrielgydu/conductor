"""Tests for integration merge trigger in conductor_run_loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import IntegrationState, ConductorState
from conductor.core.orchestrator import ConductorConfig, conductor_run_loop
from conductor.core.storage import StorageResolver
from tests.helpers import make_conductor_state, make_run_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _done_run(index: int, name: str) -> object:
    run = make_run_state(index, name, status=RunStatus.DONE)
    for stage in run.stages:
        stage.status = StageStatus.DONE
    return run


def _blocked_run(index: int, name: str) -> object:
    return make_run_state(index, name, status=RunStatus.BLOCKED)


def _active_run(index: int, name: str) -> object:
    run = make_run_state(index, name, status=RunStatus.ACTIVE)
    run.stages[0].status = StageStatus.SPEC_RUNNING
    return run


def _make_state(*runs, project_name: str = "test-proj") -> ConductorState:
    return make_conductor_state(project_name, list(runs))


def _patch_storage(tmp_path: Path):
    """Return a context manager patching StorageResolver to use tmp_path."""
    storage_base = tmp_path / "storage"
    storage_base.mkdir(parents=True, exist_ok=True)

    def patched_init(self, repo_path: Path) -> None:
        self.repo_root = tmp_path / "repo"
        self._project_key = "test-project"
        self.base_dir = storage_base

    return patch.object(StorageResolver, "__init__", patched_init)


def _patch_tmux():
    """Return a context manager patching TmuxManager with a no-op mock."""
    tmux = MagicMock()
    tmux.ensure_session = AsyncMock()
    tmux.is_window_alive = AsyncMock(return_value=False)
    tmux.spawn_in_window = AsyncMock()
    return patch(
        "conductor.core.orchestrator._tmux_module.TmuxManager", return_value=tmux
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_triggered_when_all_terminal(tmp_path):
    """All runs DONE → run_integration_merge called once."""
    state = _make_state(_done_run(0, "feat-a"), _done_run(1, "feat-b"))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=5)

    fake_result = IntegrationState(status="done", branch="integration/test-proj")

    with _patch_storage(tmp_path):
        with _patch_tmux():
            with patch(
                "conductor.integration.merge.run_integration_merge",
                new_callable=AsyncMock,
                return_value=fake_result,
            ) as mock_merge:
                result = await conductor_run_loop(state, config)

    mock_merge.assert_called_once()
    assert result.integration is not None
    assert result.integration.status == "done"


@pytest.mark.asyncio
async def test_integration_not_triggered_when_active_runs(tmp_path):
    """Some ACTIVE runs → integration not called."""
    state = _make_state(_done_run(0, "feat-a"), _active_run(1, "feat-b"))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=2)

    with _patch_storage(tmp_path):
        with _patch_tmux():
            with patch(
                "conductor.integration.merge.run_integration_merge",
                new_callable=AsyncMock,
            ) as mock_merge:
                await conductor_run_loop(state, config)

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_integration_not_triggered_fewer_than_two_done(tmp_path):
    """1 DONE + 1 BLOCKED → done_count < 2 → integration not called."""
    state = _make_state(_done_run(0, "feat-a"), _blocked_run(1, "feat-b"))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=5)

    with _patch_storage(tmp_path):
        with _patch_tmux():
            with patch(
                "conductor.integration.merge.run_integration_merge",
                new_callable=AsyncMock,
            ) as mock_merge:
                await conductor_run_loop(state, config)

    mock_merge.assert_not_called()


@pytest.mark.asyncio
async def test_integration_runs_only_once(tmp_path):
    """After integration completes, subsequent ticks don't re-trigger."""
    state = _make_state(_done_run(0, "feat-a"), _done_run(1, "feat-b"))
    # Give many iterations to ensure idempotency
    config = ConductorConfig(check_interval_s=0.0, max_iterations=10)

    fake_result = IntegrationState(status="done", branch="integration/test-proj")

    with _patch_storage(tmp_path):
        with _patch_tmux():
            with patch(
                "conductor.integration.merge.run_integration_merge",
                new_callable=AsyncMock,
                return_value=fake_result,
            ) as mock_merge:
                result = await conductor_run_loop(state, config)

    # Should only be called once regardless of iteration count
    mock_merge.assert_called_once()
    assert result.integration is not None


@pytest.mark.asyncio
async def test_integration_state_persisted(tmp_path):
    """After integration, state file contains IntegrationState."""
    state = _make_state(_done_run(0, "feat-a"), _done_run(1, "feat-b"))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=5)

    fake_result = IntegrationState(status="done", branch="integration/test-proj")

    storage_base = tmp_path / "storage"
    storage_base.mkdir(parents=True, exist_ok=True)

    def patched_init(self, repo_path: Path) -> None:
        self.repo_root = tmp_path / "repo"
        self._project_key = "test-project"
        self.base_dir = storage_base

    with patch.object(StorageResolver, "__init__", patched_init):
        with _patch_tmux():
            with patch(
                "conductor.integration.merge.run_integration_merge",
                new_callable=AsyncMock,
                return_value=fake_result,
            ):
                result = await conductor_run_loop(state, config)

    # State file should exist and contain integration data
    state_file = storage_base / "conductor" / "test-proj" / "CONDUCTOR-STATE.json"
    assert state_file.exists(), "State file was not created"
    import json

    saved = json.loads(state_file.read_text())
    assert saved["integration"] is not None
    assert saved["integration"]["branch"] == "integration/test-proj"
    assert result.integration.branch == "integration/test-proj"


@pytest.mark.asyncio
async def test_integration_failure_handled(tmp_path):
    """run_integration_merge raises → state.integration set to FAILED, loop continues."""
    state = _make_state(_done_run(0, "feat-a"), _done_run(1, "feat-b"))
    config = ConductorConfig(check_interval_s=0.0, max_iterations=5)

    with _patch_storage(tmp_path):
        with _patch_tmux():
            with patch(
                "conductor.integration.merge.run_integration_merge",
                new_callable=AsyncMock,
                side_effect=RuntimeError("merge exploded"),
            ):
                result = await conductor_run_loop(state, config)

    # Loop should complete without raising
    assert result.integration is not None
    assert result.integration.status == "failed"
    assert result.integration.branch == ""
