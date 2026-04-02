"""PR review fixer — fixes issues from GitHub PR review comments.

Reads PR review comments via `gh api` GraphQL, builds a fix prompt,
invokes Claude to fix the issues, commits and pushes.

Implementation details:
- Uses subprocess.PIPE for Claude invocation (no FIFOs)
- Uses json.dumps() for all JSON construction (no shell escaping bugs)
- Caps prompt input at 100k chars
- stdin=subprocess.DEVNULL for all background subprocesses (prevent SIGTSTP)
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from conductor.core.claude import run_claude
from runner.git_ops import git_has_any_changes, git_commit, git_push, git_current_sha
from runner.logging import log, warn, error, dim


_MAX_PROMPT_CHARS = 100_000
_MAX_REVIEW_THREADS = 15
_CI_INITIAL_WAIT = 30   # seconds before first poll


# ─── Data structures ─────────────────────────────────────────────────────────


@dataclass
class FixerConfig:
    project_dir: Path
    branch: str
    feature_name: str
    phase: int
    pr_number: int
    model: Optional[str] = None
    base_branch: Optional[str] = None
    adopted_phases: list[int] = field(default_factory=list)
    sync_mode: bool = False
    ci_poll_interval: int = 60
    ci_max_wait: int = 5400
    skip_patterns: str = "coverage|Coverage|codecov|Codecov"
    sync_dump_regen: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.adopted_phases:
            self.adopted_phases = [self.phase]


# ─── gh wrappers ─────────────────────────────────────────────────────────────


def _gh(args: list[str], cwd: Path) -> str:
    """Run gh CLI and return stdout. Raises on error."""
    result = subprocess.run(
        ["gh"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    return result.stdout.strip()


def _gh_safe(args: list[str], cwd: Path, default: str = "") -> str:
    """Run gh CLI and return stdout; return default on error."""
    try:
        return _gh(args, cwd)
    except subprocess.CalledProcessError:
        return default


def _get_repo_info(cwd: Path) -> tuple[str, str]:
    """Return (owner, repo) tuple from gh repo view."""
    full = _gh_safe(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd)
    if "/" not in full:
        raise ValueError(f"Could not determine repo from {cwd}")
    owner, repo = full.split("/", 1)
    return owner, repo


def _graphql(query: str, variables: dict, cwd: Path) -> dict:
    """Run a GraphQL query and return the parsed JSON result."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if isinstance(value, int):
            args += ["-F", f"{key}={value}"]
        else:
            args += ["-f", f"{key}={value}"]
    raw = _gh_safe(args, cwd, default="{}")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}


# ─── Status tracking ──────────────────────────────────────────────────────────


def write_fixer_status(
    status_file: Path,
    status: str,
    phase: int,
    adopted_phases: list[int],
    detail: str = "",
) -> None:
    """Write fixer status JSON file for tracking."""
    status_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "status": status,
        "phase": phase,
        "pid": os.getpid(),
        "phases": adopted_phases,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    status_file.write_text(json.dumps(data), encoding="utf-8")


# ─── Supersede older fixers ───────────────────────────────────────────────────


