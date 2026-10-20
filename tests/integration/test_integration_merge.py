"""Integration tests: real git operations for merge pipeline."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))

from conductor.core.enums import IntegrationStatus, RunStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.integration.merge import run_integration_merge

# No real Claude / npx / gh-retry sleeps; see tests/integration/conftest.py.
pytestmark = pytest.mark.usefixtures("hermetic_merge")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeStorage:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root


def _done_run_with_branch(index: int, name: str, branch: str) -> RunState:
    stage = StageState(name="impl", spec_mode="full", branch=branch, status="done")
    return RunState(
        index=index,
        name=name,
        description=f"{name} desc",
        stages=[stage],
        status=RunStatus.DONE,
    )


def _project_name(tmp_path: Path) -> str:
    """Unique short project name safe for git branch names."""
    return f"it{abs(hash(str(tmp_path))) % 999999:06d}"


def _worktree_list(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _make_state(tmp_path: Path, *runs: RunState) -> ConductorState:
    """State whose integration worktree lives under tmp_path, not /tmp."""
    return ConductorState(
        project_name=_project_name(tmp_path),
        base_branch="main",
        runs=list(runs),
        worktrees_base=str(tmp_path / "worktrees"),
    )


# ---------------------------------------------------------------------------
# Test 1: Full clean merge with real git
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_merge_real_git(real_merge_repo, tmp_path, hermetic_merge):
    """Two non-conflicting branches → both merged into integration branch."""
    real_merge_repo.create_branch("branch-a", {"file_a.txt": "content from a\n"})
    real_merge_repo.create_branch("branch-b", {"file_b.txt": "content from b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "branch-a"),
        _done_run_with_branch(1, "feat-b", "branch-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert 0 in result.merged_runs
    assert 1 in result.merged_runs

    # Both files present on integration branch
    branch_name = f"integration/{project_name}"
    ls = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "ls-tree", "--name-only", branch_name],
        capture_output=True,
        text=True,
    )
    files = ls.stdout.splitlines()
    assert "file_a.txt" in files
    assert "file_b.txt" in files

    # Post-merge validation ran once, against the integration worktree
    hermetic_merge.validate.assert_awaited_once()
    vctx = hermetic_merge.validate.await_args.args[0]
    assert vctx.stage == "integration"
    assert vctx.project_dir == tmp_path / "worktrees" / f"integration-{project_name}"


# ---------------------------------------------------------------------------
# Test 2: Content conflicts go to Claude; -X theirs is a recorded last resort
# ---------------------------------------------------------------------------


def _same_line_conflict(repo) -> None:
    """main has app.txt; conflict-a and conflict-b both rewrite line 2."""
    (repo.path / "app.txt").write_text("l1\nl2\nl3\nl4\nl5\n")
    subprocess.run(["git", "-C", str(repo.path), "add", "app.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo.path), "commit", "-q", "-m", "add app"], check=True
    )
    repo.create_branch("conflict-a", {"app.txt": "l1\nA-feature\nl3\nl4\nl5\n"})
    repo.create_branch("conflict-b", {"app.txt": "l1\nB-feature\nl3\nl4\nB-l5\n"})


def _show(repo, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo.path), "show", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.asyncio
async def test_content_conflict_goes_to_claude(real_merge_repo, tmp_path):
    """A same-line conflict reaches Claude with the markers still in the tree."""
    from conductor.integration import merge as merge_module

    _same_line_conflict(real_merge_repo)
    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "conflict-a"),
        _done_run_with_branch(1, "feat-b", "conflict-b"),
    )
    seen: list[tuple[list[str], str]] = []

    async def claude_merges_both(conflicting_files, run_description, cwd):
        seen.append((conflicting_files, (cwd / "app.txt").read_text()))
        (cwd / "app.txt").write_text("l1\nA-feature\nB-feature\nl3\nl4\nB-l5\n")
        return True

    with patch.object(
        merge_module, "resolve_conflicts_with_claude", side_effect=claude_merges_both
    ):
        with patch(
            "conductor.integration.merge._run_git_gh",
            new_callable=AsyncMock,
            return_value=(1, "", "no remote"),
        ):
            result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    [(files, content)] = seen
    assert files == ["app.txt"]
    assert "<<<<<<<" in content and ">>>>>>>" in content
    assert result.status == IntegrationStatus.DONE
    assert result.merged_runs == [0, 1]
    [record] = result.conflicts_resolved
    assert (record.file, record.feature_a, record.feature_b) == ("app.txt", "feat-a", "feat-b")
    assert result.conflicts_unresolved == []
    assert _show(real_merge_repo, f"integration/{project_name}:app.txt") == (
        "l1\nA-feature\nB-feature\nl3\nl4\nB-l5\n"
    )


@pytest.mark.asyncio
async def test_theirs_fallback_is_recorded_as_unresolved(real_merge_repo, tmp_path):
    """When Claude fails, -X theirs drops feat-a's hunk: that must not look clean."""
    from conductor.integration import merge as merge_module

    _same_line_conflict(real_merge_repo)
    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "conflict-a"),
        _done_run_with_branch(1, "feat-b", "conflict-b"),
    )
    gh = AsyncMock(return_value=(1, "", "no remote"))

    with patch.object(
        merge_module, "resolve_conflicts_with_claude", AsyncMock(return_value=False)
    ):
        with patch("conductor.integration.merge._run_git_gh", gh):
            result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.PARTIAL
    assert result.merged_runs == [0, 1]
    assert result.conflicts_resolved == []
    [record] = result.conflicts_unresolved
    assert (record.file, record.feature_a, record.feature_b) == ("app.txt", "feat-a", "feat-b")
    assert record.description.startswith("theirs overwrote feat-a")
    assert _show(real_merge_repo, f"integration/{project_name}:app.txt") == (
        "l1\nB-feature\nl3\nl4\nB-l5\n"
    )
    pr_create = [c.args[0] for c in gh.await_args_list if c.args[0][:3] == ["gh", "pr", "create"]]
    assert pr_create and all("--draft" in args for args in pr_create)


