"""Central validation module for conductor orchestrator.

Provides structured validation checks that run after runner completion,
after run completion, and after integration merge.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from conductor.core.claude import run_claude
from conductor.core.presets import CommandSpec, ValidationSettings, load_preset

if TYPE_CHECKING:
    from conductor.core.models import ConductorState

logger = logging.getLogger(__name__)

# Built-in check names in canonical order. Preset-defined checks run between
# the tool checks and "smoke" (see _check_order).
_CHECK_ORDER = ["frontend-build", "phpstan", "phpunit", "smoke", "integration-tests"]


@dataclass
class ValidationContext:
    project_dir: Path
    stage: str  # "post-runner" | "post-run" | "integration"
    feature_name: str
    state: ConductorState | None = None


@dataclass
class CheckResult:
    name: str  # "frontend-build", "phpstan", "phpunit", "smoke", "integration-tests"
    passed: bool
    output: str  # last 2000 chars
    duration_s: float


@dataclass
class ValidationResult:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Low-level subprocess helper
# ---------------------------------------------------------------------------


async def _run_cmd(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run command, return (exit_code, combined_output_last_2000_chars)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"Command timed out after {timeout}s"
    combined = stdout.decode("utf-8", errors="replace") + stderr.decode(
        "utf-8", errors="replace"
    )
    return proc.returncode or 0, combined[-2000:]


# ---------------------------------------------------------------------------
# Individual check implementations
# ---------------------------------------------------------------------------


def _preset_validation(ctx: ValidationContext) -> ValidationSettings:
    """Validation hooks from the project's preset (base defaults when unavailable)."""
    name = ctx.state.preset if ctx.state else None
    try:
        return load_preset(name, ctx.project_dir).validation
    except ValueError as exc:
        logger.warning("Preset %r not loadable (%s) — using base validation", name, exc)
        return ValidationSettings()


async def _check_frontend_build(ctx: ValidationContext) -> CheckResult:
    start = time.monotonic()
    settings = _preset_validation(ctx)
    if settings.exec_wrapper:
        # The preset's wrapper runs the shell command inside its environment.
        cmd = settings.exec_wrapper + ["cd frontend && npm run build"]
        cwd = ctx.project_dir
    else:
        cmd = ["npm", "run", "build"]
        cwd = ctx.project_dir / "frontend"
    exit_code, output = await _run_cmd(cmd, cwd=cwd, timeout=300)
    return CheckResult(
        name="frontend-build",
        passed=exit_code == 0,
        output=output,
        duration_s=time.monotonic() - start,
    )


async def _check_phpstan(ctx: ValidationContext) -> CheckResult:
    start = time.monotonic()
    settings = _preset_validation(ctx)
    if settings.exec_wrapper:
        cmd = settings.exec_wrapper + ["vendor/bin/phpstan analyse --memory-limit=512M"]
    else:
        cmd = ["vendor/bin/phpstan", "analyse", "--memory-limit=512M"]
    exit_code, output = await _run_cmd(cmd, cwd=ctx.project_dir, timeout=300)
    return CheckResult(
        name="phpstan",
        passed=exit_code == 0,
        output=output,
        duration_s=time.monotonic() - start,
    )


async def _check_phpunit(ctx: ValidationContext) -> CheckResult:
    start = time.monotonic()
    settings = _preset_validation(ctx)
    if settings.exec_wrapper:
        cmd = settings.exec_wrapper + ["vendor/bin/phpunit"]
    else:
        cmd = ["vendor/bin/phpunit"]

    exit_code, output = await _run_cmd(cmd, cwd=ctx.project_dir, timeout=600)
    return CheckResult(
        name="phpunit",
        passed=exit_code == 0,
        output=output,
        duration_s=time.monotonic() - start,
    )