def supersede_older_fixers(
    log_dir: Path,
    current_phase: int,
    current_status_file: Path,
) -> list[int]:
    """Kill older fixers in waiting_ci state for same feature, adopt their phases.
    Returns list of all adopted phases (including current)."""
    adopted = [current_phase]

    for status_file in log_dir.glob(".fixer-status-phase-*"):
        if status_file == current_status_file:
            continue
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        old_pid = data.get("pid")
        old_status = data.get("status")
        old_phase = data.get("phase")

        if not old_pid or old_status != "waiting_ci" or not old_phase:
            continue

        # Try to kill the old fixer (guard against PID reuse by checking cmdline)
        try:
            os.kill(old_pid, 0)  # check alive
            # Verify it's actually a fixer process
            try:
                cmdline = Path(f"/proc/{old_pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
                if "fixer" not in cmdline:
                    log(f"PID {old_pid} is alive but not a fixer — skipping")
                    continue
            except OSError:
                pass  # /proc not available or process gone; proceed

            os.kill(old_pid, signal.SIGTERM)
            # Wait briefly for it to die
            for _ in range(20):
                try:
                    os.kill(old_pid, 0)
                    time.sleep(0.5)
                except OSError:
                    break
            log(f"Superseded phase {old_phase} fixer (PID {old_pid})")
        except OSError:
            pass  # already dead — still adopt its phases

        adopted.append(old_phase)
        # Also adopt any phases the old fixer had
        for p in data.get("phases", []):
            if p not in adopted:
                adopted.append(p)

    adopted = sorted(set(adopted))
    return adopted


# ─── Merge conflict detection ─────────────────────────────────────────────────


def check_pr_mergeable(pr_number: int, owner: str, repo: str, cwd: Path) -> str:
    """Check PR merge status. Returns 'MERGEABLE', 'CONFLICTING', or 'UNKNOWN'."""
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          mergeable
        }
      }
    }
    """
    data = _graphql(query, {"owner": owner, "repo": repo, "number": pr_number}, cwd)
    return (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("mergeable", "UNKNOWN")
    )


# ─── CI waiting ──────────────────────────────────────────────────────────────


def _is_skip_check(name: str, skip_patterns: str) -> bool:
    import re
    return bool(re.search(skip_patterns, name))


def wait_for_ci(
    pr_number: int,
    cwd: Path,
    *,
    owner: str = "",
    repo: str = "",
    base_branch: str = "",
    poll_interval: int = 60,
    max_wait: int = 5400,
    skip_patterns: str = "coverage|Coverage|codecov|Codecov",
) -> str:
    """Poll CI until complete. Returns: CI_PASSED / CI_FAILED / CI_TIMEOUT / CI_CONFLICT."""
    log(f"Waiting for CI on PR #{pr_number} (poll: {poll_interval}s, max: {max_wait}s)...")

    time.sleep(_CI_INITIAL_WAIT)
    waited = _CI_INITIAL_WAIT

    while waited < max_wait:
        raw = _gh_safe(
            ["pr", "checks", str(pr_number), "--json", "state,name"],
            cwd,
            default="[]",
        )
        try:
            checks = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            checks = []

        if checks:
            states = {c.get("state", "UNKNOWN") for c in checks}
            pending = any(s in ("PENDING", "IN_PROGRESS", "QUEUED") for s in states)

            if not pending:
                log(f"CI completed after {waited}s")
                failed = [c["name"] for c in checks if c.get("state") == "FAILURE"]
                blocking = [n for n in failed if not _is_skip_check(n, skip_patterns)]
                if blocking:
                    for n in blocking:
                        log(f"  BLOCKING: {n}")
                    return "CI_FAILED"
                return "CI_PASSED"
        else:
            log(f"  No CI checks found after {waited}s")
            # No checks: might be a merge conflict blocking CI
            if base_branch and owner and repo:
                mergeable = check_pr_mergeable(pr_number, owner, repo, cwd)
                log(f"  PR mergeable: {mergeable}")
                if mergeable == "CONFLICTING":
                    return "CI_CONFLICT"

        # Sleep in small chunks so we can be interrupted
        remaining = min(poll_interval, max_wait - waited)
        slept = 0
        while slept < remaining:
            chunk = min(10, remaining - slept)
            time.sleep(chunk)
            slept += chunk
        waited += poll_interval

    # Before timing out, check for merge conflicts
    if base_branch and owner and repo:
        mergeable = check_pr_mergeable(pr_number, owner, repo, cwd)
        log(f"  Timeout — PR mergeable: {mergeable}")
        if mergeable == "CONFLICTING":
            return "CI_CONFLICT"

    log(f"CI timeout after {waited}s")
    return "CI_TIMEOUT"


# ─── Review threads ──────────────────────────────────────────────────────────


def get_unresolved_threads(pr_number: int, owner: str, repo: str, cwd: Path) -> list[dict]:
    """Return list of unresolved review thread dicts."""
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100) {
            nodes {
              id
              isResolved
              comments(first: 10) {
                nodes {
                  body
                  path
                  line
                  author { login }
                }
              }
            }
          }
        }
      }
    }
    """
    data = _graphql(query, {"owner": owner, "repo": repo, "number": pr_number}, cwd)
    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    return [n for n in nodes if not n.get("isResolved", True)]


def resolve_thread(thread_id: str, cwd: Path) -> None:
    """Resolve a single review thread via GraphQL mutation."""
    query = """
    mutation($threadId: ID!) {
      resolveReviewThread(input: {threadId: $threadId}) {
        thread { isResolved }
      }
    }
    """
    _graphql(query, {"threadId": thread_id}, cwd)


