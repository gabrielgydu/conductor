"""Tests for `conductor validate` (conductor.cli._cmd_validate)."""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.core.models import ConductorState
from conductor.core.storage import StorageResolver

_SMOKE_SPEC = Path("tests/Playwright/tests/conductor-smoke.spec.ts")


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A git repo with conductor state for project 'demo' using preset 'demo'."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CONDUCTOR_PRESETS_DIR", str(tmp_path / "presets"))
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    state_path = StorageResolver(repo).conductor_state("demo")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        ConductorState(project_name="demo", preset="demo").model_dump_json(),
        encoding="utf-8",
    )
    return repo


def _preset(tmp_path: Path, lint_command: str) -> None:
    presets = tmp_path / "presets"
    presets.mkdir(exist_ok=True)
    (presets / "demo.toml").write_text(
        "[validation]\n"
        'smoke_command = ["touch", "smoke-ran"]\n\n'
        "[[validation.checks]]\n"
        'name = "lint"\n'
        f'command = "{lint_command}"\n',
        encoding="utf-8",
    )


def _validate(repo: Path, *flags: str) -> tuple[str, int]:
    from conductor.cli import main

    out = StringIO()
    argv = ["conductor", "validate", "--name", "demo", "--project-dir", str(repo), *flags]
    with patch("sys.argv", argv), patch("sys.stdout", out):
        try:
            main()
        except SystemExit as exc:
            return out.getvalue(), exc.code or 0
    return out.getvalue(), 0


def test_validate_runs_the_presets_checks(project, tmp_path):
    """Without --integration the preset was ignored: '0/0 checks passed', exit 0."""
    _preset(tmp_path, "false")

    output, code = _validate(project)

    assert code == 1
    assert "✗ lint" in output
    assert "Validation failed: 0/1 checks passed. Failed: lint" in output


def test_validate_smoke_uses_the_presets_smoke_command_only(project, tmp_path):
    """--smoke used to add integration-tests too (always failing without state) and
    to ignore the preset's smoke_command in favour of a generated Playwright spec."""
    _preset(tmp_path, "true")

    output, code = _validate(project, "--smoke")

    assert code == 0, output
    assert "✓ lint" in output
    assert "✓ smoke" in output
    assert "integration-tests" not in output
    assert (project / "smoke-ran").exists()
    assert not (project / _SMOKE_SPEC).exists()


def test_validate_integration_reports_missing_specs_as_skipped(project, tmp_path):
    _preset(tmp_path, "true")

    output, code = _validate(project, "--integration")

    assert code == 0, output
    assert "✓ integration-tests (0.0s) — skipped: no conductor integration specs" in output
    assert "smoke" not in output


def test_validate_with_an_unknown_preset_is_an_error(project):
    output, code = _validate(project)  # no presets/demo.toml

    assert code == 1
    assert "Unknown preset: 'demo'" in output
