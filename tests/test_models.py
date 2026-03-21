import json
import pytest
from pathlib import Path
from pydantic import ValidationError
from conductor.core.enums import RunStatus, StageStatus, SpeccerStatus, IntegrationStatus
from conductor.core.models import (
    ConductorState,
    RunState,
    StageState,
    ContextWiring,
    MonitorState,
    SpeccerState,
    DomainState,
    IntegrationState,
    ConflictRecord,
    atomic_save,
    load_state,
    save_speccer_state,
)


def test_conductor_state_defaults():
    state = ConductorState()
    assert state.check_interval_s == 900
    assert state.runs == []


def test_conductor_state_roundtrip():
    state = ConductorState(check_interval_s=60)
    json_str = state.model_dump_json()
    restored = ConductorState.model_validate_json(json_str)
    assert restored == state


def test_run_state_with_enum_status():
    run = RunState(id="run-0", feature="feat-a", status="pending")
    assert run.status == RunStatus.PENDING


def test_stage_state_all_fields():
    wiring = ContextWiring(sources=["feature-a"], targets=["feature-b"])
    stage = StageState(
        id="stage-1",
        name="speccer",
        status="active",
        wiring=wiring,
        attempt=1,
    )
    json_str = stage.model_dump_json()
    restored = StageState.model_validate_json(json_str)
    assert restored.name == "speccer"
    assert restored.wiring.sources == ["feature-a"]


def test_context_wiring_serialization():
    wiring = ContextWiring(sources=["a", "b"], targets=["c"])
    json_str = wiring.model_dump_json()
    restored = ContextWiring.model_validate_json(json_str)
    assert restored.sources == ["a", "b"]
    assert restored.targets == ["c"]


def test_speccer_state_with_domains():
    domain = DomainState(index=0, name="auth", status="done")
    state = SpeccerState(
        feature="feat-a",
        status="active",
        domains=[domain],
    )
    json_str = state.model_dump_json()
    restored = SpeccerState.model_validate_json(json_str)
    assert len(restored.domains) == 1
    assert restored.domains[0].name == "auth"


def test_integration_state_with_conflicts():
    conflict = ConflictRecord(
        file="src/foo.py",
        feature_a="feat-a",
        feature_b="feat-b",
        description="merge conflict",
    )
    state = IntegrationState(
        status="conflict",
        conflicts=[conflict],
    )
    json_str = state.model_dump_json()
    restored = IntegrationState.model_validate_json(json_str)
    assert len(restored.conflicts) == 1
    assert restored.conflicts[0].file == "src/foo.py"


def test_monitor_state_defaults():
    monitor = MonitorState()
    assert monitor.ci_pass_count == 0
    assert monitor.ci_fail_count == 0
    assert monitor.last_checked_at is None


def test_invalid_enum_in_model():
    with pytest.raises(ValidationError):
        RunState(id="run-0", feature="feat-a", status="bogus")


def test_nested_model_validation():
    wiring = ContextWiring(sources=[], targets=[])
    stage = StageState(id="s1", name="speccer", status="pending", wiring=wiring)
    run = RunState(id="run-0", feature="feat-a", status="pending", stages=[stage])
    state = ConductorState(runs=[run])
    assert len(state.runs) == 1
    assert len(state.runs[0].stages) == 1


# ---------------------------------------------------------------------------
# Persistence helper tests
# ---------------------------------------------------------------------------


def test_atomic_save_creates_file(tmp_path):
    path = tmp_path / "state.json"
    state = ConductorState()
    atomic_save(state, path)
    assert path.exists()


def test_atomic_save_replaces_existing(tmp_path):
    path = tmp_path / "state.json"
    state1 = ConductorState(check_interval_s=60)
    atomic_save(state1, path)
    state2 = ConductorState(check_interval_s=120)
    atomic_save(state2, path)
    data = json.loads(path.read_text())
    assert data["check_interval_s"] == 120


def test_atomic_save_no_partial_write(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original_content = '{"check_interval_s": 42, "runs": [], "integration": null, "created_at": null, "updated_at": null}'
    path.write_text(original_content)

    import os
    original_replace = os.replace

    def fail_replace(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    state = ConductorState(check_interval_s=999)
    with pytest.raises(OSError):
        atomic_save(state, path)

    # Original file should be unchanged
    assert path.read_text() == original_content
    # No .tmp files should remain
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0


def test_load_state_missing_file(tmp_path):
    path = tmp_path / "nonexistent.json"
    with pytest.raises(FileNotFoundError):
        load_state(path, ConductorState)


def test_load_state_corrupt_json(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("not valid json {{{")
    with pytest.raises(Exception):
        load_state(path, ConductorState)


def _make_speccer_state() -> SpeccerState:
    domains = [
        DomainState(index=0, name="auth", status="done", file="auth.md"),
        DomainState(index=1, name="billing", status="pending"),
    ]
    return SpeccerState(
        feature="feat-a",
        status="active",
        mode="normal",
        preset="standard",
        iteration=2,
        domains=domains,
    )


def test_save_speccer_state_writes_json(tmp_path):
    state = _make_speccer_state()
    json_path = tmp_path / "SPECCER-STATE.json"
    md_path = tmp_path / "PROGRESS.md"
    save_speccer_state(state, json_path, md_path)
    assert json_path.exists()
    data = json.loads(json_path.read_text())
    assert data["feature"] == "feat-a"


def test_save_speccer_state_writes_progress_md(tmp_path):
    state = _make_speccer_state()
    json_path = tmp_path / "SPECCER-STATE.json"
    md_path = tmp_path / "PROGRESS.md"
    save_speccer_state(state, json_path, md_path)
    assert md_path.exists()
    content = md_path.read_text()
    assert "STATUS: ACTIVE" in content
    assert "auth" in content
    assert "billing" in content


def test_save_speccer_state_roundtrip(tmp_path):
    state = _make_speccer_state()
    json_path = tmp_path / "SPECCER-STATE.json"
    md_path = tmp_path / "PROGRESS.md"
    save_speccer_state(state, json_path, md_path)
    restored = load_state(json_path, SpeccerState)
    assert restored.feature == state.feature
    assert restored.iteration == state.iteration
    assert len(restored.domains) == 2


def test_progress_md_format(tmp_path):
    state = _make_speccer_state()
    json_path = tmp_path / "SPECCER-STATE.json"
    md_path = tmp_path / "PROGRESS.md"
    save_speccer_state(state, json_path, md_path)
    content = md_path.read_text()
    assert "| # | Domain | Status | File |" in content
    assert "|---|--------|--------|------|" in content
    assert "| 00 | auth | done | auth.md |" in content
    assert "| 01 | billing | pending | — |" in content
