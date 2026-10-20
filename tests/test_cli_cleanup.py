"""Tests for `conductor cleanup` against real throwaway git repos (tmux faked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conductor.core.models import IntegrationState
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

    stages = [r.stages[0] for r in saved_state(repo).runs]
    assert [(s.worktree, s.branch) for s in stages] == [
        (None, branches["unpushed"]),
        (str(wts / "dirty"), branches["dirty"]),
        (None, None),
        (None, None),
    ]


def test_cleanup_force_deletes_unsaved_work_too(repo, project):
    branches, wts, _ = project

    out, _, code = _cleanup(repo, "--force")

    assert code == 0, out
    local = git(repo, "branch", "--format=%(refname:short)").split()
    assert not set(branches.values()) & set(local)
    assert not (wts / "dirty").exists()
    assert all(s.worktree is None and s.branch is None for r in saved_state(repo).runs for s in r.stages)
