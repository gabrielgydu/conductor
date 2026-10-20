"""Integration tests: E2E testing pipeline (conductor.integration.e2e.run_integration_testing)."""

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


_SPEC = "tests/Playwright/tests/conductor-integration.spec.ts"


def _integration_worktree(repo: Path, project_name: str) -> Path:
    """Check out integration/<project> in *repo* and use it as the worktree."""
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", f"integration/{project_name}"],
        check=True,
        capture_output=True,
    )
    return repo


def _claude_writes_spec(worktree: Path) -> AsyncMock:
    """run_claude stand-in: 'generates' the integration spec in the worktree."""

    async def generate(prompt, **kwargs):
        assert kwargs["cwd"] == str(worktree)
        spec = worktree / _SPEC
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("// generated tests\n")
        return "// generated tests"

    return AsyncMock(side_effect=generate)


def _fake_npx(test_output: str = "", test_rc: int = 0, playwright_rc: int = 0) -> AsyncMock:
    """create_subprocess_exec stand-in for `npx playwright --version` / `test`.

    Never lets the real npx run: it would install Playwright from the network.
    """

    def proc(rc: int, stdout: str) -> MagicMock:
        p = MagicMock()
        p.returncode = rc
        p.communicate = AsyncMock(return_value=(stdout.encode(), b""))
        return p

    async def spawn(*args, **kwargs):
        assert args[:2] == ("npx", "playwright"), args
        if "--version" in args:
            return proc(playwright_rc, "Version 1.50.0" if playwright_rc == 0 else "")
        return proc(test_rc, test_output)

    return AsyncMock(side_effect=spawn)


# ---------------------------------------------------------------------------
# Test 1: Claude generates tests, npx executes them, results captured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_generation_and_execution(real_merge_repo, tmp_path):
    """Claude generates tests, npx executes them, results captured in E2ETestState."""
    if run_integration_testing is None:
        pytest.skip("conductor.integration.e2e not yet implemented")

    project_name = _project_name(tmp_path)
    worktree = _integration_worktree(real_merge_repo.path, project_name)
    state = _make_state_with_integration(project_name, f"integration/{project_name}")
    storage = FakeStorage(real_merge_repo.path)

    claude = _claude_writes_spec(worktree)
    npx = _fake_npx("3 passed, 0 failed, 0 skipped")
    with patch("conductor.integration.e2e.run_claude", new=claude):
        with patch("asyncio.create_subprocess_exec", new=npx):
            result = await run_integration_testing(state, storage, worktree)

    assert isinstance(result, E2ETestState)
    assert result.passed == 3
    assert result.failed == 0
    assert result.skipped == 0
    assert result.last_run_at is not None
    claude.assert_awaited_once()
    assert ("npx", "playwright", "test", _SPEC) in [c.args for c in npx.await_args_list]


# ---------------------------------------------------------------------------
# Test 2: No Playwright in the worktree — testing skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_skipped_no_framework(real_merge_repo, tmp_path):
    """No usable Playwright in the integration worktree — testing skipped, no Claude call."""
    if run_integration_testing is None:
        pytest.skip("conductor.integration.e2e not yet implemented")

    project_name = _project_name(tmp_path)
    worktree = _integration_worktree(real_merge_repo.path, project_name)
    state = _make_state_with_integration(project_name, f"integration/{project_name}")
    storage = FakeStorage(real_merge_repo.path)

    claude = _claude_writes_spec(worktree)
    npx = _fake_npx(playwright_rc=1)
    with patch("conductor.integration.e2e.run_claude", new=claude):
        with patch("asyncio.create_subprocess_exec", new=npx):
            result = await run_integration_testing(state, storage, worktree)

    assert isinstance(result, E2ETestState)
    assert result.passed == 0
    assert result.failed == 0
    assert result.skipped >= 1
    claude.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 3: npx returns failures — function returns normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_failures_dont_block(real_merge_repo, tmp_path):
    """Test failures are reported but never raise an exception or block processing."""
    if run_integration_testing is None:
        pytest.skip("conductor.integration.e2e not yet implemented")

    project_name = _project_name(tmp_path)
    worktree = _integration_worktree(real_merge_repo.path, project_name)
    state = _make_state_with_integration(project_name, f"integration/{project_name}")
    storage = FakeStorage(real_merge_repo.path)

    # Playwright is available, but the generated tests fail: 1 passed, 2 failed
    claude = _claude_writes_spec(worktree)
    npx = _fake_npx("1 passed, 2 failed, 0 skipped", test_rc=1)
    with patch("conductor.integration.e2e.run_claude", new=claude):
        with patch("asyncio.create_subprocess_exec", new=npx):
            result = await run_integration_testing(state, storage, worktree)

    # Function must return normally — no exception raised
    assert isinstance(result, E2ETestState)
    assert result.passed == 1
    assert result.failed == 2
    assert result.last_run_at is not None
