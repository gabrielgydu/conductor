"""E2E test fixtures: E2EEnvironment wrapping all mock infrastructure."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conductor.core.models import ConductorState, atomic_save
from conductor.core.storage import StorageResolver

# Import integration fixtures so pytest discovers them
from tests.integration.conftest import (  # noqa: F401
    mock_claude_cli,
    mock_tmux,
    mock_speccer,
    mock_runner,
    tmp_git_repo,
    tmp_storage_dir,
    MockClaudeCLI,
    MockTmux,
    MockSpeccer,
    MockRunner,
)


@pytest.fixture(autouse=True)
def mock_conductor_post_run_e2e(monkeypatch):
    """Prevent conductor_post_run from hitting real Claude API in all e2e tests."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "conductor.core.orchestrator.conductor_post_run",
        AsyncMock(return_value=None),
    )


@pytest.fixture(autouse=True)
def patch_orchestrator_create_worktree_e2e(monkeypatch, tmp_path):
    """Patch create_worktree to avoid real git ops in e2e tests."""
    import conductor.core.orchestrator as orch

    _wt_counter = [0]

    def mock_create_worktree(
        state, run_idx, stage_idx, storage_or_project_dir, *args, **kwargs
    ):
        run = state.runs[run_idx]
        stage = run.stages[stage_idx]
        _wt_counter[0] += 1
        wt_dir = tmp_path / "worktrees" / f"wt-{_wt_counter[0]}"
        wt_dir.mkdir(parents=True, exist_ok=True)
        stage.branch = f"conductor/{state.project_name}/{run.name}/{stage.name}"
        stage.worktree = str(wt_dir)

    monkeypatch.setattr(orch, "create_worktree", mock_create_worktree)


class E2EEnvironment:
    def __init__(
        self,
        repo_path: Path,
        storage_dir: Path,
        claude: MockClaudeCLI,
        tmux: MockTmux,
        speccer: MockSpeccer,
        runner: MockRunner,
    ) -> None:
        self.repo_path = repo_path
        self.storage_dir = storage_dir
        self.claude = claude
        self.tmux = tmux
        self.speccer = speccer
        self.runner = runner

    def init_project(self, name: str, brief: str) -> ConductorState:
        """Create a ConductorState for the project and write brief to storage."""
        storage = StorageResolver(self.repo_path)
        state = ConductorState(
            project_name=name,
            base_branch="main",
            check_interval_s=0,
            runs=[],
        )
        state_path = storage.conductor_state(name)
        atomic_save(state, state_path)

        brief_path = storage.conductor_brief(name)
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief)

        return state

    def get_state(self, project_name: str) -> ConductorState:
        """Load state from storage."""
        from conductor.core.models import load_state

        storage = StorageResolver(self.repo_path)
        state_path = storage.conductor_state(project_name)
        return load_state(state_path, ConductorState)

    def make_config(
        self, check_interval_s: float = 0.1, max_iterations: int = 50
    ) -> dict[str, Any]:
        """Return a config dict with test-appropriate defaults."""
        return {
            "check_interval_s": check_interval_s,
            "max_iterations": max_iterations,
        }


@pytest.fixture
def e2e_env(  # noqa: F811
    tmp_git_repo,  # noqa: F811
    tmp_storage_dir,  # noqa: F811
    mock_claude_cli,  # noqa: F811
    mock_tmux,  # noqa: F811
    mock_speccer,  # noqa: F811
    mock_runner,  # noqa: F811
) -> E2EEnvironment:
    return E2EEnvironment(
        repo_path=tmp_git_repo,
        storage_dir=tmp_storage_dir,
        claude=mock_claude_cli,
        tmux=mock_tmux,
        speccer=mock_speccer,
        runner=mock_runner,
    )
