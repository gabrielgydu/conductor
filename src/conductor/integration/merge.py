"""Integration merge pipeline — merges DONE runs into an integration branch."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections import deque
from pathlib import Path

from conductor.core.claude import run_claude
from conductor.core.enums import IntegrationStatus, RunStatus
from conductor.core.models import (
    ConductorState,
    ConflictRecord,
    IntegrationState,
    RunState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_conflict_markers(content: str) -> bool:
    """Return True if content contains git conflict markers."""
    return "<<<<<<<" in content


def _build_pr_body(
    merged_runs: list[RunState],
    conflicts_resolved: list[ConflictRecord],
    conflicts_unresolved: list[ConflictRecord],
) -> str:
    lines = ["## Merged Runs"]
    for run in merged_runs:
        lines.append(f"- Run {run.index}: {run.name}")

    if conflicts_resolved:
        lines.append("")
        lines.append("## Conflicts")
        for c in conflicts_resolved:
            lines.append(f"- `{c.file}`: {c.description}")

    if conflicts_unresolved:
        lines.append("")
        lines.append("## Unresolved Conflicts")
        for c in conflicts_unresolved:
            lines.append(f"- `{c.file}`: contains conflict markers")

    return "\n".join(lines)


async def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a git command, returning (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _get_conflicting_files(cwd: Path) -> list[str]:
    """Return list of files with unresolved conflicts."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    return [f for f in result.stdout.splitlines() if f.strip()]


# ---------------------------------------------------------------------------
# DAG ordering
# ---------------------------------------------------------------------------


def get_activation_order(state: ConductorState) -> list[RunState]:
    """Return DONE runs in topological (dependency) order using Kahn's algorithm."""
    eligible = {run.index: run for run in state.runs if run.status == RunStatus.DONE}

    if not eligible:
        return []

    # Build adjacency: dep → run (only edges within eligible set)
    graph: dict[int, list[int]] = {idx: [] for idx in eligible}
    in_degree: dict[int, int] = {idx: 0 for idx in eligible}

    for run in eligible.values():
        for dep in run.depends_on:
            if dep in eligible:
                graph[dep].append(run.index)
                in_degree[run.index] += 1

    queue: deque[int] = deque(idx for idx in eligible if in_degree[idx] == 0)
    result: list[RunState] = []

    while queue:
        idx = queue.popleft()
        result.append(eligible[idx])
        for neighbor in graph[idx]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(eligible):
        raise ValueError("Cycle detected among eligible runs")

    return result


# ---------------------------------------------------------------------------
# AI conflict resolution
# ---------------------------------------------------------------------------


