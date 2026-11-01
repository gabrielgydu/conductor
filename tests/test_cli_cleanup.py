"""Tests for `conductor cleanup` against real throwaway git repos (tmux faked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conductor.core.enums import RunStatus
from conductor.core.models import IntegrationState
from conductor.core.orchestrator import DependencyMergeError
from conductor.core.orchestrator import (
    create_worktree as real_create_worktree,  # unmocked
)
from conductor.core.storage import StorageResolver
from tests.cli_support import (  # noqa: F401
    NAME,
    fake_tmux_fixture,
    init_repo,
    repo_fixture,
    run_main,
    saved_state,
    seed_state,
)
from tests.helpers import make_run_state


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def commit(cwd: Path, message: str) -> None:
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message)


@pytest.fixture
def project(repo, tmp_path, fake_tmux):
    """Four stages: unpushed (clean worktree), dirty worktree, pushed, merged; plus an unrelated worktree."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    commit(repo, "init")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")

    wts = tmp_path / "wts"
    branches = {k: f"conductor/{NAME}/{k}/backend" for k in ("unpushed", "dirty", "pushed", "merged")}
    git(repo, "worktree", "add", "-q", "-b", branches["unpushed"], str(wts / "unpushed"))
    commit(wts / "unpushed", "generated work, never pushed")
    git(repo, "worktree", "add", "-q", "-b", branches["dirty"], str(wts / "dirty"))
    (wts / "dirty" / "wip.txt").write_text("uncommitted")
    git(repo, "branch", branches["pushed"])
    git(repo, "push", "-q", "origin", branches["pushed"])
    git(repo, "checkout", "-q", "-b", branches["merged"])
    commit(repo, "merged work")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", branches["merged"])
    # Someone else's worktree on an unmounted drive: not ours to prune.
    git(repo, "worktree", "add", "-q", "-b", "user/other", str(tmp_path / "other"))
    (tmp_path / "other").rename(tmp_path / "other-moved")

    runs = []
    for i, key in enumerate(branches):
        run = make_run_state(i, key)
        run.stages[0].branch = branches[key]
        if key in ("unpushed", "dirty"):
            run.stages[0].worktree = str(wts / key)
        runs.append(run)
    # Single-run path: the integration branch is the stage branch itself.
    seed_state(repo, runs=runs, integration=IntegrationState(branch=branches["unpushed"]))
    return branches, wts, tmp_path / "other"


def _cleanup(repo, *extra):
    return run_main(["cleanup", "--name", NAME, "--project-dir", str(repo), *extra])


def test_cleanup_keeps_unsaved_work_and_reports_what_it_did(repo, project):
    branches, wts, other = project

    out, _, code = _cleanup(repo)

    assert code == 1  # the dirty worktree could not be removed
    local = git(repo, "branch", "--format=%(refname:short)").split()
    assert branches["unpushed"] in local and branches["dirty"] in local
    assert branches["pushed"] not in local and branches["merged"] not in local
    assert not (wts / "unpushed").exists()
    assert (wts / "dirty" / "wip.txt").exists()
    assert "Removed 1 worktree(s), deleted 2 branch(es)." in out
    assert f"{branches['unpushed']} (1 commit(s)" in out
    assert f"worktree {wts / 'dirty'}" in out
    assert str(other) in git(repo, "worktree", "list")  # no repo-wide prune

    # Deleted branches keep their names: integration and dependent runs find
    # their commits on origin (or by the recorded tip) later.
    stages = [r.stages[0] for r in saved_state(repo).runs]
    assert [(s.worktree, s.branch) for s in stages] == [
        (None, branches["unpushed"]),
        (str(wts / "dirty"), branches["dirty"]),
        (None, branches["pushed"]),
        (None, branches["merged"]),
    ]


def test_cleanup_force_deletes_unsaved_work_too(repo, project):
    branches, wts, _ = project

    out, _, code = _cleanup(repo, "--force")

    assert code == 0, out
    local = git(repo, "branch", "--format=%(refname:short)").split()
    assert not set(branches.values()) & set(local)
    assert not (wts / "dirty").exists()
    stages = [s for r in saved_state(repo).runs for s in r.stages]
    assert [(s.worktree, s.branch) for s in stages] == [(None, b) for b in branches.values()]


@pytest.fixture
def dependency_on_origin(repo, tmp_path, fake_tmux):
    """Run 0 is DONE with its branch pushed; run 1 depends on it and has not started."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    commit(repo, "init")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    b0 = f"conductor/{NAME}/r0/backend"
    git(repo, "checkout", "-q", "-b", b0)
    commit(repo, "dependency work")
    dep = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "push", "-q", "origin", b0)
    git(repo, "checkout", "-q", "main")
    r0 = make_run_state(0, "r0", status=RunStatus.DONE)
    r0.stages[0].branch = b0
    r0.stages[0].status = "done"
    r1 = make_run_state(1, "r1", depends_on=[0])
    seed_state(repo, runs=[r0, r1], worktrees_base=str(tmp_path / "wts"))
    return b0, dep, remote


def test_dependent_run_still_builds_on_its_dependency_after_cleanup(repo, dependency_on_origin):
    """Cleanup deletes the pushed branch locally; the dependent run's
    worktree takes it from origin instead of starting from main without it."""
    _, dep, _ = dependency_on_origin
    assert _cleanup(repo)[2] == 0

    state = saved_state(repo)
    real_create_worktree(state, 1, 0, StorageResolver(repo))

    wt = state.runs[1].stages[0].worktree
    assert dep in git(Path(wt), "rev-list", "HEAD").split()


def test_dependent_run_blocks_when_its_dependency_is_gone(repo, dependency_on_origin):
    b0, _, _ = dependency_on_origin
    assert _cleanup(repo)[2] == 0
    git(repo, "push", "-q", "origin", "--delete", b0)

    state = saved_state(repo)
    with pytest.raises(DependencyMergeError, match="r0's work is gone"):
        real_create_worktree(state, 1, 0, StorageResolver(repo))
    assert state.runs[1].stages[0].worktree is None