def get_ci_failure_logs(pr_number: int, owner: str, repo: str, cwd: Path, skip_patterns: str = "coverage|Coverage|codecov|Codecov") -> str:
    """Return CI failure annotation text, or empty string if none."""
    raw = _gh_safe(
        ["pr", "checks", str(pr_number), "--json", "state,name,detailsUrl"],
        cwd,
        default="[]",
    )
    try:
        checks = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""

    failed_names = [
        c["name"]
        for c in checks
        if c.get("state") == "FAILURE" and not _is_skip_check(c.get("name", ""), skip_patterns)
    ]
    if not failed_names:
        return ""

    # Get head SHA for annotations
    head_sha = _gh_safe(
        ["pr", "view", str(pr_number), "--json", "headRefOid", "-q", ".headRefOid"],
        cwd,
    )

    logs_parts: list[str] = []
    for check_name in failed_names:
        logs_parts.append(f"=== FAILED CHECK: {check_name} ===")
        if head_sha:
            annotations = _gh_safe(
                [
                    "api",
                    f"repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                    "--jq",
                    f'.check_runs[] | select(.name == "{check_name}") | .output.annotations[]? | "\\(.path):\\(.start_line): \\(.annotation_level) - \\(.message)"',
                ],
                cwd,
            )
            if annotations:
                # Limit annotation lines
                annotation_lines = annotations.splitlines()[:50]
                logs_parts.append("\n".join(annotation_lines))
            else:
                logs_parts.append("(No detailed annotations available — check CI dashboard)")
        logs_parts.append("")

    return "\n".join(logs_parts)


# ─── Prompt builder ──────────────────────────────────────────────────────────


def build_fix_prompt(
    cfg: FixerConfig,
    ci_logs: str,
    threads: list[dict],
) -> str:
    phase_label = ",".join(str(p) for p in sorted(cfg.adopted_phases))

    prompt = (
        f"You are fixing issues found by CI and/or PR reviewers.\n\n"
        f"PROJECT: {cfg.project_dir}\n"
        f"FEATURE: {cfg.feature_name} (phases {phase_label})\n"
        f"BRANCH: {cfg.branch}\n\n"
        "AUTONOMOUS EXECUTION RULES:\n"
        "- Fix ALL issues listed below\n"
        "- DO NOT ask for confirmation — just fix the code\n"
        "- You do NOT have Docker access — do NOT attempt to run PHPStan or tests locally\n"
        "- CI will validate your fixes after push\n"
        "- Focus on understanding the error messages and making correct fixes\n"
        "- Commit your changes when done (git add + git commit)\n"
    )

    if ci_logs:
        prompt += f"\n=== CI FAILURES ===\n{ci_logs}\n\nFix the CI failures above. Read the error messages carefully, find the relevant files, and make the corrections.\n"

    if threads:
        display_threads = threads[:_MAX_REVIEW_THREADS]
        omitted = max(0, len(threads) - _MAX_REVIEW_THREADS)

        prompt += f"\n=== PR REVIEW COMMENTS ({len(threads)} unresolved threads) ===\n"
        for t in display_threads:
            comments = t.get("comments", {}).get("nodes", [])
            if comments:
                first = comments[0]
                path = first.get("path", "unknown")
                line = first.get("line", "?")
                prompt += f"\n--- Thread ---\nFile: {path}:{line}\n"
                for c in comments:
                    author = c.get("author", {}).get("login", "?")
                    body = c.get("body", "")
                    prompt += f"[{author}]: {body}\n"

        if omitted:
            prompt += f"\n({omitted} more threads omitted — address the above first)\n"

        prompt += "\nAddress each review comment by making the requested changes.\n"

    prompt += f"\nSTART NOW: Read the errors/comments above, then fix each one.\nWhen all fixes are done, commit with: git add -A && git commit -m \"fix: CI/review fixes for phases {phase_label}\"\n"

    # Hard cap
    if len(prompt) > _MAX_PROMPT_CHARS:
        overflow = len(prompt) - _MAX_PROMPT_CHARS
        log(f"Prompt too large ({len(prompt)} chars) — truncating by {overflow} chars")
        prompt = prompt[:_MAX_PROMPT_CHARS] + f"\n\n... (prompt truncated — {overflow} chars removed. Focus on the issues shown above.)\n"

    return prompt


# ─── Worktree helpers ─────────────────────────────────────────────────────────


def _git_run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )


def _worktree_add(project_dir: Path, worktree_dir: Path, fix_branch: str, base_branch: str) -> bool:
    """Create a worktree with a new fix branch based on base_branch. Returns True on success."""
    # Clean up stale worktree/branch if they exist
    _git_run(["worktree", "remove", str(worktree_dir)], project_dir, check=False)
    _git_run(["branch", "-D", fix_branch], project_dir, check=False)
    result = _git_run(
        ["worktree", "add", str(worktree_dir), "-b", fix_branch, base_branch],
        project_dir,
        check=False,
    )
    return result.returncode == 0


