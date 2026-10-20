import json
import pytest
from conductor.core.enums import (
    RunStatus,
    StageStatus,
    SpeccerStatus,
    IntegrationStatus,
    BrainAction,
    FixerStatus,
)


def test_run_status_values():
    values = [e.value for e in RunStatus]
    assert len(values) == 4
    assert all(isinstance(v, str) for v in values)
    assert "pending" in values
    assert "active" in values
    assert "done" in values
    assert "blocked" in values


def test_stage_status_values():
    values = [e.value for e in StageStatus]
    assert len(values) == 11


def test_speccer_status_values():
    values = [e.value for e in SpeccerStatus]
    assert len(values) == 6


def test_integration_status_values():
    values = [e.value for e in IntegrationStatus]
    assert len(values) == 7


def test_brain_action_values():
    values = [e.value for e in BrainAction]
    assert len(values) == 5


def test_fixer_status_values():
    values = [e.value for e in FixerStatus]
    assert len(values) == 8


def test_enum_json_serialization():
    result = json.dumps(RunStatus.PENDING)
    assert result == '"pending"'


def test_enum_from_string():
    assert RunStatus("pending") is RunStatus.PENDING


def test_invalid_enum_value():
    with pytest.raises(ValueError):
        RunStatus("invalid")


def test_stage_status_has_spec_stages():
    assert StageStatus.SPEC_INIT in StageStatus
    assert StageStatus.SPEC_RUNNING in StageStatus
    assert StageStatus.SPEC_NEEDS_INPUT in StageStatus
    assert StageStatus.SPEC_COMPLETE in StageStatus


def test_stage_status_has_stalled():
    assert StageStatus.STALLED in StageStatus


def test_speccer_status_has_exploring():
    assert SpeccerStatus.EXPLORING in SpeccerStatus


def test_fixer_status_has_waiting_ci():
    assert FixerStatus.WAITING_CI in FixerStatus


def test_brain_action_has_steer():
    assert BrainAction.STEER in BrainAction


def test_integration_status_has_conflict_resolving():
    assert IntegrationStatus.CONFLICT_RESOLVING in IntegrationStatus


def test_enum_renders_as_value_in_strings():
    # Log lines interpolate statuses directly; a (str, Enum) renders as
    # 'StageStatus.GENERATED' on Python 3.12+.
    assert f"{StageStatus.GENERATED}" == "generated"
    assert str(RunStatus.DONE) == "done"
    assert "{}".format(IntegrationStatus.NEEDS_FIX) == "needs_fix"


def test_enum_members_assigned_after_validation_persist_as_values():
    from conductor.core.models import ConductorState, IntegrationState, RunState, StageState

    state = ConductorState(
        project_name="p",
        runs=[RunState(index=0, name="r", description="", stages=[StageState(name="b", spec_mode="backend")])],
    )
    # Assignment bypasses use_enum_values, so the members themselves get dumped.
    state.runs[0].status = RunStatus.DONE
    state.runs[0].stages[0].status = StageStatus.GENERATED
    state.integration = IntegrationState(branch="x")
    state.integration.status = IntegrationStatus.NEEDS_FIX

    data = json.loads(state.model_dump_json())
    assert data["runs"][0]["status"] == "done"
    assert data["runs"][0]["stages"][0]["status"] == "generated"
    assert data["integration"]["status"] == "needs_fix"
    assert ConductorState.model_validate(data).runs[0].stages[0].status == StageStatus.GENERATED
