"""Re-integration across sessions against real git with a bare origin.

The orchestrator's _integration_due/_integrate and merge.py run for real;
Claude, validation and integration-test generation are stubbed
(hermetic_merge) and gh is a script that keeps one PR per head branch.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conductor import cli
from conductor.core import orchestrator as orch
from conductor.core.brain import BrainReply
from conductor.core.claude import ClaudeResult
from conductor.core.enums import IntegrationFailure, IntegrationStatus, RunStatus
from conductor.core.models import ConductorState, IntegrationState, RunState, StageState
from conductor.core.storage import StorageResolver
from conductor.integration import merge

pytestmark = pytest.mark.usefixtures("hermetic_merge")

_FAKE_GH = '''#!{python}
import json, pathlib, sys
state = pathlib.Path({state!r})
args = sys.argv[1:]
with (state / "calls").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if (state / "fail").exists():
    sys.exit("gh: To get started with GitHub CLI, please run:  gh auth login")
head = args[args.index("--head") + 1]
pr = state / ("pr-" + head.replace("/", "_"))
if args[:2] == ["pr", "list"]:
    print(json.dumps([{{"number": 1, "url": pr.read_text()}}] if pr.exists() else []))
elif args[:2] == ["pr", "create"]:
    if pr.exists():
        sys.exit("a pull request for branch already exists:\\n" + pr.read_text())
    n = len(list(state.glob("pr-*"))) + 1
    pr.write_text(f"https://github.com/o/r/pull/{{n}}")
    print(pr.read_text())
'''


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A gh on PATH that opens one PR per head branch; `gh.fail(True)` logs it out."""
    state = tmp_path / "gh-state"
    state.mkdir()
    bindir = tmp_path / "gh-bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(_FAKE_GH.format(python=sys.executable, state=str(state)))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def calls(command: str) -> int:
        log = state / "calls"
        lines = log.read_text().splitlines() if log.exists() else []
        return sum(json.loads(line)[:2] == ["pr", command] for line in lines)

    def fail(on: bool) -> None:
        (state / "fail").touch() if on else (state / "fail").unlink(missing_ok=True)

    return SimpleNamespace(calls=calls, fail=fail)


@pytest.fixture
def env(real_merge_repo, gh, monkeypatch):
    monkeypatch.setattr(cli, "_kill_stale_tmux_session", lambda name: False)
    return SimpleNamespace(
        repo=real_merge_repo.path,
        origin=real_merge_repo.origin,
        create_branch=real_merge_repo.create_branch,
        storage=StorageResolver(real_merge_repo.path),
        gh=gh,
    )


def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    assert not check or r.returncode == 0, r.stderr
    return r.stdout.strip()


def refs_containing(repo: Path, sha: str) -> list[str]:
    return git(repo, "for-each-ref", "--contains", sha, "--format=%(refname)", check=False).split()


def tip(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "-q", ref, check=False)


def contains(repo: Path, ref: str, sha: str) -> bool:
    is_ancestor = ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, ref]
    return subprocess.run(is_ancestor, check=False).returncode == 0


def run(index: int, name: str, branch: str, status: RunStatus = RunStatus.DONE) -> RunState:
    return RunState(
        index=index, name=name, description=name, status=status, current_stage=1,
        stages=[StageState(name="s", spec_mode="feature", branch=branch, status="done")],
    )


async def session(state: ConductorState, storage, *, force: bool = False) -> tuple | None:
    """What one `conductor run` does once all runs are terminal."""
    due = orch._integration_due(state, force=force)
    if due:
        await orch._integrate(state, *due, storage, None, None)
    return due


def three_runs(env, project: str) -> ConductorState:
    for branch, file in (("feat-a", "a.txt"), ("feat-b", "b.txt"), ("feat-c", "c.txt")):
        env.create_branch(branch, {file: f"{branch}\n"})
    return ConductorState(
        project_name=project,
        runs=[run(0, "a", "feat-a"), run(1, "b", "feat-b"), run(2, "c", "feat-c", RunStatus.BLOCKED)],
    )


# ---------------------------------------------------------------------------
# Legacy states (SC-1)
# ---------------------------------------------------------------------------


