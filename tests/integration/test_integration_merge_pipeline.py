"""Integration tests: merge pipeline scenarios (DAG ordering, conflicts, edge cases)."""
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
    return f"it{abs(hash(str(tmp_path))) % 999999:06d}"


def _make_state(project_name: str, *runs: RunState) -> ConductorState:
    return ConductorState(
        project_name=project_name,
        base_branch="main",
        runs=list(runs),
    )


# ---------------------------------------------------------------------------
# Test 1: Full clean merge — 3 branches, DAG ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_merge_pipeline(real_merge_repo, tmp_path):
    """3 non-conflicting branches merged; run 2 depends on run 0 (DAG ordering)."""
    real_merge_repo.create_branch("branch-a", {"file_a.txt": "content a\n"})
    real_merge_repo.create_branch("branch-b", {"file_b.txt": "content b\n"})
    real_merge_repo.create_branch("branch-c", {"file_c.txt": "content c\n"})

    project_name = _project_name(tmp_path)
    run0 = _done_run_with_branch(0, "feat-a", "branch-a")
    run1 = _done_run_with_branch(1, "feat-b", "branch-b")
    run2 = _done_run_with_branch(2, "feat-c", "branch-c")
    run2.depends_on = [0]

    state = _make_state(project_name, run0, run1, run2)
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
    assert 2 in result.merged_runs
    assert result.conflicts_resolved == []
    assert result.conflicts_unresolved == []

    # All 3 files present on integration branch
    branch_name = f"integration/{project_name}"
    ls = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "ls-tree", "--name-only", branch_name],
        capture_output=True,
        text=True,
    )
    files = ls.stdout.splitlines()
    assert "file_a.txt" in files
    assert "file_b.txt" in files
    assert "file_c.txt" in files

    # DAG ordering: run 0 merged before run 2 (since run 2 depends on run 0)
    idx_0 = result.merged_runs.index(0)
    idx_2 = result.merged_runs.index(2)
    assert idx_0 < idx_2, "Run 0 must be merged before run 2 (DAG dependency)"


# ---------------------------------------------------------------------------
# Test 2: Conflict — Claude resolves when -X theirs fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_with_conflicts(real_merge_repo, tmp_path):
    """Claude resolves conflicts when -X theirs strategy fails."""
    from conductor.integration import merge as merge_module

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

    original_run_git = merge_module._run_git

    async def patched_run_git(args: list[str], cwd: Path):
        if "merge" in args and "-X" in args and "theirs" in args:
            return (1, "", "simulated -X theirs failure")
        return await original_run_git(args, cwd)

    claude_calls: list[list[str]] = []

    async def mock_claude(conflicting_files: list[str], run_description: str, cwd: Path) -> bool:
        claude_calls.append(list(conflicting_files))
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

    assert result.status == IntegrationStatus.DONE
    assert 0 in result.merged_runs
    assert 1 in result.merged_runs
    assert len(result.conflicts_resolved) > 0
    assert len(claude_calls) >= 1, "Claude should have been invoked at least once"


# ---------------------------------------------------------------------------
# Test 3: Partial merge — Claude cannot resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_merge(real_merge_repo, tmp_path):
    """Status is PARTIAL when Claude fails to resolve conflicts."""
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

    real_merge_repo.create_branch("partial-a", {"shared.txt": "version from partial-a\n"})
    real_merge_repo.create_branch("partial-b", {"shared.txt": "version from partial-b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "partial-a"),
        _done_run_with_branch(1, "feat-b", "partial-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    original_run_git = merge_module._run_git

    async def patched_run_git(args: list[str], cwd: Path):
        if "merge" in args and "-X" in args and "theirs" in args:
            return (1, "", "simulated -X theirs failure")
        return await original_run_git(args, cwd)

    async def mock_claude_fail(conflicting_files: list[str], run_description: str, cwd: Path) -> bool:
        return False

    with patch.object(merge_module, "_run_git", side_effect=patched_run_git):
        with patch.object(merge_module, "resolve_conflicts_with_claude", side_effect=mock_claude_fail):
            with patch(
                "conductor.integration.merge._run_git_gh",
                new_callable=AsyncMock,
                return_value=(1, "", "no remote"),
            ):
                result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.PARTIAL
    assert len(result.conflicts_unresolved) > 0

    # Integration branch still exists (partial commit was made)
    branch_name = f"integration/{project_name}"
    branches = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "branch"],
        capture_output=True, text=True,
    ).stdout
    assert branch_name in branches


# ---------------------------------------------------------------------------
# Test 4: BLOCKED runs excluded from merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_skips_failed_runs(real_merge_repo, tmp_path):
    """BLOCKED runs are excluded from the integration merge."""
    real_merge_repo.create_branch("branch-a", {"file_a.txt": "content a\n"})
    real_merge_repo.create_branch("branch-b", {"file_b.txt": "content b\n"})
    real_merge_repo.create_branch("branch-blocked", {"blocked_file.txt": "blocked content\n"})

    project_name = _project_name(tmp_path)
    run_blocked = RunState(
        index=2,
        name="blocked",
        description="blocked desc",
        stages=[StageState(name="impl", spec_mode="full", branch="branch-blocked", status="done")],
        status=RunStatus.BLOCKED,
    )
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "branch-a"),
        _done_run_with_branch(1, "feat-b", "branch-b"),
        run_blocked,
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
    assert 2 not in result.merged_runs

    # blocked_file.txt must NOT be on integration branch
    branch_name = f"integration/{project_name}"
    ls = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "ls-tree", "--name-only", branch_name],
        capture_output=True, text=True,
    )
    files = ls.stdout.splitlines()
    assert "blocked_file.txt" not in files


# ---------------------------------------------------------------------------
# Test 5: Single DONE run — merge skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_skipped_single_run(real_merge_repo, tmp_path):
    """When only 1 DONE run exists, merge is skipped and merged_runs is empty."""
    real_merge_repo.create_branch("branch-a", {"file_a.txt": "content a\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "branch-a"),
    )
    storage = FakeStorage(real_merge_repo.path)

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert result.merged_runs == []
