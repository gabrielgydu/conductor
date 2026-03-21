"""
Constitution compliance tests — verify that the data model satisfies
all constitution principles at the type level.
"""
import pytest
from conductor.core.models import MonitorState, RunState, StageState, validate_dag
from conductor.core.storage import StorageResolver


def test_stall_count_field_exists():
    monitor = MonitorState()
    assert hasattr(monitor, "stall_count")
    assert isinstance(monitor.stall_count, int)
    assert monitor.stall_count == 0


def test_stall_count_cap_value():
    monitor = MonitorState(stall_count=5)
    assert monitor.stall_count == 5


def test_depends_on_field_exists():
    run = RunState(index=0, name="x", description="x")
    assert hasattr(run, "depends_on")
    assert isinstance(run.depends_on, list)


def test_dag_validation_rejects_cycles():
    run_a = RunState(index=0, name="a", description="a", depends_on=[1])
    run_b = RunState(index=1, name="b", description="b", depends_on=[0])
    with pytest.raises(ValueError):
        validate_dag([run_a, run_b])


def test_dag_validation_importable():
    from conductor.core.models import validate_dag as _vd
    assert callable(_vd)


def test_brain_calls_dir_exists_in_storage():
    assert hasattr(StorageResolver, "brain_calls_dir")
    assert callable(StorageResolver.brain_calls_dir)


def test_worktree_field_exists():
    stage = StageState(name="x", spec_mode="y")
    assert hasattr(stage, "worktree")
    assert stage.worktree is None


def test_branch_field_exists():
    stage = StageState(name="x", spec_mode="y")
    assert hasattr(stage, "branch")
    assert stage.branch is None
