"""E2E tests: Post-run pipeline (TDD — conductor.post_run not yet implemented)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "tests"))

from conductor.core.enums import IntegrationStatus, RunStatus
from conductor.core.models import (
    ConductorState,
    E2ETestState,
    IntegrationState,
    RunState,
    StageState,
)

try:
    from conductor.post_run import post_run_processing
except ImportError:
    post_run_processing = None


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
    return f"pr{abs(hash(str(tmp_path))) % 999999:06d}"


def _make_state(project_name: str, *runs: RunState) -> ConductorState:
    return ConductorState(
        project_name=project_name,
        base_branch="main",
        runs=list(runs),
    )


# ---------------------------------------------------------------------------
# Test 1: Full pipeline sequence — learnings → merge → e2e → audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_post_run_pipeline(tmp_path):
    """Full post-run sequence: learnings → merge → E2E → audit, all in order."""
    if post_run_processing is None:
        pytest.skip("conductor.post_run not yet implemented")

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "branch-a"),
        _done_run_with_branch(1, "feat-b", "branch-b"),
        _done_run_with_branch(2, "feat-c", "branch-c"),
    )
    storage = FakeStorage(tmp_path)

    call_order = []

    async def mock_learnings(*args, **kwargs):
        call_order.append("learnings")
        return "learnings summary"

    async def mock_merge(state, storage):
        call_order.append("merge")
        return IntegrationState(
            status=IntegrationStatus.DONE,
            branch=f"integration/{project_name}",
            merged_runs=[0, 1, 2],
        )

    async def mock_e2e(state, storage):
        call_order.append("e2e")
        return E2ETestState(
            passed=5,
            failed=0,
            skipped=0,
            last_run_at=datetime.now(timezone.utc),
        )

    async def mock_audit(*args, **kwargs):
        call_order.append("audit")
        return "audit report"

    with (
        patch("conductor.post_run.review_learnings", new=mock_learnings),
        patch("conductor.post_run.run_integration_merge", new=mock_merge),
        patch("conductor.post_run.run_integration_testing", new=mock_e2e),
        patch("conductor.post_run.generate_audit_report", new=mock_audit),
    ):
        result = await post_run_processing(state, storage)

    assert isinstance(result, ConductorState)
    assert call_order == ["learnings", "merge", "e2e", "audit"]
    assert result.integration is not None
    assert result.integration.status == IntegrationStatus.DONE
    assert result.integration.merged_runs == [0, 1, 2]
    assert result.integration.e2e is not None
    assert result.integration.e2e.passed == 5
    assert result.integration.e2e.failed == 0


# ---------------------------------------------------------------------------
# Test 2: E2E failures don't prevent audit generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_run_audit_generated_despite_e2e_failures(tmp_path):
    """E2E failures are captured but audit still runs — no step is skipped."""
    if post_run_processing is None:
        pytest.skip("conductor.post_run not yet implemented")

    project_name = _project_name(tmp_path)
    state = _make_state(
        project_name,
        _done_run_with_branch(0, "feat-a", "branch-a"),
        _done_run_with_branch(1, "feat-b", "branch-b"),
        _done_run_with_branch(2, "feat-c", "branch-c"),
    )
    storage = FakeStorage(tmp_path)

    call_order = []

    async def mock_learnings(*args, **kwargs):
        call_order.append("learnings")
        return "learnings summary"

    async def mock_merge(state, storage):
        call_order.append("merge")
        return IntegrationState(
            status=IntegrationStatus.DONE,
            branch=f"integration/{project_name}",
            merged_runs=[0, 1, 2],
        )

    async def mock_e2e_with_failures(state, storage):
        call_order.append("e2e")
        return E2ETestState(
            passed=1,
            failed=2,
            skipped=0,
            last_run_at=datetime.now(timezone.utc),
        )

    async def mock_audit(*args, **kwargs):
        call_order.append("audit")
        return "audit report"

    with (
        patch("conductor.post_run.review_learnings", new=mock_learnings),
        patch("conductor.post_run.run_integration_merge", new=mock_merge),
        patch("conductor.post_run.run_integration_testing", new=mock_e2e_with_failures),
        patch("conductor.post_run.generate_audit_report", new=mock_audit),
    ):
        result = await post_run_processing(state, storage)

    # All 4 steps must execute despite E2E failures
    assert call_order == ["learnings", "merge", "e2e", "audit"]
    assert result.integration.e2e.failed == 2
    # Function returned without exception
    assert isinstance(result, ConductorState)
