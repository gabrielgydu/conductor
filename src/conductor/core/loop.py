"""Conductor loop engine — persistent prompt loop with task checklist."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from conductor.core.claude import ClaudeResult, resolve_model, run_claude
from conductor.core.logging import live_log
from conductor.core.models import atomic_save, load_state
from conductor.core.presets import load_preset
from conductor.core.storage import StorageResolver


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


def _extract_text_from_result(output: str) -> str:
    """Extract assistant text content from stream-json output."""
    text_parts = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
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

    return (
        f"You are working through a fix/improvement plan. "
        f"This is session #{state.session_count + 1}.\n\n"
        f"## Task Progress\n\n{checklist}\n"
        f"{current_section}\n"
        f"## Instructions\n\n"
        f"1. Work on the current task (marked with <-- YOU ARE HERE)\n"
        f"2. When you have FULLY completed the current task, output exactly: {_TASK_COMPLETED_TAG}\n"
        f"3. After outputting the tag, stop and wait. The orchestrator will handle committing and advancing.\n"
        f"4. If you cannot complete the task, explain why and output: <task-failed/>\n\n"
        f"## Original Plan (full context)\n\n{plan_content}\n"
    )


# ---------------------------------------------------------------------------
# Claude output parsing
# ---------------------------------------------------------------------------


def check_output_for_signal(output: str) -> str | None:
    """Check Claude's output for task-completed or task-failed signal.
    Returns 'completed', 'failed', or None."""
    text = _extract_text_from_result(output)
    if "<task-completed/>" in text or "<task-completed />" in text:
        return "completed"
    if "<task-failed/>" in text or "<task-failed />" in text:
        return "failed"
    return None


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
# Quality gate + commit
# ---------------------------------------------------------------------------


def commit_task(cwd: Path, task: LoopTask) -> str | None:
    """Stage all changes and commit. Returns commit hash or None."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if not status.stdout.strip():
        return None

    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(cwd), check=True, capture_output=True,
    )

    msg = f"{task.name}\n\nTask {task.index}: {task.description[:200]}"
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(cwd), check=True, capture_output=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    return result.stdout.strip()


def push_branch(cwd: Path, branch: str) -> bool:
    """Push the branch to origin. Returns success."""
    result = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=str(cwd), capture_output=True, text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(event: str, message: str, log_path: Path | None, audit_path: Path | None, **kw) -> None:
    live_log(event, message, audit_data=kw or None, log_path=log_path, audit_path=audit_path)


# ---------------------------------------------------------------------------
# Loop runner (tmux-based)
# ---------------------------------------------------------------------------


def run_loop_in_tmux(state: LoopState, project_dir: Path, storage: StorageResolver) -> None:
    """Main loop — runs inside tmux. Invokes Claude CLI sessions sequentially."""
    asyncio.run(_loop_main(state, project_dir, storage))