def _worktree_remove(project_dir: Path, worktree_dir: Path, fix_branch: str) -> None:
    """Remove worktree and delete local fix branch."""
    _git_run(["worktree", "remove", str(worktree_dir)], project_dir, check=False)
    _git_run(["branch", "-D", fix_branch], project_dir, check=False)


# ─── CI conflict resolution ───────────────────────────────────────────────────


async def _resolve_conflict(
    cfg: FixerConfig,
    cwd: Path,
    owner: str,
    repo: str,
) -> bool:
    """Fetch base branch, merge it, resolve conflicts. Returns True if push succeeded."""
    base_branch = cfg.base_branch or "main"

    _git_run(["fetch", "origin", base_branch], cwd, check=False)

    result = _git_run(
        ["merge", f"origin/{base_branch}", "--no-edit"],
        cwd,
        check=False,
    )

    regen_commands: list[str] = []

    if result.returncode != 0:
        # Collect conflicting files
        conflict_result = _git_run(["diff", "--name-only", "--diff-filter=U"], cwd, check=False)
        conflict_files = [f for f in conflict_result.stdout.splitlines() if f.strip()]

        if not conflict_files:
            log("  Merge failed but no conflicts detected — aborting")
            return False

        log(f"  Conflicts: {' '.join(conflict_files)}")

        # Handle SQL dump conflicts: take --theirs, queue regen
        for cfile in conflict_files[:]:
            for pattern, command in cfg.sync_dump_regen:
                import fnmatch
                if fnmatch.fnmatch(cfile, pattern):
                    log(f"  Taking theirs for {cfile}")
                    _git_run(["checkout", "--theirs", cfile], cwd, check=False)
                    _git_run(["add", cfile], cwd, check=False)
                    if command not in regen_commands:
                        regen_commands.append(command)
                    break

        # Remaining code conflicts: let Claude resolve
        remaining_result = _git_run(["diff", "--name-only", "--diff-filter=U"], cwd, check=False)
        remaining = [f for f in remaining_result.stdout.splitlines() if f.strip()]

        if remaining:
            log(f"  Code conflicts: {' '.join(remaining)}")
            resolve_prompt = (
                "You are resolving git merge conflicts. The following files have conflict markers:\n\n"
                + "\n".join(remaining)
                + "\n\nFor EACH file:\n"
                "1. Read the file\n"
                "2. Resolve the conflict markers (<<<<<<< ======= >>>>>>>) keeping the correct combined logic\n"
                "3. Write the resolved file\n"
                "4. Run: git add <file>\n\n"
                "Do NOT ask questions. Resolve all conflicts now."
            )
            try:
                await run_claude(
                    resolve_prompt,
                    model=cfg.model,
                    max_turns=50,
                    cwd=str(cwd),
                )
            except Exception as exc:
                error(f"Claude conflict resolution failed: {exc}")

        # Check for still-unresolved conflicts
        still_result = _git_run(["diff", "--name-only", "--diff-filter=U"], cwd, check=False)
        still_unresolved = [f for f in still_result.stdout.splitlines() if f.strip()]
        if still_unresolved:
            log(f"  Unresolved conflicts remain: {' '.join(still_unresolved)} — aborting")
            _git_run(["merge", "--abort"], cwd, check=False)
            return False

        # Commit the merge
        _git_run(["commit", "--no-edit"], cwd, check=False)

    # Run queued dump-regen commands
    if regen_commands:
        log("  Running dump-regen commands...")
        for cmd in regen_commands:
            log(f"    {cmd}")
            try:
                subprocess.run(
                    cmd,
                    shell=True,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    check=False,
                    timeout=300,
                )
            except subprocess.TimeoutExpired:
                log(f"    dump-regen timed out after 300s — continuing")
        # Commit regen changes if any
        _git_run(["add", "-A"], cwd, check=False)
        staged_result = _git_run(["diff", "--cached", "--quiet"], cwd, check=False)
        if staged_result.returncode != 0:
            base_branch_name = cfg.base_branch or "main"
            _git_run(
                ["commit", "-m", f"chore: regenerate dumps after merge with {base_branch_name}"],
                cwd,
                check=False,
            )
            log("  Committed dump-regen changes")

    return True


# ─── Main fixer logic ────────────────────────────────────────────────────────