# ---------------------------------------------------------------------------
# Test 3: Claude invoked when -X theirs fails (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflict_markers_real_git(real_merge_repo, tmp_path):
    """When -X theirs fails, Claude is invoked and resolves conflict markers."""
    from conductor.integration import merge as merge_module

    shared = real_merge_repo.path / "shared.txt"
    shared.write_text("original content\n")
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "add", "shared.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "commit", "-m", "add shared"],
        check=True,
        capture_output=True,
    )
    real_merge_repo.create_branch("claude-a", {"shared.txt": "version from a\n"})
    real_merge_repo.create_branch("claude-b", {"shared.txt": "version from b\n"})

    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "claude-a"),
        _done_run_with_branch(1, "feat-b", "claude-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    original_run_git = merge_module._run_git

    async def patched_run_git(args: list[str], cwd: Path):
        # Force Claude path by failing -X theirs
        if "merge" in args and "-X" in args and "theirs" in args:
            return (1, "", "simulated -X theirs failure")
        return await original_run_git(args, cwd)

    claude_calls: list[list[str]] = []

    async def mock_claude(
        conflicting_files: list[str], run_description: str, cwd: Path
    ) -> bool:
        claude_calls.append(conflicting_files)
        for f in conflicting_files:
            (cwd / f).write_text("claude resolved content\n")
        return True

    with patch.object(merge_module, "_run_git", side_effect=patched_run_git):
        with patch.object(
            merge_module, "resolve_conflicts_with_claude", side_effect=mock_claude
        ):
            with patch(
                "conductor.integration.merge._run_git_gh",
                new_callable=AsyncMock,
                return_value=(1, "", "no remote"),
            ):
                result = await run_integration_merge(state, storage)

    assert len(claude_calls) >= 1, "Claude should have been invoked at least once"
    assert 1 in result.merged_runs
    assert len(result.conflicts_resolved) > 0


# ---------------------------------------------------------------------------
# Test 4: Worktree isolation — main repo HEAD unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_used_real_git(real_merge_repo, tmp_path):
    """Merge happens in worktree; main repo HEAD and active branch unchanged."""
    real_merge_repo.create_branch("wt-a", {"wt_file_a.txt": "wt content a\n"})
    real_merge_repo.create_branch("wt-b", {"wt_file_b.txt": "wt content b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "wt-a"),
        _done_run_with_branch(1, "feat-b", "wt-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    head_before = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    branch_before = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, storage)

    head_after = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    branch_after = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert head_before == head_after, "Main repo HEAD changed during integration merge"
    assert branch_before == branch_after, (
        "Main repo branch changed during integration merge"
    )
    assert result.status == IntegrationStatus.DONE

    # Worktree lives under worktrees_base and is removed after a clean run
    worktree = tmp_path / "worktrees" / f"integration-{project_name}"
    assert not worktree.exists()
    wt_list = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(worktree) not in wt_list


# ---------------------------------------------------------------------------
# Test 5: Rerun cleans previous branch and worktree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_cleans_previous_branch(real_merge_repo, tmp_path):
    """Second run deletes old integration branch/worktree and creates a fresh one."""
    real_merge_repo.create_branch("rerun-a", {"rerun_a.txt": "content a\n"})
    real_merge_repo.create_branch("rerun-b", {"rerun_b.txt": "content b\n"})

    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "rerun-a"),
        _done_run_with_branch(1, "feat-b", "rerun-b"),
    )
    storage = FakeStorage(real_merge_repo.path)

    mock_gh = AsyncMock(return_value=(1, "", "no remote"))

    # First run
    with patch("conductor.integration.merge._run_git_gh", mock_gh):
        result1 = await run_integration_merge(state, storage)

    assert result1.status == IntegrationStatus.DONE

    # Verify integration branch exists after first run
    branch_name = f"integration/{project_name}"
    branches = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "branch"],
        capture_output=True,
        text=True,
    ).stdout
    assert branch_name in branches

    # Second run with same state — should clean up and recreate
    with patch("conductor.integration.merge._run_git_gh", mock_gh):
        result2 = await run_integration_merge(state, storage)

    assert result2.status == IntegrationStatus.DONE
    assert 0 in result2.merged_runs
    assert 1 in result2.merged_runs

    # Integration branch still exists (freshly created)
    branches_after = subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "branch"],
        capture_output=True,
        text=True,
    ).stdout
    assert branch_name in branches_after
    assert not (tmp_path / "worktrees" / f"integration-{project_name}").exists()


