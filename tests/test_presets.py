"""Tests for conductor.core.presets"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conductor.core.presets import (
    BasePreset,
    ConfigPreset,
    ManualTestPolicy,
    PRESETS_DIR_ENV,
    detect_preset,
    load_preset,
)


@pytest.fixture
def presets_dir(tmp_path, monkeypatch) -> Path:
    """Isolated user-level presets directory."""
    directory = tmp_path / "user-presets"
    directory.mkdir()
    monkeypatch.setenv(PRESETS_DIR_ENV, str(directory))
    return directory


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


FULL_PRESET = '''
name = "demo"

[detect]
paths = ["backend", "frontend"]

[config]
fixer_enabled = true
sync_enabled = true
sync_dump_regen = [["dumps/*.sql", "make dump-regen"]]
local_ci_enabled = true
local_ci_command = "make ci"
fix_model = "sonnet"
phase_models = { 2 = "opus" }
worktrees_base = "~/worktrees"

[preflight]
commands = ["docker info"]

[[quality_gate.checks]]
name = "lint"
command = "make lint"
fail_pattern = "deny"

[[quality_gate.checks]]
name = "optional"
command = "make optional"
requires = "Makefile.optional"

[prompt]
extra = "- Run checks from {cwd}/scripts"

[teardown]
stage = ["make down"]
run = "make down --all"

[validation]
exec_wrapper = ["./scripts/env.sh", "exec"]
smoke_up_command = "./scripts/env.sh up"
smoke_command = "./scripts/env.sh ci"
smoke_timeout = 120

[[validation.checks]]
name = "typecheck"
command = "npx tsc --noEmit"
cwd = "frontend"

[manual_test]
policy_text = "- Feature flags must be toggled through the admin UI."
coverage_focus = "Cover the admin settings pages."
blocked_reason_words = ["data", "fixture", "customer"]

[[manual_test.fatal_patterns]]
pattern = "\\\\bshowNewFlow\\\\b"
reason = "feature flags must be toggled through the admin UI"

[[manual_test.check_commands]]
path_prefix = "backend/"
suffixes = [".php"]
command = "make phpstan-backend"

[[manual_test.check_commands]]
suffixes = [".spec.ts"]
command = "make e2e {path}"
'''


# ── load_preset factory ────────────────────────────────────────────────────────


def test_load_preset_base():
    assert isinstance(load_preset(""), BasePreset)


def test_load_preset_base_explicit():
    assert isinstance(load_preset("base"), BasePreset)


def test_load_preset_none():
    assert isinstance(load_preset(None), BasePreset)


def test_load_preset_unknown(presets_dir):
    with pytest.raises(ValueError, match="Unknown preset"):
        load_preset("unknown")


def test_load_preset_from_user_dir(presets_dir):
    _write(presets_dir / "demo.toml", FULL_PRESET)
    preset = load_preset("demo")
    assert isinstance(preset, ConfigPreset)
    assert preset.name == "demo"
    assert preset.source == presets_dir / "demo.toml"


def test_load_preset_name_defaults_to_file_stem(presets_dir):
    _write(presets_dir / "custom.toml", "[config]\nfixer_enabled = true\n")
    assert load_preset("custom").name == "custom"


def test_load_preset_explicit_file(tmp_path, presets_dir):
    path = _write(tmp_path / "somewhere" / "my.toml", FULL_PRESET)
    preset = load_preset(str(path))
    assert isinstance(preset, ConfigPreset)
    assert preset.name == "demo"


def test_load_preset_project_local(tmp_path, presets_dir):
    project = tmp_path / "proj"
    _write(project / ".conductor" / "preset.toml", FULL_PRESET)
    preset = load_preset("demo", project)
    assert isinstance(preset, ConfigPreset)
    assert preset.source == project / ".conductor" / "preset.toml"


def test_load_preset_project_local_name_mismatch_falls_through(tmp_path, presets_dir):
    project = tmp_path / "proj"
    _write(project / ".conductor" / "preset.toml", 'name = "other"\n')
    with pytest.raises(ValueError):
        load_preset("demo", project)


def test_load_preset_invalid_toml(presets_dir):
    _write(presets_dir / "broken.toml", "this is = not [ toml")
    with pytest.raises(ValueError, match="Invalid preset file"):
        load_preset("broken")


def test_load_preset_unknown_config_key(presets_dir):
    _write(presets_dir / "typo.toml", "[config]\nfixer_enabld = true\n")
    with pytest.raises(ValueError, match="unknown keys"):
        load_preset("typo")


def test_load_preset_unknown_section(presets_dir):
    _write(presets_dir / "typo.toml", "[bogus]\nx = 1\n")
    with pytest.raises(ValueError, match="unknown keys"):
        load_preset("typo")


# ── detect_preset ──────────────────────────────────────────────────────────────


def test_detect_preset_base_when_nothing_matches(tmp_path, presets_dir):
    assert detect_preset(tmp_path) == "base"


def test_detect_preset_project_local(tmp_path, presets_dir):
    project = tmp_path / "proj"
    _write(project / ".conductor" / "preset.toml", "[config]\nfixer_enabled = true\n")
    assert detect_preset(project) == "project"
    assert isinstance(load_preset("project", project), ConfigPreset)


def test_detect_preset_project_local_uses_declared_name(tmp_path, presets_dir):
    project = tmp_path / "proj"
    _write(project / ".conductor" / "preset.toml", 'name = "demo"\n')
    assert detect_preset(project) == "demo"


def test_detect_preset_user_dir_markers(tmp_path, presets_dir):
    _write(presets_dir / "demo.toml", FULL_PRESET)
    project = tmp_path / "proj"
    (project / "backend").mkdir(parents=True)
    assert detect_preset(project) == "base"  # only one of two markers
    (project / "frontend").mkdir()
    assert detect_preset(project) == "demo"


def test_detect_preset_skips_broken_files(tmp_path, presets_dir):
    _write(presets_dir / "broken.toml", "not [ toml")
    _write(presets_dir / "demo.toml", FULL_PRESET)
    project = tmp_path / "proj"
    (project / "backend").mkdir(parents=True)
    (project / "frontend").mkdir()
    assert detect_preset(project) == "demo"


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


def test_base_has_default_policy_and_validation():
    preset = BasePreset()
    assert preset.validation.exec_wrapper == []
    assert preset.validation.smoke_command == []
    assert preset.manual_test.fatal_patterns == []
    assert preset.manual_test.check_commands == []


# ── ConfigPreset parsing ───────────────────────────────────────────────────────


@pytest.fixture
def demo(presets_dir) -> ConfigPreset:
    _write(presets_dir / "demo.toml", FULL_PRESET)
    preset = load_preset("demo")
    assert isinstance(preset, ConfigPreset)
    return preset


def test_config_section_maps_to_preset_config(demo):
    cfg = demo.config
    assert cfg.fixer_enabled is True
    assert cfg.sync_enabled is True
    assert cfg.sync_dump_regen == [("dumps/*.sql", "make dump-regen")]
    assert cfg.local_ci_command == "make ci"
    assert cfg.fix_model == "sonnet"
    assert cfg.phase_models == {2: "opus"}
    assert cfg.worktrees_base == str(Path.home() / "worktrees")
    # untouched fields keep their defaults
    assert cfg.max_gate_retries == 5


def test_detect_paths(demo):
    assert demo.detect_paths == ["backend", "frontend"]


def test_prompt_extra_substitutes_cwd(demo, tmp_path):
    assert demo.build_prompt_extra(tmp_path) == f"- Run checks from {tmp_path}/scripts"


def test_validation_settings(demo):
    v = demo.validation
    assert v.exec_wrapper == ["./scripts/env.sh", "exec"]
    assert v.smoke_up_command == ["./scripts/env.sh", "up"]
    assert v.smoke_command == ["./scripts/env.sh", "ci"]
    assert v.smoke_timeout == 120
    assert [c.name for c in v.checks] == ["typecheck"]
    assert v.checks[0].argv == ["npx", "tsc", "--noEmit"]
    assert v.checks[0].cwd == "frontend"


def test_manual_test_policy(demo):
    policy = demo.manual_test
    assert isinstance(policy, ManualTestPolicy)
    assert "admin UI" in policy.policy_text
    assert policy.coverage_focus == "Cover the admin settings pages."
    assert policy.blocked_reason_words == ["data", "fixture", "customer"]
    assert policy.fatal_patterns[0].pattern == r"\bshowNewFlow\b"
    assert policy.check_commands[0].path_prefix == "backend/"
    assert policy.check_commands[0].suffixes == [".php"]
    assert policy.check_commands[1].argv == ["make", "e2e", "{path}"]


def test_manual_test_rejects_invalid_regex(presets_dir):
    _write(presets_dir / "bad.toml", '[[manual_test.fatal_patterns]]\npattern = "("\n')
    with pytest.raises(ValueError):
        load_preset("bad")


def test_manual_test_rejects_rule_without_matcher(presets_dir):
    _write(presets_dir / "bad.toml", '[[manual_test.check_commands]]\ncommand = "make x"\n')
    with pytest.raises(ValueError, match="path_prefix"):
        load_preset("bad")


# ── ConfigPreset quality_gate ──────────────────────────────────────────────────


def _completed(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_quality_gate_all_pass(demo, tmp_path):
    with patch("conductor.core.presets.subprocess.run", return_value=_completed()) as run:
        result = demo.quality_gate(tmp_path)
    assert result.passed is True
    # "optional" is skipped because Makefile.optional does not exist
    assert run.call_count == 1
    assert run.call_args.args[0] == ["make", "lint"]


def test_quality_gate_runs_optional_check_when_required_file_exists(demo, tmp_path):
    (tmp_path / "Makefile.optional").write_text("")
    with patch("conductor.core.presets.subprocess.run", return_value=_completed()) as run:
        result = demo.quality_gate(tmp_path)
    assert result.passed is True
    assert run.call_count == 2


def test_quality_gate_nonzero_exit_fails(demo, tmp_path):
    with patch("conductor.core.presets.subprocess.run", return_value=_completed(1, stderr="boom")):
        result = demo.quality_gate(tmp_path)
    assert result.passed is False
    assert "lint" in result.message
    assert "boom" in result.failures[0]


def test_quality_gate_fail_pattern(demo, tmp_path):
    with patch("conductor.core.presets.subprocess.run", return_value=_completed(0, stdout="deny: errors")):
        result = demo.quality_gate(tmp_path)
    assert result.passed is False
    assert "lint" in result.message


def test_quality_gate_timeout(demo, tmp_path):
    with patch(
        "conductor.core.presets.subprocess.run",
        side_effect=subprocess.TimeoutExpired("cmd", 300),
    ):
        result = demo.quality_gate(tmp_path)
    assert result.passed is False
    assert "timed out" in result.message.lower()


def test_quality_gate_command_missing(demo, tmp_path):
    with patch("conductor.core.presets.subprocess.run", side_effect=FileNotFoundError):
        result = demo.quality_gate(tmp_path)
    assert result.passed is False
    assert "not found" in result.message


def test_quality_gate_passes_stdin(presets_dir, tmp_path):
    _write(
        presets_dir / "hook.toml",
        '[[quality_gate.checks]]\ncommand = "./hook.sh"\nstdin = "payload"\n',
    )
    preset = load_preset("hook")
    with patch("conductor.core.presets.subprocess.run", return_value=_completed()) as run:
        preset.quality_gate(tmp_path)
    assert run.call_args.kwargs["input"] == "payload"


# ── ConfigPreset preflight / teardown ──────────────────────────────────────────


def test_preflight_runs_commands(demo, tmp_path):
    with patch("shutil.which", return_value="/usr/bin/claude"):
        with patch("conductor.core.presets.subprocess.run", return_value=_completed()) as run:
            assert demo.preflight(tmp_path) is True
    assert run.call_args.args[0] == ["docker", "info"]


def test_preflight_command_missing(demo, tmp_path):
    with patch("shutil.which", return_value="/usr/bin/claude"):
        with patch("conductor.core.presets.subprocess.run", side_effect=FileNotFoundError):
            assert demo.preflight(tmp_path) is False


def test_preflight_claude_missing_short_circuits(demo, tmp_path):
    with patch("shutil.which", return_value=None):
        with patch("conductor.core.presets.subprocess.run") as run:
            assert demo.preflight(tmp_path) is False
    run.assert_not_called()


def test_teardown_commands(demo, tmp_path):
    with patch("conductor.core.presets.subprocess.run", return_value=_completed()) as run:
        demo.stage_teardown(tmp_path)
        demo.run_teardown(tmp_path)
    assert [c.args[0] for c in run.call_args_list] == [["make", "down"], ["make", "down", "--all"]]


def test_teardown_swallows_errors(demo, tmp_path):
    with patch("conductor.core.presets.subprocess.run", side_effect=FileNotFoundError):
        demo.stage_teardown(tmp_path)  # must not raise
