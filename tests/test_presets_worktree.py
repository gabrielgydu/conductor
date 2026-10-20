"""Project-local presets must resolve from inside linked worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conductor.core.presets import PROJECT_PRESET_FILE, ConfigPreset, detect_preset, load_preset


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_with_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "test@example.com")
    _git(main, "config", "user.name", "Test")
    _git(main, "config", "commit.gpgsign", "false")
    (main / "README.md").write_text("hi\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    # Not committed, so `git worktree add` does not copy it.
    (main / PROJECT_PRESET_FILE).parent.mkdir()
    (main / PROJECT_PRESET_FILE).write_text(
        'name = "myproj"\n\n[[quality_gate.checks]]\ncommand = "true"\n'
    )
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(wt))
    return main, wt


def test_untracked_project_preset_loads_from_worktree(repo_with_worktree):
    main, wt = repo_with_worktree
    assert not (wt / PROJECT_PRESET_FILE).exists()

    preset = load_preset("myproj", wt)

    assert isinstance(preset, ConfigPreset)
    assert preset.name == "myproj"
    assert preset.source == main / PROJECT_PRESET_FILE


def test_untracked_project_preset_detected_from_worktree(repo_with_worktree):
    _main, wt = repo_with_worktree
    assert detect_preset(wt) == "myproj"


def test_worktree_copy_takes_precedence(repo_with_worktree):
    _main, wt = repo_with_worktree
    (wt / PROJECT_PRESET_FILE).parent.mkdir()
    (wt / PROJECT_PRESET_FILE).write_text('name = "local"\n')

    assert load_preset("local", wt).source == wt / PROJECT_PRESET_FILE


def test_plain_directory_still_reports_unknown_preset(tmp_path):
    with pytest.raises(ValueError, match="Unknown preset"):
        load_preset("myproj", tmp_path)
