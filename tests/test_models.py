import json
import pytest
from pydantic import ValidationError
from conductor.core.enums import RunStatus
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
    validate_dag,
)


def test_conductor_state_defaults():
    state = ConductorState(project_name="default-proj")
    assert state.check_interval_s == 900
    assert state.runs == []


def test_conductor_state_requires_project_name():
    with pytest.raises(ValidationError):
        ConductorState()


def test_conductor_state_roundtrip():
    state = ConductorState(
        project_name="my-project", base_branch="main", check_interval_s=60
    )
    json_str = state.model_dump_json()
    restored = ConductorState.model_validate_json(json_str)
    assert restored.project_name == "my-project"
    assert restored.base_branch == "main"


def test_run_state_with_enum_status():
    run = RunState(index=0, name="test", description="test run", status="pending")
    assert run.status == RunStatus.PENDING


def test_run_state_depends_on_serialization():
    run = RunState(index=2, name="run-2", description="run two", depends_on=[0, 1])
    json_str = run.model_dump_json()
    restored = RunState.model_validate_json(json_str)
    assert restored.depends_on == [0, 1]


def test_run_state_constitution_field():
    run = RunState(
        index=0, name="run-0", description="run zero", constitution=["rule1", "rule2"]
    )
    json_str = run.model_dump_json()
    restored = RunState.model_validate_json(json_str)
    assert restored.constitution == ["rule1", "rule2"]


def test_stage_state_all_fields():
    wiring = ContextWiring(sources=["feature-a"], targets=["feature-b"])
    stage = StageState(
        name="speccing",
        spec_mode="full",
        status="executing",
        context_wiring=wiring,
    )
    json_str = stage.model_dump_json()
    restored = StageState.model_validate_json(json_str)
    assert restored.name == "speccing"
    assert restored.context_wiring.sources == ["feature-a"]


def test_stage_state_worktree_branch():
    stage = StageState(
        name="speccing", spec_mode="full", worktree="/path", branch="feat/x"
    )
    json_str = stage.model_dump_json()
    restored = StageState.model_validate_json(json_str)
    assert restored.worktree == "/path"
    assert restored.branch == "feat/x"

    stage_none = StageState(name="speccing", spec_mode="full")
    json_str2 = stage_none.model_dump_json()
    restored2 = StageState.model_validate_json(json_str2)
    assert restored2.worktree is None
    assert restored2.branch is None


def test_stage_state_pid_tracking():
    stage = StageState(name="speccing", spec_mode="full", pid=12345)
    json_str = stage.model_dump_json()
    restored = StageState.model_validate_json(json_str)
    assert restored.pid == 12345

    stage_none = StageState(name="speccing", spec_mode="full")
    json_str2 = stage_none.model_dump_json()
    restored2 = StageState.model_validate_json(json_str2)
    assert restored2.pid is None


def test_context_wiring_serialization():
    wiring = ContextWiring(sources=["a", "b"], targets=["c"])
    json_str = wiring.model_dump_json()
    restored = ContextWiring.model_validate_json(json_str)
    assert restored.sources == ["a", "b"]
    assert restored.targets == ["c"]


def test_speccer_state_with_domains():
    domain = DomainState(index=0, name="auth", status="done")
    state = SpeccerState(
        feature_name="feat-a",
        status="speccing",
        mode="backend",
        preset="base",
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
        branch="int/test",
        conflicts_resolved=[],
        conflicts_unresolved=[conflict],
    )
    json_str = state.model_dump_json()
    restored = IntegrationState.model_validate_json(json_str)
    assert len(restored.conflicts_unresolved) == 1
    assert restored.conflicts_unresolved[0].file == "src/foo.py"


def test_integration_state_merged_runs():
    state = IntegrationState(branch="int/test", merged_runs=[0, 2])
    json_str = state.model_dump_json()
    restored = IntegrationState.model_validate_json(json_str)
    assert restored.merged_runs == [0, 2]


def test_integration_state_split_conflicts():
    resolved = ConflictRecord(
        file="a.py", feature_a="fa", feature_b="fb", description="resolved"
    )
    unresolved = ConflictRecord(
        file="b.py", feature_a="fa", feature_b="fb", description="unresolved"
    )
    state = IntegrationState(
        branch="int/test",
        conflicts_resolved=[resolved],
        conflicts_unresolved=[unresolved],
    )
    json_str = state.model_dump_json()
    restored = IntegrationState.model_validate_json(json_str)
    assert len(restored.conflicts_resolved) == 1
    assert len(restored.conflicts_unresolved) == 1
    assert restored.conflicts_resolved[0].file == "a.py"
    assert restored.conflicts_unresolved[0].file == "b.py"


