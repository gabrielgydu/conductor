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
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
        "Tool calls will be REJECTED and will waste your only turn.\n\n"
        f"diagnose-runner\n\n"
        f"Feature: {run.name}\n"
        f"Description: {run.description}\n\n"
        f"The runner appears stalled. Last activity:\n{activity_content}\n\n"
        f"Please diagnose the issue and suggest an action (retry, steer, or block)."
    )

    result = await run_claude(prompt, model="claude-sonnet-4-6", max_turns=1)

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
        f"{questions_content}\n\n"
        f"## Second Opinion — Codex (non-blocking)\n\n"
        f"Before finalizing your answers, get a second opinion from Codex (OpenAI o3).\n"
        f"Use the Agent tool with `subagent_type=\"codex:codex-rescue\"` and ask it to review\n"
        f"your draft answers. Incorporate its feedback where it makes good points, then finalize.\n\n"
        f"This is non-blocking: if the agent call fails, errors, or times out, proceed with your\n"
        f"own judgment. Do not let Codex failures delay or block your output.\n\n"
        f"**Logging requirement:** At the end of your output, include a `## Codex Second Opinion` section.\n"
        f"If Codex responded, summarize what it said and what you incorporated or disagreed with.\n"
        f"If the call failed or timed out, note that explicitly (e.g. \"Codex was unavailable: <error>\").\n"
        f"This section is important for auditability — always include it.\n"
    )

    result = await run_claude(prompt, model="claude-opus-4-8[1m]", max_turns=20)

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