async def _loop_main(state: LoopState, project_dir: Path, storage: StorageResolver) -> None:
    """Async main loop."""
    from conductor.core.tmux import TmuxManager

    state_path = storage.conductor_dir(state.name) / "LOOP-STATE.json"
    log_path = storage.conductor_dir(state.name) / "LOOP-LOG.md"
    audit_path = storage.conductor_dir(state.name) / "LOOP-AUDIT.jsonl"

    preset = load_preset(state.preset)
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

    max_sessions = len(state.tasks) * 3  # Safety cap: 3 sessions per task max
    session_num = 0

    while state.current_task_index < len(state.tasks) and session_num < max_sessions:
        session_num += 1
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
        exit_file = Path(f"/tmp/conductor-loop-exit-{state.name}-{task.index}")
        output_file = Path(f"/tmp/conductor-loop-output-{state.name}-{task.index}")
        exit_file.unlink(missing_ok=True)
        output_file.unlink(missing_ok=True)

        # Write prompt to temp file
        prompt_file = Path(f"/tmp/conductor-loop-prompt-{state.name}-{task.index}")
        prompt_file.write_text(prompt, encoding="utf-8")

        # Build claude command
        claude_cmd = (
            f"claude -p - "
            f"--dangerously-skip-permissions "
            f"--max-turns {state.max_turns} "
            f"--model {model} "
            f"--output-format stream-json "
            f"--verbose "
            f"< {prompt_file}"
        )

        # Wrap to capture output and exit code
        wrapped_cmd = (
            f"cd {cwd} && {claude_cmd} > {output_file} 2>/dev/null; "
            f"echo $? > {exit_file}"
        )

        log_file = storage.tmux_log(state.name, f"task-{task.index}_session-{state.session_count}")
        await tmux.spawn_in_window(window_name, wrapped_cmd, cwd=str(cwd), detached=True, log_file=log_file)

        # Wait for completion by polling exit file
        _log("CLAUDE_RUNNING", f"Claude running in window '{window_name}'", log_path, audit_path)

        while not exit_file.exists():
            await asyncio.sleep(15)

        # Read results
        try:
            exit_code = int(exit_file.read_text().strip())
        except (ValueError, OSError):
            exit_code = 1

        output_text = ""
        if output_file.exists():
            output_text = output_file.read_text(encoding="utf-8", errors="replace")

        _log(
            "SESSION_END",
            f"Session {state.session_count} exit {exit_code}",
            log_path, audit_path,
            exit_code=exit_code, task_index=task.index,
        )

        # Check for task signal
        signal = check_output_for_signal(output_text)

        if signal == "completed":
            _log("TASK_SIGNAL", f"Task {task.index} signaled completed", log_path, audit_path)

            commit_hash = commit_task(cwd, task)
            task.status = "completed"
            task.commit = commit_hash
            task.completed_at = datetime.now(timezone.utc)

            _log(
                "TASK_COMPLETED",
                f"Task {task.index} '{task.name}' completed (commit: {commit_hash})",
                log_path, audit_path,
                task_index=task.index, commit=commit_hash,
            )

            if preset.config.push_enabled and state.branch:
                pushed = push_branch(cwd, state.branch)
                if pushed:
                    _log("PUSH", f"Pushed {state.branch}", log_path, audit_path)

            state.current_task_index += 1

        elif signal == "failed":
            _log("TASK_SIGNAL", f"Task {task.index} signaled failed", log_path, audit_path)
            if task.attempts >= 3:
                task.status = "failed"
                state.current_task_index += 1

        else:
            # No signal — context exhaustion or crash
            _log(
                "NO_SIGNAL",
                f"Session ended without signal (exit {exit_code}). Will restart.",
                log_path, audit_path,
            )
            partial_hash = commit_task(cwd, LoopTask(
                index=task.index, name=f"WIP: {task.name}",
                description=f"Partial progress on task {task.index}",
            ))
            if partial_hash:
                _log("PARTIAL_COMMIT", f"Partial commit: {partial_hash}", log_path, audit_path)

        # Save state after each session
        state.updated_at = datetime.now(timezone.utc)
        atomic_save(state, state_path)

        # Cleanup temp files
        for f in [prompt_file, exit_file, output_file]:
            f.unlink(missing_ok=True)

    # Done
    completed = sum(1 for t in state.tasks if t.status == "completed")
    failed = sum(1 for t in state.tasks if t.status == "failed")
    state.status = "completed" if failed == 0 else "failed"
    state.updated_at = datetime.now(timezone.utc)
    atomic_save(state, state_path)

    _log(
        "LOOP_DONE",
        f"Loop finished: {completed}/{len(state.tasks)} completed, {failed} failed",
        log_path, audit_path,
    )

    # Print clear completion banner
    sep = "═" * 50
    print(f"\n{sep}", file=sys.stderr)
    print(f"  LOOP COMPLETE: {state.name}", file=sys.stderr)
    print(f"  {completed}/{len(state.tasks)} tasks done, {failed} failed", file=sys.stderr)
    if state.branch:
        print(f"  Branch: {state.branch}", file=sys.stderr)
    print(f"{sep}\n", file=sys.stderr)
