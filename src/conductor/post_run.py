"""Post-run processing pipeline: learnings -> merge -> e2e -> audit."""
from __future__ import annotations

import logging
from pathlib import Path

from conductor.core.claude import run_claude
from conductor.core.enums import IntegrationStatus
from conductor.core.models import ConductorState
from conductor.integration.e2e import run_integration_testing
from conductor.integration.merge import run_integration_merge

logger = logging.getLogger(__name__)


async def review_learnings(state: ConductorState, storage) -> str | None:
    """Collect learnings from all completed runs and review for CLAUDE.md updates."""
    learnings_parts = []
    for run in state.runs:
        feature_dir = Path(storage.repo_root) / "docs" / run.name
        learnings_file = feature_dir / "LEARNINGS.md"
        if learnings_file.exists():
            content = learnings_file.read_text(encoding="utf-8")
            if content.strip():
                learnings_parts.append(f"### Run: {run.name}\n{content}")

    if not learnings_parts:
        logger.info("No learnings found — skipping review")
        return None

    learnings_text = "\n\n".join(learnings_parts)

    prompt = (
        "Review the following learnings from completed runs.\n"
        "Determine if any should be added to CLAUDE.md.\n"
        "If no changes needed, respond with <<<NO_CHANGES>>>.\n\n"
        f"## Learnings\n\n{learnings_text}\n"
    )

    try:
        result = await run_claude(prompt, model="claude-opus-4-6", max_turns=1)
        return result.output
    except Exception:
        logger.exception("Learnings review failed")
        return None


async def generate_audit_report(state: ConductorState, storage) -> str | None:
    """Generate an overnight audit report summarizing the conductor run."""
    run_summaries = []
    for run in state.runs:
        run_summaries.append(
            f"- Run {run.index} ({run.name}): status={run.status}, "
            f"stages={len(run.stages)}"
        )

    integration_summary = "No integration merge performed"
    if state.integration:
        integration_summary = (
            f"Integration: status={state.integration.status}, "
            f"branch={state.integration.branch}, "
            f"merged_runs={state.integration.merged_runs}"
        )
        if state.integration.e2e:
            e2e = state.integration.e2e
            integration_summary += (
                f"\nE2E Tests: {e2e.passed} passed, {e2e.failed} failed, "
                f"{e2e.skipped} skipped"
            )

    prompt = (
        "Generate a concise overnight audit report for this conductor run.\n\n"
        f"## Project: {state.project_name}\n\n"
        f"## Runs\n" + "\n".join(run_summaries) + "\n\n"
        f"## Integration\n{integration_summary}\n"
    )

    try:
        result = await run_claude(prompt, model="claude-opus-4-6", max_turns=1)
        return result.output
    except Exception:
        logger.exception("Audit report generation failed")
        return None


async def post_run_processing(
    state: ConductorState,
    storage,
) -> ConductorState:
    """Run the full post-run pipeline: learnings -> merge -> e2e -> audit.

    E2E failures do not prevent audit from running.
    Returns updated ConductorState with integration results.
    """
    # 1. Learnings review
    await review_learnings(state, storage)

    # 2. Integration merge
    integration_state = await run_integration_merge(state, storage)
    state.integration = integration_state

    # 3. Integration E2E testing (only if merge succeeded)
    if integration_state.status == IntegrationStatus.DONE:
        try:
            e2e_state = await run_integration_testing(state, storage)
            state.integration.e2e = e2e_state
        except Exception:
            logger.exception("Integration E2E testing failed (non-fatal)")

    # 4. Audit report (always runs)
    await generate_audit_report(state, storage)

    return state