async def _check_smoke(ctx: ValidationContext) -> CheckResult:
    start = time.monotonic()

    # Preset-defined smoke: optionally bring the environment up, then run its CI.
    settings = _preset_validation(ctx)
    if settings.smoke_command:
        if settings.smoke_up_command:
            up_text = " ".join(settings.smoke_up_command)
            logger.warning("Smoke check: %s (%s)", up_text, ctx.project_dir)
            print(f"  smoke: {up_text} ...", flush=True)
            exit_code, output = await _run_cmd(
                settings.smoke_up_command, cwd=ctx.project_dir, timeout=settings.smoke_timeout,
            )
            if exit_code != 0:
                print(f"  smoke: {up_text} FAILED (exit {exit_code})", flush=True)
                return CheckResult(
                    name="smoke",
                    passed=False,
                    output=f"{up_text} failed:\n{output}",
                    duration_s=time.monotonic() - start,
                )
        ci_text = " ".join(settings.smoke_command)
        logger.warning("Smoke check: %s", ci_text)
        print(f"  smoke: {ci_text} ...", flush=True)
        exit_code, output = await _run_cmd(
            settings.smoke_command, cwd=ctx.project_dir, timeout=settings.smoke_timeout,
        )
        print(f"  smoke: {'PASSED' if exit_code == 0 else 'FAILED'} (exit {exit_code})", flush=True)
        return CheckResult(
            name="smoke",
            passed=exit_code == 0,
            output=output,
            duration_s=time.monotonic() - start,
        )

    # Generic fallback: generate and run Playwright smoke test
    from conductor.core.smoke_test import generate_smoke_test  # noqa: PLC0415

    smoke_src = generate_smoke_test(ctx.project_dir)
    spec_path = (
        ctx.project_dir / "tests" / "Playwright" / "tests" / "conductor-smoke.spec.ts"
    )
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(smoke_src, encoding="utf-8")

    exit_code, output = await _run_cmd(
        ["npx", "playwright", "test", str(spec_path)],
        cwd=ctx.project_dir,
        timeout=300,
    )
    return CheckResult(
        name="smoke",
        passed=exit_code == 0,
        output=output,
        duration_s=time.monotonic() - start,
    )


async def _check_integration_tests(ctx: ValidationContext) -> CheckResult:
    start = time.monotonic()

    # Lazy import to avoid circular imports
    from conductor.integration.e2e import run_integration_testing  # noqa: PLC0415

    if ctx.state is None:
        return CheckResult(
            name="integration-tests",
            passed=False,
            output="ValidationContext.state is required for integration-tests check",
            duration_s=time.monotonic() - start,
        )

    # run_integration_testing expects a storage object with a .repo_root attribute.
    # Adapt ctx.project_dir into that interface.
    class _StorageAdapter:
        def __init__(self, path: Path) -> None:
            self.repo_root = str(path)

    e2e_state = await run_integration_testing(
        ctx.state, _StorageAdapter(ctx.project_dir), ctx.project_dir
    )

    passed = e2e_state.failed == 0
    summary_parts = [
        f"passed={e2e_state.passed}",
        f"failed={e2e_state.failed}",
        f"skipped={e2e_state.skipped}",
    ]
    output = ", ".join(summary_parts)
    return CheckResult(
        name="integration-tests",
        passed=passed,
        output=output,
        duration_s=time.monotonic() - start,
    )


async def _run_preset_check(ctx: ValidationContext, spec: CommandSpec) -> CheckResult:
    """Run one ``[[validation.checks]]`` entry from the preset."""
    start = time.monotonic()
    if spec.requires and not (ctx.project_dir / spec.requires).exists():
        return CheckResult(
            name=spec.name,
            passed=True,
            output=f"skipped: {spec.requires} not present",
            duration_s=0.0,
        )
    cwd = ctx.project_dir / spec.cwd if spec.cwd else ctx.project_dir
    exit_code, output = await _run_cmd(spec.argv, cwd=cwd, timeout=spec.timeout)
    passed = exit_code == 0 and not (spec.fail_pattern and re.search(spec.fail_pattern, output))
    return CheckResult(
        name=spec.name,
        passed=passed,
        output=output,
        duration_s=time.monotonic() - start,
    )


