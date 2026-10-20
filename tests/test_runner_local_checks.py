"""runner.local_checks fix commits follow the same staging rule as phase commits."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import runner.local_checks as local_checks


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


async def test_ci_fix_commit_leaves_out_runner_and_speccer_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    docs = repo / "docs" / "feat"
    docs.mkdir(parents=True)
    (docs / "PRD.md").write_text("spec\n")  # speccer output, untracked

    ci_runs = iter([(False, "1 test failed"), (True, "ok")])

    async def fake_ci(*_args, **_kwargs):
        return next(ci_runs)

    async def fake_claude(prompt, model=None, max_turns=0, cwd=""):
        (repo / "app.py").write_text("x = 2\n")
        (docs / "activity.log").write_text("Edit: app.py\n")
        (docs / "LEARNINGS.md").write_text("## gotcha\n")
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(local_checks, "run_local_ci", fake_ci)
    monkeypatch.setattr(local_checks, "run_claude", fake_claude)

    ok = await local_checks.run_local_checks(
        repo, "feat", ci_enabled=True, ci_command="make ci", ci_max_retries=1,
        runner_owned=["docs/feat/activity.log", "docs/feat/LEARNINGS.md"],
    )

    assert ok
    assert _git(repo, "log", "-1", "--format=%s").strip() == "fix: local CI fixes (attempt 1)"
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["app.py"]
