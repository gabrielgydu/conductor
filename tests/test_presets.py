"""Tests for conductor.core.presets"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conductor.core.presets import (
    AcmePreset,
    BasePreset,
    GateResult,
    PresetConfig,
    NodeappPreset,
    load_preset,
)


# ── load_preset factory ────────────────────────────────────────────────────────

def test_load_preset_base():
    assert isinstance(load_preset(""), BasePreset)


def test_load_preset_base_explicit():
    assert isinstance(load_preset("base"), BasePreset)


def test_load_preset_none():
    assert isinstance(load_preset(None), BasePreset)


def test_load_preset_acme():
    assert isinstance(load_preset("acme"), AcmePreset)


def test_load_preset_nodeapp():
    assert isinstance(load_preset("nodeapp"), NodeappPreset)


def test_load_preset_unknown():
    with pytest.raises(ValueError):
        load_preset("unknown")


# ── BasePreset ─────────────────────────────────────────────────────────────────

def test_base_quality_gate_passes(tmp_path):
    preset = BasePreset()
    result = preset.quality_gate(tmp_path)
    assert result.passed is True


def test_base_preflight_claude_found(tmp_path):
    preset = BasePreset()
    with patch("shutil.which", return_value="/usr/bin/claude"):
        assert preset.preflight(tmp_path) is True


def test_base_preflight_claude_missing(tmp_path):
    preset = BasePreset()
    with patch("shutil.which", return_value=None):
        assert preset.preflight(tmp_path) is False


# ── AcmePreset config ────────────────────────────────────────────────────────

def test_acme_config_flags():
    preset = load_preset("acme")
    cfg = preset.config
    assert cfg.push_enabled is True
    assert cfg.fixer_enabled is True


# ── AcmePreset quality_gate ──────────────────────────────────────────────────

def test_acme_quality_gate_phpstan_pass(tmp_path):
    preset = AcmePreset()
    mock_result = MagicMock()
    mock_result.stdout = "phpstan: no errors found"
    with patch("subprocess.run", return_value=mock_result):
        result = preset.quality_gate(tmp_path)
    assert result.passed is True


def test_acme_quality_gate_phpstan_fail(tmp_path):
    preset = AcmePreset()
    mock_result = MagicMock()
    mock_result.stdout = "deny: phpstan found errors"
    with patch("subprocess.run", return_value=mock_result):
        result = preset.quality_gate(tmp_path)
    assert result.passed is False
    assert "PHPStan failed" in result.message


def test_acme_quality_gate_timeout(tmp_path):
    preset = AcmePreset()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 300)):
        result = preset.quality_gate(tmp_path)
    assert result.passed is False
    assert "timed out" in result.message.lower()


# ── AcmePreset preflight ─────────────────────────────────────────────────────

def test_acme_preflight_docker_missing(tmp_path):
    preset = AcmePreset()
    with patch("shutil.which", return_value="/usr/bin/claude"):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert preset.preflight(tmp_path) is False


# ── NodeappPreset quality_gate ───────────────────────────────────────────────

def _make_package(tmp_path: Path, name: str) -> Path:
    pkg_dir = tmp_path / "packages" / name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(json.dumps({"scripts": {}}))
    return pkg_dir


def test_nodeapp_quality_gate_all_pass(tmp_path):
    preset = NodeappPreset()
    _make_package(tmp_path, "shared")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = ""
    with patch("subprocess.run", return_value=mock_result):
        result = preset.quality_gate(tmp_path)
    assert result.passed is True


def test_nodeapp_quality_gate_tsc_fail(tmp_path):
    preset = NodeappPreset()
    _make_package(tmp_path, "shared")

    mock_fail = MagicMock()
    mock_fail.returncode = 1
    mock_fail.stderr = "error TS2345"
    with patch("subprocess.run", return_value=mock_fail):
        result = preset.quality_gate(tmp_path)
    assert result.passed is False
    assert len(result.failures) > 0
