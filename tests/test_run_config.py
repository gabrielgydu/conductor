"""run.sh manifest parsing and generate_run_config (manifest + preset -> RUN-CONFIG.json)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.core.orchestrator import ManifestError, generate_run_config, parse_manifest
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


def _write(tmp_path: Path, text: str, prompts=("01-backend.md", "02-tests.md")) -> Path:
    (tmp_path / "prompts").mkdir(exist_ok=True)
    for name in prompts:
        (tmp_path / "prompts" / name).write_text("do it\n")
    run_sh = tmp_path / "run.sh"
    run_sh.write_text(text)
    return run_sh


def _generate(tmp_path: Path, preset: PresetConfig) -> RunConfig:
    run_sh = _write(tmp_path, RUN_SH)
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


# ---------------------------------------------------------------------------
# parse_manifest: bash forms the template's arrays can take
# ---------------------------------------------------------------------------


def _phases(tmp_path: Path, text: str):
    return [(p.number, p.name, Path(p.prompt_file).name, p.token)
            for p in parse_manifest(_write(tmp_path, text)).phases]


@pytest.mark.parametrize(
    "text",
    [
        # single quotes and bare words
        "PHASES[1]='01-backend.md'\nPHASE_TOKENS[1]=BACKEND_DONE\nPHASE_NAMES[1]='Backend'\n",
        # compound declaration
        'declare -A PHASES=([1]="01-backend.md")\n'
        'declare -A PHASE_TOKENS=([1]="BACKEND_DONE")\n'
        "declare -A PHASE_NAMES=([1]='Backend')\n",
        # zero-padded keys and a prompts/ prefix
        'PHASES[01]="prompts/01-backend.md"\nPHASE_TOKENS[1]="BACKEND_DONE"\n'
        'PHASE_NAMES[001]="Backend"\n',
    ],
)
def test_manifest_accepts_the_bash_forms_of_the_arrays(tmp_path, text):
    assert _phases(tmp_path, text) == [(1, "Backend", "01-backend.md", "BACKEND_DONE")]


def test_manifest_ignores_comments_and_keeps_escaped_quotes(tmp_path):
    text = (
        '# Example: PHASES[9]="example.md"\n'
        'PHASES[1]="01-backend.md"  # the backend\n'
        'PHASE_NAMES[1]="Add \\"auth\\" API"\n'
        'PHASE_TOKENS[1]="BACKEND_DONE"\n'
    )
    assert _phases(tmp_path, text) == [(1, 'Add "auth" API', "01-backend.md", "BACKEND_DONE")]


def test_manifest_resolves_default_expansions_and_rejects_others(tmp_path):
    ok = 'CONDUCTOR_MODEL="${CONDUCTOR_MODEL:-sonnet}"\nPHASES[1]="01-backend.md"\n'
    manifest = parse_manifest(_write(tmp_path, ok))
    assert (manifest.model, manifest.fix_model) == ("sonnet", "sonnet")

    bad = 'CONDUCTOR_MODEL="$MODEL"\nPHASES[1]="01-backend.md"\n'
    with pytest.raises(ManifestError, match="cannot resolve"):
        parse_manifest(_write(tmp_path, bad))


def test_manifest_without_prompt_file_or_phases_is_rejected(tmp_path):
    with pytest.raises(ManifestError, match="missing"):
        parse_manifest(_write(tmp_path, 'PHASES[1]="03-missing.md"\n'))
    with pytest.raises(ManifestError, match="No PHASES"):
        parse_manifest(_write(tmp_path, 'CONDUCTOR_MODEL="opus"\n'))
