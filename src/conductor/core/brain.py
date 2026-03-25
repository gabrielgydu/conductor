"""Brain: Claude-powered decision making for the conductor."""
from __future__ import annotations

import json
import time
from pathlib import Path

from conductor.core.claude import run_claude
from conductor.core.models import ConductorState
from conductor.core.storage import StorageResolver


async def brain_diagnose_runner(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver | None = None,
) -> dict:
    """Diagnose a stalled runner; return action dict with 'action' and 'message' keys."""
    if storage is None:
        storage = StorageResolver(Path.cwd())

    run = state.runs[run_idx]
    feature_dir = storage.feature_dir(run.name)
    brain_calls_dir = storage.brain_calls_dir(state.project_name)

    activity_content = ""
    activity_log = feature_dir / "activity.log"
    if activity_log.exists():
        activity_content = activity_log.read_text(encoding="utf-8")

    prompt = (
        f"diagnose-runner\n\n"
        f"Feature: {run.name}\n"
        f"Description: {run.description}\n\n"
        f"The runner appears stalled. Last activity:\n{activity_content}\n\n"
        f"Please diagnos the issue and suggest an action (retry, steer, or block)."
    )

    result = await run_claude(prompt, model="claude-opus-4-6[1m]")

    answer_text = ""
    for line in result.output.splitlines():
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
            content = event.get("content", "")
            if isinstance(content, str):
                answer_text = content
                break

    action: dict = {"action": "retry", "message": "Auto-retry after stall"}
    if answer_text:
        try:
            action = json.loads(answer_text)
        except json.JSONDecodeError:
            pass

    ts = int(time.time() * 1000)
    log_file = brain_calls_dir / f"diagnose-runner-{run_idx}-{stage_idx}-{ts}.json"
    log_file.write_text(
        json.dumps(
            {
                "action": "diagnose-runner",
                "run_idx": run_idx,
                "stage_idx": stage_idx,
                "prompt": prompt,
                "response": answer_text,
                "diagnosed_action": action,
                "tokens": result.tokens_used,
                "exit_code": result.exit_code,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return action


async def brain_answer_questions(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver | None = None,
) -> str:
    """Read QUESTIONS.md from the spec dir, call Claude to answer, write answers back."""
    if storage is None:
        storage = StorageResolver(Path.cwd())

    run = state.runs[run_idx]
    spec_dir = storage.spec_dir(run.name)
    questions_file = spec_dir / "QUESTIONS.md"
    brain_calls_dir = storage.brain_calls_dir(state.project_name)

    questions_content = ""
    if questions_file.exists():
        questions_content = questions_file.read_text(encoding="utf-8")

    prompt = (
        f"answer-questions\n\n"
        f"Feature: {run.name}\n"
        f"Description: {run.description}\n\n"
        f"Please answer the following questions for spec generation:\n\n"
        f"{questions_content}"
    )

    result = await run_claude(prompt, model="claude-opus-4-6[1m]")

    # Extract assistant content from stream-json output
    answer_text = ""
    for line in result.output.splitlines():
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
            content = event.get("content", "")
            if isinstance(content, str):
                answer_text = content
                break

    # Write answers back to QUESTIONS.md if we got content
    if answer_text and questions_file.exists():
        questions_file.write_text(answer_text, encoding="utf-8")

    # Log the brain call
    ts = int(time.time() * 1000)
    log_file = brain_calls_dir / f"answer-questions-{run_idx}-{stage_idx}-{ts}.json"
    log_file.write_text(
        json.dumps(
            {
                "action": "answer-questions",
                "run_idx": run_idx,
                "stage_idx": stage_idx,
                "prompt": prompt,
                "response": answer_text,
                "tokens": result.tokens_used,
                "exit_code": result.exit_code,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return answer_text
