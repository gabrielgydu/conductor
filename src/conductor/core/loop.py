"""Conductor loop engine — persistent prompt loop with task checklist."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, ConfigDict

from conductor.core.claude import resolve_model, run_claude
from conductor.core.launch import claude_session_script
from conductor.core.logging import live_log
from conductor.core.model_catalog import set_effort
from conductor.core.models import atomic_save
from conductor.core.presets import load_preset
from conductor.core.storage import StorageResolver

if TYPE_CHECKING:
    from conductor.core.tmux import TmuxManager


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


class LoopTask(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    index: int
    name: str
    description: str
    status: str = "pending"  # pending | in_progress | completed | failed
    commit: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    attempts: int = 0
    # Output of the last rejected commit (pre-commit hook), shown to the next session
    commit_error: Optional[str] = None


class LoopState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    name: str
    base_branch: str
    worktree: Optional[str] = None
    branch: Optional[str] = None
    plan_file: str
    preset: Optional[str] = None
    tasks: list[LoopTask] = []
    session_count: int = 0
    current_task_index: int = 0
    status: str = "pending"  # pending | running | completed | failed
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    max_turns: int = 200


# ---------------------------------------------------------------------------
# Plan decomposition (Claude-based)
# ---------------------------------------------------------------------------

_DECOMPOSE_PROMPT = """\
You are decomposing a free-form improvement/fix plan into a structured task checklist.

## Rules

1. Each task must be a single, atomic unit of work that Claude can complete in one session.
2. Tasks should be ordered by execution priority (as suggested in the plan).
3. If the plan has phases/sections, preserve that ordering.
4. Large sections should be split into multiple tasks. Each task should take roughly 5-30 minutes of focused coding.
5. Each task name should be short (under 80 chars) and describe what to DO, not what's wrong.
6. Each task description should include ALL context needed to complete it — file paths, specific selectors, code snippets, etc. Copy relevant details from the plan.
7. Do NOT omit details from the plan. If the plan says "change X to Y in file Z", the task description must include X, Y, and Z.

## Output Format

Output ONLY a markdown checklist. No preamble, no explanation. Each line:

- [ ] **Task name** — Detailed description with all context from the plan needed to complete this task.

## Plan to Decompose

