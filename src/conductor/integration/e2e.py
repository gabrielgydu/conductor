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
) -> E2ETestState:
    """Generate and run E2E tests on the integration branch.

    Returns E2ETestState — never raises on test failures.
    """
    repo_root = Path(storage.repo_root)

    # Must have a completed integration merge
    if state.integration is None or state.integration.status != IntegrationStatus.DONE:
        return E2ETestState(skipped=1)

    branch = state.integration.branch

    # Check for test framework
    if not _has_test_framework(repo_root, branch):
        logger.info("No E2E test framework config found on %s — skipping", branch)
        return E2ETestState(skipped=1)

    # Build context for test generation
    run_descriptions = "\n".join(
        f"- Run {r.index}: {r.name} — {r.description}"
        for r in state.runs
    )

    prompt = (
        "Generate E2E integration tests that exercise cross-feature interactions.\n\n"
        f"## Integration Branch\n{branch}\n\n"
        f"## Runs Merged\n{run_descriptions}\n\n"
        "## Instructions\n"
        "- Focus on interactions BETWEEN features from different runs\n"
        "- Write Playwright tests\n"
        "- Do not duplicate tests that exist in individual feature branches\n"
        "- Output test files directly\n"
    )

    try:
        await run_claude(
            prompt,
            model="claude-opus-4-6",
            max_turns=50,
            cwd=str(repo_root),
        )
    except Exception:
        logger.exception("Claude test generation failed")
        return E2ETestState(skipped=1)

    # Run the tests
    try:
        proc = await asyncio.create_subprocess_exec(
            "npx", "playwright", "test",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(repo_root),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")

        passed, failed, skipped = _parse_test_results(stdout_text)

        return E2ETestState(
            passed=passed,
            failed=failed,
            skipped=skipped,
            last_run_at=datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("E2E test execution failed")
        return E2ETestState(skipped=1)
