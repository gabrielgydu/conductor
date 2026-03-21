"""PR review fixer — port of ralph/lib/fixer.sh.

Reads PR review comments via `gh api` GraphQL, builds a fix prompt,
invokes Claude to fix the issues, commits and pushes.

Key differences from the bash version:
- Uses subprocess.PIPE for Claude invocation (no FIFOs)
- Uses json.dumps() for all JSON construction (no shell escaping bugs)
- Caps prompt input at 100k chars
- stdin=subprocess.DEVNULL for all background subprocesses (prevent SIGTSTP)
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from conductor.core.claude import run_claude
from runner.git_ops import git_has_any_changes, git_commit, git_push, git_current_sha
from runner.logging import log, warn, error, dim


_MAX_PROMPT_CHARS = 100_000
_MAX_REVIEW_THREADS = 15
_CI_POLL_INTERVAL = 60  # seconds
_CI_MAX_WAIT = 5400     # 90 minutes
_CI_INITIAL_WAIT = 30   # seconds before first poll
_CI_SKIP_PATTERNS = ("coverage", "Coverage", "codecov", "Codecov")


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


# ─── CI waiting ──────────────────────────────────────────────────────────────


def _is_skip_check(name: str) -> bool:
    return any(pat in name for pat in _CI_SKIP_PATTERNS)


def wait_for_ci(pr_number: int, cwd: Path) -> str:
    """Poll CI until complete. Returns: CI_PASSED / CI_FAILED / CI_TIMEOUT / CI_CONFLICT."""
    log(f"Waiting for CI on PR #{pr_number} (max {_CI_MAX_WAIT}s)...")

    time.sleep(_CI_INITIAL_WAIT)
    waited = _CI_INITIAL_WAIT

    while waited < _CI_MAX_WAIT:
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
                blocking = [n for n in failed if not _is_skip_check(n)]
                if blocking:
                    for n in blocking:
                        log(f"  BLOCKING: {n}")
                    return "CI_FAILED"
                return "CI_PASSED"
        else:
            log(f"  No CI checks found after {waited}s")

        # Sleep in small chunks so we can be interrupted
        remaining = min(_CI_POLL_INTERVAL, _CI_MAX_WAIT - waited)
        slept = 0
        while slept < remaining:
            chunk = min(10, remaining - slept)
            time.sleep(chunk)
            slept += chunk
        waited += _CI_POLL_INTERVAL

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


def get_ci_failure_logs(pr_number: int, owner: str, repo: str, cwd: Path) -> str:
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
        if c.get("state") == "FAILURE" and not _is_skip_check(c.get("name", ""))
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


# ─── Main fixer logic ────────────────────────────────────────────────────────


async def run_fixer(cfg: FixerConfig) -> None:
    """Run the full fixer flow for a given phase/PR.

    Steps:
      1. Wait for CI
      2. Get unresolved review threads + CI failure logs
      3. If nothing to fix: exit clean
      4. Run Claude to fix issues
      5. Commit any changes
      6. Push
      7. Create fix PR (async mode) or push directly (sync mode)
      8. Resolve review threads
    """
    cwd = cfg.project_dir
    owner, repo = _get_repo_info(cwd)

    # Step 1: wait for CI
    ci_result = wait_for_ci(cfg.pr_number, cwd)
    log(f"CI result: {ci_result}")

    # Step 2: gather issues
    threads = get_unresolved_threads(cfg.pr_number, owner, repo, cwd)
    log(f"Unresolved review threads: {len(threads)}")

    ci_logs = ""
    if ci_result == "CI_FAILED":
        ci_logs = get_ci_failure_logs(cfg.pr_number, owner, repo, cwd)

    if not ci_logs and not threads:
        log("No issues found — CI passed and no review comments")
        return

    # Step 3: build prompt
    prompt = build_fix_prompt(cfg, ci_logs, threads)

    # Step 4: run Claude
    pre_sha = git_current_sha(cwd)
    log(f"Running Claude fixer in {cwd}...")

    try:
        result = await run_claude(
            prompt,
            model=cfg.model,
            max_turns=200,
            cwd=str(cwd),
        )
        if result.exit_code != 0:
            log(f"Claude exited with code {result.exit_code}")
    except Exception as exc:
        error(f"Claude fixer failed: {exc}")
        return

    # Step 5: check for changes
    has_changes = git_has_any_changes(cwd) or git_current_sha(cwd) != pre_sha
    if not has_changes:
        log("No changes made by fixer")
        return

    # Commit any uncommitted changes
    phase_label = ",".join(str(p) for p in sorted(cfg.adopted_phases))
    if git_has_any_changes(cwd):
        subprocess.run(
            ["git", "add", "-A"],
            cwd=cwd,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        git_commit(cwd, f"fix: CI/review fixes for phases {phase_label}")

    # Step 6: push
    push_ok = git_push(cwd, "origin")
    if not push_ok:
        error("Push failed")
        return

    # Step 7: resolve review threads
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

    log("Fixer complete")