def _check_order(settings: ValidationSettings) -> list[str]:
    preset_names = [c.name for c in settings.checks]
    return _CHECK_ORDER[:3] + preset_names + _CHECK_ORDER[3:]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_CHECK_DISPATCH = {
    "frontend-build": _check_frontend_build,
    "phpstan": _check_phpstan,
    "phpunit": _check_phpunit,
    "smoke": _check_smoke,
    "integration-tests": _check_integration_tests,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def detect_checks(ctx: ValidationContext) -> list[str]:
    """Return available check names by probing ctx.project_dir."""
    checks: list[str] = []

    if (ctx.project_dir / "frontend" / "package.json").exists():
        checks.append("frontend-build")

    if (ctx.project_dir / "vendor" / "bin" / "phpstan").exists():
        checks.append("phpstan")

    if (ctx.project_dir / "vendor" / "bin" / "phpunit").exists():
        checks.append("phpunit")

    checks.extend(c.name for c in _preset_validation(ctx).checks)

    if ctx.stage == "integration":
        checks.append("smoke")
        checks.append("integration-tests")

    return checks


async def run_check(name: str, ctx: ValidationContext) -> CheckResult:
    """Dispatch to the appropriate check function and time it."""
    fn = _CHECK_DISPATCH.get(name)
    if fn is None:
        spec = next((c for c in _preset_validation(ctx).checks if c.name == name), None)
        if spec is not None:
            logger.info("Running preset check: %s (stage=%s)", name, ctx.stage)
            return await _run_preset_check(ctx, spec)
        return CheckResult(
            name=name,
            passed=False,
            output=f"Unknown check: {name}",
            duration_s=0.0,
        )
    logger.info(
        "Running check: %s (stage=%s, feature=%s)", name, ctx.stage, ctx.feature_name
    )
    result = await fn(ctx)
    status = "PASS" if result.passed else "FAIL"
    logger.info("Check %s: %s (%.1fs)", name, status, result.duration_s)
    return result


async def run_validation(
    ctx: ValidationContext,
    checks: list[str] | None = None,
) -> ValidationResult:
    """Run all requested checks sequentially and return aggregate result.

    If *checks* is None, detect_checks() is called automatically.
    Checks run in canonical order: frontend-build, phpstan, phpunit, preset
    checks, smoke, integration-tests.
    """
    if checks is None:
        checks = await detect_checks(ctx)

    # Sort by canonical order (preset checks before smoke); unknown names go to the end.
    order = _check_order(_preset_validation(ctx))
    ordered = sorted(
        checks,
        key=lambda n: order.index(n) if n in order else len(order),
    )

    results: list[CheckResult] = []
    for name in ordered:
        result = await run_check(name, ctx)
        results.append(result)

    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    failures = [r.name for r in results if not r.passed]
    all_passed = passed_count == total

    if failures:
        summary = f"{passed_count}/{total} checks passed. Failed: {', '.join(failures)}"
    else:
        summary = f"{passed_count}/{total} checks passed"

    return ValidationResult(passed=all_passed, checks=results, summary=summary)


async def validate_and_fix(
    ctx: ValidationContext,
    max_attempts: int = 3,
) -> ValidationResult:
    """Run validation, and if it fails, ask Claude to fix the code, then retry.

    Loops up to *max_attempts* times. Returns the final ValidationResult.
    """
    result = await run_validation(ctx)

    for attempt in range(1, max_attempts + 1):
        if result.passed:
            logger.info(
                "Validation passed on attempt %d/%d: %s",
                attempt,
                max_attempts,
                result.summary,
            )
            return result

        logger.warning(
            "Validation failed on attempt %d/%d: %s",
            attempt,
            max_attempts,
            result.summary,
        )

        if attempt == max_attempts:
            break

        prompt = (
            "The following validation checks failed. Fix the code.\n"
            "Do NOT modify test files unless the test expectations are clearly wrong.\n\n"
            "## Failed checks\n"
        )
        for check in result.checks:
            if not check.passed:
                prompt += f"\n### {check.name}\n```\n{check.output}\n```\n"
        prompt += (
            f"\n## Context\n"
            f"Project: {ctx.feature_name}\n"
            f"Stage: {ctx.stage}\n"
            f"Attempt: {attempt}/{max_attempts}\n"
        )

        logger.info(
            "Asking Claude to fix failures (attempt %d/%d)…", attempt, max_attempts
        )
        await run_claude(
            prompt,
            model="claude-opus-4-8[1m]",
            max_turns=30,
            cwd=str(ctx.project_dir),
        )

        result = await run_validation(ctx)

    return result
