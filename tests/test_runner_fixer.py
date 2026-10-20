"""runner.fixer CI-check handling against a fake `gh` that validates --json like gh 2.96."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import runner.fixer as fixer

# `gh pr checks --json` fields in gh 2.96; anything else exits 1 like the real CLI.
_FAKE_GH = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys

    args = sys.argv[1:]
    checks = json.loads(os.environ.get("FAKE_GH_CHECKS", "[]"))
    allowed = {{"bucket", "completedAt", "description", "event", "link", "name",
               "startedAt", "state", "workflow"}}
    if args[:2] == ["pr", "checks"]:
        fields = args[args.index("--json") + 1].split(",")
        unknown = [f for f in fields if f not in allowed]
        if unknown:
            print(f'Unknown JSON field: "{{unknown[0]}}"', file=sys.stderr)
            sys.exit(1)
        print(json.dumps([{{f: c.get(f, "") for f in fields}} for c in checks]))
    elif args[:2] == ["pr", "view"]:
        print("abc123")
    elif args[:1] == ["api"]:
        pass  # no annotations
    else:
        print(f"fake gh: unsupported {{args}}", file=sys.stderr)
        sys.exit(1)
    """
)


def _check(name: str, state: str, bucket: str) -> dict:
    return {"name": name, "state": state, "bucket": bucket, "link": f"https://ci.example/{name}"}


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(_FAKE_GH.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(fixer.time, "sleep", lambda _s: None)

    def set_checks(*checks: dict) -> None:
        monkeypatch.setenv("FAKE_GH_CHECKS", json.dumps(list(checks)))

    return set_checks


def test_failure_logs_name_every_blocking_check(fake_gh, tmp_path):
    fake_gh(
        _check("tests", "FAILURE", "fail"),
        _check("e2e", "TIMED_OUT", "fail"),
        _check("lint", "SUCCESS", "pass"),
        _check("codecov/patch", "FAILURE", "fail"),  # matches the default skip pattern
    )

    logs = fixer.get_ci_failure_logs(7, "o", "r", tmp_path)

    assert "=== FAILED CHECK: tests (https://ci.example/tests) ===" in logs
    assert "=== FAILED CHECK: e2e" in logs
    assert "lint" not in logs and "codecov" not in logs


def test_wait_for_ci_uses_gh_buckets(fake_gh, tmp_path):
    fake_gh(_check("build", "TIMED_OUT", "fail"), _check("lint", "SUCCESS", "pass"))
    assert fixer.wait_for_ci(7, tmp_path, poll_interval=1, max_wait=33) == "CI_FAILED"

    fake_gh(_check("deploy", "WAITING", "pending"), _check("lint", "SUCCESS", "pass"))
    assert fixer.wait_for_ci(7, tmp_path, poll_interval=1, max_wait=33) == "CI_TIMEOUT"

    fake_gh(_check("lint", "SUCCESS", "pass"), _check("docs", "CANCELLED", "cancel"))
    assert fixer.wait_for_ci(7, tmp_path, poll_interval=1, max_wait=33) == "CI_PASSED"


async def test_unreadable_ci_failure_is_not_reported_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(fixer, "_get_repo_info", lambda _cwd: ("o", "r"))
    monkeypatch.setattr(fixer, "wait_for_ci", lambda *_a, **_k: "CI_FAILED")
    monkeypatch.setattr(fixer, "get_unresolved_threads", lambda *_a: [])
    monkeypatch.setattr(fixer, "get_ci_failure_logs", lambda *_a: "")
    cfg = fixer.FixerConfig(
        project_dir=tmp_path, branch="feat", feature_name="feat", phase=1, pr_number=7, sync_mode=True,
    )

    await fixer.run_fixer(cfg)

    status_file = tmp_path / "storage" / "logs" / "feat-build" / ".fixer-status-phase-1"
    status = json.loads(status_file.read_text())
    assert status["status"] == "error"


# ─── Review threads: resolve only what Claude saw (F71) ─────────────────────


def _git(cwd, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def pr_checkout(tmp_path, monkeypatch):
    """A clone on branch `feat` whose origin is a local bare repo; GitHub mocked."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "checkout", "-q", "-b", "feat")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feat")
    _git(work, "push", "-q", "origin", "feat")

    threads = [
        {"id": f"T{i}", "isResolved": False,
         "comments": {"nodes": [{"body": f"fix {i}", "path": "app.py", "line": 1, "author": {"login": "rev"}}]}}
        for i in range(20)
    ]
    mutations: list[str] = []

    def fake_graphql(query, variables, cwd):
        mutations.append(variables["threadId"])
        if variables["threadId"] == "T3":
            return {}  # gh failed; _graphql swallows it
        return {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}

    async def fake_claude(prompt, model=None, max_turns=0, cwd=""):
        (Path(cwd) / "app.py").write_text("x = 2\n")
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(fixer, "_get_repo_info", lambda _cwd: ("o", "r"))
    monkeypatch.setattr(fixer, "wait_for_ci", lambda *_a, **_k: "CI_PASSED")
    monkeypatch.setattr(fixer, "get_unresolved_threads", lambda *_a: threads)
    monkeypatch.setattr(fixer, "_graphql", fake_graphql)
    monkeypatch.setattr(fixer, "run_claude", fake_claude)
    monkeypatch.setattr(fixer, "_FIX_WORKTREE_ROOT", tmp_path / "fix-worktrees")
    return work, mutations


def test_prompt_reports_threads_it_shows():
    cfg = fixer.FixerConfig(project_dir=Path("."), branch="b", feature_name="f", phase=1, pr_number=1)
    threads = [
        {"id": f"T{i}", "comments": {"nodes": [{"body": "x" * 30_000, "path": "a", "line": 1}]}}
        for i in range(5)
    ]

    prompt, shown = fixer.build_fix_prompt(cfg, "", threads)

    # Each thread is ~30k chars; the 100k cap fits three of them.
    assert [t["id"] for t in shown] == ["T0", "T1", "T2"]
    assert "prompt truncated" in prompt


async def test_sync_fix_resolves_only_shown_threads(pr_checkout, capsys):
    work, mutations = pr_checkout
    cfg = fixer.FixerConfig(
        project_dir=work, branch="feat", feature_name="feat", phase=1, pr_number=7, sync_mode=True,
    )

    await fixer.run_fixer(cfg)

    assert mutations == [f"T{i}" for i in range(fixer._MAX_REVIEW_THREADS)]
    err = capsys.readouterr().err
    assert "Could not resolve thread T3" in err
    assert "Resolved thread T3" not in err


async def test_async_fix_without_fix_pr_leaves_threads_open(pr_checkout, fake_gh, capsys):
    # fake_gh rejects `gh pr create`, so the fixes never reach the PR branch.
    work, mutations = pr_checkout
    cfg = fixer.FixerConfig(project_dir=work, branch="feat", feature_name="feat", phase=1, pr_number=7)

    await fixer.run_fixer(cfg)

    err = capsys.readouterr().err
    assert "Fix PR creation failed" in err
    assert "Leaving 15 review thread(s) open" in err
    assert mutations == []


def test_dirty_fix_worktree_is_removed_with_its_branch(pr_checkout, tmp_path):
    work, _ = pr_checkout
    wt = tmp_path / "fix-wt"
    assert fixer._worktree_add(work, wt, "fix/feat-phase-1", "feat")
    (wt / "app.py").write_text("half-edited\n")
    (wt / "new.py").write_text("untracked\n")

    fixer._worktree_remove(work, wt, "fix/feat-phase-1")

    assert not wt.exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "fix/*"], cwd=work, capture_output=True, text=True, check=True
    ).stdout
    assert branches.strip() == ""
    # The next run can create the same branch again.
    assert fixer._worktree_add(work, wt, "fix/feat-phase-1", "feat")
