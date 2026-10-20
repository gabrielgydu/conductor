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
from conductor.core.models import ConductorState, RunState, StageState


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
