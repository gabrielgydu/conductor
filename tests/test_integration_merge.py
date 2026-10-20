"""Tests for integration merge helpers and pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from conductor.integration.merge import (
    _build_pr_body,
    integration_worktree_path,
)
from conductor.core.models import (
    ConflictRecord,
    RunState,
    StageState,
    ConductorState,
    IntegrationState,
)
from conductor.core.enums import RunStatus, IntegrationStatus


@pytest.fixture(autouse=True)
def _stub_post_merge_checks(monkeypatch):
    """Post-merge validation writes a smoke spec into the (fake) integration
    worktree under /tmp and reads user presets; stub it and E2E generation."""
    from conductor.core.models import E2ETestState
    from conductor.core.validation import ValidationResult

    monkeypatch.setattr(
        "conductor.integration.merge.validate_and_fix",
        AsyncMock(return_value=ValidationResult(passed=True, summary="stubbed")),
    )
    monkeypatch.setattr(
        "conductor.integration.e2e.run_integration_testing",
        AsyncMock(return_value=E2ETestState(skipped=1)),
    )


# ---------------------------------------------------------------------------
# Helper tests (Step 2)
# ---------------------------------------------------------------------------


def test_integration_worktree_path_sits_next_to_the_run_worktrees():
    state = ConductorState(project_name="proj", base_branch="main")
    assert integration_worktree_path(state, Path("/src/repo")) == Path(
        "/src/worktrees/integration-proj"
    )
    state.worktrees_base = "/wt"
    assert integration_worktree_path(state, Path("/src/repo")) == Path("/wt/integration-proj")


def test_build_pr_body_no_conflicts():
    merged_runs = [
        RunState(
            index=0,
            name="feature-a",
            description="A",
            stages=[StageState(name="s", spec_mode="full")],
        ),
        RunState(
            index=1,
            name="feature-b",
            description="B",
            stages=[StageState(name="s", spec_mode="full")],
        ),
    ]
    body = _build_pr_body(merged_runs, [], [])
    assert "## Merged Runs" in body
    assert "Run 0: feature-a" in body
    assert "Run 1: feature-b" in body
    assert "## Conflicts" not in body
    assert "## Unresolved" not in body


def test_build_pr_body_with_conflicts():
    merged_runs = [
        RunState(
            index=0,
            name="feature-a",
            description="A",
            stages=[StageState(name="s", spec_mode="full")],
        ),
    ]
    conflicts = [
        ConflictRecord(
            file="src/foo.py",
            feature_a="a",
            feature_b="b",
            description="import conflict",
        ),
    ]
    body = _build_pr_body(merged_runs, conflicts, [])
    assert "## Conflicts" in body
    assert "`src/foo.py`" in body
    assert "import conflict" in body
    assert "## Unresolved" not in body


def test_build_pr_body_with_unresolved():
    merged_runs = [
        RunState(
            index=0,
            name="feature-a",
            description="A",
            stages=[StageState(name="s", spec_mode="full")],
        ),
    ]
    unresolved = [
        ConflictRecord(
            file="src/bar.py", feature_a="a", feature_b="b", description="unresolved"
        ),
    ]
    body = _build_pr_body(merged_runs, [], unresolved)
    assert "## Unresolved Conflicts" in body
    assert "`src/bar.py`" in body


def test_build_pr_body_lists_runs_not_merged():
    run = RunState(
        index=1,
        name="feature-b",
        description="B",
        stages=[StageState(name="s", spec_mode="full")],
    )
    body = _build_pr_body([], [], [], [(run, "branch feat/b does not exist")])
    assert "## Not Merged" in body
    assert "- Run 1: feature-b (branch feat/b does not exist)" in body


# ---------------------------------------------------------------------------
# Integration pipeline tests (Step 5)
# ---------------------------------------------------------------------------


def _make_run(
    index, name, branch="feature/run-{i}", status=RunStatus.DONE, depends_on=None
):
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
                proc.communicate = AsyncMock(
                    return_value=(
                        out.encode() if isinstance(out, str) else out,
                        err.encode() if isinstance(err, str) else err,
                    )
                )
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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
            result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert 0 in result.merged_runs
    assert 1 in result.merged_runs


@pytest.mark.asyncio
async def test_clean_merge_dag_order(storage, tmp_path):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b", depends_on=[0])
    run2 = _make_run(2, "feat-c", depends_on=[1])
    state = _make_state(run0, run1, run2)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
            result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    # All 3 runs merged
    assert sorted(result.merged_runs) == [0, 1, 2]


@pytest.mark.asyncio
async def test_theirs_strategy_is_last_resort_and_recorded(storage):
    """Conflicts go to Claude first; -X theirs only after Claude fails, and it is
    recorded as unresolved (PARTIAL, draft PR) because it drops the earlier side."""
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    events = []

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        proc.returncode = 0
        stdout = b""
        if "merge" in args_str and "feature/run-1" in args_str:
            events.append("theirs" if "-X" in args_str else "merge")
            if "-X" not in args_str:
                proc.returncode = 1
        if "pr" in args_str and "list" in args_str:
            stdout = b"[]"
        if "pr" in args_str and "create" in args_str:
            events.append(("pr", "--draft" in args))
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    async def claude_fails(files, description, cwd):
        events.append(("claude", list(files)))
        return False

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch(
            "conductor.integration.merge._get_conflicting_files",
            return_value=["src/foo.py"],
        ):
            with patch(
                "conductor.integration.merge.resolve_conflicts_with_claude",
                side_effect=claude_fails,
            ):
                result = await run_integration_merge(state, storage)

    assert events == ["merge", ("claude", ["src/foo.py"]), "theirs", ("pr", True)]
    assert result.status == IntegrationStatus.PARTIAL
    assert result.merged_runs == [0, 1]
    assert result.conflicts_resolved == []
    [record] = result.conflicts_unresolved
    assert record.file == "src/foo.py"
    assert record.feature_b == "feat-b"
    assert record.description.startswith("theirs overwrote ")


@pytest.mark.asyncio
async def test_claude_resolution_fallback(storage, tmp_path):
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if (
            "merge" in args_str
            and "abort" not in args_str
            and ("feature/run-" in args_str)
        ):
            proc.returncode = 1
        elif "MERGE_HEAD" in args_str:
            proc.returncode = 1  # the resolution commit concluded the merge
        else:
            proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch(
            "conductor.integration.merge._get_conflicting_files",
            return_value=["src/foo.py"],
        ):
            with patch(
                "conductor.integration.merge.resolve_conflicts_with_claude",
                new_callable=AsyncMock,
                return_value=True,
            ):
                result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.DONE
    assert len(result.conflicts_resolved) > 0


@pytest.mark.asyncio
async def test_claude_failure_partial(storage):
    """Claude and -X theirs both fail on run 1: it is left out, never committed
    with conflict markers."""
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    state = _make_state(run0, run1)
    commits = []

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        if "merge" in args_str and "feature/run-1" in args_str:
            proc.returncode = 1
        else:
            proc.returncode = 0
        if "commit" in args:
            commits.append(args)
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch(
            "conductor.integration.merge._get_conflicting_files",
            return_value=["src/foo.py"],
        ):
            with patch(
                "conductor.integration.merge.resolve_conflicts_with_claude",
                new_callable=AsyncMock,
                return_value=False,
            ):
                result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.PARTIAL
    assert result.merged_runs == [0]
    [record] = result.conflicts_unresolved
    assert record.description.endswith("feat-b not merged")
    assert not any("Partial merge" in " ".join(c) for c in commits)


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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
            await run_integration_merge(state, storage)

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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
            await run_integration_merge(state, storage)

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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
            await run_integration_merge(state, storage)

    assert len(pr_body) >= 1
    assert "## Merged Runs" in pr_body[0]


@pytest.mark.asyncio
async def test_skip_run_with_no_branch(storage):
    from conductor.integration.merge import run_integration_merge

    # run with no branch
    stage_no_branch = StageState(
        name="impl", spec_mode="full", branch=None, status="done"
    )
    run0 = RunState(
        index=0,
        name="feat-a",
        description="a",
        stages=[stage_no_branch],
        status=RunStatus.DONE,
    )
    run1 = _make_run(1, "feat-b")
    run2 = _make_run(2, "feat-c")
    state = _make_state(run0, run1, run2)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
            result = await run_integration_merge(state, storage)

    # run0 should be skipped (no branch), run1 and run2 merged
    assert 0 not in result.merged_runs
    assert 1 in result.merged_runs
    assert 2 in result.merged_runs


@pytest.mark.asyncio
async def test_mixed_conflicts(storage):
    """Clean merge, Claude-resolved conflict, and -X theirs fallback in one integration."""
    from conductor.integration.merge import run_integration_merge

    run0 = _make_run(0, "feat-a")
    run1 = _make_run(1, "feat-b")
    run2 = _make_run(2, "feat-c")
    state = _make_state(run0, run1, run2)

    async def fake_git(*args, **kwargs):
        proc = AsyncMock()
        args_str = " ".join(str(a) for a in args)
        conflicted = "feature/run-1" in args_str or "feature/run-2" in args_str
        if "merge" in args_str and conflicted and "-X" not in args_str:
            proc.returncode = 1
        elif "MERGE_HEAD" in args_str:
            proc.returncode = 1  # the resolution commit concluded the merge
        else:
            proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    async def claude(files, description, cwd):
        return "feat-b" in description  # resolves run 1, gives up on run 2

    with patch("asyncio.create_subprocess_exec", side_effect=fake_git):
        with patch(
            "conductor.integration.merge._get_conflicting_files",
            return_value=["src/foo.py"],
        ):
            with patch(
                "conductor.integration.merge.resolve_conflicts_with_claude",
                side_effect=claude,
            ):
                result = await run_integration_merge(state, storage)

    assert result.status == IntegrationStatus.PARTIAL
    assert result.merged_runs == [0, 1, 2]
    assert [(c.feature_b, c.description) for c in result.conflicts_resolved] == [
        ("feat-b", "Resolved by Claude AI")
    ]
    assert [c.feature_b for c in result.conflicts_unresolved] == ["feat-c"]


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
        with patch(
            "conductor.integration.merge._get_conflicting_files", return_value=[]
        ):
            result = await run_integration_merge(state, storage)

    # Should still complete without raising
    assert result.status == IntegrationStatus.DONE
