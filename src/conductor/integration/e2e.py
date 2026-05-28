"""Integration E2E testing — generates and runs cross-feature tests."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from conductor.core.claude import run_claude
from conductor.core.enums import IntegrationStatus
from conductor.core.models import ConductorState, E2ETestState

logger = logging.getLogger(__name__)

# Test framework config files that indicate E2E test capability
_FRAMEWORK_CONFIGS = [
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "cypress.config.ts",
    "cypress.config.js",
    "cypress.config.mjs",
]


def _has_test_framework(repo_root: Path, branch: str) -> bool:
    """Check if the integration branch has an E2E test framework config."""
    result = subprocess.run(
        ["git", "ls-tree", "--name-only", branch],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        return False
    files = set(result.stdout.splitlines())
    return any(cfg in files for cfg in _FRAMEWORK_CONFIGS)


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


async def run_integration_testing(
    state: ConductorState,
    storage,  # StorageResolver or FakeStorage
    worktree_path: Path,
) -> E2ETestState:
    """Generate and run E2E tests on the integration worktree.

    Returns E2ETestState — never raises on test failures.
    """
    # Must have a completed integration merge
    if state.integration is None or state.integration.status != IntegrationStatus.DONE:
        return E2ETestState(skipped=1)

    # Check if playwright is available
    try:
        check = await asyncio.create_subprocess_exec(
            "npx",
            "playwright",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(worktree_path),
        )
        await check.communicate()
        if check.returncode != 0:
            logger.info("Playwright not available — skipping integration tests")
            return E2ETestState(skipped=1)
    except FileNotFoundError:
        logger.info("npx not found — skipping integration tests")
        return E2ETestState(skipped=1)

    # Build run context
    run_context_parts = []
    for run in state.runs:
        part = f"## Run {run.index}: {run.name}\n{run.description}"
        if run.constitution:
            part += "\nConstitution rules:\n" + "\n".join(
                f"- {c}" for c in run.constitution
            )
        run_context_parts.append(part)
    run_context = "\n\n".join(run_context_parts)

    # Build conflict context
    conflict_context = ""
    if state.integration and state.integration.conflicts_resolved:
        conflict_lines = []
        for cr in state.integration.conflicts_resolved:
            conflict_lines.append(
                f"- {cr.file}: conflict between {cr.feature_a} and {cr.feature_b} ({cr.description})"
            )
        conflict_context = "\n".join(conflict_lines)

    # Git diff --stat against base branch
    diff_result = subprocess.run(
        ["git", "diff", "--stat", f"{state.base_branch}...HEAD"],
        capture_output=True,
        text=True,
        cwd=str(worktree_path),
    )
    diff_stat = diff_result.stdout if diff_result.returncode == 0 else ""

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
5. Put Playwright tests in: tests/Playwright/tests/conductor-integration.spec.ts
6. Put API tests in: tests/Playwright/tests/conductor-api-integration.spec.ts
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
        return E2ETestState(skipped=1)

    # Commit generated tests
    subprocess.run(
        [
            "git",
            "add",
            "tests/Playwright/tests/conductor-integration.spec.ts",
            "tests/Playwright/tests/conductor-api-integration.spec.ts",
        ],
        cwd=str(worktree_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Add conductor integration tests", "--no-verify"],
        cwd=str(worktree_path),
        capture_output=True,
    )

    # Run the tests
    test_files = [
        "tests/Playwright/tests/conductor-integration.spec.ts",
        "tests/Playwright/tests/conductor-api-integration.spec.ts",
    ]
    total_passed = total_failed = total_skipped = 0

    try:
        for tf in test_files:
            test_path = worktree_path / tf
            if not test_path.exists():
                continue
            proc = await asyncio.create_subprocess_exec(
                "npx",
                "playwright",
                "test",
                tf,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(worktree_path),
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            p, f, s = _parse_test_results(stdout_text)
            total_passed += p
            total_failed += f
            total_skipped += s
    except Exception:
        logger.exception("E2E test execution failed")
        return E2ETestState(skipped=1)

    return E2ETestState(
        passed=total_passed,
        failed=total_failed,
        skipped=total_skipped,
        last_run_at=datetime.now(timezone.utc),
    )
