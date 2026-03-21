import pytest
from pathlib import Path

from conductor.core.enums import RunStatus, StageStatus, IntegrationStatus
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


@pytest.fixture
def sample_conductor_state() -> ConductorState:
    wiring = ContextWiring(sources=["feat-a"], targets=["feat-b"])

    def make_run(run_id: str, feature: str) -> RunState:
        stages = [
            StageState(id=f"{run_id}-s1", name="speccer", status="done", wiring=wiring),
            StageState(id=f"{run_id}-s2", name="brain", status="active", wiring=wiring),
            StageState(id=f"{run_id}-s3", name="fixer", status="pending", wiring=wiring),
        ]
        monitor = MonitorState(ci_pass_count=3, ci_fail_count=1)
        return RunState(
            id=run_id,
            feature=feature,
            status="active",
            stages=stages,
            monitor=monitor,
        )

    runs = [
        make_run("run-0", "feature-alpha"),
        make_run("run-1", "feature-beta"),
    ]

    integration = IntegrationState(
        status="conflict",
        conflicts=[
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
        check_interval_s=300,
        runs=runs,
        integration=integration,
    )


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    return tmp_path