async def run_fixer(cfg: FixerConfig) -> None:
    """Run the full fixer flow for a given phase/PR.

    Steps:
      1. Supersede older fixers (async mode only), write status
      2. Wait for CI
      3. Handle CI_CONFLICT: merge base branch, resolve conflicts, push fix PR
      4. Get unresolved review threads + CI failure logs
      5. If nothing to fix: exit clean
      6. Setup working directory (worktree in async mode, project_dir in sync)
      7. Run Claude to fix issues
      8. Commit any changes
      9. Push
      10. Create fix PR (async mode) or push directly (sync mode)
      11. Resolve review threads
      12. Cleanup worktree (async mode)
    """
    cwd = cfg.project_dir
    owner, repo = _get_repo_info(cwd)

    log_dir = cwd / "storage" / "logs" / f"{cfg.feature_name}-build"
    log_dir.mkdir(parents=True, exist_ok=True)
    status_file = log_dir / f".fixer-status-phase-{cfg.phase}"

    fix_branch = f"fix/{cfg.branch}-phase-{cfg.phase}"
    worktree_dir = Path(f"/tmp/conductor-fix-{cfg.feature_name}-phase-{cfg.phase}-{os.getpid()}")

    # Step 1: Supersede older fixers (async mode only)
    if not cfg.sync_mode:
        adopted = supersede_older_fixers(log_dir, cfg.phase, status_file)
        cfg.adopted_phases = adopted
        if len(adopted) > 1:
            log(f"Now responsible for phases: {adopted}")

    write_fixer_status(status_file, "waiting_ci", cfg.phase, cfg.adopted_phases)

    # Step 2: wait for CI
    ci_result = wait_for_ci(
        cfg.pr_number,
        cwd,
        owner=owner,
        repo=repo,
        base_branch=cfg.base_branch or "",
        poll_interval=cfg.ci_poll_interval,
        max_wait=cfg.ci_max_wait,
        skip_patterns=cfg.skip_patterns,
    )
    log(f"CI result: {ci_result}")

    # Step 3: handle merge conflicts
    if ci_result == "CI_CONFLICT":
        write_fixer_status(status_file, "fixing_conflict", cfg.phase, cfg.adopted_phases)

        if cfg.sync_mode:
            log(f"PR has merge conflicts — resolving directly on {cfg.branch}")
            work_dir = cwd
        else:
            log("PR has merge conflicts — creating fix branch with merge resolution")
            if not _worktree_add(cwd, worktree_dir, fix_branch, cfg.branch):
                error("Failed to create worktree for conflict resolution")
                write_fixer_status(status_file, "conflict_unresolvable", cfg.phase, cfg.adopted_phases, "worktree creation failed")
                return
            work_dir = worktree_dir

        success = await _resolve_conflict(cfg, work_dir, owner, repo)

        if not success:
            write_fixer_status(status_file, "conflict_unresolvable", cfg.phase, cfg.adopted_phases, "Could not resolve all merge conflicts")
            if not cfg.sync_mode:
                _worktree_remove(cwd, worktree_dir, fix_branch)
            return

        # Push
        push_branch = cfg.branch if cfg.sync_mode else fix_branch
        log(f"Pushing to {push_branch}...")
        push_result = _git_run(["push", "origin", push_branch], work_dir, check=False)
        if push_result.returncode != 0:
            error(f"Push failed: {push_result.stderr.strip()}")
            write_fixer_status(status_file, "push_failed", cfg.phase, cfg.adopted_phases, "Could not push conflict fix")
            if not cfg.sync_mode:
                _worktree_remove(cwd, worktree_dir, fix_branch)
            return

        # Create fix PR (async mode only)
        if not cfg.sync_mode:
            base_branch_name = cfg.base_branch or "main"
            log(f"Creating fix PR targeting {cfg.branch}...")
            pr_result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--base", cfg.branch,
                    "--head", fix_branch,
                    "--title", f"fix: merge {base_branch_name} into {cfg.branch}",
                    "--body", (
                        f"Automated merge of {base_branch_name} to resolve conflicts blocking CI.\n\n"
                        "Created by conductor background fixer."
                    ),
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            if pr_result.returncode == 0:
                log(f"Fix PR created: {pr_result.stdout.strip()}")
            else:
                log(f"Fix PR creation failed: {pr_result.stderr.strip()}")

            _worktree_remove(cwd, worktree_dir, fix_branch)

        write_fixer_status(status_file, "done", cfg.phase, cfg.adopted_phases, "Conflict fix resolved")
        log("Fixer complete (conflict resolution)")
        return

    # Step 4: gather issues
    threads = get_unresolved_threads(cfg.pr_number, owner, repo, cwd)
    log(f"Unresolved review threads: {len(threads)}")

    ci_logs = ""
    if ci_result == "CI_FAILED":
        ci_logs = get_ci_failure_logs(cfg.pr_number, owner, repo, cwd, cfg.skip_patterns)

    if not ci_logs and not threads:
        log("No issues found — CI passed and no review comments")
        write_fixer_status(status_file, "clean", cfg.phase, cfg.adopted_phases, "No fixes needed")
        return

    write_fixer_status(status_file, "fixing", cfg.phase, cfg.adopted_phases)

    # Step 5: build prompt
    prompt = build_fix_prompt(cfg, ci_logs, threads)

    # Step 6: setup working directory
    if cfg.sync_mode:
        log(f"Running fixer directly in {cwd}...")
        work_dir = cwd
    else:
        log(f"Creating worktree at {worktree_dir}...")
        if not _worktree_add(cwd, worktree_dir, fix_branch, cfg.branch):
            error("Failed to create worktree")
            write_fixer_status(status_file, "error", cfg.phase, cfg.adopted_phases, "worktree creation failed")
            return
        work_dir = worktree_dir

    # Step 7: run Claude
    pre_sha = git_current_sha(work_dir)
    log(f"Running Claude fixer in {work_dir}...")

    try:
        result = await run_claude(
            prompt,
            model=cfg.model,
            max_turns=200,
            cwd=str(work_dir),
        )
        if result.exit_code != 0:
            log(f"Claude exited with code {result.exit_code}")
    except Exception as exc:
        error(f"Claude fixer failed: {exc}")
        if not cfg.sync_mode:
            _worktree_remove(cwd, worktree_dir, fix_branch)
        return

    # Step 8: check for changes
    has_changes = git_has_any_changes(work_dir) or git_current_sha(work_dir) != pre_sha
    if not has_changes:
        log("No changes made by fixer")
        write_fixer_status(status_file, "no_changes", cfg.phase, cfg.adopted_phases, "Claude made no changes")
        if not cfg.sync_mode:
            _worktree_remove(cwd, worktree_dir, fix_branch)
        return

    # Commit any uncommitted changes
    phase_label = ",".join(str(p) for p in sorted(cfg.adopted_phases))
    if git_has_any_changes(work_dir):
        subprocess.run(
            ["git", "add", "-A"],
            cwd=work_dir,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        git_commit(work_dir, f"fix: CI/review fixes for phases {phase_label}")

    # Step 9: push
    push_branch = cfg.branch if cfg.sync_mode else fix_branch
    log(f"Pushing to {push_branch}...")
    push_result = _git_run(["push", "origin", push_branch], work_dir, check=False)
    if push_result.returncode != 0:
        error(f"Push failed: {push_result.stderr.strip()}")
        write_fixer_status(status_file, "push_failed", cfg.phase, cfg.adopted_phases, "Could not push fixes")
        if not cfg.sync_mode:
            _worktree_remove(cwd, worktree_dir, fix_branch)
        return

    # Step 10: create fix PR (async mode only)
    if not cfg.sync_mode:
        log(f"Creating fix PR targeting {cfg.branch}...")
        pr_result = subprocess.run(
            [
                "gh", "pr", "create",
                "--base", cfg.branch,
                "--head", fix_branch,
                "--title", f"fix: CI/review fixes for phases {phase_label}",
                "--body", (
                    f"Automated fixes for CI failures and/or PR review comments on phases {phase_label}.\n\n"
                    "Created by conductor background fixer."
                ),
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        if pr_result.returncode == 0:
            log(f"Fix PR created: {pr_result.stdout.strip()}")
        else:
            log(f"Fix PR creation failed: {pr_result.stderr.strip()}")

    # Step 11: resolve review threads
    if threads:
        log(f"Resolving {len(threads)} review threads...")
        for t in threads:
            tid = t.get("id", "")
            if tid:
                try:
                    resolve_thread(tid, cwd)
                    log(f"  Resolved thread {tid}")
                except Exception as exc:
                    warn(f"  Could not resolve thread {tid}: {exc}")

    # Step 12: cleanup worktree
    if not cfg.sync_mode:
        _worktree_remove(cwd, worktree_dir, fix_branch)

    write_fixer_status(status_file, "done", cfg.phase, cfg.adopted_phases, "Fixes applied")
    log("Fixer complete")
