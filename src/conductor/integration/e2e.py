"""Integration E2E testing — generates and runs cross-feature tests.

The integration merge has Claude generate the specs once
(generate_integration_tests); the "integration-tests" validation check
runs them (run_integration_specs), so validate_and_fix retries execute the
same specs instead of regenerating them.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from collections.abc import Sequence
from pathlib import Path

from conductor.core.claude import run_claude
from conductor.core.models import ConductorState, ConflictRecord, E2ETestState, RunState
from conductor.core.smoke_test import playwright_unavailable

logger = logging.getLogger(__name__)

INTEGRATION_SPECS = (
    "tests/Playwright/tests/conductor-integration.spec.ts",
    "tests/Playwright/tests/conductor-api-integration.spec.ts",
)
_SPEC_TIMEOUT_S = 1800


async def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run git, return (exit_code, stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(cwd),
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


def _parse_test_results(stdout: str) -> tuple[int, int, int]:
    """Parse 'N passed, N failed, N skipped' from test runner output."""
    passed = failed = skipped = 0
    m = re.search(r"(\d+)\s+passed", stdout)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", stdout)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+)\s+skipped", stdout)
    if m:
        skipped = int(m.group(1))
    return passed, failed, skipped


async def generate_integration_tests(
    state: ConductorState,
    worktree_path: Path,
    runs: Sequence[RunState],
    conflicts_resolved: Sequence[ConflictRecord],
) -> bool:
    """Have Claude write cross-feature specs into the worktree, and commit them.

    *runs* and *conflicts_resolved* come from the merge in progress: only the
    runs actually on the branch, so no spec targets a feature that is not
    there. Skipped (False) when the worktree has no Playwright setup to run
    them. Returns True when at least one spec exists afterwards. Never raises
    on Claude failures.
    """
    reason = playwright_unavailable(worktree_path)
    if reason:
        logger.warning("Integration E2E generation skipped: %s", reason)
        return False

    # Build run context
    run_context_parts = []
    for run in runs:
        part = f"## Run {run.index}: {run.name}\n{run.description}"
        if run.constitution:
            part += "\nConstitution rules:\n" + "\n".join(
                f"- {c}" for c in run.constitution
            )
        run_context_parts.append(part)
    run_context = "\n\n".join(run_context_parts)

    # Build conflict context
    conflict_context = "\n".join(
        f"- {cr.file}: conflict between {cr.feature_a} and {cr.feature_b} ({cr.description})"
        for cr in conflicts_resolved
    )

    # Git diff --stat against base branch
    rc, diff_stat = await _git(["diff", "--stat", f"{state.base_branch}...HEAD"], worktree_path)
    if rc != 0:
        diff_stat = ""

    prompt = f"""You are in the integration worktree of a project where multiple features were developed in parallel and merged together. Your job is to write integration tests that verify the features work correctly TOGETHER — not individually.

## Features merged
{run_context}

## Files with merge conflicts (resolved — high interaction risk)
{conflict_context or "None"}

## Changes summary (git diff --stat)
{diff_stat or "Not available"}

## Instructions
1. Read the codebase to understand what each feature actually implemented
2. Identify interaction points between features:
   - Shared API endpoints modified by multiple runs
   - UI components that compose features from different runs
   - Data models touched by multiple runs
   - Event/notification chains crossing feature boundaries
3. Write Playwright tests for cross-feature UI flows
4. Write API tests (using fetch/HTTP requests) for cross-feature backend interactions
5. Put Playwright tests in: {INTEGRATION_SPECS[0]}
6. Put API tests in: {INTEGRATION_SPECS[1]}
7. Do NOT test features in isolation — only test interactions
8. Do NOT duplicate tests that exist in individual feature branches
9. Each test should have a clear name describing which features interact
10. Use BASE_URL from process.env.APP_URL or default to 'http://localhost:5173'
11. Use API_BASE from process.env.API_URL or default to 'http://localhost:8000'
"""

    try:
        await run_claude(
            prompt,
            model="claude-opus-4-8[1m]",
            max_turns=50,
            cwd=str(worktree_path),
        )
    except Exception:
        logger.exception("Claude test generation failed")
        return False

    specs = _existing_specs(worktree_path)
    if not specs:
        logger.warning("Integration E2E generation wrote no spec file")
        return False

    # Commit generated tests
    await _git(["add", "--", *specs], worktree_path)
    await _git(["commit", "-m", "Add conductor integration tests", "--no-verify"], worktree_path)
    return True


def _existing_specs(worktree_path: Path) -> list[str]:
    return [spec for spec in INTEGRATION_SPECS if (worktree_path / spec).is_file()]


async def run_integration_specs(worktree_path: Path) -> tuple[E2ETestState, str]:
    """Run the integration specs present in the worktree; never generates any.

    Returns the counts and a summary plus the tail of the runner's output. A
    spec run that exits non-zero without reporting a failed test (no tests
    found, webServer timeout, syntax error) counts as one failure.
    """
    specs = _existing_specs(worktree_path)
    if not specs:
        return E2ETestState(skipped=1), "skipped: no conductor integration specs"
    reason = playwright_unavailable(worktree_path)
    if reason:
        return E2ETestState(skipped=1), f"skipped: {reason}"

    total_passed = total_failed = total_skipped = 0
    outputs = []
    for spec in specs:
        try:
            # --no: never let npx download Playwright
            proc = await asyncio.create_subprocess_exec(
                "npx",
                "--no",
                "playwright",
                "test",
                spec,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(worktree_path),
            )
        except OSError as exc:
            total_failed += 1
            outputs.append(f"{spec}: cannot run npx: {exc}")
            continue
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), _SPEC_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            total_failed += 1
            outputs.append(f"{spec}: timed out after {_SPEC_TIMEOUT_S}s")
            continue
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        p, f, s = _parse_test_results(stdout_text)
        if proc.returncode != 0 and f == 0:
            f = 1
        total_passed += p
        total_failed += f
        total_skipped += s
        outputs.append(f"{spec} (exit {proc.returncode}):\n{stdout_text[-1500:]}")

    e2e = E2ETestState(
        passed=total_passed,
        failed=total_failed,
        skipped=total_skipped,
        last_run_at=datetime.now(timezone.utc),
    )
    summary = f"passed={e2e.passed}, failed={e2e.failed}, skipped={e2e.skipped}"
    return e2e, "\n".join([summary, *outputs])[-2000:]


async def run_integration_testing(
    state: ConductorState,
    storage,  # StorageResolver or FakeStorage
    worktree_path: Path,
) -> E2ETestState:
    """Generate the integration specs, then run them (both steps in one call).

    Returns E2ETestState — never raises on test failures.
    """
    conflicts = state.integration.conflicts_resolved if state.integration else []
    await generate_integration_tests(state, worktree_path, state.runs, conflicts)
    e2e, _ = await run_integration_specs(worktree_path)
    return e2e
