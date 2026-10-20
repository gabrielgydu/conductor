"""An installed conductor (a wheel, not the repo checkout) must find its own files."""

from __future__ import annotations

import shlex
import sys
import tomllib
from pathlib import Path

import pytest

import conductor.cli as cli
from tests.cli_support import (  # noqa: F401
    NAME,
    execvp_fixture,
    fake_tmux_fixture,
    repo_fixture,
    run_main,
    seed_state,
    write_brief,
)

ROOT = Path(__file__).resolve().parents[1]


def test_package_data_ships_every_non_python_file():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    for package in ("conductor", "speccer", "runner"):
        package_dir = ROOT / "src" / package
        shipped = {p for pattern in package_data.get(package, []) for p in package_dir.glob(pattern)}
        data_files = {
            p for p in package_dir.rglob("*")
            if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
        }
        assert data_files - shipped == set(), f"{package}: not in package-data"


def _reexec_argv(fake_tmux) -> list[str]:
    new = next(c for c in fake_tmux.calls if c[0] == "new-session")
    return shlex.split(shlex.split(new[new.index("-n") + 2])[2])


def test_run_reexecs_through_the_running_interpreter(repo, fake_tmux, execvp):
    seed_state(repo)

    _, _, code = run_main(["run", "--name", NAME, "--project-dir", str(repo)])

    assert code == 0
    assert _reexec_argv(fake_tmux)[:5] == [sys.executable, "-P", "-m", "conductor", "run"]


def test_loop_reexecs_through_the_running_interpreter(repo, fake_tmux, execvp):
    plan = repo / "plan.md"
    plan.write_text("- [ ] Fix the thing\n")

    out, _, code = run_main(
        ["loop", "--name", NAME, "--project-dir", str(repo), "--plan", str(plan), "--no-worktree", "--preset", "base"]
    )

    assert code == 0, out
    assert _reexec_argv(fake_tmux)[:5] == [sys.executable, "-P", "-m", "conductor", "loop"]


@pytest.mark.parametrize("missing", ["plan", "completeness-review"])
def test_plan_fails_before_claude_when_a_template_is_missing(repo, monkeypatch, missing):
    seed_state(repo, runs=[])
    write_brief(repo)
    real = cli._load_prompt_template

    def load(name):
        if name == missing:
            raise FileNotFoundError(f"Prompt template not found: {name}-prompt.md")
        return real(name)

    monkeypatch.setattr(cli, "_load_prompt_template", load)
    monkeypatch.setattr(cli, "_generate_and_review_plan", lambda *a: pytest.fail("called Claude"))

    out, _, code = run_main(["plan", "--name", NAME, "--project-dir", str(repo)])

    assert code == 1
    assert f"{missing}-prompt.md" in out and "reinstall" in out
