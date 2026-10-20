"""Integration tests: E2E testing pipeline (conductor.integration.e2e)."""

from __future__ import annotations

import asyncio
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
    ConflictRecord,
    E2ETestState,
    IntegrationState,
    RunState,
    StageState,
)
from conductor.integration.e2e import (
    generate_integration_tests,
    run_integration_specs,
    run_integration_testing,
)


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


def _make_state(project_name: str) -> ConductorState:
    return ConductorState(
        project_name=project_name,
        base_branch="main",
        runs=[
            _done_run_with_branch(0, "feat-a", "branch-a"),
            _done_run_with_branch(1, "feat-b", "branch-b"),
        ],
    )


_SPEC = "tests/Playwright/tests/conductor-integration.spec.ts"


def _integration_worktree(repo: Path, project_name: str, playwright: bool = True) -> Path:
    """Check out integration/<project> in *repo* and use it as the worktree.

    With *playwright*, give it the setup the specs need: a root config and an
    installed @playwright/test.
    """
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", f"integration/{project_name}"],
        check=True,
        capture_output=True,
    )
    if playwright:
        (repo / "playwright.config.ts").write_text("export default {};\n")
        (repo / "node_modules" / "@playwright" / "test").mkdir(parents=True)
    return repo


def _write_spec(worktree: Path) -> None:
    spec = worktree / _SPEC
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("// generated tests\n")


def _claude_writes_spec(worktree: Path) -> AsyncMock:
    """run_claude stand-in: 'generates' the integration spec in the worktree."""

    async def generate(prompt, **kwargs):
        assert kwargs["cwd"] == str(worktree)
        _write_spec(worktree)
        return "// generated tests"

    return AsyncMock(side_effect=generate)


_real_exec = asyncio.create_subprocess_exec


def _fake_npx(test_output: str = "", test_rc: int = 0) -> AsyncMock:
    """create_subprocess_exec stand-in for `npx --no playwright test <spec>`.

    git still runs for real; npx never does (it could install Playwright from
    the network).
    """

    async def spawn(*args, **kwargs):
        if args[0] == "git":
            return await _real_exec(*args, **kwargs)
        assert args[:4] == ("npx", "--no", "playwright", "test"), args
        p = MagicMock()
        p.returncode = test_rc
        p.communicate = AsyncMock(return_value=(test_output.encode(), b""))
        return p

    return AsyncMock(side_effect=spawn)


def _npx_calls(npx: AsyncMock) -> list[tuple]:
    return [c.args for c in npx.await_args_list if c.args[0] == "npx"]


def _last_commit(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# ---------------------------------------------------------------------------
# Generation: once, from the merge's own conflict records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_writes_and_commits_specs(real_merge_repo, tmp_path):
    project_name = _project_name(tmp_path)
    worktree = _integration_worktree(real_merge_repo.path, project_name)
    state = _make_state(project_name)  # state.integration is None mid-merge
    conflicts = [ConflictRecord(file="src/shared.ts", feature_a="feat-a",
                                feature_b="feat-b", description="Resolved by Claude AI")]

    claude = _claude_writes_spec(worktree)
    with patch("conductor.integration.e2e.run_claude", new=claude):
        generated = await generate_integration_tests(
            state, worktree, state.runs[:1], conflicts
        )

    assert generated is True
    claude.assert_awaited_once()
    prompt = claude.await_args.args[0]
    assert "src/shared.ts: conflict between feat-a and feat-b" in prompt
    assert "## Run 0: feat-a" in prompt
    assert "feat-b desc" not in prompt  # only the runs that were merged
    assert _last_commit(worktree).split() == [
        "Add", "conductor", "integration", "tests", _SPEC
    ]


@pytest.mark.asyncio
async def test_generation_skipped_without_playwright_setup(real_merge_repo, tmp_path):
    """No usable Playwright in the worktree: no Opus session for specs nothing can run."""
    project_name = _project_name(tmp_path)
    worktree = _integration_worktree(real_merge_repo.path, project_name, playwright=False)

    claude = _claude_writes_spec(worktree)
    with patch("conductor.integration.e2e.run_claude", new=claude):
        state = _make_state(project_name)
        generated = await generate_integration_tests(state, worktree, state.runs, [])

    assert generated is False
    claude.assert_not_awaited()


# ---------------------------------------------------------------------------
# Execution: existing specs only, exit code counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specs_are_run_and_counted(real_merge_repo, tmp_path):
    worktree = _integration_worktree(real_merge_repo.path, _project_name(tmp_path))
    _write_spec(worktree)

    npx = _fake_npx("3 passed, 0 failed, 0 skipped")
    with patch("conductor.integration.e2e.run_claude", new=AsyncMock()) as claude:
        with patch("asyncio.create_subprocess_exec", new=npx):
            result, output = await run_integration_specs(worktree)

    assert (result.passed, result.failed, result.skipped) == (3, 0, 0)
    assert result.last_run_at is not None
    assert output.startswith("passed=3, failed=0, skipped=0")
    assert _npx_calls(npx) == [("npx", "--no", "playwright", "test", _SPEC)]
    claude.assert_not_awaited()  # running never regenerates


@pytest.mark.parametrize(
    "runner_output",
    [
        "Error: No tests found",
        "Error: Timed out waiting 120000ms from config.webServer.",
        "SyntaxError: Unexpected token (3:4)",
    ],
)
@pytest.mark.asyncio
async def test_failing_run_without_failed_tests_counts_as_failure(
    real_merge_repo, tmp_path, runner_output
):
    worktree = _integration_worktree(real_merge_repo.path, _project_name(tmp_path))
    _write_spec(worktree)

    with patch("asyncio.create_subprocess_exec", new=_fake_npx(runner_output, test_rc=1)):
        result, output = await run_integration_specs(worktree)

    assert result.failed == 1
    assert runner_output in output


@pytest.mark.asyncio
async def test_no_specs_is_a_visible_skip(real_merge_repo, tmp_path):
    worktree = _integration_worktree(real_merge_repo.path, _project_name(tmp_path))

    npx = _fake_npx()
    with patch("asyncio.create_subprocess_exec", new=npx):
        result, output = await run_integration_specs(worktree)

    assert (result.failed, result.skipped) == (0, 1)
    assert output == "skipped: no conductor integration specs"
    assert _npx_calls(npx) == []


# ---------------------------------------------------------------------------
# run_integration_testing: generate + run in one call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_failures_dont_block(real_merge_repo, tmp_path):
    """Test failures are reported but never raise an exception or block processing."""
    project_name = _project_name(tmp_path)
    worktree = _integration_worktree(real_merge_repo.path, project_name)
    state = _make_state(project_name)
    state.integration = IntegrationState(
        status=IntegrationStatus.DONE,
        branch=f"integration/{project_name}",
        merged_runs=[0, 1],
    )

    claude = _claude_writes_spec(worktree)
    npx = _fake_npx("1 passed, 2 failed, 0 skipped", test_rc=1)
    with patch("conductor.integration.e2e.run_claude", new=claude):
        with patch("asyncio.create_subprocess_exec", new=npx):
            result = await run_integration_testing(state, FakeStorage(worktree), worktree)

    assert isinstance(result, E2ETestState)
    assert result.passed == 1
    assert result.failed == 2
    assert result.last_run_at is not None
