"""Git operations for the runner."""
from __future__ import annotations

import subprocess
from pathlib import Path

from runner.logging import log, warn, dim


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )


def git_stage_all(cwd: Path) -> None:
    """Stage all changes (git add -A)."""
    _run(["git", "add", "-A"], cwd=cwd)


def git_unstage_all(cwd: Path) -> None:
    """Unstage all staged changes (git reset HEAD --)."""
    _run(["git", "reset", "HEAD", "--", "."], cwd=cwd, check=False)


def git_has_staged_changes(cwd: Path) -> bool:
    """Return True if there are staged changes."""
    result = _run(["git", "diff", "--cached", "--quiet"], cwd=cwd, check=False)
    return result.returncode != 0


def git_has_any_changes(cwd: Path) -> bool:
    """Return True if there are staged or unstaged changes."""
    staged = _run(["git", "diff", "--cached", "--quiet"], cwd=cwd, check=False)
    unstaged = _run(["git", "diff", "--quiet"], cwd=cwd, check=False)
    return staged.returncode != 0 or unstaged.returncode != 0


def git_commit(cwd: Path, message: str) -> tuple[bool, str]:
    """Attempt a git commit. Returns (success, output)."""
    result = _run(["git", "commit", "-m", message], cwd=cwd, check=False)
    output = result.stdout + result.stderr
    return result.returncode == 0, output


def git_push(cwd: Path, remote: str) -> bool:
    """Push current branch to remote. Returns True on success."""
    branch_result = _run(["git", "branch", "--show-current"], cwd=cwd, check=False)
    branch = branch_result.stdout.strip()
    if not branch:
        warn("Could not determine current branch — skipping push")
        return False

    result = _run(["git", "push", remote, branch], cwd=cwd, check=False)
    if result.returncode != 0:
        warn(f"Push failed (non-fatal): {result.stderr.strip()}")
        return False

    log(dim(f"  Pushed to {remote}/{branch}"))
    return True


def git_snapshot_untracked(cwd: Path, snapshot_file: Path) -> set[str]:
    """Snapshot currently untracked files to a file; return the set.

    Reuses existing snapshot if it exists (for cross-iteration consistency).
    """
    if snapshot_file.exists():
        existing = {
            line for line in snapshot_file.read_text("utf-8").splitlines() if line.strip()
        }
        log(dim(f"  Reusing untracked snapshot ({len(existing)} files)"))
        return existing

    result = _run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd,
        check=False,
    )
    files = {
        line for line in result.stdout.splitlines() if line.strip()
    }
    snapshot_file.write_text("\n".join(sorted(files)), "utf-8")
    log(dim(f"  Saved untracked snapshot ({len(files)} files)"))
    return files


def git_unstage_pre_existing(cwd: Path, pre_existing: set[str]) -> None:
    """Unstage files that were already untracked before this phase."""
    if not pre_existing:
        return
    result = subprocess.run(
        ["git", "reset", "HEAD", "--"] + sorted(pre_existing),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    _ = result  # ignore errors; files may not exist


def git_current_sha(cwd: Path) -> str:
    result = _run(["git", "rev-parse", "HEAD"], cwd=cwd, check=False)
    return result.stdout.strip()
