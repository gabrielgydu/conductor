"""Prompt construction for the runner — port of _build_prompt() from runner.sh."""
from __future__ import annotations

from pathlib import Path


def build_prompt(
    prompt_file: Path,
    promise_token: str,
    phase_num: int,
    phase_count: int,
    project_dir: Path,
    feature_name: str,
    fix_context: str = "",
    learnings_file: Path | None = None,
    prompt_extra: str = "",
) -> str:
    """Build the full prompt string to send to Claude for a phase iteration.

    Args:
        prompt_file: Path to the plan/spec file for this phase.
        promise_token: The token Claude must emit to signal completion.
        phase_num: 1-based current phase number.
        phase_count: Total number of phases.
        project_dir: Root directory of the project being built.
        feature_name: Feature name (used for learnings path).
        fix_context: If non-empty, prepend a QUALITY GATE FAILED block.
        learnings_file: Path to LEARNINGS.md; injected if it exists and is non-empty.
        prompt_extra: Extra context injected from the preset's build_prompt_extra().
    """
    fix_block = ""
    if fix_context:
        fix_block = f"""
QUALITY GATE FAILED — FIX BEFORE PROCEEDING
The previous iteration claimed completion but failed the checks below.
You MUST fix these errors before outputting the promise token again.
DO NOT output <promise>{promise_token}</promise> until all tests pass and linter is clean.

{fix_context}

Fix all the above errors, then re-run the checks yourself to verify.
"""

    learnings_block = ""
    if learnings_file is not None and learnings_file.exists() and learnings_file.stat().st_size > 0:
        learnings_block = f"""
LEARNINGS FROM PREVIOUS ITERATIONS:
The following gotchas, tips, and solutions were discovered in earlier iterations.
Read them BEFORE starting work to avoid repeating the same mistakes.

{learnings_file.read_text("utf-8")}

END OF LEARNINGS
"""

    prev_phases = phase_num - 1
    plan_content = prompt_file.read_text("utf-8") if prompt_file.exists() else f"(prompt file not found: {prompt_file})"

    return f"""Execute the plan in: {prompt_file}

AUTONOMOUS EXECUTION RULES:
- You are running autonomously in a loop with NO human input
- DO NOT ask 'Would you like me to...' or present options
- DO NOT wait for user confirmation or input
- READ the plan file, FIND the first [PENDING] or [IN_PROGRESS] step, DO the work
- If stuck, try a different approach - do not ask for help
- Update the plan file to mark progress as you complete steps
- After completing ALL steps in the plan, output <promise>{promise_token}</promise>
- Only output the promise token when ALL steps are genuinely [COMPLETED]
- BEFORE outputting the promise token, run these quality checks yourself and fix any failures
- If checks fail, fix the errors first. Do NOT emit the token with failing checks.
- DO NOT run git add or git commit — the runner handles commits automatically after the quality gate passes
- LEARNINGS: When you encounter a non-obvious problem and find the solution, append it to docs/{feature_name}/LEARNINGS.md so future iterations don't repeat the same mistake. Format: ## Problem title, what went wrong, what fixed it. Keep entries concise. Also write learnings when you discover environment quirks, selector gotchas, timing issues, or anything that cost you significant effort to figure out.

PROJECT CONTEXT:
- Working directory: {project_dir}
- Feature: {feature_name}
- This is phase {phase_num} of {phase_count}
- Previous phases (1-{prev_phases}) are already complete — do NOT redo their work
- Read the plan file first, then check what's already done before starting
{prompt_extra}{learnings_block}{fix_block}
START NOW: Read {prompt_file}, find the first incomplete step, execute it.

{plan_content}
"""


def build_ci_fix_prompt(
    ci_command: str,
    ci_output: str,
    attempt: int,
    max_retries: int,
    project_dir: Path,
    feature_name: str,
) -> str:
    """Build prompt for a local-CI fix iteration."""
    return f"""LOCAL CI FAILED — FIX THE ERRORS (attempt {attempt}/{max_retries})

The CI command failed:
  {ci_command}

CI OUTPUT:
{ci_output}

INSTRUCTIONS:
- Analyze the CI output above and fix ALL errors
- This is an autonomous fix loop — do NOT ask questions
- After fixing, the CI will be re-run automatically
- Focus on the root cause, not workarounds
- Working directory: {project_dir}
- Feature: {feature_name}

FIX ALL ERRORS NOW.
"""
