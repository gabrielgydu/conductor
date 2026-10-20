"""Worktree chaining integration tests (WC-1 through WC-4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.orchestrator import DependencyMergeError, create_worktree


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


def assert_branch_derives_from(
    repo_path: Path, child_branch: str, parent_branch: str
) -> None:
    parent_sha = get_branch_tip_sha(repo_path, parent_branch)
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", parent_sha, child_branch],
        cwd=repo_path,
    )
    assert result.returncode == 0, (
        f"{child_branch} does not derive from {parent_branch}"
    )


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


def test_subsequent_stage_branches_from_previous(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
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


# ---------------------------------------------------------------------------
# WC-5..7: Resume when git still has worktree registrations conductor lost
# ---------------------------------------------------------------------------


def _one_stage_state(project: str, base_branch: str) -> ConductorState:
    return ConductorState(
        project_name=project,
        base_branch=base_branch,
        runs=[
            RunState(
                index=0,
                name="auth",
                description="Auth",
                stages=[StageState(name="main", spec_mode="full")],
            )
        ],
    )


def test_recreates_worktree_whose_directory_was_deleted(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """Dir gone (rm, tmpfs reboot) but still registered: recreate, don't raise."""
    import shutil

    repo = tmp_git_repo
    worktrees_base = tmp_path / "worktrees"
    state = _one_stage_state("proj", get_default_branch(repo))
    create_worktree(state, 0, 0, repo, worktrees_base)
    wt = Path(state.runs[0].stages[0].worktree)
    shutil.rmtree(wt)

    state.runs[0].stages[0].worktree = None
    create_worktree(state, 0, 0, repo, worktrees_base)

    assert (wt / ".git").exists()
    assert state.runs[0].stages[0].worktree == str(wt)


def test_two_projects_with_same_run_names_get_separate_worktrees(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    repo = tmp_git_repo
    worktrees_base = tmp_path / "worktrees"
    base = get_default_branch(repo)
    v1 = _one_stage_state("proj-v1", base)
    v2 = _one_stage_state("proj-v2", base)

    create_worktree(v1, 0, 0, repo, worktrees_base)
    create_worktree(v2, 0, 0, repo, worktrees_base)

    assert v1.runs[0].stages[0].worktree != v2.runs[0].stages[0].worktree
    assert v2.runs[0].stages[0].branch == "conductor/proj-v2/auth/main"


def test_reuses_worktree_after_worktrees_base_changed(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """The stage's branch is checked out under the old base: reuse that worktree."""
    repo = tmp_git_repo
    state = _one_stage_state("proj", get_default_branch(repo))
    create_worktree(state, 0, 0, repo, tmp_path / "old-base")
    old_wt = state.runs[0].stages[0].worktree

    state.runs[0].stages[0].worktree = None
    create_worktree(state, 0, 0, repo, tmp_path / "new-base")

    assert state.runs[0].stages[0].worktree == old_wt


def test_branch_checked_out_in_the_main_checkout_is_not_adopted(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """Never run a stage in the user's own checkout, even on the stage's branch."""
    repo = tmp_git_repo
    state = _one_stage_state("proj", get_default_branch(repo))
    subprocess.run(
        ["git", "checkout", "-q", "-b", "conductor/proj/auth/main"], cwd=repo, check=True
    )

    with pytest.raises(RuntimeError, match="the repository's own checkout"):
        create_worktree(state, 0, 0, repo, tmp_path / "worktrees")

    assert state.runs[0].stages[0].worktree is None


# ---------------------------------------------------------------------------
# Runs with several dependencies
# ---------------------------------------------------------------------------


def _commit_on(repo: Path, branch: str, base: str, files: dict[str, str]) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", branch, base], cwd=repo, check=True)
    for name, text in files.items():
        (repo / name).write_text(text)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"work on {branch}"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)


def _diamond(repo: Path, a_files: dict[str, str], b_files: dict[str, str]) -> ConductorState:
    """Runs A and B are DONE; run D depends on both."""
    base = get_default_branch(repo)
    _commit_on(repo, "conductor/p/a/impl", base, a_files)
    _commit_on(repo, "conductor/p/b/impl", base, b_files)

    def done(index: int, name: str) -> RunState:
        return RunState(
            index=index, name=name, description=name, status=RunStatus.DONE,
            stages=[StageState(name="impl", spec_mode="full", branch=f"conductor/p/{name}/impl")],
        )

    return ConductorState(
        project_name="p",
        base_branch=base,
        runs=[
            done(0, "a"),
            done(1, "b"),
            RunState(index=2, name="d", description="d", depends_on=[0, 1],
                     stages=[StageState(name="spec", spec_mode="full")]),
        ],
    )


def _head(wt: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_run_with_two_dependencies_sees_both(tmp_git_repo: Path, tmp_path: Path) -> None:
    state = _diamond(tmp_git_repo, {"a.txt": "a"}, {"b.txt": "b"})

    create_worktree(state, 2, 0, tmp_git_repo, tmp_path / "worktrees")

    wt = Path(state.runs[2].stages[0].worktree)
    assert (wt / "a.txt").read_text() == "a" and (wt / "b.txt").read_text() == "b"
    head = _head(wt)

    # Re-entering the stage (resume, restart) merges nothing new.
    create_worktree(state, 2, 0, tmp_git_repo, tmp_path / "worktrees")
    assert _head(wt) == head


def test_resume_redoes_a_merge_left_half_done(tmp_git_repo: Path, tmp_path: Path) -> None:
    state = _diamond(tmp_git_repo, {"a.txt": "a"}, {"b.txt": "b"})
    create_worktree(state, 2, 0, tmp_git_repo, tmp_path / "worktrees")
    wt = Path(state.runs[2].stages[0].worktree)
    # Simulate a crash in the middle of the merge of a.
    subprocess.run(["git", "reset", "-q", "--hard", "conductor/p/b/impl"], cwd=wt, check=True)
    subprocess.run(["git", "merge", "--no-commit", "-q", "conductor/p/a/impl"], cwd=wt, check=True)

    create_worktree(state, 2, 0, tmp_git_repo, tmp_path / "worktrees")

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout
    assert status == ""
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "conductor/p/a/impl", "HEAD"], cwd=wt
    )
    assert merged.returncode == 0


def test_conflicting_dependencies_raise_and_leave_a_clean_worktree(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    state = _diamond(tmp_git_repo, {"README.md": "from a\n"}, {"README.md": "from b\n"})

    with pytest.raises(DependencyMergeError, match="README.md"):
        create_worktree(state, 2, 0, tmp_git_repo, tmp_path / "worktrees")

    stage = state.runs[2].stages[0]
    wt = Path(stage.worktree)
    assert stage.branch == "conductor/p/d/spec"
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout == ""
