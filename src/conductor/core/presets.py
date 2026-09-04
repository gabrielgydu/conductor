"""Preset system for conductor — quality gates, preflight, teardown, and config.

Presets describe project-specific tooling (CI commands, quality gates, container
teardown, manual-test policy) and live *outside* this repository as TOML files:

* ``<project>/.conductor/preset.toml`` — project-local preset, auto-detected
  whenever it exists.
* ``~/.conductor/presets/<name>.toml`` — user-level presets, selected with
  ``--preset <name>`` or auto-detected through their ``[detect] paths``.
  Override the directory with ``CONDUCTOR_PRESETS_DIR``.
* ``--preset /path/to/file.toml`` — an explicit file.

``base`` is the built-in no-op preset. See ``examples/preset.example.toml``
for the full schema.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, List, Optional

PROJECT_PRESET_FILE = Path(".conductor") / "preset.toml"
PRESETS_DIR_ENV = "CONDUCTOR_PRESETS_DIR"
_DEFAULT_PROJECT_PRESET_NAME = "project"


@dataclass
class GateResult:
    passed: bool
    message: str = ""
    failures: List[str] = field(default_factory=list)


@dataclass
class PresetConfig:
    fixer_enabled: bool = False
    fixer_async: bool = False
    sync_enabled: bool = False
    sync_base_branch: str = "master"
    sync_dump_regen: list[tuple[str, str]] = field(default_factory=list)  # [(glob_pattern, regen_command)]
    testing_mode: bool = False
    local_ci_enabled: bool = False
    local_ci_command: str = ""
    local_ci_full_command: str = ""
    local_ci_max_retries: int = 3
    local_review_enabled: bool = False
    local_review_command: str = ""
    local_review_full_command: str = ""
    local_review_max_retries: int = 2
    max_turns: int = 200
    max_iterations_per_phase: int = 8
    max_gate_retries: int = 5
    model: str = ""
    fix_model: str = ""
    phase_models: dict[int, str] = field(default_factory=dict)
    fixer_ci_poll_interval: int = 60
    fixer_ci_max_wait: int = 5400
    fixer_skip_patterns: str = "coverage|Coverage|codecov|Codecov"
    overnight_cap_hours: int = 8
    max_retries: int = 3
    worktrees_base: str = ""


@dataclass
class CommandSpec:
    """A single command run by a preset (quality gate, validation check, ...)."""

    name: str
    argv: list[str]
    cwd: str = ""  # relative to the working directory the command runs in
    timeout: int = 300
    requires: str = ""  # relative path that must exist, otherwise the command is skipped
    stdin: str = ""
    fail_pattern: str = ""  # regex; a match in the output marks the command failed


@dataclass
class ValidationSettings:
    """Preset hooks for the post-run validation pipeline (validation.py)."""

    exec_wrapper: list[str] = field(default_factory=list)  # prefix used to run tool checks inside the env
    smoke_up_command: list[str] = field(default_factory=list)  # optional: bring the environment up
    smoke_command: list[str] = field(default_factory=list)  # replaces the generated smoke test when set
    smoke_timeout: int = 1800
    checks: list[CommandSpec] = field(default_factory=list)  # extra named checks


@dataclass
class FatalPattern:
    pattern: str
    reason: str


@dataclass
class CheckCommandRule:
    """Maps changed files to a focused verification command (manual-test fixer)."""

    argv: list[str]  # "{path}" is replaced with the changed file
    path_prefix: str = ""
    suffixes: list[str] = field(default_factory=list)

    def matches(self, path: str) -> bool:
        if self.path_prefix and not path.startswith(self.path_prefix):
            return False
        if self.suffixes and not any(path.lower().endswith(s.lower()) for s in self.suffixes):
            return False
        return bool(self.path_prefix or self.suffixes)


@dataclass
class ManualTestPolicy:
    """Project-specific rules for manual-test mode."""

    policy_text: str = ""  # extra "Data Setup Policy" rules for the scenario prompt
    coverage_focus: str = ""  # extra guidance for the coverage-discovery prompt
    fatal_patterns: list[FatalPattern] = field(default_factory=list)
    check_commands: list[CheckCommandRule] = field(default_factory=list)
    blocked_reason_words: list[str] = field(default_factory=lambda: ["data", "fixture", "record", "account"])


class Preset(ABC):
    name: str = "base"
    config: PresetConfig
    validation: ValidationSettings
    manual_test: ManualTestPolicy

    @abstractmethod
    def quality_gate(self, cwd: Path) -> GateResult: ...

    @abstractmethod
    def preflight(self, cwd: Path) -> bool: ...

    @abstractmethod
    def build_prompt_extra(self, cwd: Path) -> str: ...

    @abstractmethod
    def stage_teardown(self, cwd: Path) -> None: ...

    @abstractmethod
    def run_teardown(self, cwd: Path) -> None: ...


class BasePreset(Preset):
    def __init__(self) -> None:
        self.name = "base"
        self.config = PresetConfig()
        self.validation = ValidationSettings()
        self.manual_test = ManualTestPolicy()

    def quality_gate(self, cwd: Path) -> GateResult:
        return GateResult(passed=True)

    def preflight(self, cwd: Path) -> bool:
        return shutil.which("claude") is not None

    def build_prompt_extra(self, cwd: Path) -> str:
        return ""

    def stage_teardown(self, cwd: Path) -> None:
        pass

    def run_teardown(self, cwd: Path) -> None:
        pass


# ---------------------------------------------------------------------------
# TOML-backed preset
# ---------------------------------------------------------------------------


def _argv(value: Any, *, where: str) -> list[str]:
    """Normalise a command given as a shell string or an argv list."""
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    raise ValueError(f"{where}: command must be a string or a list of strings")


def _command_list(value: Any, *, where: str) -> list[list[str]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_argv(value, where=where)]
    if isinstance(value, list):
        return [_argv(v, where=where) for v in value]
    raise ValueError(f"{where}: expected a command or a list of commands")


def _check_unknown(section: dict, allowed: set[str], *, where: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")


def _parse_command_spec(raw: dict, *, where: str, default_timeout: int = 300) -> CommandSpec:
    _check_unknown(raw, {"name", "command", "cwd", "timeout", "requires", "stdin", "fail_pattern"}, where=where)
    argv = _argv(raw.get("command"), where=where)
    if not argv:
        raise ValueError(f"{where}: 'command' is required")
    return CommandSpec(
        name=str(raw.get("name") or argv[0]),
        argv=argv,
        cwd=str(raw.get("cwd", "")),
        timeout=int(raw.get("timeout", default_timeout)),
        requires=str(raw.get("requires", "")),
        stdin=str(raw.get("stdin", "")),
        fail_pattern=str(raw.get("fail_pattern", "")),
    )


def _parse_config(raw: dict) -> PresetConfig:
    allowed = {f.name for f in fields(PresetConfig)}
    _check_unknown(raw, allowed, where="[config]")
    data: dict[str, Any] = dict(raw)
    if "sync_dump_regen" in data:
        entries = []
        for entry in data["sync_dump_regen"]:
            if not (isinstance(entry, list) and len(entry) == 2):
                raise ValueError("[config] sync_dump_regen entries must be [glob, command] pairs")
            entries.append((str(entry[0]), str(entry[1])))
        data["sync_dump_regen"] = entries
    if "phase_models" in data:
        data["phase_models"] = {int(k): str(v) for k, v in dict(data["phase_models"]).items()}
    if data.get("worktrees_base"):
        data["worktrees_base"] = str(Path(str(data["worktrees_base"])).expanduser())
    return PresetConfig(**data)


def _parse_validation(raw: dict) -> ValidationSettings:
    _check_unknown(
        raw,
        {"exec_wrapper", "smoke_up_command", "smoke_command", "smoke_timeout", "checks"},
        where="[validation]",
    )
    return ValidationSettings(
        exec_wrapper=_argv(raw.get("exec_wrapper"), where="[validation] exec_wrapper"),
        smoke_up_command=_argv(raw.get("smoke_up_command"), where="[validation] smoke_up_command"),
        smoke_command=_argv(raw.get("smoke_command"), where="[validation] smoke_command"),
        smoke_timeout=int(raw.get("smoke_timeout", 1800)),
        checks=[
            _parse_command_spec(c, where=f"[[validation.checks]] #{i}")
            for i, c in enumerate(raw.get("checks", []))
        ],
    )


def _parse_manual_test(raw: dict) -> ManualTestPolicy:
    _check_unknown(
        raw,
        {"policy_text", "coverage_focus", "fatal_patterns", "check_commands", "blocked_reason_words"},
        where="[manual_test]",
    )
    policy = ManualTestPolicy(
        policy_text=str(raw.get("policy_text", "")).strip(),
        coverage_focus=str(raw.get("coverage_focus", "")).strip(),
    )
    for i, item in enumerate(raw.get("fatal_patterns", [])):
        where = f"[[manual_test.fatal_patterns]] #{i}"
        _check_unknown(item, {"pattern", "reason"}, where=where)
        pattern = str(item.get("pattern", ""))
        if not pattern:
            raise ValueError(f"{where}: 'pattern' is required")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{where}: invalid regex {pattern!r}: {exc}") from exc
        policy.fatal_patterns.append(FatalPattern(pattern=pattern, reason=str(item.get("reason", "forbidden by preset policy"))))
    for i, item in enumerate(raw.get("check_commands", [])):
        where = f"[[manual_test.check_commands]] #{i}"
        _check_unknown(item, {"command", "path_prefix", "suffixes"}, where=where)
        rule = CheckCommandRule(
            argv=_argv(item.get("command"), where=where),
            path_prefix=str(item.get("path_prefix", "")),
            suffixes=[str(s) for s in item.get("suffixes", [])],
        )
        if not rule.argv:
            raise ValueError(f"{where}: 'command' is required")
        if not rule.path_prefix and not rule.suffixes:
            raise ValueError(f"{where}: needs 'path_prefix' and/or 'suffixes'")
        policy.check_commands.append(rule)
    if "blocked_reason_words" in raw:
        policy.blocked_reason_words = [str(w) for w in raw["blocked_reason_words"]]
    return policy


_TOP_LEVEL_KEYS = {
    "name", "detect", "config", "preflight", "quality_gate", "prompt", "teardown", "validation", "manual_test",
}


class ConfigPreset(BasePreset):
    """Preset defined by a TOML file."""

    def __init__(self, data: dict, *, name: str = "", source: Path | None = None) -> None:
        super().__init__()
        _check_unknown(data, _TOP_LEVEL_KEYS, where="preset")
        self.source = source
        self.name = str(data.get("name") or name or (source.stem if source else "preset"))

        detect = data.get("detect", {})
        _check_unknown(detect, {"paths"}, where="[detect]")
        self.detect_paths: list[str] = [str(p) for p in detect.get("paths", [])]

        self.config = _parse_config(data.get("config", {}))

        preflight = data.get("preflight", {})
        _check_unknown(preflight, {"commands", "timeout"}, where="[preflight]")
        self._preflight_commands = _command_list(preflight.get("commands"), where="[preflight] commands")
        self._preflight_timeout = int(preflight.get("timeout", 30))

        gate = data.get("quality_gate", {})
        _check_unknown(gate, {"checks"}, where="[quality_gate]")
        self._gate_checks = [
            _parse_command_spec(c, where=f"[[quality_gate.checks]] #{i}")
            for i, c in enumerate(gate.get("checks", []))
        ]

        prompt = data.get("prompt", {})
        _check_unknown(prompt, {"extra"}, where="[prompt]")
        self._prompt_extra = str(prompt.get("extra", ""))

        teardown = data.get("teardown", {})
        _check_unknown(teardown, {"stage", "run", "timeout"}, where="[teardown]")
        self._stage_teardown = _command_list(teardown.get("stage"), where="[teardown] stage")
        self._run_teardown = _command_list(teardown.get("run"), where="[teardown] run")
        self._teardown_timeout = int(teardown.get("timeout", 60))

        self.validation = _parse_validation(data.get("validation", {}))
        self.manual_test = _parse_manual_test(data.get("manual_test", {}))

    @classmethod
    def from_file(cls, path: Path, *, name: str = "") -> "ConfigPreset":
        path = Path(path)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid preset file {path}: {exc}") from exc
        try:
            return cls(data, name=name, source=path)
        except ValueError as exc:
            raise ValueError(f"Invalid preset file {path}: {exc}") from exc

    def matches(self, project_dir: Path) -> bool:
        """True when all ``[detect] paths`` exist under *project_dir*."""
        return bool(self.detect_paths) and all((project_dir / p).exists() for p in self.detect_paths)

    # -- hooks --------------------------------------------------------------

    def quality_gate(self, cwd: Path) -> GateResult:
        failures: list[str] = []
        for check in self._gate_checks:
            outcome = run_command_spec(check, cwd)
            if outcome is not None:
                failures.append(outcome)
        if failures:
            return GateResult(passed=False, message=failures[0], failures=failures)
        return GateResult(passed=True)

    def preflight(self, cwd: Path) -> bool:
        if not super().preflight(cwd):
            return False
        for argv in self._preflight_commands:
            try:
                subprocess.run(argv, capture_output=True, timeout=self._preflight_timeout, check=True, cwd=cwd)
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return False
        return True

    def build_prompt_extra(self, cwd: Path) -> str:
        return self._prompt_extra.replace("{cwd}", str(cwd))

    def stage_teardown(self, cwd: Path) -> None:
        _run_ignoring_errors(self._stage_teardown, cwd, self._teardown_timeout)

    def run_teardown(self, cwd: Path) -> None:
        _run_ignoring_errors(self._run_teardown, cwd, self._teardown_timeout)


def run_command_spec(check: CommandSpec, cwd: Path) -> str | None:
    """Run *check* under *cwd*. Returns None on success (or skip), else a failure message."""
    if check.requires and not (cwd / check.requires).exists():
        return None
    run_cwd = cwd / check.cwd if check.cwd else cwd
    try:
        result = subprocess.run(
            check.argv,
            capture_output=True,
            text=True,
            cwd=run_cwd,
            timeout=check.timeout,
            input=check.stdin or None,
        )
    except subprocess.TimeoutExpired:
        return f"{check.name}: timed out after {check.timeout}s"
    except FileNotFoundError:
        return f"{check.name}: command not found ({check.argv[0]})"
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return f"{check.name}: exit {result.returncode}\n{output.strip()}"
    if check.fail_pattern and re.search(check.fail_pattern, output):
        return f"{check.name}: failed\n{output.strip()}"
    return None


def _run_ignoring_errors(commands: list[list[str]], cwd: Path, timeout: int) -> None:
    for argv in commands:
        try:
            subprocess.run(argv, cwd=cwd, timeout=timeout)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def presets_dir() -> Path:
    override = os.environ.get(PRESETS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".conductor" / "presets"


def _project_preset_file(project_dir: Path | str | None) -> Path | None:
    if project_dir is None:
        return None
    candidate = Path(project_dir) / PROJECT_PRESET_FILE
    return candidate if candidate.is_file() else None


def _user_presets() -> list[Path]:
    directory = presets_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.toml"))


def detect_preset(project_dir: Path | str) -> str:
    """Return the preset name for *project_dir*.

    Order: project-local ``.conductor/preset.toml``, then the first user-level
    preset whose ``[detect] paths`` all exist, else ``"base"``.
    """
    project_dir = Path(project_dir)
    local = _project_preset_file(project_dir)
    if local is not None:
        return ConfigPreset.from_file(local, name=_DEFAULT_PROJECT_PRESET_NAME).name
    for path in _user_presets():
        try:
            preset = ConfigPreset.from_file(path, name=path.stem)
        except ValueError:
            continue
        if preset.matches(project_dir):
            return preset.name
    return "base"


def load_preset(name: Optional[str], project_dir: Path | str | None = None) -> Preset:
    """Resolve a preset by name.

    ``name`` may be empty/``"base"`` (built-in), a path to a ``.toml`` file, the
    name of the project-local preset, or the stem of a user-level preset file.
    """
    if name is None or name in ("", "base"):
        return BasePreset()

    explicit = Path(name).expanduser()
    if explicit.suffix == ".toml" and explicit.is_file():
        return ConfigPreset.from_file(explicit)

    local = _project_preset_file(project_dir)
    if local is not None:
        preset = ConfigPreset.from_file(local, name=_DEFAULT_PROJECT_PRESET_NAME)
        if preset.name == name:
            return preset

    candidate = presets_dir() / f"{name}.toml"
    if candidate.is_file():
        return ConfigPreset.from_file(candidate, name=name)

    raise ValueError(f"Unknown preset: {name!r} (no {candidate}, no matching {PROJECT_PRESET_FILE})")
