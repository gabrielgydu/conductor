"""Sync with base branch — merge upstream changes and resolve conflicts."""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from conductor.core.claude import run_claude, resolve_model
from runner.logging import log, success, warn, error, dim


async def sync_with_base_branch(
    project_dir: Path,
    base_branch: str,
    push_enabled: bool = False,
    push_remote: str = "origin",
    sync_dump_regen: list[tuple[str, str]] | None = None,
    fix_model: str | None = None,
) -> bool:
    """Sync with base branch. Returns True if sync succeeded (or was unnecessary)."""

    # Clean stuck merge state
    merge_head = project_dir / ".git" / "MERGE_HEAD"
    if merge_head.exists():
        warn("Cleaning up stuck merge state...")
        subprocess.run(["git", "merge", "--abort"], cwd=project_dir, capture_output=True)

    log(f"Syncing with {base_branch}...")

    # Fetch
    result = subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        warn(f"Fetch failed — skipping sync")
        return True

    # Check commits behind
    result = subprocess.run(
        ["git", "rev-list", "--count", f"HEAD..origin/{base_branch}"],
        cwd=project_dir, capture_output=True, text=True,
    )
    behind = int(result.stdout.strip()) if result.returncode == 0 else 0

    if behind == 0:
        success(f"Already up to date with {base_branch}")
        return True

    log(f"{behind} commits behind origin/{base_branch} — merging...")

    # Attempt merge
    result = subprocess.run(
        ["git", "merge", f"origin/{base_branch}", "--no-edit"],
        cwd=project_dir, capture_output=True, text=True,
    )

    if result.returncode == 0:
        success("Merged cleanly")
        if push_enabled:
            _push_changes(project_dir, push_remote)
        return True

    # Conflicts — get conflicting files
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=project_dir, capture_output=True, text=True,
    )
    conflict_files = [f for f in result.stdout.strip().splitlines() if f]

    if not conflict_files:
        warn("Merge failed but no conflicts detected — aborting")
        subprocess.run(["git", "merge", "--abort"], cwd=project_dir, capture_output=True)
        return True

    warn(f"Conflicts in: {' '.join(conflict_files)}")

    # Separate SQL dump conflicts from code conflicts
    sql_conflicts = []
    code_conflicts = []
    regen_commands: list[str] = []

    for cfile in conflict_files:
        is_dump = False
        if sync_dump_regen:
            for pattern, command in sync_dump_regen:
                if fnmatch.fnmatch(cfile, pattern):
                    is_dump = True
                    if command not in regen_commands:
                        regen_commands.append(command)
                    break

        if is_dump:
            sql_conflicts.append(cfile)
        else:
            code_conflicts.append(cfile)

    # Handle SQL dump conflicts: take master version
    for sfile in sql_conflicts:
        log(f"  Taking master version of {sfile}")
        subprocess.run(["git", "checkout", "--theirs", sfile], cwd=project_dir, capture_output=True)
        subprocess.run(["git", "add", sfile], cwd=project_dir, capture_output=True)

    # Handle code conflicts: Claude resolves
    if code_conflicts:
        await _resolve_merge_conflicts(project_dir, code_conflicts, fix_model)

    # Check for remaining unresolved
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=project_dir, capture_output=True, text=True,
    )
    remaining = [f for f in result.stdout.strip().splitlines() if f]

    if remaining:
        error(f"Unresolved conflicts remain — aborting merge")
        error(f"Files: {' '.join(remaining)}")
        subprocess.run(["git", "merge", "--abort"], cwd=project_dir, capture_output=True)
        return True  # Non-fatal

    # Commit merge
    result = subprocess.run(
        ["git", "commit", "--no-edit"],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        warn("Merge commit failed — aborting")
        subprocess.run(["git", "merge", "--abort"], cwd=project_dir, capture_output=True)
        return True

    success("Merge committed")

    # Run queued dump-regen commands
    if regen_commands:
        _run_dump_regen(project_dir, regen_commands, base_branch)

    # Push
    if push_enabled:
        _push_changes(project_dir, push_remote)

    return True


async def _resolve_merge_conflicts(
    project_dir: Path,
    conflict_files: list[str],
    fix_model: str | None = None,
) -> None:
    """Use Claude to resolve merge conflicts in code files."""
    model = resolve_model(fix_model) if fix_model else None

    prompt = (
        "You are resolving git merge conflicts. The following files have conflict markers:\n\n"
        + "\n".join(conflict_files)
        + "\n\nFor EACH file:\n"
        "1. Read the file\n"
        "2. Resolve the conflict markers (<<<<<<< ======= >>>>>>>) keeping the correct combined logic\n"
        "3. Write the resolved file\n"
        "4. Run: git add <file>\n\n"
        "Do NOT ask questions. Resolve all conflicts now."
    )

    log("  Running Claude to resolve code conflicts...")
    result = await run_claude(
        prompt,
        model=model,
        max_turns=50,
        cwd=str(project_dir),
    )

    if result.exit_code != 0:
        warn(f"Claude exited with code {result.exit_code} during conflict resolution")


def _run_dump_regen(
    project_dir: Path,
    regen_commands: list[str],
    base_branch: str,
) -> None:
    """Run dump regeneration commands after merge."""
    log("  Running dump-regen commands...")
    for cmd in regen_commands:
        log(dim(f"    {cmd}"))
        result = subprocess.run(
            ["bash", "-c", cmd],
            cwd=project_dir, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            warn(f"    dump-regen failed (exit {result.returncode}) — continuing")

    # Commit regen changes if any
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=project_dir, capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", f"chore: regenerate dumps after merge with {base_branch}"],
            cwd=project_dir, capture_output=True, text=True,
        )
        success("Committed dump-regen changes")


def _push_changes(project_dir: Path, remote: str = "origin") -> None:
    """Push to remote."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_dir, capture_output=True, text=True,
    )
    branch = result.stdout.strip()
    if not branch:
        return

    log(f"Pushing to {remote}/{branch}...")
    result = subprocess.run(
        ["git", "push", remote, branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        warn(f"Push failed (non-fatal): {result.stderr.strip()}")
    else:
        success("Pushed")
