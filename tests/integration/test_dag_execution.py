"""DAG execution tests — verify dependency-based run scheduling."""

from __future__ import annotations


from conductor.core.enums import RunStatus, StageStatus
from conductor.core.orchestrator import activate_ready_runs
from tests.helpers import (
    block_run,
    complete_run,
    make_conductor_state,
    make_run_state,
)


def test_parallel_independent_runs():
    """DAG-1: Two runs with no deps both activate simultaneously."""
    runs = [
        make_run_state(0, "run-a"),
        make_run_state(1, "run-b"),
    ]
    state = make_conductor_state("dag-test", runs)

    activated = activate_ready_runs(state)

    assert activated == [0, 1]
    assert state.runs[0].status == RunStatus.ACTIVE
    assert state.runs[1].status == RunStatus.ACTIVE


def test_sequential_dependent_runs():
    """DAG-2: Run B depends on A. B stays PENDING until A is DONE."""
    runs = [
        make_run_state(0, "run-a"),
        make_run_state(1, "run-b", depends_on=[0]),
    ]
    state = make_conductor_state("dag-test", runs)

    # Pass 1: only A activates
    activated = activate_ready_runs(state)
    assert activated == [0]
    assert state.runs[0].status == RunStatus.ACTIVE
    assert state.runs[1].status == RunStatus.PENDING

    # Pass 2: after A done, B activates
    complete_run(state, 0)
    activated = activate_ready_runs(state)
    assert activated == [1]
    assert state.runs[1].status == RunStatus.ACTIVE


def test_diamond_dependency():
    """DAG-3: Diamond DAG A -> {B, C} -> D."""
    runs = [
        make_run_state(0, "A"),
        make_run_state(1, "B", depends_on=[0]),
        make_run_state(2, "C", depends_on=[0]),
        make_run_state(3, "D", depends_on=[1, 2]),
    ]
    state = make_conductor_state("dag-test", runs)

    # Pass 1: only A
    activated = activate_ready_runs(state)
    assert activated == [0]

    # Pass 2: A done -> B and C activate
    complete_run(state, 0)
    activated = activate_ready_runs(state)
    assert activated == [1, 2]

    # Pass 3: B done, C still active -> D cannot activate (C still active)
    complete_run(state, 1)
    activated = activate_ready_runs(state)
    assert 3 not in activated  # D should not be activated yet

    # Pass 4: C done -> D activates
    complete_run(state, 2)
    activated = activate_ready_runs(state)
    assert activated == [3]


def test_blocked_run_blocks_dependents():
    """DAG-4: Failed/blocked run cascades BLOCKED to dependents."""
    runs = [
        make_run_state(0, "A"),
        make_run_state(1, "B", depends_on=[0]),
        make_run_state(2, "C", depends_on=[1]),
    ]
    state = make_conductor_state("dag-test", runs)
    block_run(state, 0)  # A is BLOCKED

    # First pass: B becomes BLOCKED (direct dep on A)
    activated = activate_ready_runs(state)
    assert activated == []
    assert state.runs[1].status == RunStatus.BLOCKED

    # Second pass: C becomes BLOCKED (transitive via B)
    activated = activate_ready_runs(state)
    assert activated == []
    assert state.runs[2].status == RunStatus.BLOCKED