async def test_legacy_integration_with_branches_only_on_origin_gets_the_new_run_on_top(env):
    """The owner's layout: stage and integration branches exist only on
    origin, and the saved integration (merged [0, 1]) predates
    considered_runs. Run 2 finished since."""
    state = three_runs(env, "etm")
    state.runs[2].status = RunStatus.DONE
    git(env.repo, "checkout", "-q", "-b", "integration/etm", "main")
    for b in ("feat-a", "feat-b"):
        git(env.repo, "merge", "-q", "--no-ff", "--no-edit", b)
    git(env.repo, "checkout", "-q", "main")
    git(env.repo, "push", "-q", "origin", "feat-a", "feat-b", "feat-c", "integration/etm")
    old_tip = tip(env.repo, "integration/etm")
    git(env.repo, "branch", "-q", "-D", "feat-a", "feat-b", "feat-c", "integration/etm")
    env.gh.fail(False)
    state.integration = IntegrationState.model_validate(
        {"status": "needs_fix", "branch": "integration/etm", "merged_runs": [0, 1]}
    )

    due = await session(state, env.storage)

    assert due == ([0, 1, 2], "finished since the last integration: c")
    assert contains(env.origin, "integration/etm", old_tip)
    assert contains(env.origin, "integration/etm", tip(env.origin, "feat-c"))
    assert state.integration.merged_runs == [0, 1, 2]
    assert state.integration.status == IntegrationStatus.DONE
    for b in ("feat-a", "feat-b", "feat-c"):
        assert tip(env.origin, b)


async def test_forced_reintegration_of_a_legacy_needs_fix_merges_nothing_and_stays_needs_fix(env):
    """Nothing new to merge or validate: a legacy NEEDS_FIX (validation may
    have failed) does not turn DONE just because it was attempted again."""
    env.create_branch("feat-a", {"a.txt": "a\n"})
    env.create_branch("feat-b", {"b.txt": "b\n"})
    git(env.repo, "checkout", "-q", "-b", "integration/ln", "main")
    for b in ("feat-a", "feat-b"):
        git(env.repo, "merge", "-q", "--no-ff", "--no-edit", b)
    git(env.repo, "checkout", "-q", "main")
    git(env.repo, "push", "-q", "origin", "integration/ln")
    old_tip = tip(env.repo, "integration/ln")
    state = ConductorState(project_name="ln", runs=[run(0, "a", "feat-a"), run(1, "b", "feat-b")])
    state.integration = IntegrationState.model_validate({
        "status": "needs_fix", "branch": "integration/ln", "merged_runs": [0, 1],
        "pr_url": "https://github.com/o/r/pull/9",
    })
    assert orch._integration_due(state) is None

    await session(state, env.storage, force=True)

    assert state.integration.status == IntegrationStatus.NEEDS_FIX
    assert state.integration.pr_url == "https://github.com/o/r/pull/9"
    assert tip(env.origin, "integration/ln") == old_tip
    assert env.gh.calls("create") == 0


async def test_legacy_failed_integration_loses_no_commit_and_is_not_retried(env):
    """decision-log: stage branches gone everywhere, origin's integration
    branch holds commits no other ref has, saved as failed with merged []."""
    git(env.repo, "checkout", "-q", "-b", "integration/dl", "main")
    for i in range(3):
        (env.repo / f"f{i}.txt").write_text(f"{i}\n")
        git(env.repo, "add", f"f{i}.txt")
        git(env.repo, "commit", "-q", "-m", f"integrated work {i}")
    git(env.repo, "checkout", "-q", "main")
    git(env.repo, "push", "-q", "origin", "integration/dl")
    old_tip = tip(env.repo, "integration/dl")
    git(env.repo, "branch", "-q", "-D", "integration/dl")
    state = ConductorState(project_name="dl", runs=[run(0, "a", "gone-a"), run(1, "b", "gone-b")])
    state.integration = IntegrationState.model_validate(
        {"status": "failed", "branch": "integration/dl", "merged_runs": []}
    )

    due = await session(state, env.storage)

    assert due is not None
    assert tip(env.origin, "integration/dl") == old_tip
    assert state.integration.failure == IntegrationFailure.NOTHING_MERGEABLE
    assert orch._integration_due(state) is None


# ---------------------------------------------------------------------------
# Re-integration after cleanup, user commits, a checked-out branch
# ---------------------------------------------------------------------------


async def test_cleanup_then_reintegration_keeps_the_earlier_runs(env):
    """SC-2 / SEAM-IO-1: cleanup deletes a's and b's branches (they are on
    the integration branch); c's later integration builds on that branch."""
    state = three_runs(env, "pc")
    a_sha = tip(env.repo, "feat-a")
    await session(state, env.storage)
    first = state.integration
    assert first.status == IntegrationStatus.DONE, first
    orch.atomic_save(state, env.storage.conductor_state("pc"))

    cli._cmd_cleanup(argparse.Namespace(name="pc", project_dir=str(env.repo), force=False))
    assert not tip(env.repo, "refs/heads/feat-a")
    state = ConductorState.model_validate_json(env.storage.conductor_state("pc").read_text())
    state.runs[2].status = RunStatus.DONE
    await session(state, env.storage)

    integration = state.integration
    assert integration.status == IntegrationStatus.DONE, integration
    assert integration.merged_runs == [0, 1, 2]
    assert integration.pr_url == first.pr_url
    for repo in (env.repo, env.origin):
        assert "refs/heads/integration/pc" in refs_containing(repo, a_sha)
    assert env.gh.calls("create") == 1


