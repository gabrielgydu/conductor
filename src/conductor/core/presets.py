"""Preset system for conductor — quality gates, preflight, and config."""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class GateResult:
    passed: bool
    message: str = ""
    failures: List[str] = field(default_factory=list)


@dataclass
class PresetConfig:
    push_enabled: bool = False
    push_remote: str = "origin"
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


class Preset(ABC):
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
        self.config = PresetConfig()

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


class AcmePreset(BasePreset):
    def __init__(self) -> None:
        self.config = PresetConfig(
            push_enabled=True,
            fixer_enabled=True,
            sync_enabled=True,
            sync_dump_regen=[
                ("test-dumps/partner.sql", "./scripts/worktree-env.sh dump-regen partner"),
                ("test-dumps/shop.sql", "./scripts/worktree-env.sh dump-regen shop"),
                ("test-dumps/app.sql", "./scripts/worktree-env.sh dump-regen app"),
                ("test-dumps/fleet.sql", "./scripts/worktree-env.sh dump-regen fleet"),
            ],
            local_ci_enabled=True,
            local_ci_command="./scripts/worktree-env.sh ci auto",
            local_ci_full_command="./scripts/worktree-env.sh ci auto",
            local_review_enabled=True,
            local_review_command="./scripts/local-review.sh",
            local_review_full_command="./scripts/local-review.sh --full",
            fix_model="sonnet",
            worktrees_base=str(Path.home() / "acme" / "acme-bare"),
        )

    def quality_gate(self, cwd: Path) -> GateResult:
        hook = cwd / ".claude/hooks/pre-commit-phpstan.sh"
        payload = '{"tool_input":{"command":"git commit -m phase"}}'
        try:
            result = subprocess.run(
                [str(hook)],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=300,
                input=payload,
            )
        except subprocess.TimeoutExpired:
            return GateResult(passed=False, message="PHPStan timed out")
        except FileNotFoundError:
            return GateResult(passed=False, message="PHPStan hook not found")

        if "deny" in result.stdout:
            return GateResult(passed=False, message="PHPStan failed", failures=[result.stdout])
        return GateResult(passed=True)

    def preflight(self, cwd: Path) -> bool:
        if not super().preflight(cwd):
            return False
        try:
            subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        return True

    def build_prompt_extra(self, cwd: Path) -> str:
        return (
            f"- PARTNER PHPStan: {cwd}/scripts/worktree-env.sh phpstan partner\n"
            f"- APP PHPStan: {cwd}/scripts/worktree-env.sh phpstan app\n"
            f"- Playwright tests: {cwd}/scripts/worktree-env.sh playwright [args]\n"
            f"  NEVER run npx playwright or cd into app/tests/Playwright to run tests directly.\n"
            f"  ALL Playwright tests MUST run via ./scripts/worktree-env.sh playwright which executes inside Docker.\n"
            f"  Examples: ./scripts/worktree-env.sh playwright --grep 'Non-destructive'\n"
            f"            ./scripts/worktree-env.sh playwright tests/05-basket.spec.ts\n"
        )

    def stage_teardown(self, cwd: Path) -> None:
        try:
            subprocess.run(
                ["./scripts/worktree-env.sh", "down"],
                cwd=cwd,
                timeout=60,
            )
        except Exception:
            pass

    def run_teardown(self, cwd: Path) -> None:
        try:
            subprocess.run(
                ["./scripts/worktree-env.sh", "down"],
                cwd=cwd,
                timeout=60,
            )
        except Exception:
            pass


class NodeappPreset(BasePreset):
    def __init__(self) -> None:
        self.config = PresetConfig(
            push_enabled=False,
            fixer_enabled=False,
            fix_model="sonnet",
        )

    def quality_gate(self, cwd: Path) -> GateResult:
        packages = ["shared", "backend", "api", "frontend"]
        failures: List[str] = []

        for pkg in packages:
            pkg_dir = cwd / "packages" / pkg
            if not pkg_dir.is_dir():
                continue

            # tsc check
            try:
                r = subprocess.run(
                    ["npx", "tsc", "--noEmit"],
                    cwd=pkg_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if r.returncode != 0:
                    msg = r.stderr if r.stderr else f"tsc exited {r.returncode}"
                    failures.append(f"{pkg}/tsc: {msg}")
            except subprocess.TimeoutExpired:
                failures.append(f"{pkg}/tsc: timed out")

            # eslint check if config exists
            eslint_configs = list(pkg_dir.glob(".eslintrc*"))
            if eslint_configs:
                try:
                    r = subprocess.run(
                        ["npx", "eslint", "."],
                        cwd=pkg_dir,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if r.returncode != 0:
                        msg = r.stderr if r.stderr else f"eslint exited {r.returncode}"
                        failures.append(f"{pkg}/eslint: {msg}")
                except subprocess.TimeoutExpired:
                    failures.append(f"{pkg}/eslint: timed out")

            # pnpm test if script exists
            pkg_json_path = pkg_dir / "package.json"
            if pkg_json_path.exists():
                import json
                try:
                    pkg_json = json.loads(pkg_json_path.read_text())
                    if "test" in pkg_json.get("scripts", {}):
                        r = subprocess.run(
                            ["pnpm", "test"],
                            cwd=pkg_dir,
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if r.returncode != 0:
                            msg = r.stderr if r.stderr else f"pnpm test exited {r.returncode}"
                            failures.append(f"{pkg}/test: {msg}")
                except (json.JSONDecodeError, subprocess.TimeoutExpired):
                    failures.append(f"{pkg}/test: error reading package.json or timed out")

        return GateResult(passed=len(failures) == 0, failures=failures)


def detect_preset(project_dir: Path) -> str:
    """Auto-detect preset from project directory markers."""
    # Acme: has app/ and partner/ directories
    if (project_dir / "app").is_dir() and (project_dir / "partner").is_dir():
        return "acme"
    # Nodeapp: has packages/ with shared/backend/frontend
    if (project_dir / "packages" / "shared").is_dir():
        return "nodeapp"
    return "base"


def load_preset(name: Optional[str]) -> Preset:
    if name is None or name in ("", "base"):
        return BasePreset()
    if name == "acme":
        return AcmePreset()
    if name == "nodeapp":
        return NodeappPreset()
    raise ValueError(f"Unknown preset: {name!r}")
