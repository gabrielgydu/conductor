"""generate_run_config: run.sh manifest + preset -> RUN-CONFIG.json."""

from __future__ import annotations

from pathlib import Path

from conductor.core.orchestrator import generate_run_config
from conductor.core.presets import PresetConfig
from runner.config import RunConfig

RUN_SH = """#!/usr/bin/env bash
CONDUCTOR_MODEL="opus"
CONDUCTOR_FIX_MODEL="opus"
declare -A PHASES PHASE_TOKENS PHASE_NAMES
PHASES[1]="01-backend.md"
PHASE_TOKENS[1]="BACKEND_DONE"
PHASE_NAMES[1]="Backend"
PHASES[2]="02-tests.md"
PHASE_TOKENS[2]="TESTS_DONE"
PHASE_NAMES[2]="Tests"
"""


def _generate(tmp_path: Path, preset: PresetConfig) -> RunConfig:
    run_sh = tmp_path / "run.sh"
    run_sh.write_text(RUN_SH)
    return RunConfig.load(generate_run_config(run_sh, "feat", tmp_path, preset))


def test_preset_models_win_over_run_sh(tmp_path):
    cfg = _generate(
        tmp_path, PresetConfig(model="sonnet", fix_model="haiku", phase_models={2: "haiku"})
    )

    assert cfg.model == "sonnet"
    assert cfg.fix_model == "haiku"
    assert [p.model for p in cfg.phases] == [None, "haiku"]


def test_run_sh_models_apply_without_preset_models(tmp_path):
    cfg = _generate(tmp_path, PresetConfig())

    assert cfg.model == "opus"
    assert cfg.fix_model == "opus"
    assert [p.model for p in cfg.phases] == [None, None]
    assert [(p.number, p.name, p.token) for p in cfg.phases] == [
        (1, "Backend", "BACKEND_DONE"), (2, "Tests", "TESTS_DONE"),
    ]
