"""Brain: short Claude calls the orchestrator makes to decide what to do next.

Shared plumbing for those calls: rendering the prompt templates in
conductor/prompts/, running the call with a timeout, reading the final answer
out of the CLI's stream-json output, and parsing an ``ACTION: X`` verdict.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from conductor.core.claude import ClaudeResult, run_claude

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

# Diagnosis calls answer from the context they are given in one turn; a tool
# call would only burn that turn.
NO_TOOLS = ""


def render_prompt(name: str, context: str) -> str:
    """Return prompts/<name>-prompt.md with its {CONTEXT} slot filled."""
    template = (PROMPTS_DIR / f"{name}-prompt.md").read_text(encoding="utf-8")
    return template.replace("{CONTEXT}", context)


@dataclass
class BrainReply:
    text: str  # final answer, "" if there is none
    ok: bool  # CLI exited 0, the result is not an error, and text came back
    detail: str = ""  # why the call is not ok
    tokens: dict[str, int] | None = None
    subtype: str = ""  # result event subtype, e.g. "success", "error_max_turns"


def read_reply(result: ClaudeResult) -> BrainReply:
    """Extract the final answer from a stream-json run.

    The result event's ``result`` string is the last assistant message and
    is preferred. Without one (error results, other output shapes), the
    top-level assistant text written after the last tool call is used: text
    before a tool call is a preamble ("Let me check the code first"), not the
    answer.
    """
    final: str | None = None
    failed_result = False
    subtype = ""
    blocks: list[str] = []
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
        etype = event.get("type")
        if etype == "assistant" and event.get("parent_tool_use_id") is None:
            content = (event.get("message") or {}).get("content", [])
            if isinstance(content, str):
                blocks.append(content)
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    blocks.append(block["text"])
                elif block.get("type") == "tool_use":
                    blocks = []
        elif etype == "result":
            subtype = str(event.get("subtype") or "")
            failed_result = bool(event.get("is_error")) or subtype.startswith("error")
            if isinstance(event.get("result"), str):
                final = event["result"]

    text = (final if final and final.strip() else "\n\n".join(blocks)).strip()
    if result.exit_code != 0 or failed_result:
        detail = f"claude exited {result.exit_code}" + (f" ({subtype})" if subtype else "")
        return BrainReply(text, False, detail, result.tokens_used, subtype)
    if not text:
        return BrainReply("", False, "empty reply", result.tokens_used, subtype)
    return BrainReply(text, True, "", result.tokens_used, subtype)


async def ask(
    prompt: str,
    *,
    model: str,
    timeout_s: float,
    max_turns: int = 1,
    tools: str | None = NO_TOOLS,
    cwd: str | None = None,
) -> BrainReply:
    """Run one brain call; never raises for CLI or timeout failures.

    *tools* is passed as ``--tools`` (``""`` disables the built-in tools,
    None keeps the CLI default set).
    """
    append_args = ["--tools", tools] if tools is not None else None
    try:
        result = await asyncio.wait_for(
            run_claude(
                prompt,
                model=model,
                max_turns=max_turns,
                append_args=append_args,
                cwd=cwd,
            ),
            timeout_s,
        )
    except TimeoutError:
        return BrainReply("", False, f"timed out after {timeout_s:.0f}s")
    except OSError as exc:  # claude not on PATH, broken pipe, ...
        return BrainReply("", False, f"could not run claude: {exc}")
    return read_reply(result)


def parse_action(text: str, actions: tuple[str, ...]) -> tuple[str | None, str]:
    """Find an ``ACTION: <X>`` verdict in *text*; return (X, text after it).

    Lenient about case and markdown around it (``**Action:** retry``, code
    fences). Returns (None, "") when no allowed action is named.
    """
    alternatives = "|".join(re.escape(a) for a in actions)
    match = re.search(
        rf"^[\W_]*action[\W_]*({alternatives})\b", text, re.IGNORECASE | re.MULTILINE
    ) or re.search(rf"\baction\b\W{{0,6}}({alternatives})\b", text, re.IGNORECASE)
    if match is None:
        return None, ""
    rest = [
        line.strip(" *_")
        for line in text[match.end():].splitlines()
        if line.strip(" *_") and not line.strip().startswith("```")
    ]
    return match.group(1).upper(), "\n".join(rest)


def log_brain_call(
    brain_calls_dir: Path,
    kind: str,
    run_idx: int,
    stage_idx: int,
    prompt: str,
    reply: BrainReply,
    **extra,
) -> Path:
    """Write one brain call (prompt and reply) to brain-calls/ for auditing."""
    import time

    ts = int(time.time() * 1000)
    path = brain_calls_dir / f"{kind}-{run_idx}-{stage_idx}-{ts}.json"
    path.write_text(
        json.dumps(
            {
                "action": kind,
                "run_idx": run_idx,
                "stage_idx": stage_idx,
                "prompt": prompt,
                "response": reply.text,
                "ok": reply.ok,
                "detail": reply.detail,
                "tokens": reply.tokens,
                **extra,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path
