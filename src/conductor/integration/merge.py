"""Integration merge pipeline — merges DONE runs into an integration branch."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from conductor.core.claude import run_claude
from conductor.core.enums import IntegrationFailure, IntegrationStatus, RunStatus
from conductor.core.models import (
    ConductorState,
    ConflictRecord,
    E2ETestState,
    IntegrationState,
    RunState,
    StageState,
)
from conductor.core.presets import Preset, load_preset
from conductor.core.validation import (
    ValidationContext,
    ValidationResult,
    commit_validation_fixes,
    validate_and_fix,
)

logger = logging.getLogger(__name__)

# Back-off between `gh pr list` / `gh pr create` retries (tests set it to 0).
_GH_RETRY_DELAY_S = 5.0


def _print(msg: str) -> None:
    """Print to stdout so it shows in tmux."""
    from conductor.core.logging import live_log  # noqa: PLC0415

    live_log("INTEGRATION", msg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pr_body(
    merged_runs: list[RunState],
    conflicts_resolved: list[ConflictRecord],
    conflicts_unresolved: list[ConflictRecord],
    not_merged: Sequence[tuple[RunState, str]] = (),
) -> str:
    lines = ["## Merged Runs"]
    for run in merged_runs:
        lines.append(f"- Run {run.index}: {run.name}")

    if not_merged:
        lines.append("")
        lines.append("## Not Merged")
        for run, reason in not_merged:
            lines.append(f"- Run {run.index}: {run.name} ({reason})")

    if conflicts_resolved:
        lines.append("")
        lines.append("## Conflicts")
        for c in conflicts_resolved:
            lines.append(f"- `{c.file}`: {c.description}")

    if conflicts_unresolved:
        lines.append("")
        lines.append("## Unresolved Conflicts")
        for c in conflicts_unresolved:
            lines.append(f"- `{c.file}`: {c.description}")

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


# Where the integration worktree went before integration_worktree_path, when
# state.worktrees_base was unset.
_LEGACY_WORKTREE_BASE = Path("/tmp")


def integration_worktree_path(state: ConductorState, repo_root: Path) -> Path:
    """Integration worktree location, next to the run worktrees.

    Same base as orchestrator.create_worktree: state.worktrees_base, else
    <repo_root.parent>/worktrees.
    """
    if state.worktrees_base:
        base = Path(state.worktrees_base)
    else:
        base = Path(repo_root).parent / "worktrees"
    return base / f"integration-{state.project_name}"


async def _remove_worktree(wt: Path, repo_root: Path) -> None:
    """Force-remove the integration worktree (non-fatal).

    Runs after everything worth keeping was committed; what is left is check
    output (test-results/ ...), which would make a plain remove fail.
    """
    try:
        try:
            _, leftover, _ = await _run_git(["status", "--porcelain"], wt)
        except OSError:  # the worktree directory is already gone
            leftover = ""
        if leftover.strip():
            logger.warning(
                "Discarding uncommitted files in %s: %s",
                wt,
                ", ".join(line[3:] for line in leftover.splitlines()),
            )
        rc, _, err = await _run_git(["worktree", "remove", "--force", str(wt)], repo_root)
        if rc != 0:
            logger.warning("Failed to remove worktree %s: %s", wt, err.strip())
    except Exception as e:
        logger.warning("Exception removing worktree %s: %s", wt, e)


async def _link_node_modules(repo_root: Path, wt: Path) -> None:
    """Make the main checkout's installed node packages resolvable in *wt*.

    The integration worktree is a fresh checkout, so without this the
    Playwright gate (smoke_test.playwright_unavailable) always closes and the
    integration specs and generated smoke test never run. wt/node_modules is
    a real directory of per-package symlinks rather than one symlink: an
    ignore pattern written as `node_modules/` does not match a symlink, and
    an `npm install` by a fix session then replaces links in the worktree
    instead of deleting or writing packages in the main checkout. Scope
    directories and .bin are linked entry by entry for the same reason;
    other dot entries (tool caches, npm's hidden lockfile) are left out.
    """
    src, dst = repo_root / "node_modules", wt / "node_modules"
    if not src.is_dir() or dst.exists() or dst.is_symlink():
        return
    try:
        dst.mkdir()
        for entry in src.iterdir():
            if entry.name.startswith("@") or entry.name == ".bin":
                (dst / entry.name).mkdir()
                for sub in entry.iterdir():
                    (dst / entry.name / sub.name).symlink_to(sub)
            elif not entry.name.startswith("."):
                (dst / entry.name).symlink_to(entry)
    except OSError as exc:
        logger.warning("Linking %s into %s failed: %s", src, wt, exc)
        shutil.rmtree(dst, ignore_errors=True)
        return
    # A Claude session's `git add -A` would put unignored links on the branch.
    _, untracked, _ = await _run_git(
        ["ls-files", "--others", "--exclude-standard", "--directory", "--", "node_modules"], wt
    )
    if untracked.strip():
        shutil.rmtree(dst, ignore_errors=True)
        logger.warning("Not linking %s: node_modules is not gitignored in %s", src, wt)
        return
    _print(f"Linked the packages in {src} into the worktree")


async def _runs_touching(
    path: str, runs: list[RunState], tips: dict[int, str], start: str, cwd: Path
) -> list[str]:
    """Names of *runs* whose tip (*tips*) changed *path* since *start* (the merge base)."""
    names = []
    for run in runs:
        rc, _, _ = await _run_git(
            ["diff", "--quiet", f"{start}...{tips[run.index]}", "--", path], cwd
        )
        if rc == 1:
            names.append(run.name)
    return names


async def _rev(ref: str, cwd: Path) -> str | None:
    """The commit *ref* names, or None if there is none."""
    rc, out, _ = await _run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    return out.strip() if rc == 0 and out.strip() else None


async def _is_ancestor(ref: str, of: str, cwd: Path) -> bool:
    """Whether *of* already contains *ref*."""
    rc, _, _ = await _run_git(["merge-base", "--is-ancestor", ref, of], cwd)
    return rc == 0


async def _stage_tip(stage: StageState, cwd: Path) -> str | None:
    """What to merge for a run's last *stage* (StageState.work_refs); None if
    its work is gone."""
    for ref, name in stage.work_refs():
        if await _rev(ref, cwd) is not None:
            return name
    return None


async def _checked_out_at(branch: str, repo_root: Path) -> str | None:
    """Path of an existing worktree (the main checkout included) that has *branch*."""
    _, out, _ = await _run_git(["worktree", "list", "--porcelain"], repo_root)
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path and Path(path).exists():
            return path
    return None


def _final_status(has_partial: bool, validation_passed: bool, published: bool) -> IntegrationStatus:
    """PARTIAL while a run or a conflict resolution is missing; DONE only once
    validation passed and the branch is pushed with a PR; NEEDS_FIX otherwise."""
    if has_partial:
        return IntegrationStatus.PARTIAL
    if not validation_passed or not published:
        return IntegrationStatus.NEEDS_FIX
    return IntegrationStatus.DONE


# Merge commits skip hooks, like every commit conductor makes outside the
# runner: in the fresh integration worktree a hook can fail for reasons that
# have nothing to do with the code (husky without node_modules) and leave the
# merge open. Post-merge validation runs the project's checks afterwards.
_MERGE = ["merge", "--no-verify", "--no-edit"]


async def _conclude_merge(cwd: Path) -> bool:
    """Commit the resolved merge; True once it is concluded (no MERGE_HEAD)."""
    await _run_git(["add", "-A"], cwd)
    rc, out, err = await _run_git(["commit", "--no-verify", "--no-edit"], cwd)
    if rc != 0:
        logger.warning("Merge commit in %s failed: %s", cwd, (err or out).strip())
        return False
    rc, _, _ = await _run_git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd)
    return rc != 0


async def _abort_merge(cwd: Path) -> None:
    """Abort the merge in progress and drop untracked files the attempt left
    (Claude's scratch files), so the next merge starts from a clean tree."""
    await _run_git(["merge", "--abort"], cwd)
    await _run_git(["clean", "-fdq"], cwd)


async def _conflicts_cleared(cwd: Path, files: list[str]) -> bool:
    """Stage *files* and report whether git considers their conflicts resolved.

    Resolved means no unmerged index entry is left and none of the lines the
    merge adds relative to HEAD is a conflict marker (<<<<<<<, |||||||,
    ======= or >>>>>>> at a line start, as `git diff --check` reports them).
    Markers already present in HEAD (git docs, test fixtures) do not count,
    and deleting a file that was modified on one side and deleted on the
    other is a valid resolution.
    """
    for f in files:
        # Per file: a path already removed from index and tree fails to stage
        # (nothing to do) and must not stop the others from being staged.
        await _run_git(["add", "-A", "--", f], cwd)
    rc, unmerged, _ = await _run_git(["ls-files", "--unmerged", "--", *files], cwd)
    if rc != 0 or unmerged.strip():
        return False
    _, check, _ = await _run_git(["diff", "--cached", "--check", "HEAD", "--", *files], cwd)
    return "leftover conflict marker" not in check


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
    """Use Claude to resolve git merge conflicts in *cwd*.

    Returns True when every file is resolved (see _conflicts_cleared); the
    resolution is then staged.
    """
    if not conflicting_files:
        return True

    try:
        file_sections = []
        for file_path in conflicting_files:
            try:
                content = (cwd / file_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except FileNotFoundError:
                content = "(not in the working tree: deleted on one side of the merge)"
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
            "- A file modified on one side and deleted on the other: keep it (edited)"
            " or delete it, whichever preserves both sides' intent\n"
            "- Preserve the intent of both sides where possible\n"
            "- If unclear, prefer the incoming changes (theirs)\n"
            "- Do not stage, commit or abort the merge; only edit the files"
        )

        result = await run_claude(
            prompt,
            model="claude-sonnet-4-6",
            max_turns=50,
            cwd=str(cwd),
        )

        if result.exit_code != 0:
            return False

        return await _conflicts_cleared(cwd, conflicting_files)

    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def run_integration_merge(
    state: ConductorState,
    storage,  # StorageResolver
    *,
    before_publish: Callable[[Path], Awaitable[None]] | None = None,
) -> IntegrationState:
    """Merge the DONE runs into integration/<project> and open (or reuse) its PR.

    Incremental and never destructive: the integration branch is not deleted
    or force-pushed. An attempt starts from the existing branch (the newer of
    the local one and origin's), merges only the runs it does not contain
    yet, and moves and pushes the branch (fast-forward) only after new merges
    or while it is not published. A checkout that has the branch, or local
    and origin branches that diverged, stop the attempt before anything
    changes (failure SETUP or DIVERGED).

    *before_publish* is awaited with the worktree after new merges, before
    validation and the push (the learnings commit); it must not raise.
    """
    eligible_runs = get_activation_order(state)

    branch_name = f"integration/{state.project_name}"
    repo_root = Path(storage.repo_root)
    worktree_path = integration_worktree_path(state, repo_root)
    _print(f"Starting integration merge: {len(eligible_runs)} runs → {branch_name}")

    if len(eligible_runs) < 2:
        return IntegrationState(
            status=IntegrationStatus.DONE,
            branch=branch_name,
            merged_runs=[],
        )

    def stop(failure: IntegrationFailure, detail: str, status=IntegrationStatus.FAILED):
        _print(f"Integration stopped, nothing changed: {detail}")
        return IntegrationState(
            status=status, branch=branch_name, failure=failure, failure_detail=detail
        )

    # Integration and stage branches that exist only on origin count too
    # (ignore errors: no origin, offline).
    await _run_git(["fetch", "--quiet", "origin"], repo_root)

    # --- Our worktree from an earlier attempt (ignore errors) ---
    await _run_git(["worktree", "remove", "--force", str(worktree_path)], repo_root)
    if not state.worktrees_base:
        # A worktree left at the old location, even one whose directory is
        # gone, would keep the branch checked out.
        legacy = _LEGACY_WORKTREE_BASE / f"conductor-integration-{state.project_name}"
        await _run_git(["worktree", "remove", "--force", str(legacy)], repo_root)
    holder = await _checked_out_at(branch_name, repo_root)
    if holder is not None:
        return stop(
            IntegrationFailure.SETUP,
            f"{branch_name} is checked out in {holder}; switch that checkout to "
            "another branch",
        )

    # --- Start from the integration branch when there is one ---
    local = await _rev(f"refs/heads/{branch_name}", repo_root)
    remote = await _rev(f"refs/remotes/origin/{branch_name}", repo_root)
    start = local or remote
    if local and remote and local != remote:
        if await _is_ancestor(local, remote, repo_root):
            start = remote
        elif not await _is_ancestor(remote, local, repo_root):
            return stop(
                IntegrationFailure.DIVERGED,
                f"{branch_name} and origin/{branch_name} have diverged; merge one "
                "into the other (or reset one), then re-run with --reintegrate",
                IntegrationStatus.NEEDS_FIX,
            )

    # --- Create worktree (detached: the branch moves only at the end) ---
    starts = [start] if start else [f"origin/{state.base_branch}", state.base_branch]
    for ref in starts:
        rc, _, err = await _run_git(
            ["worktree", "add", "--detach", str(worktree_path), ref], repo_root
        )
        if rc == 0:
            break
    else:
        logger.error("Failed to create worktree: %s", err)
        return stop(
            IntegrationFailure.SETUP,
            f"git worktree add {worktree_path} failed: {err.strip()}",
        )

    # From the repo root, like the runs: an uncommitted .conductor/preset.toml
    # is not in the worktree.
    try:
        preset = load_preset(state.preset, repo_root)
    except ValueError as exc:
        logger.warning("Preset %r not loadable (%s) — using base validation", state.preset, exc)
        preset = None

    try:
        return await _integrate(
            state, repo_root, worktree_path, branch_name, eligible_runs, preset,
            local=local, remote=remote, before_publish=before_publish,
        )
    finally:
        await _remove_worktree(worktree_path, repo_root)


async def _integrate(
    state: ConductorState,
    repo_root: Path,
    wt: Path,
    branch_name: str,
    eligible_runs: list[RunState],
    preset: Preset | None,
    *,
    local: str | None,
    remote: str | None,
    before_publish: Callable[[Path], Awaitable[None]] | None,
) -> IntegrationState:
    """Merge, validate, commit, push and open the PR inside worktree *wt*.

    *local* and *remote* are the integration branch's tips before this
    attempt (None where it does not exist); *wt* starts at the newer one.
    """
    previous = state.integration
    on_branch = bool(local or remote)
    merged_runs: list[RunState] = []  # on the branch: merged now or already in it
    new_runs: list[RunState] = []  # merged by this attempt
    tips: dict[int, str] = {}
    not_merged: list[tuple[RunState, str]] = []
    conflicts_resolved: list[ConflictRecord] = []
    conflicts_unresolved: list[ConflictRecord] = []
    has_partial = False
    start_label = branch_name if on_branch else state.base_branch

    def skip(run: RunState, reason: str) -> None:
        _print(f"  ✗ Run {run.index} ({run.name}) not merged: {reason}")
        not_merged.append((run, reason))

    def contained(run: RunState) -> None:
        _print(f"  ✓ Run {run.index} ({run.name}) already in {start_label}")
        merged_runs.append(run)

    # --- Merge loop ---
    _print(f"Worktree created at {wt}, starting from {start_label}")
    _, start, _ = await _run_git(["rev-parse", "HEAD"], wt)
    start = start.strip()
    for run in eligible_runs:
        stage = run.stages[-1] if run.stages else None
        tip = await _stage_tip(stage, wt) if stage else None
        if tip is None:
            if on_branch and previous is not None and run.index in previous.merged_runs:
                # Merged by an earlier attempt; its branch is gone since.
                contained(run)
            elif stage is None or not stage.branch:
                skip(run, "no branch recorded for its last stage")
            else:
                skip(run, f"branch {stage.branch} does not exist")
            continue
        if await _is_ancestor(tip, "HEAD", wt):
            contained(run)
            continue
        tips[run.index] = tip

        _print(f"Merging run {run.index}: {run.name} ({tip})")
        # Try clean merge
        rc, out, err = await _run_git([*_MERGE, tip], wt)
        if rc == 0:
            _print("  ✓ Clean merge")
            merged_runs.append(run)
            new_runs.append(run)
            continue

        # Conflicts: record the files while the markers are in the tree (after a
        # merge completes, --diff-filter=U is empty) and let Claude combine both sides.
        conflicting = _get_conflicting_files(wt)
        if not conflicting:
            # Not a conflict (unrelated histories, untracked files in the way, ...)
            await _abort_merge(wt)
            reason = next((ln.strip() for ln in (err + out).splitlines() if ln.strip()), "")
            skip(run, f"git merge failed: {reason or f'exit {rc}'}")
            continue
        _print(f"  Conflicts in {len(conflicting)} file(s): {', '.join(conflicting)}")
        earlier = {f: await _runs_touching(f, new_runs, tips, start, wt) for f in conflicting}

        run_description = f"Merging run {run.index}: {run.name}\n{run.description}"
        claude_ok = await resolve_conflicts_with_claude(
            conflicting, run_description, wt
        )

        # A merge left open would be aborted by the next run's merge, silently
        # dropping this run, so only a concluded merge counts.
        if claude_ok and await _conclude_merge(wt):
            _print("  ✓ Resolved by Claude")
            for f in conflicting:
                conflicts_resolved.append(
                    ConflictRecord(
                        file=f,
                        feature_a=", ".join(earlier[f]) or branch_name,
                        feature_b=run.name,
                        description="Resolved by Claude AI",
                    )
                )
            merged_runs.append(run)
            new_runs.append(run)
            continue

        # Last resort: -X theirs keeps this run's side of every conflicting
        # hunk and drops the integration branch's (the earlier runs') side.
        # Recorded as unresolved so the status and PR cannot claim a clean merge.
        await _abort_merge(wt)
        rc, _, _ = await _run_git([*_MERGE, "-X", "theirs", tip], wt)
        if rc == 0:
            _print(f"  ! Claude could not resolve; -X theirs overwrote: {', '.join(conflicting)}")
            for f in conflicting:
                overwritten = ", ".join(earlier[f]) or branch_name
                conflicts_unresolved.append(
                    ConflictRecord(
                        file=f,
                        feature_a=overwritten,
                        feature_b=run.name,
                        description=(
                            f"theirs overwrote {overwritten}: Claude could not resolve, "
                            f"so -X theirs kept {run.name}'s side of each conflicting "
                            "hunk; review manually"
                        ),
                    )
                )
            has_partial = True
            merged_runs.append(run)
            new_runs.append(run)
            continue

        # -X theirs failed too (modify/delete, rename): leave the run out
        # rather than commit conflict markers.
        await _abort_merge(wt)
        for f in conflicting:
            conflicts_unresolved.append(
                ConflictRecord(
                    file=f,
                    feature_a=", ".join(earlier[f]) or branch_name,
                    feature_b=run.name,
                    description=(
                        f"neither Claude nor -X theirs could resolve it; {run.name} not merged"
                    ),
                )
            )
        skip(run, f"unresolved conflicts in {', '.join(conflicting)}")

    has_partial = has_partial or bool(not_merged)
    already = len(merged_runs) - len(new_runs)
    _print(
        f"Merged {len(merged_runs)} of {len(eligible_runs)} runs"
        + (f" ({already} already in {start_label})" if already else "")
    )
    if not merged_runs:
        return IntegrationState(
            status=IntegrationStatus.FAILED,
            branch=branch_name,
            conflicts_unresolved=conflicts_unresolved,
            failure=IntegrationFailure.NOTHING_MERGEABLE,
            failure_detail="no run could be merged: "
            + "; ".join(f"{run.name}: {reason}" for run, reason in not_merged),
        )

    # Runs an earlier attempt merged keep its conflict records: the branch
    # still has what Claude or -X theirs made of them.
    if on_branch and previous is not None:
        kept = {run.name for run in merged_runs if run not in new_runs}
        conflicts_resolved[:0] = [c for c in previous.conflicts_resolved if c.feature_b in kept]
        carried = [c for c in previous.conflicts_unresolved if c.feature_b in kept]
        conflicts_unresolved[:0] = carried
        has_partial = has_partial or bool(carried)

    if new_runs:
        tip, validation_passed, e2e_result = await _finish_new_merges(
            state, repo_root, wt, merged_runs, conflicts_resolved, preset, before_publish
        )
        # Move the branch only now, and only from the tip this attempt saw.
        rc, _, err = await _run_git(
            ["update-ref", f"refs/heads/{branch_name}", tip, local or ""], repo_root
        )
        if rc != 0:
            return IntegrationState(
                status=IntegrationStatus.FAILED,
                branch=branch_name,
                failure=IntegrationFailure.SETUP,
                failure_detail=f"{branch_name} moved during the integration, so it was "
                f"left alone: {err.strip()}",
            )
        publish = True
    else:
        # The branch is unchanged: nothing to validate or commit. A state
        # from before validation_passed existed passed unless it is NEEDS_FIX.
        tip = start
        if previous is None:
            validation_passed = True
        elif previous.validation_passed is not None:
            validation_passed = previous.validation_passed
        else:
            validation_passed = previous.status != IntegrationStatus.NEEDS_FIX
        e2e_result = previous.e2e if previous is not None else None
        publish = (
            previous is None
            or previous.published is False
            or previous.status == IntegrationStatus.FAILED
            or (local is not None and start == local and local != remote)
        )

    if publish:
        body = _build_pr_body(merged_runs, conflicts_resolved, conflicts_unresolved, not_merged)
        pushed, pr_url, publish_error = await _push_and_open_pr(
            state, wt, branch_name, tip, body, draft=has_partial
        )
        published: bool | None = pushed and pr_url is not None
    else:
        assert previous is not None  # without one, publish is always True
        pr_url, published, publish_error = previous.pr_url, previous.published, ""

    return IntegrationState(
        status=_final_status(has_partial, validation_passed, published is not False),
        branch=branch_name,
        merged_runs=[r.index for r in merged_runs],
        conflicts_resolved=conflicts_resolved,
        conflicts_unresolved=conflicts_unresolved,
        e2e=e2e_result,
        pr_url=pr_url,
        published=published,
        validation_passed=validation_passed,
        failure=IntegrationFailure.PUBLISH if published is False else None,
        failure_detail=publish_error or None,
    )


async def _finish_new_merges(
    state: ConductorState,
    repo_root: Path,
    wt: Path,
    merged_runs: list[RunState],
    conflicts_resolved: list[ConflictRecord],
    preset: Preset | None,
    before_publish: Callable[[Path], Awaitable[None]] | None,
) -> tuple[str, bool, E2ETestState | None]:
    """Learnings, integration specs, validation and their commits on top of
    the new merges; returns the new tip, whether validation passed and the
    integration specs' results."""
    if before_publish is not None:
        await before_publish(wt)

    # Generation and the Playwright checks below need the project's packages.
    await _link_node_modules(repo_root, wt)

    # --- Integration E2E tests: generated once here, run by validation ---
    from conductor.integration.e2e import generate_integration_tests

    if len(merged_runs) >= 2:
        _print("Generating cross-feature integration tests")
        await generate_integration_tests(state, wt, merged_runs, conflicts_resolved)

    # --- Post-merge validation with self-healing ---
    # The smoke check writes tests/Playwright/tests/conductor-smoke.spec.ts itself
    # when it falls back to the generated Playwright smoke test.
    _print("Running post-merge validation (smoke + integration-tests)")
    vctx = ValidationContext(
        project_dir=wt,
        stage="integration",
        feature_name=state.project_name,
        state=state,
        preset=preset,
    )
    try:
        vresult = await validate_and_fix(vctx, max_attempts=3)
    except Exception as exc:
        # The merged runs are still worth publishing: push and PR go ahead.
        logger.exception("Post-merge validation crashed")
        vresult = ValidationResult(passed=False, summary=f"validation crashed: {exc}")
    validation_msg = vresult.summary
    if vresult.passed:
        logger.info("Post-merge validation passed: %s", validation_msg)
    else:
        logger.error("Post-merge validation FAILED: %s", validation_msg)
    # The fix sessions only edit the worktree; the branch and PR get what is committed.
    if vresult.fix_attempts and await commit_validation_fixes(
        wt,
        f"Apply post-merge validation fixes\n\n{validation_msg}",
        keep_untracked=vresult.untracked_before_fix,
    ):
        _print("Committed post-merge validation fixes")

    e2e_result = next((c.e2e for c in vresult.checks if c.e2e is not None), None)

    # Commit any generated test files
    await _run_git(["add", "tests/Playwright/tests/conductor-*.spec.ts"], wt)
    await _run_git(
        ["commit", "-m", "Add conductor integration & smoke tests", "--no-verify"], wt
    )
    _, tip, _ = await _run_git(["rev-parse", "HEAD"], wt)
    return tip.strip(), vresult.passed, e2e_result


async def _push_and_open_pr(
    state: ConductorState, wt: Path, branch_name: str, tip: str, body: str, *, draft: bool
) -> tuple[bool, str | None, str]:
    """Push *tip* to origin's *branch_name* (fast-forward only) and find or
    open its PR; returns (pushed, PR URL, what failed)."""
    _print(f"Pushing {branch_name}")
    rc, _, err = await _run_git(["push", "origin", f"{tip}:refs/heads/{branch_name}"], wt)
    pushed = rc == 0
    error = ""
    if not pushed:
        error = f"git push origin {branch_name} failed: {err.strip()[-500:]}"
        logger.error("%s", error)

    pr_url: str | None = None
    try:
        # Check for existing PR
        for pr_check_attempt in range(3):
            rc, stdout, stderr = await _run_git_gh(
                ["gh", "pr", "list", "--head", branch_name, "--json", "number,url"],
                wt,
            )
            if rc == 0:
                break
            logger.warning(
                "gh pr list attempt %d failed: %s", pr_check_attempt + 1, stderr.strip()
            )
            if pr_check_attempt < 2:
                await asyncio.sleep(_GH_RETRY_DELAY_S)

        existing_prs = []
        if rc == 0 and stdout.strip():
            try:
                existing_prs = json.loads(stdout)
            except json.JSONDecodeError:
                existing_prs = []

        if existing_prs:
            pr_url = existing_prs[0].get("url")
        else:
            title = f"Integration: {state.project_name}"
            # Unresolved conflicts or missing runs need a human before this can be merged.
            draft_flag = ["--draft"] if draft else []
            for attempt in range(3):
                rc, stdout, stderr = await _run_git_gh(
                    [
                        "gh", "pr", "create",
                        "--title", title,
                        "--body", body,
                        "--head", branch_name,
                        "--base", state.base_branch,
                        *draft_flag,
                    ],
                    wt,
                )
                if rc == 0:
                    pr_url = stdout.strip() or None
                    break
                logger.warning(
                    "gh pr create attempt %d/3 failed (rc=%d): %s",
                    attempt + 1, rc, stderr.strip(),
                )
                if attempt < 2:
                    await asyncio.sleep(_GH_RETRY_DELAY_S)
            else:
                logger.error("PR creation failed after 3 attempts for branch %s", branch_name)
                error = error or f"gh pr create failed: {stderr.strip()[-500:]}"
    except Exception as e:
        logger.error("PR creation failed: %s", e)
        error = error or f"PR creation failed: {e}"
    if pushed and pr_url is None and not error:
        error = "no PR was found or created"
    return pushed, pr_url, error


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
