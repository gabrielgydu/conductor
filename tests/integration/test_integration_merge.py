"""Integration tests: real git operations for merge pipeline."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))

from conductor.core.enums import IntegrationStatus, RunStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.integration.merge import run_integration_merge


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
    """Unique short project name safe for git branch names."""
    return f"it{abs(hash(str(tmp_path))) % 999999:06d}"


def _make_state(project_name: str, *runs: RunState) -> ConductorState:
    return ConductorState(
        project_name=project_name,
        base_branch="main",
        runs=list(runs),
    )


# ---------------------------------------------------------------------------
# Test 1: Full clean merge with real git
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_merge_real_git(real_merge_repo, tmp_path):
    """Two non-conflicting branches → both merged into integration branch."""
    real_merge_repo.create_branch("branch-a", {"file_a.txt": "content from a\n"})
    real_merge_repo.create_branch("branch-b", {"file_b.txt": "content from b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "branch-a"),
        _done_run_with_branch(1, "feat-b", "branch-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert 0 in result.merged_runs
    assert 1 in result.merged_runs

    # Both files present on integration branch
    branch_name = f"integration/{project_name}"
    ls = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "ls-tree", "--name-only", branch_name],
        capture_output=True,
        text=True,
    )
    files = ls.stdout.splitlines()
    assert "file_a.txt" in files
    assert "file_b.txt" in files


# ---------------------------------------------------------------------------
# Test 2: Conflict resolved with -X theirs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflict_theirs_real_git(real_merge_repo, tmp_path):
    """Both branches modify same line → -X theirs resolves conflict."""
    # Add shared.txt to main
    shared = real_merge_repo.path / "shared.txt"
    shared.write_text("original content\n")
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "add", "shared.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "commit", "-m", "add shared"],
        check=True, capture_output=True,
    )

    real_merge_repo.create_branch("conflict-a", {"shared.txt": "version from branch-a\n"})
    real_merge_repo.create_branch("conflict-b", {"shared.txt": "version from branch-b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "conflict-a"),
        _done_run_with_branch(1, "feat-b", "conflict-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert 0 in result.merged_runs
    assert 1 in result.merged_runs

    # "Theirs" (conflict-b) content should win
    branch_name = f"integration/{project_name}"
    show = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "show", f"{branch_name}:shared.txt"],
        capture_output=True,
        text=True,
    )
    assert "version from branch-b" in show.stdout


# ---------------------------------------------------------------------------
# Test 3: Claude invoked when -X theirs fails (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflict_markers_real_git(real_merge_repo, tmp_path):
    """When -X theirs fails, Claude is invoked and resolves conflict markers."""
    from conductor.integration import merge as merge_module

    shared = real_merge_repo.path / "shared.txt"
    shared.write_text("original content\n")
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "add", "shared.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "commit", "-m", "add shared"],
        check=True, capture_output=True,
    )
    real_merge_repo.create_branch("claude-a", {"shared.txt": "version from a\n"})
    real_merge_repo.create_branch("claude-b", {"shared.txt": "version from b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "claude-a"),
        _done_run_with_branch(1, "feat-b", "claude-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    original_run_git = merge_module._run_git

    async def patched_run_git(args: list[str], cwd: Path):
        # Force Claude path by failing -X theirs
        if "merge" in args and "-X" in args and "theirs" in args:
            return (1, "", "simulated -X theirs failure")
        return await original_run_git(args, cwd)

    claude_calls: list[list[str]] = []

    async def mock_claude(conflicting_files: list[str], run_description: str, cwd: Path) -> bool:
        claude_calls.append(conflicting_files)
        for f in conflicting_files:
            (cwd / f).write_text("claude resolved content\n")
        return True

    with patch.object(merge_module, "_run_git", side_effect=patched_run_git):
        with patch.object(merge_module, "resolve_conflicts_with_claude", side_effect=mock_claude):
            with patch(
                "conductor.integration.merge._run_git_gh",
                new_callable=AsyncMock,
                return_value=(1, "", "no remote"),
            ):
                result = await run_integration_merge(state, storage)

    assert len(claude_calls) >= 1, "Claude should have been invoked at least once"
    assert 1 in result.merged_runs
    assert len(result.conflicts_resolved) > 0


# ---------------------------------------------------------------------------
# Test 4: Worktree isolation — main repo HEAD unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_used_real_git(real_merge_repo, tmp_path):
    """Merge happens in worktree; main repo HEAD and active branch unchanged."""
    real_merge_repo.create_branch("wt-a", {"wt_file_a.txt": "wt content a\n"})
    real_merge_repo.create_branch("wt-b", {"wt_file_b.txt": "wt content b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "wt-a"),
        _done_run_with_branch(1, "feat-b", "wt-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    head_before = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    branch_before = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, storage)

    head_after = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    branch_after = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert head_before == head_after, "Main repo HEAD changed during integration merge"
    assert branch_before == branch_after, "Main repo branch changed during integration merge"
    assert result.status == IntegrationStatus.DONE


# ---------------------------------------------------------------------------
# Test 5: Rerun cleans previous branch and worktree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_cleans_previous_branch(real_merge_repo, tmp_path):
    """Second run deletes old integration branch/worktree and creates a fresh one."""
    real_merge_repo.create_branch("rerun-a", {"rerun_a.txt": "content a\n"})
    real_merge_repo.create_branch("rerun-b", {"rerun_b.txt": "content b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "rerun-a"),
        _done_run_with_branch(1, "feat-b", "rerun-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    mock_gh = AsyncMock(return_value=(1, "", "no remote"))

    # First run
    with patch("conductor.integration.merge._run_git_gh", mock_gh):
        result1 = await run_integration_merge(state, storage)

    assert result1.status == IntegrationStatus.DONE

    # Verify integration branch exists after first run
    branch_name = f"integration/{project_name}"
    branches = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "branch"],
        capture_output=True, text=True,
    ).stdout
    assert branch_name in branches

    # Second run with same state — should clean up and recreate
    with patch("conductor.integration.merge._run_git_gh", mock_gh):
        result2 = await run_integration_merge(state, storage)

    assert result2.status == IntegrationStatus.DONE
    assert 0 in result2.merged_runs
    assert 1 in result2.merged_runs

    # Integration branch still exists (freshly created)
    branches_after = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "branch"],
        capture_output=True, text=True,
    ).stdout
    assert branch_name in branches_after