# ---------------------------------------------------------------------------
# Test 6: Failed post-merge validation -> NEEDS_FIX
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_marks_needs_fix(
    real_merge_repo, tmp_path, hermetic_merge
):
    """A clean merge whose post-merge validation fails ends as NEEDS_FIX."""
    from conductor.core.validation import ValidationResult

    real_merge_repo.create_branch("vf-a", {"vf_a.txt": "a\n"})
    real_merge_repo.create_branch("vf-b", {"vf_b.txt": "b\n"})
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "vf-a"),
        _done_run_with_branch(1, "feat-b", "vf-b"),
    )
    hermetic_merge.validate.return_value = ValidationResult(
        passed=False, summary="1/2 checks passed. Failed: smoke"
    )

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.NEEDS_FIX
    assert result.merged_runs == [0, 1]


# ---------------------------------------------------------------------------
# Test 7: Fixes made by post-merge validation reach the integration branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_fixes_are_committed(real_merge_repo, tmp_path, hermetic_merge):
    """Claude's fix sessions edit the worktree; the branch must carry the fix."""
    from conductor.core.validation import ValidationResult

    real_merge_repo.create_branch("fx-a", {"app.txt": "broken\n"})
    real_merge_repo.create_branch("fx-b", {"other.txt": "b\n"})
    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "fx-a"),
        _done_run_with_branch(1, "feat-b", "fx-b"),
    )

    async def validate_with_fix(vctx, max_attempts):
        (vctx.project_dir / "app.txt").write_text("fixed\n")
        (vctx.project_dir / "helper.txt").write_text("added by the fix\n")
        return ValidationResult(passed=True, summary="1/1 checks passed", fix_attempts=1)

    hermetic_merge.validate.side_effect = validate_with_fix

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.DONE
    branch_name = f"integration/{project_name}"

    def show(path: str) -> str:
        return subprocess.run(
            ["git", "-C", str(real_merge_repo.path), "show", f"{branch_name}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    assert show("app.txt") == "fixed\n"
    assert show("helper.txt") == "added by the fix\n"


# ---------------------------------------------------------------------------
# Test 8: Default worktree location; leftovers do not keep the worktree alive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_worktree_next_to_repo_is_removed_when_dirty(
    real_merge_repo, tmp_path, hermetic_merge
):
    """Without worktrees_base the worktree goes beside the repo (not /tmp) and is
    force-removed even though validation left check output behind."""
    from conductor.core.validation import ValidationResult

    real_merge_repo.create_branch("dw-a", {"dw_a.txt": "a\n"})
    real_merge_repo.create_branch("dw-b", {"dw_b.txt": "b\n"})
    project_name = _project_name(tmp_path)
    state = ConductorState(
        project_name=project_name,
        base_branch="main",
        runs=[
            _done_run_with_branch(0, "feat-a", "dw-a"),
            _done_run_with_branch(1, "feat-b", "dw-b"),
        ],
    )
    expected = real_merge_repo.path.parent / "worktrees" / f"integration-{project_name}"
    validated_in: list[Path] = []

    async def validate_leaving_output(vctx, max_attempts):
        validated_in.append(vctx.project_dir)
        (vctx.project_dir / "test-results").mkdir()
        (vctx.project_dir / "test-results" / "trace.zip").write_text("x")
        return ValidationResult(passed=True, summary="1/1 checks passed")

    hermetic_merge.validate.side_effect = validate_leaving_output

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.DONE
    assert validated_in == [expected]
    assert not expected.exists()
    assert str(expected) not in _worktree_list(real_merge_repo.path)


# ---------------------------------------------------------------------------
# Test 9: Runs whose branch cannot be merged are not reported as merged
# ---------------------------------------------------------------------------


def _unrelated_branch(repo: Path, name: str) -> None:
    """A branch with its own root commit: git refuses to merge it."""
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "checkout", "-q", "--orphan", name], check=True)
    subprocess.run([*git, "rm", "-rq", "--cached", "."], check=True)
    (repo / "README.md").unlink()
    (repo / "orphan.txt").write_text("orphan\n")
    subprocess.run([*git, "add", "orphan.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "orphan root"], check=True)
    subprocess.run([*git, "checkout", "-q", "-f", "main"], check=True)


@pytest.mark.asyncio
async def test_unmergeable_runs_are_not_reported_as_merged(
    real_merge_repo, tmp_path, hermetic_merge
):
    real_merge_repo.create_branch("nm-a", {"nm_a.txt": "a\n"})
    real_merge_repo.create_branch("nm-d", {"nm_d.txt": "d\n"})
    _unrelated_branch(real_merge_repo.path, "nm-orphan")
    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "nm-a"),
        _done_run_with_branch(1, "feat-b", "feat/does-not-exist"),
        _done_run_with_branch(2, "feat-c", "nm-orphan"),
        _done_run_with_branch(3, "feat-d", "nm-d"),
    )
    async def gh_without_remote(args, cwd):
        return (0, "[]", "") if "list" in args else (1, "", "no remote")

    gh = AsyncMock(side_effect=gh_without_remote)

    with patch("conductor.integration.merge._run_git_gh", gh):
        result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.merged_runs == [0, 3]
    assert result.status == IntegrationStatus.PARTIAL
    files = _show(real_merge_repo, f"integration/{project_name}:").split()
    assert "nm_a.txt" in files and "nm_d.txt" in files and "orphan.txt" not in files

    [create] = {tuple(c.args[0]) for c in gh.await_args_list if "create" in c.args[0]}
    body = create[create.index("--body") + 1]
    assert "- Run 1: feat-b (branch feat/does-not-exist does not exist)" in body
    assert (
        "- Run 2: feat-c (git merge failed: fatal: refusing to merge unrelated histories)"
        in body
    )
    assert "--draft" in create