{PLAN}
"""


def _stream_events(output: str) -> Iterator[dict]:
    """Yield the JSON objects in stream-json *output*, skipping anything else."""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _extract_text_from_result(output: str) -> str:
    """Extract assistant text content from stream-json output."""
    text_parts = []
    for event in _stream_events(output):
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
    return "\n".join(text_parts)


_CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[[ x]\]\s*(.+)$", re.MULTILINE)


def parse_checklist(checklist_text: str) -> list[LoopTask]:
    """Parse tasks from a markdown checklist (- [ ] **Name** — Description)."""
    tasks = []
    for i, m in enumerate(_CHECKLIST_RE.finditer(checklist_text)):
        text = m.group(1).strip()
        # Try to split on **name** — description
        bold_match = re.match(r"\*\*(.+?)\*\*\s*[—–-]\s*(.+)", text, re.DOTALL)
        if bold_match:
            name = bold_match.group(1).strip()[:80]
            desc = bold_match.group(2).strip()
        else:
            # Fallback: split on " — " or use whole line
            parts = re.split(r"\s+[—–-]\s+", text, maxsplit=1)
            name = parts[0].strip()[:80]
            desc = parts[1].strip() if len(parts) > 1 else text
        tasks.append(LoopTask(index=i, name=name, description=desc))
    return tasks


def decompose_plan(plan_content: str, model: str = "sonnet") -> list[LoopTask]:
    """Use Claude to decompose a free-form plan into structured tasks."""
    prompt = _DECOMPOSE_PROMPT.replace("{PLAN}", plan_content)

    print("Decomposing plan into tasks via Claude...", file=sys.stderr)
    result = asyncio.run(run_claude(prompt, model=resolve_model(model), max_turns=1))

    checklist_text = _extract_text_from_result(result.output)
    if not checklist_text.strip():
        raise ValueError("Claude returned empty response when decomposing plan")

    tasks = parse_checklist(checklist_text)
    if not tasks:
        # Maybe Claude output without checkboxes — try the raw text
        raise ValueError(
            f"Could not parse tasks from Claude's response. "
            f"Expected '- [ ] task' format. Got:\n{checklist_text[:500]}"
        )

    return tasks, checklist_text


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_TASK_COMPLETED_TAG = "<task-completed/>"
# How much of a rejected commit's output the next prompt quotes
_COMMIT_ERROR_PROMPT_CHARS = 4000


def build_prompt(state: LoopState, plan_content: str) -> str:
    """Build the prompt Claude sees each session.

    Includes both the original plan (full context) and the task checklist (progress).
    """
    checklist_lines = []
    for t in state.tasks:
        if t.status == "completed":
            commit_info = f" (commit {t.commit})" if t.commit else ""
            checklist_lines.append(f"- [x] **{t.name}**{commit_info}")
        elif t.index == state.current_task_index:
            checklist_lines.append(f"- [ ] **{t.name}**  <-- YOU ARE HERE")
        else:
            checklist_lines.append(f"- [ ] **{t.name}**")

    checklist = "\n".join(checklist_lines)

    current_task = None
    for t in state.tasks:
        if t.index == state.current_task_index:
            current_task = t
            break

    current_section = ""
    if current_task:
        current_section = (
            f"\n## Current Task\n\n"
            f"**{current_task.name}**\n\n"
            f"{current_task.description}\n"
        )
        if current_task.commit_error:
            current_section += (
                f"\n## Commit Rejected\n\n"
                f"The orchestrator's last attempt to commit this task's changes was "
                f"rejected (usually a pre-commit hook):\n\n"
                f"```\n{current_task.commit_error.strip()[-_COMMIT_ERROR_PROMPT_CHARS:]}\n```\n\n"
                f"Fix the cause before you signal completion. Do not bypass or disable the hook.\n"
            )

    return (
        f"You are working through a fix/improvement plan. "
        f"This is session #{state.session_count + 1}.\n\n"
        f"## Task Progress\n\n{checklist}\n"
        f"{current_section}\n"
        f"## Instructions\n\n"
        f"1. Work on the current task (marked with <-- YOU ARE HERE)\n"
        f"2. When you have FULLY completed the current task, output exactly this, on a line by itself: "
        f"{_TASK_COMPLETED_TAG}\n"
        f"3. After outputting the tag, stop and wait. The orchestrator will handle committing and advancing.\n"
        f"4. If you cannot complete the task, explain why and then output, on a line by itself: <task-failed/>\n\n"
        f"## Original Plan (full context)\n\n{plan_content}\n"
    )


# ---------------------------------------------------------------------------
# Claude output parsing
# ---------------------------------------------------------------------------


def session_reached_model(output: str) -> bool:
    """True if the session got at least one real assistant message.

    False means Claude never got to work: empty output (claude or
    stream_filter did not start) or only the synthetic message the CLI
    emits for an API error such as an unknown model, an expired login or a
    usage limit (flagged ``is_api_error_message``).
    """
    return any(
        e.get("type") == "assistant" and not e.get("is_api_error_message")
        for e in _stream_events(output)
    )


def session_error(output: str) -> str:
    """The error the CLI reported in the session's result event, if any."""
    for event in _stream_events(output):
        if event.get("type") == "result" and event.get("is_error"):
            return str(event.get("result") or event.get("subtype") or "")
    return ""


# A signal tag counts only on a line of its own, so prose that mentions it
# ("I will not output <task-completed/> yet") is not mistaken for it.
_SIGNAL_RE = re.compile(r"^[ \t]*<task-(completed|failed)\s*/>[ \t]*$", re.MULTILINE)


def check_output_for_signal(output: str) -> str | None:
    """Check Claude's output for task-completed or task-failed signal.

    Returns 'completed', 'failed', or None. If both appear, the last wins.
    """
    signals = _SIGNAL_RE.findall(_extract_text_from_result(output))
    return signals[-1] if signals else None


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


