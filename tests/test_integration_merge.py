"""Tests for integration merge helpers and pipeline."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from conductor.integration.merge import (
    _has_conflict_markers,
    _build_pr_body,
)
from conductor.core.models import ConflictRecord, RunState, StageState, ConductorState, IntegrationState
from conductor.core.enums import RunStatus, IntegrationStatus


# ---------------------------------------------------------------------------
# Helper tests (Step 2)
# ---------------------------------------------------------------------------


def test_has_conflict_markers_true():
    content = "some code\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    assert _has_conflict_markers(content) is True


def test_has_conflict_markers_false():
    content = "def foo():\n    return 42\n"
    assert _has_conflict_markers(content) is False


def test_build_pr_body_no_conflicts():
    merged_runs = [
        RunState(index=0, name="feature-a", description="A", stages=[StageState(name="s", spec_mode="full")]),
        RunState(index=1, name="feature-b", description="B", stages=[StageState(name="s", spec_mode="full")]),
    ]
    body = _build_pr_body(merged_runs, [], [])
    assert "## Merged Runs" in body
    assert "Run 0: feature-a" in body
    assert "Run 1: feature-b" in body
    assert "## Conflicts" not in body
    assert "## Unresolved" not in body


def test_build_pr_body_with_conflicts():
    merged_runs = [
        RunState(index=0, name="feature-a", description="A", stages=[StageState(name="s", spec_mode="full")]),
    ]
    conflicts = [
        ConflictRecord(file="src/foo.py", feature_a="a", feature_b="b", description="import conflict"),
    ]
    body = _build_pr_body(merged_runs, conflicts, [])
    assert "## Conflicts" in body
    assert "`src/foo.py`" in body
    assert "import conflict" in body
    assert "## Unresolved" not in body


def test_build_pr_body_with_unresolved():
    merged_runs = [
        RunState(index=0, name="feature-a", description="A", stages=[StageState(name="s", spec_mode="full")]),
    ]
    unresolved = [
        ConflictRecord(file="src/bar.py", feature_a="a", feature_b="b", description="unresolved"),
    ]
    body = _build_pr_body(merged_runs, [], unresolved)
    assert "## Unresolved Conflicts" in body
    assert "`src/bar.py`" in body


# ---------------------------------------------------------------------------
# Integration pipeline tests (Step 5)
# ---------------------------------------------------------------------------


def _make_run(index, name, branch="feature/run-{i}", status=RunStatus.DONE, depends_on=None):
    b = branch.format(i=index) if "{i}" in branch else branch
    stage = StageState(name="impl", spec_mode="full", branch=b, status="done")
    return RunState(
        index=index,
        name=name,
        description=f"{name} desc",
        depends_on=depends_on or [],
        stages=[stage],
        status=status,
    )


def _make_state(*runs):
    return ConductorState(project_name="test-proj", base_branch="main", runs=list(runs))


def _mock_proc(returncode=0, stdout=b"", stderr=b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class FakeStorageResolver:
    def __init__(self):
        self.repo_root = Path("/fake/repo")


@pytest.fixture
def storage():
    return FakeStorageResolver()


def _make_subprocess_mock(responses: dict):
    """responses: {tuple_of_args_fragments: (returncode, stdout, stderr)}"""

    async def fake_create_subprocess_exec(*args, **kwargs):
        args_str = " ".join(str(a) for a in args)
        for key, val in responses.items():
            if all(k in args_str for k in (key if isinstance(key, tuple) else (key,))):
                rc, out, err = val
                proc = AsyncMock()
                proc.returncode = rc
                proc.communicate = AsyncMock(return_value=(out.encode() if isinstance(out, str) else out, err.encode() if isinstance(err, str) else err))
                return proc
        # default: success
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    return fake_create_subprocess_exec


@pytest.mark.asyncio
async def test_skip_when_fewer_than_two_done(storage):
    from conductor.integration.merge import run_integration_merge

    state = _make_state(_make_run(0, "only-one"))
    result = await run_integration_merge(state, storage)
    assert isinstance(result, IntegrationState)
    assert result.status == IntegrationStatus.DONE
    assert result.merged_runs == []


@pytest.mark.asyncio
async def test_clean_merge_two_runs(storage, tmp_path):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert 0 in result.merged_runs
    assert 1 in result.merged_runs


@pytest.mark.asyncio
async def test_clean_merge_dag_order(storage, tmp_path):
    from conductor.integration.merge import run_integration_merge, get_activation_order

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b", depends_on=[0])
    run2 = _make_run(2, "feat-c", depends_on=[1])
    state = _make_state(run0, run1, run2)

    merge_order = []

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    # All 3 runs merged
    assert sorted(result.merged_runs) == [0, 1, 2]


@pytest.mark.asyncio
async def test_theirs_strategy_fallback(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    call_count = {"merge": 0, "merge_theirs": 0}

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "merge" in args_str and "-X" not in args_str and "abort" not in args_str and "--no-edit" in args_str:
            call_count["merge"] += 1
            # fail for feature branches (not initial branch creation)
            if "feature/run-" in args_str:
                proc.returncode = 1
            else:
                proc.returncode = 0
        elif "merge" in args_str and "-X" in args_str:
            call_count["merge_theirs"] += 1
            proc.returncode = 0
        else:
            proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=["src/foo.py"]):
            result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert len(result.conflicts_resolved) > 0


@pytest.mark.asyncio
async def test_claude_resolution_fallback(storage, tmp_path):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "merge" in args_str and "abort" not in args_str and ("feature/run-" in args_str):
            proc.returncode = 1
        else:
            proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=["src/foo.py"]):
            with patch("conductor.integration.merge.resolve_conflicts_with_claude", new_callable=AsyncMock, return_value=True):
                result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert len(result.conflicts_resolved) > 0


@pytest.mark.asyncio
async def test_claude_failure_partial(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "merge" in args_str and "abort" not in args_str and ("feature/run-" in args_str):
            proc.returncode = 1
        else:
            proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=["src/foo.py"]):
            with patch("conductor.integration.merge.resolve_conflicts_with_claude", new_callable=AsyncMock, return_value=False):
                result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.PARTIAL
    assert len(result.conflicts_unresolved) > 0


@pytest.mark.asyncio
async def test_worktree_creation(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    worktree_calls = []

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "worktree" in args_str and "add" in args_str:
            worktree_calls.append(args)
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            await run_integration_merge(state, storage)

    assert len(worktree_calls) >= 1
    # Verify worktree add was called
    worktree_add_args = " ".join(str(a) for a in worktree_calls[0])
    assert "worktree" in worktree_add_args
    assert "add" in worktree_add_args


@pytest.mark.asyncio
async def test_worktree_cleanup(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    worktree_remove_calls = []

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "worktree" in args_str and "remove" in args_str:
            worktree_remove_calls.append(args)
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            await run_integration_merge(state, storage)

    assert len(worktree_remove_calls) >= 1


@pytest.mark.asyncio
async def test_branch_creation_fallback_no_remote(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    worktree_add_calls = []

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "worktree" in args_str and "add" in args_str:
            worktree_add_calls.append(list(args))
            # Fail first attempt (with origin/main), succeed second (with main)
            if "origin/" in args_str:
                proc.returncode = 1
            else:
                proc.returncode = 0
        else:
            proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            result = await run_integration_merge(state, storage)

    # Should have tried twice
    assert len(worktree_add_calls) >= 2


@pytest.mark.asyncio
async def test_existing_branch_cleanup(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    delete_calls = []

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "branch" in args_str and "-D" in args_str:
            delete_calls.append(list(args))
        if "push" in args_str and "--delete" in args_str:
            delete_calls.append(list(args))
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            await run_integration_merge(state, storage)

    # Cleanup calls should have happened
    assert len(delete_calls) >= 1


@pytest.mark.asyncio
async def test_push_and_pr_creation(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    push_calls = []
    pr_calls = []

    async def fake_exec(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "push" in args_str and "--delete" not in args_str and "origin" in args_str:
            if "branch" not in args_str:
                push_calls.append(list(args))
        if "pr" in args_str and "create" in args_str:
            pr_calls.append(list(args))
        if "pr" in args_str and "list" in args_str:
            proc.communicate = AsyncMock(return_value=(b"[]", b""))
        else:
            proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            result = await run_integration_merge(state, storage)

    assert len(push_calls) >= 1
    assert len(pr_calls) >= 1


@pytest.mark.asyncio
async def test_existing_pr_skips_creation(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    pr_create_calls = []

    async def fake_exec(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "pr" in args_str and "create" in args_str:
            pr_create_calls.append(list(args))
        if "pr" in args_str and "list" in args_str:
            # Return existing PR
            proc.communicate = AsyncMock(return_value=(b'[{"number": 42}]', b""))
        else:
            proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            await run_integration_merge(state, storage)

    assert len(pr_create_calls) == 0


@pytest.mark.asyncio
async def test_pr_body_format(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    pr_body = []

    async def fake_exec(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "pr" in args_str and "create" in args_str:
            # Capture body arg
            args_list = list(args)
            for i, a in enumerate(args_list):
                if a == "--body" and i + 1 < len(args_list):
                    pr_body.append(args_list[i + 1])
        if "pr" in args_str and "list" in args_str:
            proc.communicate = AsyncMock(return_value=(b"[]", b""))
        else:
            proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            await run_integration_merge(state, storage)

    assert len(pr_body) >= 1
    assert "## Merged Runs" in pr_body[0]


@pytest.mark.asyncio
async def test_skip_run_with_no_branch(storage):
    from conductor.integration.merge import run_integration_merge

    # run with no branch
    stage_no_branch = StageState(name="impl", spec_mode="full", branch=None, status="done")
    run0 = RunState(index=0, name="feat-a", description="a", stages=[stage_no_branch], status=RunStatus.DONE)
    run1 = _make_run(1, "feat-b")
    run2 = _make_run(2, "feat-c")
    state = _make_state(run0, run1, run2)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            result = await run_integration_merge(state, storage)

    # run0 should be skipped (no branch), run1 and run2 merged
    assert 0 not in result.merged_runs
    assert 1 in result.merged_runs
    assert 2 in result.merged_runs


@pytest.mark.asyncio
async def test_mixed_conflicts(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    run2 = _make_run(2, "feat-c")
    state = _make_state(run0, run1, run2)

    merge_attempt = {"count": 0}

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "merge" in args_str and "abort" not in args_str and "feature/run-" in args_str:
            merge_attempt["count"] += 1
            if merge_attempt["count"] == 1:
                # First run: clean merge
                proc.returncode = 0
            elif merge_attempt["count"] == 2:
                # Second run: fails clean, theirs works (handled by -X theirs)
                if "-X" in args_str:
                    proc.returncode = 0
                else:
                    proc.returncode = 1
            else:
                proc.returncode = 0
        else:
            proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=["src/foo.py"]):
            result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert len(result.conflicts_resolved) > 0


@pytest.mark.asyncio
async def test_worktree_cleanup_failure_non_fatal(storage):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "worktree" in args_str and "remove" in args_str:
            proc.returncode = 1
            proc.communicate = AsyncMock(return_value=(b"", b"error removing worktree"))
        else:
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch("conductor.integration.merge._get_conflicting_files", return_value=[]):
            result = await run_integration_merge(state, storage)

    # Should still complete without raising
    assert result.status == IntegrationStatus.DONE
