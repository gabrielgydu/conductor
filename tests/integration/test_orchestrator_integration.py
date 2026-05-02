"""Integration test: full pipeline with orchestrator triggering integration merge."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))

from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import IntegrationState, ConductorState
from conductor.core.orchestrator import ConductorConfig, conductor_run_loop
from conductor.core.storage import StorageResolver
from helpers import make_conductor_state, make_run_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _done_run(index: int, name: str):
    run = make_run_state(index, name, status=RunStatus.DONE)
    for stage in run.stages:
        stage.status = StageStatus.DONE
    return run


def _patch_storage(tmp_path: Path):
    storage_base = tmp_path / "storage"
    storage_base.mkdir(parents=True, exist_ok=True)

    def patched_init(self, repo_path: Path) -> None:
        self.repo_root = tmp_path / "repo"
        self._project_key = "test-project"
        self.base_dir = storage_base

    return patch.object(StorageResolver, "__init__", patched_init)


def _patch_tmux():
    tmux = MagicMock()
    tmux.ensure_session = AsyncMock()
    tmux.is_window_alive = AsyncMock(return_value=False)
    tmux.is_runner_idle = AsyncMock(return_value=True)
    tmux.spawn_in_window = AsyncMock()
    tmux.spawn_in_window_and_wait = AsyncMock(return_value=0)
    tmux.spawn_runner_in_window = AsyncMock()
    return patch(
        "conductor.core.orchestrator._tmux_module.TmuxManager", return_value=tmux
    )


def _patch_post_run():
    return patch(
        "conductor.core.orchestrator.conductor_post_run",
        new_callable=AsyncMock,
    )


# ---------------------------------------------------------------------------
# Test: full pipeline → integration merge triggered → state reflects result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_with_merge(tmp_path):
    """Runs complete → integration merge triggered → state.integration populated."""
    state = make_conductor_state(
        "test-pipeline",
        [_done_run(0, "feat-a"), _done_run(1, "feat-b")],
    )
    config = ConductorConfig(check_interval_s=0.0, max_iterations=5)

    fake_integration = IntegrationState(
        status="done",
        branch="integration/test-pipeline",
        merged_runs=[0, 1],
    )

    with _patch_storage(tmp_path), _patch_tmux(), _patch_post_run():
        with patch(
            "conductor.integration.merge.run_integration_merge",
            new_callable=AsyncMock,
            return_value=fake_integration,
        ) as mock_merge:
            result = await conductor_run_loop(state, config)

    mock_merge.assert_called_once()
    assert result.integration is not None
    assert result.integration.status == "done"
    assert result.integration.branch == "integration/test-pipeline"
    assert result.integration.merged_runs == [0, 1]