async def resolve_conflicts_with_claude(
    conflicting_files: list[str],
    run_description: str,
    cwd: Path,
) -> bool:
    """Use Claude to resolve git merge conflicts. Returns True if all resolved."""
    if not conflicting_files:
        return True

    try:
        file_sections = []
        for file_path in conflicting_files:
            try:
                content = (cwd / file_path).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                return False
            file_sections.append(f"### {file_path}\n```\n{content}\n```")

        files_block = "\n\n".join(file_sections)

        prompt = (
            "You are resolving git merge conflicts. For each file below, resolve the conflict\n"
            "by choosing the best combination of both sides. Remove all conflict markers.\n\n"
            "## Context\n"
            f"{run_description}\n\n"
            "## Conflicting Files\n\n"
            f"{files_block}\n\n"
            "## Instructions\n"
            "- Edit each file to resolve the conflicts\n"
            "- Remove ALL conflict markers (<<<<<<, ======, >>>>>>)\n"
            "- Preserve the intent of both sides where possible\n"
            "- If unclear, prefer the incoming changes (theirs)"
        )

        result = await run_claude(
            prompt,
            model="claude-opus-4-6",
            max_turns=50,
            cwd=str(cwd),
        )

        if result.exit_code != 0:
            return False

        for file_path in conflicting_files:
            try:
                content = (cwd / file_path).read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                return False
            if _has_conflict_markers(content):
                return False

        return True

    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def run_integration_merge(
    state: ConductorState,
    storage,  # StorageResolver
) -> IntegrationState:
    """Merge all DONE runs into an integration branch and open a PR."""
    eligible_runs = get_activation_order(state)

    branch_name = f"integration/{state.project_name}"
    worktree_path = Path("/tmp") / f"conductor-integration-{state.project_name}"
    repo_root = Path(storage.repo_root)

    if len(eligible_runs) < 2:
        return IntegrationState(
            status=IntegrationStatus.DONE,
            branch=branch_name,
            merged_runs=[],
        )

    # --- Cleanup phase (ignore errors) ---
    await _run_git(["worktree", "remove", "--force", str(worktree_path)], repo_root)
    await _run_git(["branch", "-D", branch_name], repo_root)
    await _run_git(["push", "origin", "--delete", branch_name], repo_root)

    # --- Create worktree ---
    rc, _, _ = await _run_git(
        ["worktree", "add", str(worktree_path), "-b", branch_name, f"origin/{state.base_branch}"],
        repo_root,
    )
    if rc != 0:
        # Fallback: use local base branch
        rc2, _, err = await _run_git(
            ["worktree", "add", str(worktree_path), "-b", branch_name, state.base_branch],
            repo_root,
        )
        if rc2 != 0:
            logger.error("Failed to create worktree: %s", err)
            return IntegrationState(
                status=IntegrationStatus.FAILED,
                branch=branch_name,
            )

    wt = worktree_path

    merged_runs: list[RunState] = []
    conflicts_resolved: list[ConflictRecord] = []
    conflicts_unresolved: list[ConflictRecord] = []
    has_partial = False

    # --- Merge loop ---
    for run in eligible_runs:
        if not run.stages:
            logger.warning("Run %s has no stages, skipping", run.index)
            continue

        branch = run.stages[-1].branch
        if branch is None:
            logger.warning("Run %s has no branch on last stage, skipping", run.index)
            continue

        # Try clean merge
        rc, _, _ = await _run_git(["merge", branch, "--no-edit"], wt)
        if rc == 0:
            merged_runs.append(run)
            continue

        # Abort and try -X theirs
        await _run_git(["merge", "--abort"], wt)
        rc, _, _ = await _run_git(["merge", "-X", "theirs", branch, "--no-edit"], wt)
        if rc == 0:
            conflicting = _get_conflicting_files(wt)
            for f in conflicting:
                conflicts_resolved.append(
                    ConflictRecord(
                        file=f,
                        feature_a=state.project_name,
                        feature_b=run.name,
                        description=f"Resolved with -X theirs strategy",
                    )
                )
            merged_runs.append(run)
            continue

        # Abort and try Claude resolution
        await _run_git(["merge", "--abort"], wt)
        # Re-merge without strategy to get conflict markers
        await _run_git(["merge", branch, "--no-edit"], wt)
        conflicting = _get_conflicting_files(wt)

        run_description = f"Merging run {run.index}: {run.name}\n{run.description}"
        claude_ok = await resolve_conflicts_with_claude(conflicting, run_description, wt)

        if claude_ok:
            await _run_git(["add", "."], wt)
            await _run_git(["commit", "--no-edit"], wt)
            for f in conflicting:
                conflicts_resolved.append(
                    ConflictRecord(
                        file=f,
                        feature_a=state.project_name,
                        feature_b=run.name,
                        description=f"Resolved by Claude AI",
                    )
                )
            merged_runs.append(run)
        else:
            # Partial: commit whatever we have
            await _run_git(["add", "."], wt)
            await _run_git(
                ["commit", "--no-verify", "--no-edit", "-m", f"Partial merge: {run.name}"],
                wt,
            )
            for f in conflicting:
                conflicts_unresolved.append(
                    ConflictRecord(
                        file=f,
                        feature_a=state.project_name,
                        feature_b=run.name,
                        description=f"Claude could not resolve conflict",
                    )
                )
            has_partial = True
            merged_runs.append(run)

    # --- Push ---
    await _run_git(["push", "origin", branch_name], wt)

    # --- PR ---
    try:
        rc, stdout, _ = await _run_git_gh(
            ["gh", "pr", "list", "--head", branch_name, "--json", "number"],
            wt,
        )
        existing_prs = []
        if rc == 0 and stdout.strip():
            try:
                existing_prs = json.loads(stdout)
            except json.JSONDecodeError:
                existing_prs = []

        if not existing_prs:
            body = _build_pr_body(merged_runs, conflicts_resolved, conflicts_unresolved)
            title = f"Integration: {state.project_name}"
            await _run_git_gh(
                ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch_name],
                wt,
            )
    except Exception as e:
        logger.warning("PR creation failed (gh CLI may not be available): %s", e)

    # --- Cleanup worktree (non-fatal) ---
    try:
        rc, _, err = await _run_git(["worktree", "remove", str(wt)], repo_root)
        if rc != 0:
            logger.warning("Failed to remove worktree %s: %s", wt, err)
    except Exception as e:
        logger.warning("Exception removing worktree %s: %s", wt, e)

    status = IntegrationStatus.PARTIAL if has_partial else IntegrationStatus.DONE

    return IntegrationState(
        status=status,
        branch=branch_name,
        merged_runs=[r.index for r in merged_runs],
        conflicts_resolved=conflicts_resolved,
        conflicts_unresolved=conflicts_unresolved,
    )


async def _run_git_gh(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run git or gh command, returning (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )
