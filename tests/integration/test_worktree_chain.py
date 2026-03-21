"""Worktree chaining integration tests (WC-1 through WC-4)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import create_worktree


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def get_default_branch(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def create_branch_with_commit(repo_path: Path, branch: str, base: str) -> str:
    """Create a real git branch with a commit. Returns commit SHA."""
    subprocess.run(
        ["git", "checkout", "-b", branch, base],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    (repo_path / f"{branch.replace('/', '_')}.txt").write_text("work")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"work on {branch}"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    sha = result.stdout.strip()
    subprocess.run(
        ["git", "checkout", base],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    return sha


def get_branch_tip_sha(repo_path: Path, branch: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def assert_branch_derives_from(repo_path: Path, child_branch: str, parent_branch: str) -> None:
    parent_sha = get_branch_tip_sha(repo_path, parent_branch)
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", parent_sha, child_branch],
        cwd=repo_path,
    )
    assert result.returncode == 0, f"{child_branch} does not derive from {parent_branch}"


# ---------------------------------------------------------------------------
# WC-1: First stage branches from base
# ---------------------------------------------------------------------------


def test_first_stage_branches_from_base(tmp_git_repo: Path, tmp_path: Path) -> None:
    repo = tmp_git_repo
    base_branch = get_default_branch(repo)

    state = ConductorState(
        project_name="worktree-test",
        base_branch=base_branch,
        runs=[
            RunState(
                index=0,
                name="api-endpoints",
                description="API endpoints feature",
                stages=[StageState(name="spec", spec_mode="full")],
            )
        ],
    )

    worktrees_base = tmp_path / "worktrees"
    create_worktree(state, 0, 0, repo, worktrees_base)

    stage = state.runs[0].stages[0]
    assert stage.branch == "conductor/worktree-test/api-endpoints/spec"
    assert stage.worktree is not None
    assert Path(stage.worktree).exists()
    assert_branch_derives_from(repo, stage.branch, base_branch)


# ---------------------------------------------------------------------------
# WC-2: Subsequent stage branches from previous stage
# ---------------------------------------------------------------------------


def test_subsequent_stage_branches_from_previous(tmp_git_repo: Path, tmp_path: Path) -> None:
    repo = tmp_git_repo
    base_branch = get_default_branch(repo)

    stage1_branch = "conductor/chain-test/api-endpoints/spec"
    create_branch_with_commit(repo, stage1_branch, base_branch)

    state = ConductorState(
        project_name="chain-test",
        base_branch=base_branch,
        runs=[
            RunState(
                index=0,
                name="api-endpoints",
                description="API endpoints feature",
                stages=[
                    StageState(
                        name="spec",
                        spec_mode="full",
                        branch=stage1_branch,
                        status=StageStatus.DONE,
                    ),
                    StageState(name="impl", spec_mode="full"),
                ],
            )
        ],
    )

    worktrees_base = tmp_path / "worktrees"
    create_worktree(state, 0, 1, repo, worktrees_base)

    stage2 = state.runs[0].stages[1]
    assert stage2.branch == "conductor/chain-test/api-endpoints/impl"
    assert stage2.branch != stage1_branch
    assert_branch_derives_from(repo, stage2.branch, stage1_branch)


# ---------------------------------------------------------------------------
# WC-3: First stage of dependent run branches from dep's last stage
# ---------------------------------------------------------------------------


def test_dependent_run_branches_from_dep(tmp_git_repo: Path, tmp_path: Path) -> None:
    repo = tmp_git_repo
    base_branch = get_default_branch(repo)

    dep_last_branch = "conductor/chain-test/data-models/impl"
    create_branch_with_commit(repo, dep_last_branch, base_branch)

    state = ConductorState(
        project_name="chain-test",
        base_branch=base_branch,
        runs=[
            RunState(
                index=0,
                name="data-models",
                description="Data models",
                status=RunStatus.DONE,
                stages=[
                    StageState(
                        name="spec",
                        spec_mode="full",
                        branch="conductor/chain-test/data-models/spec",
                    ),
                    StageState(
                        name="impl",
                        spec_mode="full",
                        branch=dep_last_branch,
                    ),
                ],
            ),
            RunState(
                index=1,
                name="api-endpoints",
                description="API endpoints feature",
                depends_on=[0],
                stages=[
                    StageState(name="spec", spec_mode="full"),
                ],
            ),
        ],
    )

    worktrees_base = tmp_path / "worktrees"
    create_worktree(state, 1, 0, repo, worktrees_base)

    stage = state.runs[1].stages[0]
    assert stage.branch == "conductor/chain-test/api-endpoints/spec"
    assert_branch_derives_from(repo, stage.branch, dep_last_branch)


# ---------------------------------------------------------------------------
# WC-4: Branch naming convention
# ---------------------------------------------------------------------------


def test_branch_naming_convention(tmp_git_repo: Path, tmp_path: Path) -> None:
    repo = tmp_git_repo
    base_branch = get_default_branch(repo)

    state = ConductorState(
        project_name="my-project",
        base_branch=base_branch,
        runs=[
            RunState(
                index=0,
                name="api-endpoints",
                description="API endpoints feature",
                stages=[
                    StageState(name="spec", spec_mode="full"),
                    StageState(name="impl", spec_mode="full"),
                ],
            )
        ],
    )

    worktrees_base = tmp_path / "worktrees"
    create_worktree(state, 0, 0, repo, worktrees_base)
    assert state.runs[0].stages[0].branch == "conductor/my-project/api-endpoints/spec"

    create_worktree(state, 0, 1, repo, worktrees_base)
    assert state.runs[0].stages[1].branch == "conductor/my-project/api-endpoints/impl"