@pytest.mark.asyncio
async def test_no_mergeable_run_fails_without_publishing(
    real_merge_repo, tmp_path, hermetic_merge
):
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "gone-a"),
        _done_run_with_branch(1, "feat-b", "gone-b"),
    )
    gh = AsyncMock(return_value=(1, "", "no remote"))

    with patch("conductor.integration.merge._run_git_gh", gh):
        result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.FAILED
    assert result.merged_runs == []
    hermetic_merge.validate.assert_not_awaited()
    gh.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 10: Conflict markers never reach the branch
# ---------------------------------------------------------------------------


def _modify_delete_conflict(repo) -> None:
    """md-a edits other.txt and app.txt line 2; md-d deletes other.txt and edits
    the same line, so -X theirs cannot settle it (modify/delete)."""
    git = ["git", "-C", str(repo.path)]
    (repo.path / "app.txt").write_text("l1\nl2\nl3\n")
    (repo.path / "other.txt").write_text("other\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base files"], check=True)
    repo.create_branch("md-a", {"app.txt": "l1\nA\nl3\n", "other.txt": "other, edited\n"})
    subprocess.run([*git, "checkout", "-q", "-b", "md-d", "main"], check=True)
    subprocess.run([*git, "rm", "-q", "other.txt"], check=True)
    (repo.path / "app.txt").write_text("l1\nD\nl3\n")
    subprocess.run([*git, "commit", "-q", "-am", "md-d"], check=True)
    subprocess.run([*git, "checkout", "-q", "main"], check=True)


