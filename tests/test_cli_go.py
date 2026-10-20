"""Tests for `conductor go` phase detection and brief handling (plan and run mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest

import conductor.cli as cli
from conductor.core.models import ConductorState, atomic_save
from conductor.core.storage import StorageResolver
from tests.cli_support import NAME, repo_fixture, run_main, seed_state, write_brief  # noqa: F401
from tests.helpers import make_run_state


@pytest.fixture
def calls(monkeypatch):
    """Replace plan and run; plan saves a one-run plan so the state looks planned."""
    seen: list[str] = []

    def plan(args, **kwargs):
        seen.append("plan")
        path = StorageResolver(Path(args.project_dir)).conductor_state(args.name)
        state = ConductorState.model_validate_json(path.read_text(encoding="utf-8"))
        state.runs = [make_run_state(0, "r0")]
        atomic_save(state, path)

    monkeypatch.setattr(cli, "_cmd_plan", plan)
    monkeypatch.setattr(cli, "_cmd_run", lambda args: seen.append("run"))
    return seen


def _go(repo, *extra: str):
    return run_main(["go", "--name", NAME, "--project-dir", str(repo), *extra])


def _brief(repo) -> Path:
    return StorageResolver(repo).conductor_brief(NAME)


@pytest.mark.parametrize("brief", ["## Goal\nAdd /health route\n", None])
def test_planned_project_resumes_whatever_the_brief_says(repo, calls, brief):
    seed_state(repo)
    if brief is not None:
        write_brief(repo, brief)

    _, _, code = _go(repo)

    assert code == 0
    assert calls == ["run"]


def test_planned_project_warns_that_a_different_plan_is_ignored(repo, calls, tmp_path):
    seed_state(repo)
    write_brief(repo)
    new = tmp_path / "new.md"
    new.write_text("## Goal\nSomething else entirely, with enough words.\n")

    _, err, code = _go(repo, "--plan", str(new))

    assert code == 0
    assert calls == ["run"]
    assert "--plan ignored" in err
    assert "Something else" not in _brief(repo).read_text()


def test_resuming_with_the_same_plan_file_is_silent(repo, calls, tmp_path):
    seed_state(repo)
    write_brief(repo)
    same = tmp_path / "same.md"
    same.write_text(_brief(repo).read_text())

    _, err, code = _go(repo, "--plan", str(same))

    assert code == 0
    assert "ignored" not in err


def test_unplanned_project_takes_the_new_plan_over_an_old_brief(repo, calls, tmp_path):
    seed_state(repo, runs=[])
    write_brief(repo, "## Goal\nThe old idea that never got planned.\n")
    new = tmp_path / "new.md"
    new.write_text("## Goal\nThe new idea, which should be planned.\n")

    _, err, code = _go(repo, "--plan", str(new))

    assert code == 0
    assert calls == ["plan", "run"]
    assert _brief(repo).read_text() == new.read_text()
    assert "Replacing the existing brief" in err


def test_unplanned_project_with_only_the_template_needs_a_plan(repo, calls):
    out, _, code = _go(repo)

    assert code == 1
    assert "Brief is empty" in out
    assert calls == []
    # init ran, so the template is there to fill in
    assert _brief(repo).exists()


def test_go_does_not_print_the_manual_next_steps(repo, calls, tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("## Goal\nAdd a /health endpoint with a DB check.\n")

    out, _, code = _go(repo, "--plan", str(brief))

    assert code == 0
    assert "Initialized conductor project" in out
    assert "Then run" not in out and "Fill in" not in out


def test_plan_refuses_an_unfilled_template(repo, monkeypatch):
    run_main(["init", "--name", NAME, "--project-dir", str(repo)])
    monkeypatch.setattr(cli, "_generate_and_review_plan", lambda *a: pytest.fail("planned the template"))

    out, _, code = run_main(["plan", "--name", NAME, "--project-dir", str(repo)])

    assert code == 1
    assert "Fill it in before planning" in out
