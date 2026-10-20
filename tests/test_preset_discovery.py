"""Preset discovery: file-stem naming, wrongly typed values, no silent fallback to base."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.core.presets import PRESETS_DIR_ENV, ConfigPreset, detect_preset, load_preset
from conductor.core.storage import StorageResolver
from tests.cli_support import NAME, repo_fixture, run_main  # noqa: F401

MATCHING = '[detect]\npaths = ["backend", "frontend"]\n'


@pytest.fixture
def presets_dir(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "user-presets"
    directory.mkdir()
    monkeypatch.setenv(PRESETS_DIR_ENV, str(directory))
    return directory


@pytest.fixture
def project(tmp_path) -> Path:
    project = tmp_path / "proj"
    (project / "backend").mkdir(parents=True)
    (project / "frontend").mkdir()
    return project


def test_detected_user_preset_is_named_by_its_file(presets_dir, project):
    # e.g. a copy of examples/preset.example.toml that still says name = "example"
    (presets_dir / "myapp.toml").write_text('name = "example"\n' + MATCHING)

    name = detect_preset(project)

    assert name == "myapp"
    assert load_preset(name, project).source == presets_dir / "myapp.toml"


def test_user_preset_can_also_be_loaded_by_its_internal_name(presets_dir):
    (presets_dir / "myapp.toml").write_text('name = "example"\n' + MATCHING)

    assert load_preset("example").source == presets_dir / "myapp.toml"


@pytest.mark.parametrize(
    "broken",
    [
        '[[quality_gate.checks]]\ncommand = "make"\ntimeout = [1]\n',
        "[manual_test]\nblocked_reason_words = 5\n",
        "detect = 5\n",
        "detect = []\n",
        "[preflight]\ntimeout = 2024-01-01\n",
    ],
)
def test_wrongly_typed_values_are_invalid_preset_errors(presets_dir, broken):
    path = presets_dir / "other.toml"
    path.write_text(broken)

    with pytest.raises(ValueError, match="Invalid preset file"):
        ConfigPreset.from_file(path)


def test_broken_preset_for_another_project_is_skipped_with_a_warning(presets_dir, project, capsys):
    (presets_dir / "other.toml").write_text('[detect]\npaths = ["nope"]\n[[quality_gate.checks]]\ncommand = "x"\ntimeout = [1]\n')

    assert detect_preset(project) == "base"
    assert "skipping preset" in capsys.readouterr().err


def test_broken_preset_that_matches_the_project_is_an_error_not_base(presets_dir, project):
    (presets_dir / "myapp.toml").write_text(MATCHING + '[config]\nlocal_ci_comand = "make ci"\n')

    with pytest.raises(ValueError, match="local_ci_comand") as excinfo:
        detect_preset(project)

    assert "match" in str(excinfo.value)


def test_init_reports_a_broken_matching_preset(repo, presets_dir):
    (repo / "backend").mkdir()
    (repo / "frontend").mkdir()
    (presets_dir / "myapp.toml").write_text(MATCHING + '[config]\nlocal_ci_comand = "make ci"\n')

    out, _, code = run_main(["init", "--name", NAME, "--project-dir", str(repo)])

    assert code == 1
    assert "local_ci_comand" in out
    assert not StorageResolver(repo).conductor_state(NAME).exists()


def test_loop_checks_the_preset_before_paying_for_plan_decomposition(repo, presets_dir, monkeypatch):
    (repo / "backend").mkdir()
    (repo / "frontend").mkdir()
    (presets_dir / "myapp.toml").write_text(MATCHING + '[config]\nlocal_ci_comand = "make ci"\n')
    plan = repo / "fix-plan.md"
    plan.write_text("Free-form plan without a checklist.\n")
    monkeypatch.setattr("conductor.core.loop.decompose_plan", lambda text: pytest.fail("decomposed anyway"))

    out, _, code = run_main(["loop", "--name", NAME, "--project-dir", str(repo), "--plan", str(plan)])

    assert code == 1
    assert "local_ci_comand" in out
