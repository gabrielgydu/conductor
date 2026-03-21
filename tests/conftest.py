import pytest
from pathlib import Path

from conductor.core.enums import RunStatus, StageStatus, IntegrationStatus, SpeccerStatus
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
        StageState(name="brain", spec_mode="full", status="active", context_wiring=wiring),
        StageState(name="fixer", spec_mode="full", status="pending", context_wiring=wiring),
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
