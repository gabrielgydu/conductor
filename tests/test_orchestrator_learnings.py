"""_review_learnings edits CLAUDE.md files only inside the finished run's
worktree, commits them there, and never touches the user's checkout."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import conductor.core.brain as brain
import conductor.core.orchestrator as orch
from conductor.core.claude import ClaudeResult
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, IntegrationState, RunState, StageState


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


class _FakeClaude:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, **kwargs) -> ClaudeResult:
        self.prompts.append(prompt)
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": self.reply})
        return ClaudeResult(exit_code=0, output=out, tokens_used=None, cost=None, duration=1)


@pytest.fixture
def project(tmp_path, monkeypatch):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "CLAUDE.md").write_text("# Root rules\n")
    (main / ".claude").mkdir()
    (main / ".claude" / "CLAUDE.md").write_text("# Claude dir rules\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "conductor/p/feat/s0", str(wt))
    _git(wt, "config", "user.name", "t")
    _git(wt, "config", "user.email", "t@t")
    learnings = wt / "docs" / "feat" / "LEARNINGS.md"
    learnings.parent.mkdir(parents=True)
    learnings.write_text("- migrations need `make db-reset` first\n")

    stage = StageState(name="s0", spec_mode="full", status=StageStatus.DONE)
    stage.worktree = str(wt)
    stage.branch = "conductor/p/feat/s0"
    stage.completed_at = datetime.now(timezone.utc)
    run = RunState(index=0, name="feat", description="d", stages=[stage],
                   status=RunStatus.DONE, current_stage=1)
    state = ConductorState(project_name="p", runs=[run])
    home = tmp_path / "home"
    home.mkdir()
    storage = SimpleNamespace(
        repo_root=main,
        conductor_state=lambda name: home / "state.json",
    )
    return SimpleNamespace(main=main, wt=wt, state=state, storage=storage, outside=tmp_path)


@pytest.mark.asyncio
async def test_learnings_are_committed_in_the_run_worktree_only(project, monkeypatch):
    reply = (
        "<<<FILE: CLAUDE.md>>>\n- Run `make db-reset` before migrations.\n<<<END>>>\n"
        "<<<FILE: CLAUDE.md>>>\n- Second rule.\n<<<END>>>\n"
        "<<<FILE: /etc/outside/CLAUDE.md>>>\n- nope\n<<<END>>>\n"
        "<<<FILE: ../escaped/CLAUDE.md>>>\n- nope\n<<<END>>>\n"
        "<<<FILE: docs/new/CLAUDE.md>>>\n- nope\n<<<END>>>\n"
    )
    fake = _FakeClaude(reply)
    monkeypatch.setattr(brain, "run_claude", fake)

    await orch._review_learnings(project.state, project.storage)

    # Both existing CLAUDE.md files were shown to the reviewer, once each.
    assert fake.prompts[0].count("### FILE: CLAUDE.md") == 1
    assert fake.prompts[0].count("### FILE: .claude/CLAUDE.md") == 1
    root_md = (project.wt / "CLAUDE.md").read_text()
    assert "- Run `make db-reset` before migrations.\n- Second rule." in root_md
    assert root_md.count("## Conductor Learnings") == 1
    # Committed on the run's branch; nothing else was written.
    assert _git(project.wt, "status", "--porcelain").strip() == "?? docs/"
    assert "Record conductor learnings" in _git(project.wt, "log", "-1", "--format=%s")
    assert _git(project.main, "status", "--porcelain").strip() == ""
    assert (project.main / "CLAUDE.md").read_text() == "# Root rules\n"
    assert not (project.outside / "escaped").exists()
    assert not (project.wt / "docs" / "new" / "CLAUDE.md").exists()


@pytest.mark.asyncio
async def test_unchanged_learnings_are_reviewed_once(project, monkeypatch):
    fake = _FakeClaude("<<<FILE: CLAUDE.md>>>\n- A rule.\n<<<END>>>\n")
    monkeypatch.setattr(brain, "run_claude", fake)

    await orch._review_learnings(project.state, project.storage)
    await orch._review_learnings(project.state, project.storage)

    assert len(fake.prompts) == 1
    assert (project.wt / "CLAUDE.md").read_text().count("- A rule.") == 1


@pytest.mark.asyncio
async def test_review_persists_only_its_own_field(project, monkeypatch):
    # `conductor learnings` holds a snapshot for minutes; a live orchestrator
    # saves progress meanwhile, and that progress must survive the review.
    from conductor.core.models import atomic_save

    path = project.storage.conductor_state("p")
    newer = project.state.model_copy(deep=True)
    newer.runs[0].status = RunStatus.ACTIVE
    newer.runs[0].current_stage = 0
    atomic_save(newer, path)
    monkeypatch.setattr(brain, "run_claude", _FakeClaude("<<<FILE: CLAUDE.md>>>\n- A rule.\n<<<END>>>\n"))

    await orch._review_learnings(project.state, project.storage)

    saved = ConductorState.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.runs[0].status == RunStatus.ACTIVE
    assert saved.runs[0].current_stage == 0
    assert saved.learnings_reviewed == project.state.learnings_reviewed is not None


@pytest.mark.asyncio
async def test_review_persists_the_tip_its_commit_moved(project, monkeypatch):
    # The learnings commit moves the run's tip; head_sha is how a run whose
    # branch was deleted is still found, so the saved state must have it.
    from conductor.core.models import atomic_save

    path = project.storage.conductor_state("p")
    atomic_save(project.state, path)
    monkeypatch.setattr(brain, "run_claude", _FakeClaude("<<<FILE: CLAUDE.md>>>\n- A rule.\n<<<END>>>\n"))

    await orch._review_learnings(project.state.model_copy(deep=True), project.storage)

    saved = ConductorState.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.runs[0].stages[-1].head_sha == _git(project.wt, "rev-parse", "HEAD").strip()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("integrated", "expected"),
    [([], "they go out with the run's PR"), ([0], "push or merge it by hand")],
)
async def test_learnings_message_says_whether_the_commit_still_goes_out(
    project, monkeypatch, capsys, integrated, expected
):
    project.state.integration = IntegrationState(
        status="done", branch="conductor/p/feat/s0", considered_runs=integrated
    )
    monkeypatch.setattr(
        brain, "run_claude", _FakeClaude("<<<FILE: CLAUDE.md>>>\n- A rule.\n<<<END>>>")
    )

    await orch._review_learnings(project.state, project.storage)

    assert expected in capsys.readouterr().err


@pytest.mark.asyncio
async def test_learnings_commit_skips_hooks(project, monkeypatch):
    """A hook that fails in the fresh worktree must not leave the learnings
    uncommitted, so that they never reach the PR."""
    hooks = project.outside / "hooks"
    hooks.mkdir()
    for hook in ("pre-commit", "commit-msg"):
        (hooks / hook).write_text("#!/bin/sh\nexit 1\n")
        (hooks / hook).chmod(0o755)
    _git(project.wt, "config", "core.hooksPath", str(hooks))
    monkeypatch.setattr(
        brain, "run_claude", _FakeClaude("<<<FILE: CLAUDE.md>>>\n- A rule.\n<<<END>>>")
    )

    await orch._review_learnings(project.state, project.storage)

    assert "Record conductor learnings" in _git(project.wt, "log", "-1", "--format=%s")
    assert "CLAUDE.md" not in _git(project.wt, "status", "--porcelain")


@pytest.mark.asyncio
async def test_symlinked_claude_md_is_edited_through_its_target(project, monkeypatch):
    wt = project.wt
    _git(wt, "mv", "CLAUDE.md", "AGENTS.md")
    (wt / "CLAUDE.md").symlink_to("AGENTS.md")
    outside = project.outside / "global-CLAUDE.md"
    outside.write_text("# Global rules\n")
    (wt / ".claude" / "CLAUDE.md").unlink()
    (wt / ".claude" / "CLAUDE.md").symlink_to(outside)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "symlinks")
    monkeypatch.setattr(brain, "run_claude", _FakeClaude(
        "<<<FILE: CLAUDE.md>>>\n- A rule.\n<<<END>>>\n"
        "<<<FILE: .claude/CLAUDE.md>>>\n- nope\n<<<END>>>\n"
    ))

    await orch._review_learnings(project.state, project.storage)

    assert (wt / "CLAUDE.md").is_symlink()
    assert "- A rule." in (wt / "AGENTS.md").read_text()
    assert "AGENTS.md" in _git(wt, "show", "--name-only", "--format=", "HEAD")
    assert outside.read_text() == "# Global rules\n"


# ---------------------------------------------------------------------------
# Overnight audit report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_report_uses_the_template_and_the_auto_answers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    calls = home / "brain-calls"
    calls.mkdir(parents=True)
    (calls / "answer-questions-0-0-1.json").write_text(json.dumps({
        "questions": "### Q1: Soft delete?", "response": "### Q1\nYes, soft delete.",
    }))
    stage = StageState(name="s0", spec_mode="full", status=StageStatus.DONE)
    run = RunState(index=0, name="feat", description="d", stages=[stage], status=RunStatus.DONE)
    state = ConductorState(project_name="p", runs=[run])
    storage = SimpleNamespace(
        brain_calls_dir=lambda name: calls,
        conductor_dir=lambda name: home,
    )
    fake = _FakeClaude("# Overnight Audit Report\n\n## Summary\nFine.")
    monkeypatch.setattr(brain, "run_claude", fake)

    await orch._generate_audit_report(state, storage)

    prompt = fake.prompts[0]
    assert "## Decisions & Auto-Answers" in prompt  # the template's report structure
    assert "### Q1: Soft delete?" in prompt and "Yes, soft delete." in prompt
    [report] = home.glob("OVERNIGHT-AUDIT-*.md")
    assert report.read_text().startswith("# Overnight Audit Report")
