"""A relative --plan means the current directory for go, loop and manual-test."""

from __future__ import annotations

import pytest

from conductor import cli
from tests.cli_support import repo_fixture, run_main  # noqa: F401


def test_relative_plan_resolves_against_cwd(repo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "fix.md").write_text("- [ ] one\n")
    monkeypatch.chdir(elsewhere)

    assert cli._resolve_plan_path("fix.md", repo) == elsewhere / "fix.md"


def test_relative_plan_falls_back_to_project_dir_with_a_note(repo, tmp_path, monkeypatch, capsys):
    (repo / "fix.md").write_text("- [ ] one\n")
    monkeypatch.chdir(tmp_path)

    assert cli._resolve_plan_path("fix.md", repo) == repo / "fix.md"
    assert "using " + str(repo / "fix.md") in capsys.readouterr().err


def test_missing_plan_names_the_cwd_path(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cli._resolve_plan_path("nope.md", repo)

    assert exc.value.code == 1
    assert str(tmp_path / "nope.md") in capsys.readouterr().out


def test_loop_reads_a_plan_kept_in_cwd(repo, tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "fix.md").write_text("- [ ] one\n")
    monkeypatch.chdir(plans)
    monkeypatch.setattr(cli, "_launch_in_tmux", lambda *a, **k: None)

    out, _, code = run_main(
        ["loop", "--name", "l", "--project-dir", str(repo), "--plan", "fix.md", "--no-worktree"]
    )

    assert code == 0, out
    assert "Plan file not found" not in out