@pytest.mark.parametrize(
    "claude_leaves",
    [
        pytest.param(None, id="claude-fails"),
        # Claude drops only the opening markers; ======= and >>>>>>> remain.
        pytest.param("l1\nA\n=======\nD\n>>>>>>> md-d\nl3\n", id="partial-markers"),
    ],
)
@pytest.mark.asyncio
async def test_unresolved_conflicts_are_left_out_not_committed(
    real_merge_repo, tmp_path, claude_leaves
):
    from conductor.core.claude import ClaudeResult

    _modify_delete_conflict(real_merge_repo)
    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "md-a"),
        _done_run_with_branch(1, "feat-d", "md-d"),
    )

    async def fake_claude(prompt, **kwargs):
        if claude_leaves is None:
            return ClaudeResult(exit_code=1, output="", tokens_used=None, cost=None, duration=0)
        (Path(kwargs["cwd"]) / "app.txt").write_text(claude_leaves)
        (Path(kwargs["cwd"]) / "other.txt").unlink()
        return ClaudeResult(exit_code=0, output="", tokens_used=None, cost=None, duration=0)

    with patch("conductor.integration.merge.run_claude", side_effect=fake_claude):
        with patch(
            "conductor.integration.merge._run_git_gh",
            new_callable=AsyncMock,
            return_value=(1, "", "no remote"),
        ):
            result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.PARTIAL
    assert result.merged_runs == [0]
    assert sorted(c.file for c in result.conflicts_unresolved) == ["app.txt", "other.txt"]
    branch = f"integration/{project_name}"
    assert _show(real_merge_repo, f"{branch}:app.txt") == "l1\nA\nl3\n"
    assert _show(real_merge_repo, f"{branch}:other.txt") == "other, edited\n"
    log = _show(real_merge_repo, branch)
    assert "Partial merge" not in log


