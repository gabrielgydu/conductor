"""Tests for preset config completeness — all PresetConfig fields present."""

from __future__ import annotations

import pytest
from dataclasses import fields

from conductor.core.presets import (
    PRESETS_DIR_ENV,
    PresetConfig,
    BasePreset,
    load_preset,
)


# ---------------------------------------------------------------------------
# PresetConfig field completeness
# ---------------------------------------------------------------------------

EXPECTED_FIELDS = {
    "fixer_enabled",
    "fixer_async",
    "sync_enabled",
    "sync_base_branch",
    "sync_dump_regen",
    "testing_mode",
    "local_ci_enabled",
    "local_ci_command",
    "local_ci_full_command",
    "local_ci_max_retries",
    "local_review_enabled",
    "local_review_command",
    "local_review_full_command",
    "local_review_max_retries",
    "max_turns",
    "max_iterations_per_phase",
    "max_gate_retries",
    "model",
    "fix_model",
    "phase_models",
    "fixer_ci_poll_interval",
    "fixer_ci_max_wait",
    "fixer_skip_patterns",
    "worktrees_base",
}


def test_preset_config_has_all_expected_fields():
    actual_fields = {f.name for f in fields(PresetConfig)}
    missing = EXPECTED_FIELDS - actual_fields
    assert not missing, f"PresetConfig is missing fields: {missing}"


def test_preset_config_default_values():
    cfg = PresetConfig()
    assert cfg.fixer_enabled is False
    assert cfg.fixer_async is False
    assert cfg.sync_enabled is False
    assert cfg.sync_base_branch == "master"
    assert cfg.sync_dump_regen == []
    assert cfg.testing_mode is False
    assert cfg.local_ci_enabled is False
    assert cfg.local_ci_command == ""
    assert cfg.local_ci_full_command == ""
    assert cfg.local_ci_max_retries == 3
    assert cfg.local_review_enabled is False
    assert cfg.local_review_command == ""
    assert cfg.local_review_full_command == ""
    assert cfg.local_review_max_retries == 2
    assert cfg.max_turns == 200
    assert cfg.max_iterations_per_phase == 8
    assert cfg.max_gate_retries == 5
    assert cfg.model == ""
    assert cfg.fix_model == ""
    assert cfg.phase_models == {}
    assert cfg.fixer_ci_poll_interval == 60
    assert cfg.fixer_ci_max_wait == 5400
    assert cfg.fixer_skip_patterns == "coverage|Coverage|codecov|Codecov"
    assert cfg.worktrees_base == ""


# ---------------------------------------------------------------------------
# Every PresetConfig field is settable from a TOML [config] section
# ---------------------------------------------------------------------------


@pytest.fixture
def presets_dir(tmp_path, monkeypatch):
    directory = tmp_path / "presets"
    directory.mkdir()
    monkeypatch.setenv(PRESETS_DIR_ENV, str(directory))
    return directory


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[]"
    if isinstance(value, dict):
        return "{}"
    raise TypeError(type(value))


def test_every_config_field_accepted_from_toml(presets_dir):
    defaults = PresetConfig()
    lines = ["[config]"]
    for f in fields(PresetConfig):
        lines.append(f"{f.name} = {_toml_value(getattr(defaults, f.name))}")
    (presets_dir / "full.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg = load_preset("full").config
    assert cfg == defaults


# ---------------------------------------------------------------------------
# BasePreset config — sensible defaults
# ---------------------------------------------------------------------------


def test_base_config_fixer_disabled():
    cfg = BasePreset().config
    assert cfg.fixer_enabled is False


def test_base_config_sync_disabled():
    cfg = BasePreset().config
    assert cfg.sync_enabled is False


# ---------------------------------------------------------------------------
# All presets expose the same interface
# ---------------------------------------------------------------------------


@pytest.fixture
def preset_names(presets_dir):
    (presets_dir / "custom.toml").write_text("[config]\nfixer_enabled = true\n", encoding="utf-8")
    return ["base", "custom"]


def test_all_presets_load(preset_names):
    for name in preset_names:
        preset = load_preset(name)
        assert preset is not None
        assert hasattr(preset, "config")
        assert isinstance(preset.config, PresetConfig)


def test_all_presets_have_required_methods(preset_names):
    for name in preset_names:
        preset = load_preset(name)
        assert callable(getattr(preset, "quality_gate", None))
        assert callable(getattr(preset, "preflight", None))
        assert callable(getattr(preset, "build_prompt_extra", None))
        assert callable(getattr(preset, "stage_teardown", None))
        assert callable(getattr(preset, "run_teardown", None))
