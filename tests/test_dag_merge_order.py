"""Tests for get_activation_order (DAG topological sort over DONE runs)."""
from __future__ import annotations

import pytest

from conductor.core.enums import RunStatus
from conductor.integration.merge import get_activation_order
from tests.helpers import make_run_state, make_conductor_state


def _done(index, name, depends_on=None):
    return make_run_state(index, name, depends_on=depends_on, status=RunStatus.DONE)


def _blocked(index, name, depends_on=None):
    return make_run_state(index, name, depends_on=depends_on, status=RunStatus.BLOCKED)


def test_single_done_run():
    run = _done(0, "feat-a")
    state = make_conductor_state("proj", [run])
    result = get_activation_order(state)
    assert len(result) == 1
    assert result[0].index == 0


def test_independent_runs_order():
    runs = [_done(0, "a"), _done(1, "b"), _done(2, "c")]
    state = make_conductor_state("proj", runs)
    result = get_activation_order(state)
    assert len(result) == 3
    indices = {r.index for r in result}
    assert indices == {0, 1, 2}


def test_linear_chain():
    runs = [
        _done(0, "a"),
        _done(1, "b", depends_on=[0]),
        _done(2, "c", depends_on=[1]),
    ]
    state = make_conductor_state("proj", runs)
    result = get_activation_order(state)
    assert len(result) == 3
    indices = [r.index for r in result]
    assert indices.index(0) < indices.index(1) < indices.index(2)


def test_diamond_dag():
    # A→B, A→C, B→D, C→D
    runs = [
        _done(0, "A"),
        _done(1, "B", depends_on=[0]),
        _done(2, "C", depends_on=[0]),
        _done(3, "D", depends_on=[1, 2]),
    ]
    state = make_conductor_state("proj", runs)
    result = get_activation_order(state)
    assert len(result) == 4
    indices = [r.index for r in result]
    assert indices[0] == 0   # A first
    assert indices[-1] == 3  # D last
    # B and C are in the middle
    assert set(indices[1:3]) == {1, 2}


def test_skips_blocked_runs():
    runs = [
        _done(0, "a"),
        _blocked(1, "b"),
        _done(2, "c"),
    ]
    state = make_conductor_state("proj", runs)
    result = get_activation_order(state)
    indices = {r.index for r in result}
    assert indices == {0, 2}


def test_skips_failed_runs():
    runs = [
        _done(0, "a"),
        _blocked(1, "failed-one"),  # failed runs become BLOCKED
        _done(2, "c"),
    ]
    state = make_conductor_state("proj", runs)
    result = get_activation_order(state)
    indices = {r.index for r in result}
    assert 1 not in indices
    assert {0, 2} == indices


def test_fewer_than_two_eligible_zero():
    state = make_conductor_state("proj", [])
    result = get_activation_order(state)
    assert result == []


def test_fewer_than_two_eligible_one():
    state = make_conductor_state("proj", [_done(0, "only")])
    result = get_activation_order(state)
    assert len(result) == 1


def test_done_run_with_non_done_dep():
    # B depends on A, B is DONE but A is BLOCKED
    # B should still be included (dep edges only among eligible runs)
    runs = [
        _blocked(0, "A"),
        _done(1, "B", depends_on=[0]),
    ]
    state = make_conductor_state("proj", runs)
    result = get_activation_order(state)
    assert len(result) == 1
    assert result[0].index == 1