async def test_commit_pushed_to_the_integration_branch_survives_reintegration(env, tmp_path):
    """SEAM-IO-2: a reviewer's fix on the PR branch stays, and so does the PR."""
    state = three_runs(env, "pr")
    await session(state, env.storage)
    first = state.integration
    user = tmp_path / "user"
    git(tmp_path, "clone", "-q", "-b", "integration/pr", str(env.origin), str(user))
    git(user, "commit", "-q", "--allow-empty", "-m", "review fix")
    git(user, "push", "-q", "origin", "integration/pr")
    fix = tip(user, "HEAD")

    state.runs[2].status = RunStatus.DONE
    await session(state, env.storage)

    for sha in (fix, tip(env.repo, "feat-c")):
        assert contains(env.origin, "integration/pr", sha)
    assert state.integration.status == IntegrationStatus.DONE
    assert state.integration.pr_url == first.pr_url
    assert env.gh.calls("create") == 1


async def test_checked_out_integration_branch_stops_cleanly_and_keeps_the_pr(env):
    """SEAM-IO-3: nothing is deleted or moved, the earlier integration stays
    in state, and the attempt is retried a bounded number of times."""
    state = three_runs(env, "pk")
    await session(state, env.storage)
    first = state.integration
    git(env.repo, "checkout", "-q", "integration/pk")  # the user tries the PR
    before = (tip(env.repo, "integration/pk"), tip(env.origin, "integration/pk"))
    state.runs[2].status = RunStatus.DONE

    await session(state, env.storage)

    integration = state.integration
    assert (tip(env.repo, "integration/pk"), tip(env.origin, "integration/pk")) == before
    assert integration.failure == IntegrationFailure.SETUP
    assert "checked out in" in integration.failure_detail
    assert (integration.pr_url, integration.merged_runs, integration.considered_runs) == (
        first.pr_url, [0, 1], [0, 1]
    )
    # Retried in two more sessions, then left to --reintegrate.
    assert await session(state, env.storage) is not None
    assert await session(state, env.storage) is not None
    assert state.integration.failed_attempts == orch.MAX_INTEGRATION_RETRIES
    assert orch._integration_due(state) is None
    assert "--reintegrate" in orch._integration_retry_note(state.integration)

    git(env.repo, "checkout", "-q", "main")
    await session(state, env.storage, force=True)
    assert state.integration.status == IntegrationStatus.DONE
    assert state.integration.merged_runs == [0, 1, 2]
    assert state.integration.failure is None


async def test_diverged_integration_branches_change_nothing(env, tmp_path):
    state = three_runs(env, "pd")
    await session(state, env.storage)
    user = tmp_path / "user"
    git(tmp_path, "clone", "-q", "-b", "integration/pd", str(env.origin), str(user))
    git(user, "commit", "-q", "--allow-empty", "-m", "pushed fix")
    git(user, "push", "-q", "origin", "integration/pd")
    git(env.repo, "checkout", "-q", "integration/pd")
    git(env.repo, "commit", "-q", "--allow-empty", "-m", "local fix")
    git(env.repo, "checkout", "-q", "main")
    before = (tip(env.repo, "integration/pd"), tip(env.origin, "integration/pd"))
    state.runs[2].status = RunStatus.DONE

    await session(state, env.storage)

    assert (tip(env.repo, "integration/pd"), tip(env.origin, "integration/pd")) == before
    assert state.integration.failure == IntegrationFailure.DIVERGED
    assert state.integration.status == IntegrationStatus.NEEDS_FIX
    assert state.integration.merged_runs == [0, 1]
    assert orch._integration_due(state) is None


# ---------------------------------------------------------------------------
# Publish contract (SEAM-IO-4)
# ---------------------------------------------------------------------------


