"""Tests for conductor.core.validation: the fix loop and committing its fixes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import conductor.core.validation as validation
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.validation import (
    CheckResult,
    ValidationContext,
    ValidationResult,
    commit_validation_fixes,
    validate_and_fix,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repo on branch feat with a committed app.txt."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "feat", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "app.txt").write_text("broken\n")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _failing(name: str = "lint") -> ValidationResult:
    return ValidationResult(
        passed=False,
        checks=[CheckResult(name, False, "app broken", 0.0)],
        summary=f"0/1 checks passed. Failed: {name}",
    )


def _fix_loop_mocks(monkeypatch, repo: Path) -> AsyncMock:
    """run_validation fails until app.txt is fixed; run_claude fixes it and adds a file."""

    async def fake_validation(ctx, checks=None):
        if (ctx.project_dir / "app.txt").read_text() == "fixed\n":
            return ValidationResult(passed=True, summary="1/1 checks passed")
        return _failing()

    async def fake_claude(prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "app.txt").write_text("fixed\n")
        (cwd / "helper.txt").write_text("new module\n")

    claude = AsyncMock(side_effect=fake_claude)
    monkeypatch.setattr(validation, "run_validation", fake_validation)
    monkeypatch.setattr(validation, "run_claude", claude)
    return claude


# ---------------------------------------------------------------------------
# validate_and_fix bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_and_fix_records_fix_sessions(repo, monkeypatch):
    (repo / "notes.md").write_text("speccer notes, untracked on purpose\n")
    claude = _fix_loop_mocks(monkeypatch, repo)

    ctx = ValidationContext(project_dir=repo, stage="post-run", feature_name="feat")
    result = await validate_and_fix(ctx, max_attempts=3)

    assert result.passed
    claude.assert_awaited_once()
    assert result.fix_attempts == 1
    # Snapshot taken before Claude ran: its new file is not part of it.
    assert result.untracked_before_fix == frozenset({"notes.md"})


@pytest.mark.asyncio
async def test_validate_and_fix_without_failures_runs_no_fix_session(repo, monkeypatch):
    claude = AsyncMock()
    monkeypatch.setattr(
        validation,
        "run_validation",
        AsyncMock(return_value=ValidationResult(passed=True, summary="1/1 checks passed")),
    )
    monkeypatch.setattr(validation, "run_claude", claude)

    ctx = ValidationContext(project_dir=repo, stage="post-run", feature_name="feat")
    result = await validate_and_fix(ctx, max_attempts=3)

    claude.assert_not_awaited()
    assert result.fix_attempts == 0


# ---------------------------------------------------------------------------
# commit_validation_fixes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_validation_fixes_commits_fixes_but_not_check_output(repo):
    (repo / "notes.md").write_text("untracked before the fix loop\n")
    # What a fix session changes ...
    (repo / "app.txt").write_text("fixed\n")
    (repo / "src").mkdir()
    (repo / "src" / "helper.py").write_text("def helper(): ...\n")
    # ... and what the checks leave behind.
    (repo / "test-results").mkdir()
    (repo / "test-results" / "trace.zip").write_text("x")
    (repo / "frontend" / "dist").mkdir(parents=True)
    (repo / "frontend" / "dist" / "app.js").write_text("x")
    (repo / ".env").write_text("SECRET=1\n")

    committed = await commit_validation_fixes(
        repo, "Apply validation fixes", keep_untracked={"notes.md"}
    )

    assert committed is True
    assert _git(repo, "log", "-1", "--format=%s").strip() == "Apply validation fixes"
    assert sorted(_git(repo, "show", "--name-only", "--format=", "HEAD").split()) == [
        "app.txt",
        "src/helper.py",
    ]
    assert _git(repo, "show", "HEAD:app.txt") == "fixed\n"
    leftover = _git(repo, "status", "--porcelain", "--untracked-files=all").split("\n")
    assert "?? notes.md" in leftover
    assert "?? .env" in leftover


@pytest.mark.asyncio
async def test_commit_validation_fixes_bypasses_failing_hooks(repo):
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    (repo / "app.txt").write_text("fixed\n")

    assert await commit_validation_fixes(repo, "Apply validation fixes") is True
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_commit_validation_fixes_with_nothing_to_commit(repo):
    (repo / "test-results").mkdir()
    (repo / "test-results" / "report.json").write_text("{}")
    head = _git(repo, "rev-parse", "HEAD")

    assert await commit_validation_fixes(repo, "Apply validation fixes") is False
    assert _git(repo, "rev-parse", "HEAD") == head


# ---------------------------------------------------------------------------
# Post-run validation in advance_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_run_validation_fixes_reach_the_stage_branch(repo, monkeypatch, tmp_path):
    """Integration merges the committed stage branch, so the fix must be committed."""
    from conductor.core.orchestrator import advance_run

    _fix_loop_mocks(monkeypatch, repo)
    stage = StageState(
        name="impl",
        spec_mode="full",
        status=StageStatus.DONE,
        branch="feat",
        worktree=str(repo),
    )
    run = RunState(
        index=0,
        name="run-0",
        description="d",
        stages=[stage],
        status=RunStatus.ACTIVE,
        current_stage=1,
    )
    state = ConductorState(project_name="proj", base_branch="main", runs=[run])

    await advance_run(state, 0, MagicMock(), MagicMock(), MagicMock())

    assert run.status == RunStatus.DONE
    assert _git(repo, "show", "feat:app.txt") == "fixed\n"
    assert _git(repo, "show", "feat:helper.txt") == "new module\n"
    assert _git(repo, "status", "--porcelain") == ""