# ---------------------------------------------------------------------------
# Test 11: Failing git hooks cannot leave a merge open or drop a run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_hooks_do_not_drop_a_resolved_run(real_merge_repo, tmp_path):
    """Hooks that fail in the fresh worktree (husky without node_modules) must not
    leave run d's resolved merge open for run e's merge to abort."""
    from conductor.core.claude import ClaudeResult

    _modify_delete_conflict(real_merge_repo)
    real_merge_repo.create_branch("md-e", {"e.txt": "e\n"})
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("pre-commit", "pre-merge-commit", "commit-msg"):
        (hooks / hook).write_text("#!/bin/sh\nexit 1\n")
        (hooks / hook).chmod(0o755)
    subprocess.run(
        ["git", "-C", str(real_merge_repo.path), "config", "core.hooksPath", str(hooks)],
        check=True,
    )
    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "md-a"),
        _done_run_with_branch(1, "feat-d", "md-d"),
        _done_run_with_branch(2, "feat-e", "md-e"),
    )
    state.runs[2].depends_on = [1]  # merge order a, d, e

    async def claude_resolves(prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "app.txt").write_text("l1\nA+D\nl3\n")
        (cwd / "other.txt").unlink()
        return ClaudeResult(exit_code=0, output="", tokens_used=None, cost=None, duration=0)

    with patch("conductor.integration.merge.run_claude", side_effect=claude_resolves):
        with patch(
            "conductor.integration.merge._run_git_gh",
            new_callable=AsyncMock,
            return_value=(1, "", "no remote"),
        ):
            result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.DONE
    assert result.merged_runs == [0, 1, 2]
    assert sorted(c.file for c in result.conflicts_resolved) == ["app.txt", "other.txt"]
    branch = f"integration/{project_name}"
    assert _show(real_merge_repo, f"{branch}:app.txt") == "l1\nA+D\nl3\n"
    assert _show(real_merge_repo, f"{branch}:e.txt") == "e\n"
    files = _show(real_merge_repo, f"{branch}:").split()
    assert "other.txt" not in files


# ---------------------------------------------------------------------------
# Test 12: A crash in validation does not cost the push, the PR or cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_crash_still_publishes(real_merge_repo, tmp_path, hermetic_merge):
    real_merge_repo.create_branch("vc-a", {"vc_a.txt": "a\n"})
    real_merge_repo.create_branch("vc-b", {"vc_b.txt": "b\n"})
    project_name = _project_name(tmp_path)
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "vc-a"),
        _done_run_with_branch(1, "feat-b", "vc-b"),
    )
    hermetic_merge.validate.side_effect = FileNotFoundError("npx")
    gh = AsyncMock(return_value=(1, "", "no remote"))

    with patch("conductor.integration.merge._run_git_gh", gh):
        result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert result.status == IntegrationStatus.NEEDS_FIX
    assert result.merged_runs == [0, 1]
    assert any(c.args[0][:3] == ["gh", "pr", "create"] for c in gh.await_args_list)
    assert not (tmp_path / "worktrees" / f"integration-{project_name}").exists()


# ---------------------------------------------------------------------------
# Test 13: Integration specs are generated once, run by validation, recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_specs_generated_once_and_results_recorded(
    real_merge_repo, tmp_path, hermetic_merge
):
    from conductor.core.models import E2ETestState
    from conductor.core.validation import CheckResult, ValidationResult

    real_merge_repo.create_branch("e2e-a", {"e2e_a.txt": "a\n"})
    real_merge_repo.create_branch("e2e-b", {"e2e_b.txt": "b\n"})
    state = _make_state(
        tmp_path,
        _done_run_with_branch(0, "feat-a", "e2e-a"),
        _done_run_with_branch(1, "feat-b", "e2e-b"),
        _done_run_with_branch(2, "feat-gone", "no-such-branch"),
    )
    counts = E2ETestState(passed=4, failed=1)
    order = []

    async def validate(ctx, max_attempts):
        order.append("validate")
        return ValidationResult(
            passed=False,
            checks=[CheckResult("integration-tests", False, "1 failed", 0.0, e2e=counts)],
            summary="0/1 checks passed. Failed: integration-tests",
        )

    hermetic_merge.e2e.side_effect = lambda *a, **k: order.append("generate")
    hermetic_merge.validate.side_effect = validate

    with patch(
        "conductor.integration.merge._run_git_gh",
        new_callable=AsyncMock,
        return_value=(1, "", "no remote"),
    ):
        result = await run_integration_merge(state, FakeStorage(real_merge_repo.path))

    assert order == ["generate", "validate"]
    hermetic_merge.e2e.assert_awaited_once()
    _, _, runs, conflicts = hermetic_merge.e2e.await_args.args
    assert [r.name for r in runs] == ["feat-a", "feat-b"]  # not the unmerged run
    assert conflicts == []
    assert result.e2e == counts
