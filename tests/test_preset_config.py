"""Tests for preset config completeness — all PresetConfig fields present."""

from __future__ import annotations

import pytest
from dataclasses import fields

from conductor.core.presets import (
    PresetConfig,
    BasePreset,
    AcmePreset,
    NodeappPreset,
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


# ---------------------------------------------------------------------------
# AcmePreset config completeness
# ---------------------------------------------------------------------------


def test_acme_config_fixer_enabled():
    cfg = AcmePreset().config
    assert cfg.fixer_enabled is True


def test_acme_config_sync_enabled():
    cfg = AcmePreset().config
    assert cfg.sync_enabled is True


def test_acme_config_local_ci_command():
    cfg = AcmePreset().config
    assert cfg.local_ci_command != ""
    assert "worktree-env" in cfg.local_ci_command


def test_acme_config_local_review_enabled():
    cfg = AcmePreset().config
    assert cfg.local_review_enabled is True


def test_acme_config_sync_dump_regen_has_4_entries():
    cfg = AcmePreset().config
    assert len(cfg.sync_dump_regen) == 4


def test_acme_config_sync_dump_regen_format():
    cfg = AcmePreset().config
    for entry in cfg.sync_dump_regen:
        assert len(entry) == 2, (
            "Each sync_dump_regen entry must be (glob, command) tuple"
        )
        glob_pat, cmd = entry
        assert isinstance(glob_pat, str)
        assert isinstance(cmd, str)
        assert glob_pat  # non-empty
        assert cmd  # non-empty


# ---------------------------------------------------------------------------
# NodeappPreset config completeness
# ---------------------------------------------------------------------------


def test_nodeapp_config_model_set():
    cfg = NodeappPreset().config
    assert cfg.model != ""


def test_nodeapp_config_fix_model_set():
    cfg = NodeappPreset().config
    assert cfg.fix_model != ""


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
# All presets instantiate without error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["base", "acme", "nodeapp"])
def test_all_presets_load(name):
    preset = load_preset(name)
    assert preset is not None
    assert hasattr(preset, "config")
    assert isinstance(preset.config, PresetConfig)


@pytest.mark.parametrize("name", ["base", "acme", "nodeapp"])
def test_all_presets_have_required_methods(name):
    preset = load_preset(name)
    assert callable(getattr(preset, "quality_gate", None))
    assert callable(getattr(preset, "preflight", None))
    assert callable(getattr(preset, "build_prompt_extra", None))
    assert callable(getattr(preset, "stage_teardown", None))