def create_loop_worktree(
    project_dir: Path, branch_name: str, base_branch: str,
    worktrees_base: Path | None = None,
) -> Path:
    """Create a git worktree for the loop."""
    if worktrees_base:
        wt_path = worktrees_base / branch_name
    else:
        wt_path = project_dir.parent / f"{project_dir.name}-{branch_name}"
    if wt_path.exists():
        return wt_path

    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(wt_path), base_branch],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
    )
    return wt_path


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


class CommitRejected(Exception):
    """git refused to stage or commit, usually a pre-commit hook; ``output`` says why."""

    def __init__(self, output: str) -> None:
        super().__init__(output.strip() or "git commit failed")
        self.output = output


_SUBJECT_MAX = 72


def _commit_subject(name: str) -> str:
    """Shorten a task name to a commit subject, cutting at a word boundary.

    Task names are the checklist line clipped to 80 characters, so a cut can
    land inside a `code span`; an unbalanced backtick is dropped with the
    words after it.
    """
    name = " ".join(name.split())
    if len(name) <= _SUBJECT_MAX:
        return name
    cut = name[:_SUBJECT_MAX - 1]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    if cut.count("`") % 2:
        cut = cut[:cut.rindex("`")]
    cut = cut.rstrip(" ,;:—–-")
    return (cut or name[:_SUBJECT_MAX - 1]) + "…"