async def test_pr_failure_is_not_done_and_the_next_session_only_publishes(env, hermetic_merge):
    env.create_branch("feat-a", {"a.txt": "a\n"})
    env.create_branch("feat-b", {"b.txt": "b\n"})
    state = ConductorState(project_name="pf", runs=[run(0, "a", "feat-a"), run(1, "b", "feat-b")])
    env.gh.fail(True)

    await session(state, env.storage)

    integration = state.integration
    assert integration.status == IntegrationStatus.NEEDS_FIX
    assert (integration.published, integration.pr_url) == (False, None)
    assert integration.failure == IntegrationFailure.PUBLISH
    assert "gh auth login" in integration.failure_detail
    assert tip(env.origin, "integration/pf")  # the push went through

    env.gh.fail(False)
    due = await session(state, env.storage)

    assert due is not None and "publish" in due[1]
    assert state.integration.status == IntegrationStatus.DONE
    assert state.integration.published is True
    assert state.integration.pr_url == "https://github.com/o/r/pull/1"
    hermetic_merge.validate.assert_awaited_once()  # nothing merged or validated again


async def test_single_run_push_failure_is_retried_at_most_three_times(env, monkeypatch):
    monkeypatch.setattr(orch.asyncio, "sleep", AsyncMock())
    env.create_branch("feat-a", {"a.txt": "a\n"})
    state = ConductorState(project_name="sr", runs=[run(0, "a", "feat-a")])
    env.gh.fail(True)

    for _ in range(orch.MAX_INTEGRATION_RETRIES):
        assert await session(state, env.storage) is not None

    assert state.integration.status == IntegrationStatus.NEEDS_FIX
    assert state.integration.published is False
    assert orch._integration_due(state) is None


# ---------------------------------------------------------------------------
# Runs that cannot be merged (SEAM-IO-6)
# ---------------------------------------------------------------------------


async def test_unmergeable_runs_are_not_retried_every_session(env, monkeypatch):
    (env.repo / "shared.txt").write_text("1\n")
    git(env.repo, "add", "shared.txt")
    git(env.repo, "commit", "-q", "-m", "shared")
    env.create_branch("feat-a", {"shared.txt": "A\n"})
    env.create_branch("feat-b", {"shared.txt": "B\n"})
    git(env.repo, "rm", "-q", "shared.txt")
    git(env.repo, "commit", "-q", "-m", "main drops shared.txt")
    calls = []

    async def failing_claude(prompt, **kwargs):
        calls.append(prompt)
        return ClaudeResult(exit_code=1, output="", tokens_used=None, cost=None, duration=0.0)

    monkeypatch.setattr(merge, "run_claude", failing_claude)
    state = ConductorState(project_name="pu", runs=[run(0, "a", "feat-a"), run(1, "b", "feat-b")])

    await session(state, env.storage)

    assert state.integration.status == IntegrationStatus.FAILED and calls
    assert state.integration.failure == IntegrationFailure.NOTHING_MERGEABLE
    assert orch._integration_due(state) is None
    assert not tip(env.origin, "integration/pu")


# ---------------------------------------------------------------------------
# Learnings (SEAM-IO-5)
# ---------------------------------------------------------------------------


async def test_learnings_are_committed_once_on_the_integration_branch(env, tmp_path, monkeypatch):
    """Each session's review used to commit CLAUDE.md on a different run
    branch, so every re-integration conflicted in CLAUDE.md."""
    (env.repo / "CLAUDE.md").write_text("# Project\n")
    git(env.repo, "add", "CLAUDE.md")
    git(env.repo, "commit", "-q", "-m", "CLAUDE.md")
    state = three_runs(env, "pl")
    branches = {}
    for r in state.runs:
        stage = r.stages[0]
        branches[r.name] = tip(env.repo, stage.branch)
        stage.worktree = str(tmp_path / f"wt-{r.name}")
        git(env.repo, "worktree", "add", "-q", stage.worktree, stage.branch)
        learnings = Path(stage.worktree) / "docs" / f"{r.name}{stage.feature_suffix}" / "LEARNINGS.md"
        learnings.parent.mkdir(parents=True)
        learnings.write_text(f"- learned: rule from {r.name}\n")

    async def ask(prompt, **kwargs):
        # A reviewer that adds every learning it is shown.
        lines = sorted({line for line in prompt.splitlines() if line.startswith("- learned:")})
        return BrainReply(text="<<<FILE: CLAUDE.md>>>\n" + "\n".join(lines) + "\n<<<END>>>", ok=True)

    monkeypatch.setattr(orch.brain, "ask", ask)

    await session(state, env.storage)
    state.runs[2].status = RunStatus.DONE
    await session(state, env.storage)

    assert state.integration.status == IntegrationStatus.DONE, state.integration
    assert state.integration.conflicts_resolved == []
    claude_md = git(env.repo, "show", "integration/pl:CLAUDE.md")
    for name in ("a", "b", "c"):
        assert claude_md.count(f"- learned: rule from {name}") == 1
    # No run branch got a learnings commit.
    assert {r.name: tip(env.repo, r.stages[0].branch) for r in state.runs} == branches
