"""Local CI and review fix loops for the runner."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conductor.core.claude import run_claude, resolve_model
from runner.logging import log, success, warn, error, dim, bold


async def run_local_ci(
    ci_command: str,
    project_dir: Path,
    feature_name: str,
    scripts_root: Path | None = None,
) -> tuple[bool, str]:
    """Run a CI command. Returns (passed, output)."""
    cwd = scripts_root or project_dir
    ci_log = Path(f"/tmp/conductor-local-ci-{feature_name}.log")

    # Strip Claude env vars, add timeout
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")}

    try:
        result = subprocess.run(
            ["bash", "-c", ci_command],
            cwd=cwd, capture_output=True, text=True,
            env=env, timeout=900,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        output = "CI command timed out after 900s"
        result = type('R', (), {'returncode': 1})()

    # Write log
    ci_log.write_text(output, encoding="utf-8")

    # Truncate if too large
    if len(output) > 50000:
        output = f"... (truncated) ...{output[-50000:]}"

    if result.returncode != 0:
        log(dim(f"  CI output saved to {ci_log}"))

    return result.returncode == 0, output


def _build_ci_fix_prompt(
    ci_command: str,
    ci_output: str,
    attempt: int,
    max_retries: int,
    project_dir: Path,
    feature_name: str,
) -> str:
    return (
        f"LOCAL CI FAILED — FIX THE ERRORS (attempt {attempt}/{max_retries})\n\n"
        f"The CI command failed:\n  {ci_command}\n\n"
        f"CI OUTPUT:\n{ci_output}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Analyze the CI output above and fix ALL errors\n"
        f"- This is an autonomous fix loop — do NOT ask questions\n"
        f"- After fixing, the CI will be re-run automatically\n"
        f"- Focus on the root cause, not workarounds\n"
        f"- Working directory: {project_dir}\n"
        f"- Feature: {feature_name}\n\n"
        f"FIX ALL ERRORS NOW."
    )


async def local_ci_fix_loop(
    ci_command: str,
    max_retries: int,
    project_dir: Path,
    feature_name: str,
    fix_model: str | None = None,
    scripts_root: Path | None = None,
) -> bool:
    """Run CI, and if it fails, loop up to max_retries calling Claude to fix.
    Returns True if CI eventually passes."""
    model = resolve_model(fix_model) if fix_model else None

    log(f"Running local CI: {dim(ci_command)}")
    passed, ci_output = await run_local_ci(ci_command, project_dir, feature_name, scripts_root)

    if passed:
        success("Local CI passed")
        return True

    error(f"Local CI failed — entering fix loop (max {max_retries} retries)")

    for attempt in range(1, max_retries + 1):
        log(f"  Local CI fix attempt {attempt}/{max_retries}")

        prompt = _build_ci_fix_prompt(ci_command, ci_output, attempt, max_retries, project_dir, feature_name)

        await run_claude(prompt, model=model, max_turns=50, cwd=str(project_dir))

        # Stage and commit fix
        subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_dir, capture_output=True,
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", f"fix: local CI fixes (attempt {attempt})"],
                cwd=project_dir, capture_output=True, text=True,
            )
            log(dim(f"  Committed CI fix (attempt {attempt})"))

        # Re-run CI
        log("  Re-running local CI...")
        passed, ci_output = await run_local_ci(ci_command, project_dir, feature_name, scripts_root)
        if passed:
            success(f"Local CI passed after {attempt} fix attempt(s)")
            return True
        error("  Local CI still failing")

    error(f"Local CI failed after {max_retries} fix attempts")
    return False


def _build_review_fix_prompt(
    findings_file: Path,
    attempt: int,
    max_retries: int,
    project_dir: Path,
    feature_name: str,
) -> str:
    findings = findings_file.read_text(encoding="utf-8") if findings_file.exists() else "[]"
    return (
        f"LOCAL CODE REVIEW FOUND ISSUES — FIX THEM (attempt {attempt}/{max_retries})\n\n"
        f"Review findings (from {findings_file}):\n{findings}\n\n"
        f"INSTRUCTIONS:\n"
        f"- Fix ALL high-confidence findings above\n"
        f"- This is an autonomous fix loop — do NOT ask questions\n"
        f"- After fixing, the review will be re-run automatically\n"
        f"- Working directory: {project_dir}\n"
        f"- Feature: {feature_name}\n\n"
        f"FIX ALL ISSUES NOW."
    )


def _count_high_severity(findings_file: Path) -> int:
    """Count high-severity findings in findings.json."""
    if not findings_file.exists():
        return 0
    try:
        data = json.loads(findings_file.read_text(encoding="utf-8"))
        return sum(
            1 for f in data.get("findings", [])
            if f.get("severity", "").lower() == "high"
        )
    except (json.JSONDecodeError, KeyError):
        return 0


async def local_review_fix_loop(
    review_command: str,
    max_retries: int,
    project_dir: Path,
    feature_name: str,
    fix_model: str | None = None,
    scripts_root: Path | None = None,
) -> bool:
    """Run review once, fix once, done. Never blocks the phase."""
    model = resolve_model(fix_model) if fix_model else None
    cwd = scripts_root or project_dir
    findings_file = project_dir / ".claude" / "reviews" / "findings.json"

    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")}

    log(f"Running local review: {dim(review_command)}")
    subprocess.run(
        ["bash", "-c", review_command],
        cwd=cwd, capture_output=True, text=True,
        env=env, timeout=900, stdin=subprocess.DEVNULL,
    )

    count = _count_high_severity(findings_file)
    if count == 0:
        success("Local review passed (no high-severity findings)")
        return True

    warn(f"Local review found {count} high-severity issue(s) — attempting fix")

    prompt = _build_review_fix_prompt(findings_file, 1, 1, project_dir, feature_name)
    await run_claude(prompt, model=model, max_turns=50, cwd=str(project_dir))

    # Stage and commit if there are changes
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=project_dir, capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", "fix: local review fixes"],
            cwd=project_dir, capture_output=True, text=True,
        )
        success("Committed review fixes")
    else:
        warn(f"No changes made — {count} finding(s) likely false positives, continuing")

    return True


async def run_local_checks(
    project_dir: Path,
    feature_name: str,
    *,
    ci_enabled: bool = False,
    ci_command: str = "",
    ci_full_command: str = "",
    ci_max_retries: int = 3,
    review_enabled: bool = False,
    review_command: str = "",
    review_full_command: str = "",
    review_max_retries: int = 2,
    fix_model: str | None = None,
    full_mode: bool = False,
    scripts_root: Path | None = None,
) -> bool:
    """Run all local checks (CI + review). Returns True if all pass."""
    if not ci_enabled:
        return True

    ci_cmd = ci_full_command if full_mode and ci_full_command else ci_command
    rev_cmd = review_full_command if full_mode and review_full_command else review_command

    if ci_cmd:
        if not await local_ci_fix_loop(ci_cmd, ci_max_retries, project_dir, feature_name, fix_model, scripts_root):
            return False

    if review_enabled and rev_cmd:
        if not await local_review_fix_loop(rev_cmd, review_max_retries, project_dir, feature_name, fix_model, scripts_root):
            return False

    return True
