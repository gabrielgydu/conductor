"""Shared test helpers — state factories, manipulation helpers, assertion helpers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conductor.core.enums import IntegrationStatus, RunStatus, StageStatus
from conductor.core.models import (
    ConductorState,
    IntegrationState,
    RunState,
    StageState,
    load_state,
)


# ---------------------------------------------------------------------------
# State factories
# ---------------------------------------------------------------------------


def make_run_state(
    index: int,
    name: str,
    depends_on: list[int] | None = None,
    stages: int = 1,
    status: RunStatus = RunStatus.PENDING,
) -> RunState:
    stage_list = [
        StageState(name=f"stage-{i}", spec_mode="full") for i in range(stages)
    ]
    return RunState(
        index=index,
        name=name,
        description=f"{name} feature run",
        depends_on=depends_on or [],
        stages=stage_list,
        status=status,
    )


def make_integration_state(
    status: IntegrationStatus = IntegrationStatus.PENDING,
    branch: str = "integration/test",
    merged_runs: list[int] | None = None,
) -> IntegrationState:
    return IntegrationState(
        status=status,
        branch=branch,
        merged_runs=merged_runs or [],
    )


def make_conductor_state(
    project_name: str,
    runs: list[RunState],
    check_interval_s: int = 0,
) -> ConductorState:
    return ConductorState(
        project_name=project_name,
        base_branch="main",
        check_interval_s=check_interval_s,
        runs=runs,
    )


# ---------------------------------------------------------------------------
# State manipulation helpers
# ---------------------------------------------------------------------------


def complete_run(state: ConductorState, run_index: int) -> None:
    run = state.runs[run_index]
    run.status = RunStatus.DONE
    for stage in run.stages:
        stage.status = StageStatus.DONE


def block_run(state: ConductorState, run_index: int) -> None:
    state.runs[run_index].status = RunStatus.BLOCKED


def get_active_runs(state: ConductorState) -> list[int]:
    return [r.index for r in state.runs if r.status == RunStatus.ACTIVE]


def get_pending_runs(state: ConductorState) -> list[int]:
    return [r.index for r in state.runs if r.status == RunStatus.PENDING]


def get_blocked_runs(state: ConductorState) -> list[int]:
    return [r.index for r in state.runs if r.status == RunStatus.BLOCKED]


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def assert_stage_status(
    state: ConductorState,
    run_index: int,
    stage_index: int,
    expected: StageStatus,
) -> None:
    run = state.runs[run_index]
    stage = run.stages[stage_index]
    assert stage.status == expected, (
        f"run[{run_index}].stage[{stage_index}].status = {stage.status!r}, expected {expected!r}"
    )


def assert_brain_call_logged(brain_calls_dir: Path) -> None:
    files = list(brain_calls_dir.glob("*"))
    assert len(files) > 0, f"No brain call logs found in {brain_calls_dir}"


def assert_log_contains_events(log_path: Path, events: list[str]) -> None:
    content = log_path.read_text(encoding="utf-8")
    for event in events:
        assert event in content, f"Expected event {event!r} not found in {log_path}"


def assert_audit_has_entries(audit_path: Path, min_count: int) -> None:
    lines = [
        ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(lines) >= min_count, (
        f"Audit at {audit_path} has {len(lines)} entries, expected at least {min_count}"
    )


def assert_stats_has_entries(stats_path: Path, min_count: int) -> None:
    data = json.loads(stats_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        count = len(data)
    elif isinstance(data, dict):
        count = len(data)
    else:
        count = 1
    assert count >= min_count, (
        f"Stats at {stats_path} has {count} entries, expected at least {min_count}"
    )


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def wait_for_status(
    state_path: Path,
    expected_status: str,
    timeout: float = 5.0,
) -> ConductorState:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if state_path.exists():
            state = load_state(state_path, ConductorState)
            if getattr(state, "status", None) == expected_status:
                return state
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(
                f"State at {state_path} did not reach status {expected_status!r} within {timeout}s"
            )
        await asyncio.sleep(0.1)
