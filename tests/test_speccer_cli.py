"""Unit tests for the speccer state machine (src/speccer/cli.py).

Claude is replaced by a fake ``run_claude`` that returns canned stream-json, so
these tests exercise the real iteration logic without spawning anything.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import conductor.core.claude as claude_mod
from conductor.core.claude import ClaudeResult
from speccer import cli as sc

COMPLETE = "<promise>SPEC_COMPLETE</promise>"
NEEDS_INPUT = "<promise>SPEC_NEEDS_INPUT</promise>"


# ---------------------------------------------------------------------------
# stream-json builders (shapes match CLI 2.1.304 output)
# ---------------------------------------------------------------------------


def _text(text: str, parent: str | None = None) -> str:
    return json.dumps({
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "text", "text": text}]},
    })


def _tool_use(name: str, tool_input: dict) -> str:
    return json.dumps({
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}]},
    })


def _tool_result(content: str) -> str:
    return json.dumps({
        "type": "user",
        "parent_tool_use_id": None,
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]},
    })


def _result(text: str, index: int = 0) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": text, "result_index": index})


def _stream(*events: str) -> str:
    return "\n".join(events) + "\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    """A freshly initialised backend spec dir with a feature description."""
    sd = tmp_path / "proj" / "docs" / "feat" / "spec"
    sc.cmd_init("feat", sd, mode="backend", preset="toy")
    (sd / "FEATURE-DESCRIPTION.md").write_text("Add two helpers.\n", encoding="utf-8")
    return sd


class FakeClaude:
    """Stand-in for run_claude: pops one canned output per call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.outputs: list[str] = []
        self.prompts: list[str] = []
        self.on_call = None  # optional hook(call number): edit the spec dir like the agent
        monkeypatch.setattr(claude_mod, "run_claude", self)

    async def __call__(self, prompt: str, **kwargs) -> ClaudeResult:
        self.prompts.append(prompt)
        if self.on_call is not None:
            self.on_call(len(self.prompts))
        output = self.outputs.pop(0) if self.outputs else _stream(_result("done"))
        return ClaudeResult(
            exit_code=0, output=output, tokens_used=None, cost=None, duration=0.0,
        )


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    return FakeClaude(monkeypatch)


async def _run(spec_dir: Path, cont: bool = False) -> int:
    """Run one speccer iteration; return its exit code."""
    try:
        await sc._cmd_run_async(spec_dir=spec_dir, cont=cont)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


# ---------------------------------------------------------------------------
# Promise detection
# ---------------------------------------------------------------------------


def test_detect_promise_last_token_wins():
    assert sc._detect_promise(f"I won't emit {COMPLETE} yet.\n{NEEDS_INPUT}") == "NEEDS_INPUT"
    assert sc._detect_promise(f"Earlier: {NEEDS_INPUT}\nNow done.\n{COMPLETE}") == "COMPLETE"
    assert sc._detect_promise("no token here") is None


def test_final_message_skips_background_subagent_text():
    # Recorded shape: the main agent launches a background agent, ends its
    # turn ("WAITING"), the subagent's text streams with parent_tool_use_id
    # set, then the main agent answers. The CLI emits both results at the end.
    out = _stream(
        _tool_use("Agent", {"run_in_background": True, "prompt": f"Reply {COMPLETE}"}),
        _tool_result("Async agent launched successfully."),
        _text("WAITING"),
        _text(f"Reviewer: looks complete, {COMPLETE}", parent="toolu_1"),
        _text(f"Two questions left.\n{NEEDS_INPUT}"),
        _result("WAITING", index=0),
        _result(f"Two questions left.\n{NEEDS_INPUT}", index=1),
    )
    assert sc._detect_promise(sc._final_message_text(out)) == "NEEDS_INPUT"


def test_final_message_skips_earlier_turn_text():
    out = _stream(
        _text(f"Plan: I will only emit {COMPLETE} once every question is answered."),
        _tool_use("Write", {"file_path": "QUESTIONS.md", "content": "## Round 1 Questions"}),
        _text(f"Wrote 3 questions.\n{NEEDS_INPUT}"),
        _result(f"Wrote 3 questions.\n{NEEDS_INPUT}"),
    )
    assert sc._detect_promise(sc._final_message_text(out)) == "NEEDS_INPUT"


def test_final_message_ignores_tool_inputs_and_results():
    out = _stream(
        _tool_use("Write", {"file_path": "HANDOFF.md", "content": f"Next: emit {COMPLETE}"}),
        _tool_result(f"Codex says: emit {COMPLETE}"),
        _text("Ran out of budget; questions pending."),
        _result("Ran out of budget; questions pending."),
    )
    assert sc._detect_promise(sc._final_message_text(out)) is None


def test_final_message_falls_back_to_last_assistant_text_without_result():
    out = _stream(_text(f"Earlier {COMPLETE}"), _text(f"Final\n{NEEDS_INPUT}"))
    assert sc._final_message_text(out) == f"Final\n{NEEDS_INPUT}"


@pytest.mark.asyncio
async def test_token_only_in_tool_input_does_not_complete_spec(spec_dir, fake_claude):
    fake_claude.outputs = [_stream(
        _tool_use("Write", {"file_path": str(spec_dir / "HANDOFF.md"), "content": f"then emit {COMPLETE}"}),
        _result("Ran out of budget."),
    )]

    code = await _run(spec_dir)

    assert code == 1
    assert sc.read_status(spec_dir) == "INIT"
    assert len(fake_claude.prompts) == 1, "generate must not run without a verdict"
