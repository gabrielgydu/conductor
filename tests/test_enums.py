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
    assert len(values) == 6


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
