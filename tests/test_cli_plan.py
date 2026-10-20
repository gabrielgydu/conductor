"""Tests for `conductor plan` (Claude mocked out)."""

from __future__ import annotations

import subprocess

import pytest

import conductor.cli as cli
from conductor.core.enums import IntegrationStatus, RunStatus
from conductor.core.models import IntegrationState
from tests.cli_support import (  # noqa: F401
    NAME,
    repo_fixture,
    run_main,
    saved_state,
    seed_state,
    write_brief,
)
from tests.helpers import make_run_state

PLAN = [
    {
        "name": "health-api",
        "description": "Health endpoint",
        "depends_on": [],
        "stages": [{"name": "backend", "spec_mode": "backend"}],
    }
]
DESCRIPTIONS = {"run-0-backend": "Build GET /health."}


@pytest.fixture
def claude_calls(monkeypatch):
    calls = []

    def fake_generate(prompt, brief, repo_path, run_claude):
        calls.append(prompt)
        return PLAN, DESCRIPTIONS

    monkeypatch.setattr(cli, "_generate_and_review_plan", fake_generate)
    return calls


def _plan(repo, *extra):
    return run_main(["plan", "--name", NAME, "--project-dir", str(repo), *extra])


def test_plan_keeps_the_base_branch_chosen_at_init(repo, claude_calls):
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature-x"], check=True)
    run_main(["init", "--name", NAME, "--project-dir", str(repo), "--base-branch", "develop"])
    write_brief(repo)

    _, _, code = _plan(repo)

    assert code == 0
    assert saved_state(repo).base_branch == "develop"


def test_replan_keeps_project_settings_and_resets_integration(repo, claude_calls):
    seed_state(
        repo,
        runs=[make_run_state(0, "old", status=RunStatus.DONE)],
        preset="myapp",
        overnight=False,
        quick=True,
        max_parallel=3,
        worktrees_base="/srv/wt",
        check_interval_s=30,
        integration=IntegrationState(branch="integration/proj", status=IntegrationStatus.NEEDS_FIX, pr_url="u"),
    )
    write_brief(repo)

    _, _, code = _plan(repo, "--force")

    assert code == 0
    state = saved_state(repo)
    assert [r.name for r in state.runs] == ["health-api"]
    assert state.integration is None
    assert (state.preset, state.overnight, state.quick) == ("myapp", False, True)
    assert (state.max_parallel, state.worktrees_base, state.check_interval_s) == (3, "/srv/wt", 30)
    assert state.base_branch == "main"


@pytest.mark.parametrize("started", ["status", "worktree", "integration"])
def test_replan_refuses_to_drop_started_runs_without_force(repo, claude_calls, started):
    run = make_run_state(0, "old")
    integration = None
    if started == "status":
        run.status = RunStatus.ACTIVE
    elif started == "worktree":
        run.stages[0].worktree = "/srv/wt/old-stage-0"
        run.stages[0].branch = "conductor/proj/old/stage-0"
    else:
        integration = IntegrationState(branch="integration/proj")
    seed_state(repo, runs=[run], integration=integration)
    write_brief(repo)

    out, _, code = _plan(repo)

    assert code == 1
    assert "--force" in out and "cleanup" in out
    assert claude_calls == []
    assert [r.name for r in saved_state(repo).runs] == ["old"]


def test_replan_of_an_unstarted_plan_needs_no_force(repo, claude_calls):
    seed_state(repo, runs=[make_run_state(0, "old")])
    write_brief(repo)

    _, _, code = _plan(repo)

    assert code == 0
    assert [r.name for r in saved_state(repo).runs] == ["health-api"]
