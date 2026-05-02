"""Integration tests: E2E testing pipeline (TDD — conductor.integration.e2e not yet implemented)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))

from conductor.core.enums import IntegrationStatus, RunStatus
from conductor.core.models import (
    ConductorState,
    E2ETestState,
    IntegrationState,
    RunState,
    StageState,
)

try:
    from conductor.integration.e2e import run_integration_testing
except ImportError:
    run_integration_testing = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeStorage:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root


def _done_run_with_branch(index: int, name: str, branch: str) -> RunState:
    stage = StageState(name="impl", spec_mode="full", branch=branch, status="done")
    return RunState(
        index=index,
        name=name,
        description=f"{name} desc",
        stages=[stage],
        status=RunStatus.DONE,
    )


def _project_name(tmp_path: Path) -> str:
    return f"it{abs(hash(str(tmp_path))) % 999999:06d}"


def _make_state(project_name: str, *runs: RunState) -> ConductorState:
    return ConductorState(
        project_name=project_name,
        base_branch="main",
        runs=list(runs),
    )


def _add_file_to_branch(repo_path: Path, branch: str, filename: str, content: str):
    """Checkout branch, add file, commit, checkout main."""
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", branch],
        check=True,
        capture_output=True,
    )
    (repo_path / filename).write_text(content)
    subprocess.run(
        ["git", "-C", str(repo_path), "add", filename], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", f"add {filename}"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "checkout", "main"],
        check=True,
        capture_output=True,
    )


def _make_state_with_integration(project_name: str, branch_name: str) -> ConductorState:
    """ConductorState with completed merge (integration branch set)."""
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "branch-a"),
        _done_run_with_branch(1, "feat-b", "branch-b"),
    )
    state.integration = IntegrationState(
        status=IntegrationStatus.DONE,
        branch=branch_name,
        merged_runs=[0, 1],
    )
    return state


# ---------------------------------------------------------------------------
# Test 1: Claude generates tests, npx executes them, results captured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_generation_and_execution(real_merge_repo, tmp_path):
    """Claude generates tests, npx executes them, results captured in E2ETestState."""
    if run_integration_testing is None:
        pytest.skip("conductor.integration.e2e not yet implemented")

    project_name = _project_name(tmp_path)
    integration_branch = f"integration/{project_name}"

    # Create integration branch with playwright config
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "checkout", "-b", integration_branch],
        check=True,
        capture_output=True,
    )
    _add_file_to_branch(
        real_merge_repo.path,
        integration_branch,
        "playwright.config.ts",
        "export default { testDir: './tests' }\n",
    )
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    state = _make_state_with_integration(project_name, integration_branch)
    storage = FakeStorage(real_merge_repo.path)

    # Mock process with communicate() returning 3 passed
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(
        return_value=(b"3 passed, 0 failed, 0 skipped", b"")
    )

    with patch(
        "conductor.integration.e2e.run_claude",
        new_callable=AsyncMock,
        return_value="// generated tests",
    ):
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            result = await run_integration_testing(state, storage)

    assert isinstance(result, E2ETestState)
    assert result.passed == 3
    assert result.failed == 0
    assert result.skipped == 0
    assert result.last_run_at is not None


# ---------------------------------------------------------------------------
# Test 2: No playwright/cypress config — testing skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_skipped_no_framework(real_merge_repo, tmp_path):
    """No test framework config on integration branch — testing skipped."""
    if run_integration_testing is None:
        pytest.skip("conductor.integration.e2e not yet implemented")

    project_name = _project_name(tmp_path)
    integration_branch = f"integration/{project_name}"

    # Create integration branch with NO test framework config
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "checkout", "-b", integration_branch],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    state = _make_state_with_integration(project_name, integration_branch)
    storage = FakeStorage(real_merge_repo.path)

    result = await run_integration_testing(state, storage)

    assert isinstance(result, E2ETestState)
    assert result.passed == 0
    assert result.failed == 0
    assert result.skipped >= 1


# ---------------------------------------------------------------------------
# Test 3: npx returns failures — function returns normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_failures_dont_block(real_merge_repo, tmp_path):
    """Test failures are reported but never raise an exception or block processing."""
    if run_integration_testing is None:
        pytest.skip("conductor.integration.e2e not yet implemented")

    project_name = _project_name(tmp_path)
    integration_branch = f"integration/{project_name}"

    # Create integration branch with playwright config
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "checkout", "-b", integration_branch],
        check=True,
        capture_output=True,
    )
    _add_file_to_branch(
        real_merge_repo.path,
        integration_branch,
        "playwright.config.ts",
        "export default { testDir: './tests' }\n",
    )
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    state = _make_state_with_integration(project_name, integration_branch)
    storage = FakeStorage(real_merge_repo.path)

    # Mock process returning exit code 1 with 1 passed, 2 failed
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(
        return_value=(b"1 passed, 2 failed, 0 skipped", b"")
    )

    with patch(
        "conductor.integration.e2e.run_claude",
        new_callable=AsyncMock,
        return_value="// generated tests",
    ):
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ):
            result = await run_integration_testing(state, storage)

    # Function must return normally — no exception raised
    assert isinstance(result, E2ETestState)
    assert result.passed == 1
    assert result.failed == 2
    assert result.last_run_at is not None
