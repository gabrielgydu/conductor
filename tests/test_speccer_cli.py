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


# ---------------------------------------------------------------------------
# PROGRESS.md header ownership
# ---------------------------------------------------------------------------


def _rewrite_progress_without_header(spec_dir: Path) -> None:
    """What an agent following the old prompt example wrote: no MODE/PRESET."""
    (spec_dir / "PROGRESS.md").write_text(
        "STATUS: SPECCING\nITERATION: 3\n\n## Domain Progress\n\n"
        "| # | Domain | Status | File |\n|---|--------|--------|------|\n"
        "| 01 | Core | COMPLETE | 01-core.md |\n",
        encoding="utf-8",
    )
    (spec_dir / "domains" / "01-core.md").write_text("# Core\nDone.\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_header_restored_after_agent_rewrites_progress(spec_dir, fake_claude):
    fake_claude.on_call = lambda n: _rewrite_progress_without_header(spec_dir)
    fake_claude.outputs = [_stream(_result(f"Questions written.\n{NEEDS_INPUT}"))]

    assert await _run(spec_dir) == 0

    assert sc.read_status(spec_dir) == "NEEDS_INPUT"
    assert sc.read_mode(spec_dir) == "backend"
    assert sc.read_preset(spec_dir) == "toy"
    assert sc.read_iteration(spec_dir) == 1
    assert "| 01 | Core | COMPLETE | 01-core.md |" in (spec_dir / "PROGRESS.md").read_text()


@pytest.mark.asyncio
async def test_backend_generate_keeps_backend_template_after_rewrite(spec_dir, fake_claude):
    fake_claude.on_call = lambda n: n == 1 and _rewrite_progress_without_header(spec_dir)
    fake_claude.outputs = [_stream(_result(f"All domains done.\n{COMPLETE}"))]

    await _run(spec_dir)  # generate then fails its output check; irrelevant here

    assert len(fake_claude.prompts) == 2
    assert "API-CONTRACTS.md" in fake_claude.prompts[1], "generate used the generic template"


@pytest.mark.asyncio
async def test_header_rebuilt_when_agent_deletes_progress(spec_dir, fake_claude):
    fake_claude.on_call = lambda n: (spec_dir / "PROGRESS.md").unlink()
    fake_claude.outputs = [_stream(_result(f"Questions written.\n{NEEDS_INPUT}"))]

    assert await _run(spec_dir) == 0

    assert sc.read_status(spec_dir) == "NEEDS_INPUT"
    assert sc.read_mode(spec_dir) == "backend"
    assert sc.read_iteration(spec_dir) == 1


# ---------------------------------------------------------------------------
# SPEC_COMPLETE marker gate
# ---------------------------------------------------------------------------


def _answered(spec_dir: Path) -> Path:
    """Put the spec at NEEDS_INPUT with one answered question."""
    sc.update_status(spec_dir, "NEEDS_INPUT")
    (spec_dir / "QUESTIONS.md").write_text(
        "## Round 1 Questions\n\n### Q1: Auth\n\n**Answer:**\n> Use the existing session auth\n",
        encoding="utf-8",
    )
    return spec_dir


def _domain(spec_dir: Path, body: str) -> None:
    (spec_dir / "domains" / "01-core.md").write_text(f"# Core\n{body}\n", encoding="utf-8")


def _complete() -> str:
    return _stream(_result(f"All domains done.\n{COMPLETE}"))


@pytest.mark.asyncio
async def test_prose_mention_of_marker_does_not_block_completion(spec_dir, fake_claude):
    _answered(spec_dir)
    _domain(spec_dir, "## Changelog\n- Resolved former [NEEDS CLARIFICATION] markers from round 1.")
    fake_claude.outputs = [_complete()]

    await _run(spec_dir, cont=True)  # generate then fails its output check; irrelevant here

    assert sc.read_status(spec_dir) == "COMPLETE"
    assert len(fake_claude.prompts) == 2, "generate should have run"


@pytest.mark.asyncio
async def test_rejection_reason_reaches_next_iteration_prompt(spec_dir, fake_claude):
    _answered(spec_dir)
    _domain(spec_dir, "Auth: [NEEDS CLARIFICATION: which auth scheme?]")
    fake_claude.outputs = [_complete(), _stream(_result(f"Asked again.\n{NEEDS_INPUT}"))]

    assert await _run(spec_dir, cont=True) == 1

    assert sc.read_status(spec_dir) == "NEEDS_INPUT"
    assert sc.read_rejected_completions(spec_dir) == 1
    handoff = (spec_dir / "HANDOFF.md").read_text()
    assert "SPEC_COMPLETE rejected" in handoff
    assert "domains/01-core.md:2: [NEEDS CLARIFICATION: which auth scheme?]" in handoff

    assert await _run(spec_dir, cont=True) == 0
    assert "domains/01-core.md:2: [NEEDS CLARIFICATION: which auth scheme?]" in fake_claude.prompts[1]
    assert sc.read_rejected_completions(spec_dir) == 0, "an accepted verdict resets the count"


@pytest.mark.asyncio
async def test_continue_after_rejection_needs_no_new_answers(spec_dir, fake_claude):
    # First iteration: no questions written, a marker left behind, COMPLETE claimed.
    _domain(spec_dir, "Retention: [NEEDS CLARIFICATION: how long?]")
    fake_claude.outputs = [_complete(), _stream(_result(f"Fixed.\n{NEEDS_INPUT}"))]
    assert await _run(spec_dir) == 1
    assert (spec_dir / "QUESTIONS.md").read_text() == ""

    assert await _run(spec_dir, cont=True) == 0

    assert len(fake_claude.prompts) == 2, "the continue run must start a session to fix the marker"


@pytest.mark.asyncio
async def test_consecutive_rejections_stop_for_a_human(spec_dir, fake_claude):
    _answered(spec_dir)
    _domain(spec_dir, "Auth: [NEEDS CLARIFICATION: which auth scheme?]")
    # The agent rewrites PROGRESS.md each session; the count must survive it.
    fake_claude.on_call = lambda n: _rewrite_progress_without_header(spec_dir) or _domain(
        spec_dir, "Auth: [NEEDS CLARIFICATION: which auth scheme?]"
    )
    fake_claude.outputs = [_complete() for _ in range(sc.MAX_REJECTED_COMPLETIONS)]

    codes = [await _run(spec_dir, cont=True) for _ in range(sc.MAX_REJECTED_COMPLETIONS)]

    assert codes == [1] * (sc.MAX_REJECTED_COMPLETIONS - 1) + [sc.EXIT_NEEDS_HUMAN]
    assert sc.read_rejected_completions(spec_dir) == sc.MAX_REJECTED_COMPLETIONS

    assert await _run(spec_dir, cont=True) == sc.EXIT_NEEDS_HUMAN
    assert len(fake_claude.prompts) == sc.MAX_REJECTED_COMPLETIONS, "no session after the cap"
    assert sc.read_status(spec_dir) == "NEEDS_INPUT"


# ---------------------------------------------------------------------------
# Prompt selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["fullstack", "backend", "frontend", "testing"])
def test_init_with_existing_domains_resumes_instead_of_starting_over(spec_dir, mode):
    fresh = sc.build_spec_prompt(spec_dir, "feat", mode, "INIT", 1)
    assert "First Iteration" in fresh and "Resuming Spec Work" not in fresh

    _domain(spec_dir, "Half-written by an iteration that crashed.")
    resumed = sc.build_spec_prompt(spec_dir, "feat", mode, "INIT", 2)
    assert "Resuming Spec Work" in resumed and "First Iteration" not in resumed
    assert "Half-written by an iteration that crashed." in resumed


# ---------------------------------------------------------------------------
# generate output check
# ---------------------------------------------------------------------------


def _write_generate_outputs(docs_dir: Path, run_dirs: list[Path]) -> None:
    for name in ("PRD.md", "TECHNICAL-DESIGN.md", "API-CONTRACTS.md", "IMPLEMENTATION-PLAN.md"):
        (docs_dir / name).write_text("x\n", encoding="utf-8")
    for run_dir in run_dirs:
        (run_dir / "prompts").mkdir(parents=True)
        (run_dir / "prompts" / "phase-1.md").write_text("x\n", encoding="utf-8")
        (run_dir / "run.sh").write_text('PHASES[1]="phase-1.md"\n', encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("split_prs", [False, True])
async def test_generate_accepts_the_layout_its_template_asks_for(spec_dir, fake_claude, split_prs):
    docs_dir = spec_dir.parent
    _domain(spec_dir, "Done.")
    sc.update_status(spec_dir, "COMPLETE")
    run_dirs = [docs_dir / "pr-1", docs_dir / "pr-2"] if split_prs else [docs_dir]
    fake_claude.on_call = lambda n: _write_generate_outputs(docs_dir, run_dirs)

    await sc._cmd_generate_async(spec_dir=spec_dir, split_prs=split_prs)

    assert sc.read_status(spec_dir) == "GENERATED"


@pytest.mark.asyncio
async def test_split_generate_rejects_a_pr_without_prompts(spec_dir, fake_claude):
    docs_dir = spec_dir.parent
    _domain(spec_dir, "Done.")
    sc.update_status(spec_dir, "COMPLETE")

    def outputs(n):
        _write_generate_outputs(docs_dir, [docs_dir / "pr-1"])
        (docs_dir / "pr-2").mkdir()
        (docs_dir / "pr-2" / "run.sh").write_text("", encoding="utf-8")

    fake_claude.on_call = outputs

    with pytest.raises(SystemExit):
        await sc._cmd_generate_async(spec_dir=spec_dir, split_prs=True)
    assert sc.read_status(spec_dir) == "COMPLETE"
