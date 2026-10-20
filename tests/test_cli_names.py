"""Project names are validated where projects, loops and manual-tests are created."""

from __future__ import annotations

import pytest

from conductor.core.storage import conductor_home
from tests.cli_support import repo_fixture, run_main  # noqa: F401


@pytest.mark.parametrize("command", ["init", "go", "loop", "manual-test"])
@pytest.mark.parametrize("name", ["v1.2-auth", "a:b", "feat/x", "my proj", "-dash", "..", ""])
def test_unsafe_names_are_rejected_before_anything_is_written(repo, command, name):
    plan = [] if command == "init" else ["--plan", str(repo / "plan.md")]
    out, _, code = run_main([command, f"--name={name}", "--project-dir", str(repo), *plan])

    assert code == 1
    assert "invalid --name" in out
    assert not (conductor_home() / "projects").exists()


@pytest.mark.parametrize("name", ["dispatch-wp1-manual-r2", "finance_module", "V2", "a"])
def test_safe_names_are_accepted(repo, name):
    out, _, code = run_main(["init", "--name", name, "--project-dir", str(repo)])

    assert code == 0, out
