import pytest
from pathlib import Path
import glob

from conductor.core.models import (
    ConductorState,
    RunState,
    StageState,
    ContextWiring,
    MonitorState,
    IntegrationState,
    ConflictRecord,
    E2ETestState,
)


def make_run(index: int, name: str) -> RunState:
    wiring = ContextWiring(sources=["feat-a"], targets=["feat-b"])
    stages = [
        StageState(name="speccing", spec_mode="full", context_wiring=wiring),
        StageState(
            name="brain", spec_mode="full", status="active", context_wiring=wiring
        ),
        StageState(
            name="fixer", spec_mode="full", status="pending", context_wiring=wiring
        ),
    ]
    monitor = MonitorState(stall_count=0, ci_pass_count=3, ci_fail_count=1)
    return RunState(
        index=index,
        name=name,
        description=f"{name} feature run",
        depends_on=[],
        stages=stages,
        monitor=monitor,
    )


@pytest.fixture
def sample_conductor_state() -> ConductorState:
    runs = [
        make_run(0, "feature-alpha"),
        make_run(1, "feature-beta"),
    ]

    integration = IntegrationState(
        branch="integration/test",
        conflicts_resolved=[],
        conflicts_unresolved=[
            ConflictRecord(
                file="src/shared.py",
                feature_a="feature-alpha",
                feature_b="feature-beta",
                description="conflicting imports",
            )
        ],
        e2e=E2ETestState(passed=10, failed=2, skipped=1),
    )

    return ConductorState(
        project_name="test-project",
        check_interval_s=300,
        runs=runs,
        integration=integration,
    )


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture(autouse=True)
def clean_tmp_files():
    """Clean up conductor temp files before and after each test."""
    # Remove old conductor exit and activity files to avoid test pollution
    for pattern in ["/tmp/conductor-exit-*", "/tmp/ralph-activity-*", "/tmp/conductor-speccer-exit-*"]:
        for f in glob.glob(pattern):
            try:
                Path(f).unlink()
            except (OSError, FileNotFoundError):
                pass
    yield
    # Also clean up after test
    for pattern in ["/tmp/conductor-exit-*", "/tmp/ralph-activity-*", "/tmp/conductor-speccer-exit-*"]:
        for f in glob.glob(pattern):
            try:
                Path(f).unlink()
            except (OSError, FileNotFoundError):
                pass


@pytest.fixture(autouse=True)
def patch_orchestrator_create_worktree(monkeypatch, tmp_path):
    """Patch create_worktree to avoid real git ops in tests."""
    import conductor.core.orchestrator as orch

    _wt_counter = [0]

    def mock_create_worktree(state, run_idx, stage_idx, storage_or_project_dir, *args, **kwargs):
        run = state.runs[run_idx]
        stage = run.stages[stage_idx]
        _wt_counter[0] += 1
        wt_dir = tmp_path / "worktrees" / f"wt-{_wt_counter[0]}"
        wt_dir.mkdir(parents=True, exist_ok=True)
        stage.branch = f"conductor/{state.project_name}/{run.name}/{stage.name}"
        stage.worktree = str(wt_dir)

    monkeypatch.setattr(orch, "create_worktree", mock_create_worktree)