def commit_task(cwd: Path, task: LoopTask) -> str | None:
    """Stage all changes and commit. Returns commit hash, or None if nothing changed.

    Raises CommitRejected (with git's output) when git refuses the commit.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if not status.stdout.strip():
        return None

    msg = f"{_commit_subject(task.name)}\n\nTask {task.index}: {task.description[:200]}"
    for args in (["git", "add", "-A"], ["git", "commit", "-m", msg]):
        r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
        if r.returncode != 0:
            raise CommitRejected(r.stdout + r.stderr)

    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(event: str, message: str, log_path: Path | None, audit_path: Path | None, **kw) -> None:
    live_log(event, message, audit_data=kw or None, log_path=log_path, audit_path=audit_path)


# ---------------------------------------------------------------------------
# Zombie / stall detection
# ---------------------------------------------------------------------------

# Consecutive shallow-pstree polls before declaring zombie
_ZOMBIE_SHALLOW_THRESHOLD = 3
# Seconds with no new output before declaring stall (Claude stuck)
_STALL_TIMEOUT_SECS = 1800  # 30 minutes
# Seconds between checks while waiting for a session's exit file
_EXIT_POLL_SECS = 15


async def _wait_for_exit(
    exit_file: Path,
    output_file: Path,
    tmux: "TmuxManager",
    window_name: str,
    log_path: Path | None,
    audit_path: Path | None,
) -> None:
    """Poll for exit file with zombie and stall detection.

    Detects two failure modes that leave the loop stuck forever:
    1. Zombie — Claude exited but orphaned children hold the pipe open,
       preventing stream_filter EOF and exit-file creation.
    2. Stall — Claude is stuck (e.g. waiting on hung children) and has
       produced no output for an extended period.
    """
    from conductor.core.tmux import check_pstree_depth

    shallow_count = 0
    last_activity = time.monotonic()
    last_output_size = 0

    while not exit_file.exists():
        await asyncio.sleep(_EXIT_POLL_SECS)

        # ---- window disappeared (crash, user killed it) ----
        if not await tmux.is_window_alive(window_name):
            _log("ZOMBIE_DETECTED",
                 f"Window '{window_name}' gone, writing synthetic exit",
                 log_path, audit_path)
            exit_file.write_text("1")
            return

        # ---- track output progress ----
        if output_file.exists():
            try:
                sz = output_file.stat().st_size
                if sz > last_output_size:
                    last_output_size = sz
                    last_activity = time.monotonic()
                    shallow_count = 0
            except OSError:
                pass

        # ---- zombie: Claude exited, pipe stuck ----
        pane_pid = await tmux.get_pane_pid(window_name)
        if pane_pid is not None and not check_pstree_depth(pane_pid):
            shallow_count += 1
            if shallow_count >= _ZOMBIE_SHALLOW_THRESHOLD:
                _log("ZOMBIE_DETECTED",
                     f"Shallow process tree ({shallow_count} consecutive checks), "
                     f"killing '{window_name}'",
                     log_path, audit_path)
                await tmux.kill_window(window_name)
                exit_file.write_text("1")
                return
        elif pane_pid is not None:
            shallow_count = 0

        # ---- stall: no output for too long ----
        stall_secs = time.monotonic() - last_activity
        if stall_secs > _STALL_TIMEOUT_SECS:
            _log("STALL_DETECTED",
                 f"No output for {int(stall_secs)}s, killing '{window_name}'",
                 log_path, audit_path)
            await tmux.kill_window(window_name)
            exit_file.write_text("1")
            return


# ---------------------------------------------------------------------------
# Task bookkeeping
# ---------------------------------------------------------------------------

# Sessions a task gets before it is marked failed and the loop moves on
_MAX_ATTEMPTS_PER_TASK = 3
# Sessions in a row in which Claude produced nothing before the loop stops
_MAX_EMPTY_SESSIONS = 2


def _fail_task(
    state: LoopState, task: LoopTask, why: str,
    log_path: Path | None, audit_path: Path | None,
) -> None:
    """Mark *task* failed and move the loop on to the next one."""
    task.status = "failed"
    state.current_task_index += 1
    _log(
        "TASK_FAILED",
        f"Task {task.index} '{task.name}' failed after {task.attempts} attempts: {why}",
        log_path, audit_path, task_index=task.index,
    )


# ---------------------------------------------------------------------------
# Session files
# ---------------------------------------------------------------------------


@dataclass
class _SessionFiles:
    """Files one Claude session reads and writes, in the loop's conductor dir.

    They used to live in /tmp keyed by loop name only, so two projects
    running a loop with the same name overwrote each other's files.
    """

    prompt: Path
    output: Path
    exit: Path
    stderr: Path

    @classmethod
    def create(cls, session_dir: Path, task_index: int, prompt: str) -> _SessionFiles:
        """Clear leftovers from an earlier session and write *prompt*."""
        session_dir.mkdir(parents=True, exist_ok=True)
        stem = f"task-{task_index}"
        files = cls(
            prompt=session_dir / f"{stem}-prompt.md",
            output=session_dir / f"{stem}-output.jsonl",
            exit=session_dir / f"{stem}-exit",
            stderr=session_dir / f"{stem}-stderr.log",
        )
        files.remove()
        files.prompt.write_text(prompt, encoding="utf-8")
        return files

    def exit_code(self) -> int:
        try:
            return int(self.exit.read_text().strip())
        except (ValueError, OSError):
            return 1

    def output_text(self) -> str:
        try:
            return self.output.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stderr_tail(self, lines: int = 20) -> str:
        try:
            text = self.stderr.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])

    def remove(self) -> None:
        for f in (self.prompt, self.output, self.exit, self.stderr):
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Loop runner (tmux-based)
# ---------------------------------------------------------------------------


def run_loop_in_tmux(state: LoopState, project_dir: Path, storage: StorageResolver) -> None:
    """Main loop — runs inside tmux. Invokes Claude CLI sessions sequentially."""
    try:
        asyncio.run(_loop_main(state, project_dir, storage))
    except Exception:
        # Don't leave LOOP-STATE.json saying "running" for a loop that died
        state.status = "failed"
        state.updated_at = datetime.now(timezone.utc)
        atomic_save(state, storage.conductor_dir(state.name) / "LOOP-STATE.json")
        raise


async def _loop_main(state: LoopState, project_dir: Path, storage: StorageResolver) -> None:
    """Async main loop."""
    from conductor.core.tmux import TmuxManager

    state_path = storage.conductor_dir(state.name) / "LOOP-STATE.json"
    log_path = storage.conductor_dir(state.name) / "LOOP-LOG.md"
    audit_path = storage.conductor_dir(state.name) / "LOOP-AUDIT.jsonl"
    session_dir = storage.conductor_dir(state.name) / "session"

    preset = load_preset(state.preset, project_dir)
    set_effort(state.effort or preset.config.effort)
    tmux = TmuxManager(session_name=f"conductor-loop-{state.name}")
    await tmux.ensure_session()

    # Read plan file
    plan_path = Path(state.plan_file)
    if not plan_path.is_absolute():
        plan_path = project_dir / plan_path
    plan_content = plan_path.read_text(encoding="utf-8")

    # Determine working directory
    cwd = Path(state.worktree) if state.worktree else project_dir

    state.status = "running"
    state.updated_at = datetime.now(timezone.utc)
    atomic_save(state, state_path)

    _log("LOOP_START", f"Starting loop: {state.name}, {len(state.tasks)} tasks", log_path, audit_path)

    # Every session either finishes its task or uses one of its
    # _MAX_ATTEMPTS_PER_TASK attempts; sessions in which Claude never ran
    # don't count against the task, but _MAX_EMPTY_SESSIONS of them in a
    # row stop the loop.
    empty_sessions = 0
    stop_reason = ""

    while state.current_task_index < len(state.tasks):
        task = state.tasks[state.current_task_index]

        if task.status == "completed":
            state.current_task_index += 1
            continue

        task.status = "in_progress"
        task.attempts += 1
        task.started_at = task.started_at or datetime.now(timezone.utc)
        state.session_count += 1
        state.updated_at = datetime.now(timezone.utc)
        atomic_save(state, state_path)

        prompt = build_prompt(state, plan_content)

        _log(
            "SESSION_START",
            f"Session {state.session_count}: task {task.index} '{task.name}' (attempt {task.attempts})",
            log_path, audit_path,
            task_index=task.index, session=state.session_count,
        )

        # Run Claude via tmux
        model = resolve_model(state.model or preset.config.model or "opus")
        window_name = f"task-{task.index}"
        files = _SessionFiles.create(session_dir, task.index, prompt)

        # Claude piped through stream_filter: pretty-prints to the pane + saves raw json to file
        script = claude_session_script(
            cwd=cwd, prompt_file=files.prompt, output_file=files.output,
            exit_file=files.exit, stderr_file=files.stderr,
            model=model, max_turns=state.max_turns,
        )

        log_file = storage.tmux_log(state.name, f"task-{task.index}_session-{state.session_count}")
        await tmux.spawn_in_window(window_name, script, cwd=str(cwd), detached=True, log_file=log_file)

        # Wait for completion (with zombie/stall detection)
        _log("CLAUDE_RUNNING", f"Claude running in window '{window_name}'", log_path, audit_path)

        await _wait_for_exit(files.exit, files.output, tmux, window_name, log_path, audit_path)

        # Read results
        exit_code = files.exit_code()
        output_text = files.output_text()

        _log(
            "SESSION_END",
            f"Session {state.session_count} exit {exit_code}",
            log_path, audit_path,
            exit_code=exit_code, task_index=task.index, stderr_tail=files.stderr_tail(),
        )

        # Check for task signal
        signal = check_output_for_signal(output_text)
        reached_model = session_reached_model(output_text)
        empty_sessions = 0 if reached_model else empty_sessions + 1

        if not reached_model:
            # Login, model, usage limit or launch problem: not the task's fault
            task.attempts -= 1
            why = session_error(output_text) or files.stderr_tail() or f"no output, see {log_file}"
            _log(
                "NO_SIGNAL",
                f"Session {state.session_count}: Claude produced no output (exit {exit_code}): {why}",
                log_path, audit_path, task_index=task.index,
            )
            if empty_sessions >= _MAX_EMPTY_SESSIONS:
                stop_reason = f"{empty_sessions} sessions in a row without Claude output: {why}"
                _log("FAILURE", f"Stopping loop: {stop_reason}", log_path, audit_path)

        elif signal == "completed":
            _log("TASK_SIGNAL", f"Task {task.index} signaled completed", log_path, audit_path)

            try:
                commit_hash = commit_task(cwd, task)
            except CommitRejected as rejected:
                # Keep the task open; the next session sees the hook's output
                task.commit_error = rejected.output
                _log(
                    "GATE_FAILED",
                    f"Task {task.index}: commit rejected, task stays open\n{rejected}",
                    log_path, audit_path, task_index=task.index,
                )
                if task.attempts >= _MAX_ATTEMPTS_PER_TASK:
                    _fail_task(state, task, "its commit kept being rejected", log_path, audit_path)
            else:
                task.status = "completed"
                task.commit = commit_hash
                task.commit_error = None
                task.completed_at = datetime.now(timezone.utc)

                _log(
                    "TASK_COMPLETED",
                    f"Task {task.index} '{task.name}' completed (commit: {commit_hash})",
                    log_path, audit_path,
                    task_index=task.index, commit=commit_hash,
                )

                state.current_task_index += 1

        elif signal == "failed":
            _log("TASK_SIGNAL", f"Task {task.index} signaled failed", log_path, audit_path)
            if task.attempts >= _MAX_ATTEMPTS_PER_TASK:
                _fail_task(state, task, "it signaled failure", log_path, audit_path)

        else:
            # No signal — context exhaustion or crash
            _log(
                "NO_SIGNAL",
                f"Session ended without signal (exit {exit_code}).",
                log_path, audit_path,
            )
            try:
                partial_hash = commit_task(cwd, LoopTask(
                    index=task.index, name=f"WIP: {task.name}",
                    description=f"Partial progress on task {task.index}",
                ))
            except CommitRejected as rejected:
                # Leave the work uncommitted for the next session, which sees why
                task.commit_error = rejected.output
                _log(
                    "GATE_FAILED",
                    f"Task {task.index}: WIP commit rejected, changes left uncommitted\n{rejected}",
                    log_path, audit_path, task_index=task.index,
                )
            else:
                # Nothing is left uncommitted, so an earlier rejection no longer applies
                task.commit_error = None
                if partial_hash:
                    _log("PARTIAL_COMMIT", f"Partial commit: {partial_hash}", log_path, audit_path)
            if task.attempts >= _MAX_ATTEMPTS_PER_TASK:
                _fail_task(state, task, "no completion signal", log_path, audit_path)

        # Save state after each session
        state.updated_at = datetime.now(timezone.utc)
        atomic_save(state, state_path)

        files.remove()

        if stop_reason:
            break

    # Done
    completed = sum(1 for t in state.tasks if t.status == "completed")
    failed = sum(1 for t in state.tasks if t.status == "failed")
    # Anything short of every task done is a failed loop, not a completed one
    state.status = "completed" if completed == len(state.tasks) else "failed"
    state.updated_at = datetime.now(timezone.utc)
    atomic_save(state, state_path)

    _log(
        "LOOP_DONE",
        f"Loop finished: {completed}/{len(state.tasks)} completed, {failed} failed"
        + (f" (stopped: {stop_reason})" if stop_reason else ""),
        log_path, audit_path,
    )

    # Print clear completion banner
    sep = "═" * 50
    title = "LOOP COMPLETE" if state.status == "completed" else "LOOP FAILED"
    print(f"\n{sep}", file=sys.stderr)
    print(f"  {title}: {state.name}", file=sys.stderr)
    print(f"  {completed}/{len(state.tasks)} tasks done, {failed} failed", file=sys.stderr)
    if stop_reason:
        print(f"  Stopped: {stop_reason}", file=sys.stderr)
        print(
            f"  Fix the cause, then resume: conductor loop --name {state.name} --project-dir {project_dir}",
            file=sys.stderr,
        )
    if state.branch:
        print(f"  Branch: {state.branch}", file=sys.stderr)
    print(f"{sep}\n", file=sys.stderr)
