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
    run_validation,
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
# Smoke check: opt-in
# ---------------------------------------------------------------------------

_SPEC = Path("tests/Playwright/tests/conductor-smoke.spec.ts")


@pytest.mark.asyncio
async def test_smoke_without_any_setup_is_skipped(tmp_path, monkeypatch):
    run_cmd = AsyncMock(return_value=(1, "must not run"))
    monkeypatch.setattr(validation, "_run_cmd", run_cmd)

    ctx = ValidationContext(project_dir=tmp_path, stage="integration", feature_name="p")
    result = await run_validation(ctx, checks=["smoke"])

    assert result.passed
    [smoke] = result.checks
    assert smoke.output.startswith("skipped: no smoke test configured")
    run_cmd.assert_not_awaited()
    assert not (tmp_path / _SPEC).exists()


@pytest.mark.asyncio
async def test_smoke_skips_when_playwright_is_not_installed(tmp_path, monkeypatch):
    """A config alone is not enough: npx would download Playwright or fail, and an
    Opus fix session cannot install it."""
    (tmp_path / "playwright.config.ts").write_text("export default {};\n")
    run_cmd = AsyncMock(return_value=(1, "must not run"))
    monkeypatch.setattr(validation, "_run_cmd", run_cmd)

    ctx = ValidationContext(project_dir=tmp_path, stage="integration", feature_name="p")
    result = await run_validation(ctx, checks=["smoke"])

    [smoke] = result.checks
    assert smoke.passed
    assert "@playwright/test is not installed" in smoke.output
    run_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_smoke_runs_generated_spec_with_a_playwright_setup(tmp_path, monkeypatch):
    (tmp_path / "playwright.config.ts").write_text("export default {};\n")
    (tmp_path / "node_modules" / "@playwright" / "test").mkdir(parents=True)
    run_cmd = AsyncMock(return_value=(0, "1 passed"))
    monkeypatch.setattr(validation, "_run_cmd", run_cmd)

    ctx = ValidationContext(project_dir=tmp_path, stage="integration", feature_name="p")
    result = await run_validation(ctx, checks=["smoke"])

    assert result.passed
    assert (tmp_path / _SPEC).is_file()
    assert run_cmd.await_args.args[0] == [
        "npx", "--no", "playwright", "test", str(tmp_path / _SPEC)
    ]


@pytest.mark.asyncio
async def test_run_cmd_reports_a_missing_binary_instead_of_raising(tmp_path):
    exit_code, output = await validation._run_cmd(
        ["conductor-no-such-binary"], cwd=tmp_path, timeout=5
    )
    assert exit_code == 127
    assert "conductor-no-such-binary" in output


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


@pytest.mark.asyncio
async def test_a_crashing_fix_attempt_still_reports_its_session(repo, monkeypatch):
    """A crash after the session edited files must not lose fix_attempts: the
    callers commit only when it is set."""

    async def claude_edits_then_crashes(prompt, **kwargs):
        (Path(kwargs["cwd"]) / "app.txt").write_text("half fixed\n")
        raise RuntimeError("stream closed")

    monkeypatch.setattr(validation, "run_validation", AsyncMock(return_value=_failing()))
    monkeypatch.setattr(validation, "run_claude", claude_edits_then_crashes)

    ctx = ValidationContext(project_dir=repo, stage="post-run", feature_name="feat")
    result = await validate_and_fix(ctx, max_attempts=3)

    assert not result.passed
    assert result.fix_attempts == 1
    assert result.summary.endswith("fix attempt 1 crashed: stream closed")
    assert [c.name for c in result.checks] == ["lint"]
    assert await commit_validation_fixes(repo, "fixes", keep_untracked=result.untracked_before_fix)
    assert _git(repo, "show", "HEAD:app.txt") == "half fixed\n"


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


@pytest.mark.asyncio
async def test_post_run_validation_uses_the_runs_preset(repo, tmp_path):
    """advance_run built the post-run context without state or preset, so preset
    checks never ran and exec_wrapper was not applied."""
    from conductor.core.orchestrator import advance_run
    from conductor.core.presets import ConfigPreset

    (repo / "vendor" / "bin").mkdir(parents=True)
    (repo / "vendor" / "bin" / "phpunit").write_text("#!/bin/sh\nexit 1\n")
    preset_file = tmp_path / "proj.toml"
    preset_file.write_text(
        "[validation]\n"
        'exec_wrapper = ["sh", "-c", \'touch wrapped; exit 0\', "wrapper"]\n\n'
        "[[validation.checks]]\n"
        'name = "api-tests"\n'
        'command = ["touch", "api-tests-ran"]\n',
        encoding="utf-8",
    )
    preset = ConfigPreset.from_file(preset_file, name="proj")
    stage = StageState(
        name="impl", spec_mode="full", status=StageStatus.DONE, branch="feat", worktree=str(repo)
    )
    run = RunState(
        index=0, name="run-0", description="d", stages=[stage],
        status=RunStatus.ACTIVE, current_stage=1,
    )
    # A preset name the worktree cannot resolve: the loaded preset must be used.
    state = ConductorState(project_name="proj", base_branch="main", runs=[run], preset="proj")

    await advance_run(state, 0, MagicMock(), MagicMock(), MagicMock(), preset=preset)

    assert run.status == RunStatus.DONE
    assert (repo / "api-tests-ran").exists()
    assert (repo / "wrapped").exists()  # phpunit ran through exec_wrapper