def test_monitor_state_defaults():
    monitor = MonitorState()
    assert monitor.stall_count == 0
    assert monitor.last_progress_hash is None
    assert monitor.last_check_ts is None
    assert monitor.retry_count == 0
    assert monitor.ci_pass_count == 0
    assert monitor.ci_fail_count == 0
    assert monitor.last_ci_url is None


def test_monitor_state_stall_and_ci_coexist():
    monitor = MonitorState(stall_count=3, ci_fail_count=1)
    json_str = monitor.model_dump_json()
    restored = MonitorState.model_validate_json(json_str)
    assert restored.stall_count == 3
    assert restored.ci_fail_count == 1


def test_invalid_enum_in_model():
    with pytest.raises(ValidationError):
        RunState(index=0, name="run-0", description="run zero", status="bogus")


def test_nested_model_validation():
    wiring = ContextWiring(sources=[], targets=[])
    stage = StageState(
        name="speccing", spec_mode="full", status="pending", context_wiring=wiring
    )
    run = RunState(index=0, name="run-0", description="run zero", stages=[stage])
    state = ConductorState(project_name="proj", runs=[run])
    assert len(state.runs) == 1
    assert len(state.runs[0].stages) == 1


# ---------------------------------------------------------------------------
# Persistence helper tests
# ---------------------------------------------------------------------------


def test_atomic_save_creates_file(tmp_path):
    path = tmp_path / "state.json"
    state = ConductorState(project_name="proj")
    atomic_save(state, path)
    assert path.exists()


def test_atomic_save_replaces_existing(tmp_path):
    path = tmp_path / "state.json"
    state1 = ConductorState(project_name="proj", check_interval_s=60)
    atomic_save(state1, path)
    state2 = ConductorState(project_name="proj", check_interval_s=120)
    atomic_save(state2, path)
    data = json.loads(path.read_text())
    assert data["check_interval_s"] == 120


def test_atomic_save_no_partial_write(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state1 = ConductorState(project_name="proj", check_interval_s=42)
    atomic_save(state1, path)
    original_content = path.read_text()

    import os

    def fail_replace(src, dst):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    state = ConductorState(project_name="proj", check_interval_s=999)
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
        feature_name="feat-a",
        status="speccing",
        mode="backend",
        preset="base",
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
    assert data["feature_name"] == "feat-a"


def test_save_speccer_state_writes_progress_md(tmp_path):
    state = _make_speccer_state()
    json_path = tmp_path / "SPECCER-STATE.json"
    md_path = tmp_path / "PROGRESS.md"
    save_speccer_state(state, json_path, md_path)
    assert md_path.exists()
    content = md_path.read_text()
    assert "STATUS: SPECCING" in content
    assert "auth" in content
    assert "billing" in content


def test_save_speccer_state_roundtrip(tmp_path):
    state = _make_speccer_state()
    json_path = tmp_path / "SPECCER-STATE.json"
    md_path = tmp_path / "PROGRESS.md"
    save_speccer_state(state, json_path, md_path)
    restored = load_state(json_path, SpeccerState)
    assert restored.feature_name == state.feature_name
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


# ---------------------------------------------------------------------------
# DAG validation tests
# ---------------------------------------------------------------------------


def _make_run(index: int, depends_on=None) -> RunState:
    return RunState(
        index=index,
        name=f"run-{index}",
        description=f"Run {index}",
        depends_on=depends_on or [],
    )


def test_validate_dag_valid():
    runs = [_make_run(0), _make_run(1, [0]), _make_run(2, [0, 1])]
    validate_dag(runs)  # no error


def test_validate_dag_cycle_detected():
    runs = [_make_run(0, [1]), _make_run(1, [0])]
    with pytest.raises(ValueError, match="cycle"):
        validate_dag(runs)


def test_validate_dag_missing_ref():
    runs = [_make_run(0, [99])]
    with pytest.raises(ValueError, match="non-existent"):
        validate_dag(runs)


def test_validate_dag_self_ref():
    runs = [_make_run(0, [0])]
    with pytest.raises(ValueError, match="itself"):
        validate_dag(runs)


def test_validate_dag_empty():
    validate_dag([])  # no error


def test_validate_dag_disconnected():
    runs = [_make_run(0), _make_run(1), _make_run(2)]
    validate_dag(runs)  # no error


def test_validate_dag_duplicate_index():
    run_a = RunState(index=0, name="run-a", description="a", depends_on=[])
    run_b = RunState(index=0, name="run-b", description="b", depends_on=[])
    with pytest.raises(ValueError, match="Duplicate"):
        validate_dag([run_a, run_b])
