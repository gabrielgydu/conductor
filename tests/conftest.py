import os
import pytest
from collections import Counter
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
from conductor.core.storage import CONDUCTOR_HOME_ENV
from conductor.core.presets import PRESETS_DIR_ENV


# ---------------------------------------------------------------------------
# Hermeticity guards
# ---------------------------------------------------------------------------

# CLIs the suite must never run for real: they cost money (claude; node runs
# the Codex companion), act on GitHub with the developer's credentials (gh),
# download or launch browsers (npx, playwright), or touch the developer's live
# tmux server. A test that needs one installs its own mock earlier on PATH
# (e.g. tests/integration/conftest.py::MockClaudeCLI); that still wins because
# it is prepended after this session-wide stub directory.
# Set CONDUCTOR_TESTS_ALLOW_REAL_CLIS=1 to opt out.
_BLOCKED_CLIS = ("claude", "gh", "node", "npx", "playwright", "tmux")
_BLOCKED_CLI_EXIT = 97
_BLOCKED_CLI_STUB = """#!/bin/sh
printf '%s :: %s %.120s\\n' "${{PYTEST_CURRENT_TEST:-?}}" "$(basename "$0")" "$*" >> "{log}"
echo "tests/conftest.py: blocked real '$(basename "$0")' (exit {code})" >&2
exit {code}
"""
_blocked_cli_log: Path | None = None


@pytest.fixture(scope="session", autouse=True)
def _block_external_clis(tmp_path_factory):
    """Put fail-fast stubs for paid/networked CLIs first on PATH."""
    global _blocked_cli_log
    if os.environ.get("CONDUCTOR_TESTS_ALLOW_REAL_CLIS") == "1":
        yield None
        return
    bindir = tmp_path_factory.mktemp("blocked-clis")
    _blocked_cli_log = bindir / "invocations.log"
    _blocked_cli_log.touch()
    stub = _BLOCKED_CLI_STUB.format(log=_blocked_cli_log, code=_BLOCKED_CLI_EXIT)
    for name in _BLOCKED_CLIS:
        path = bindir / name
        path.write_text(stub)
        path.chmod(0o755)
    mp = pytest.MonkeyPatch()
    mp.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    yield bindir
    mp.undo()


def pytest_terminal_summary(terminalreporter):
    """List every attempted call to a blocked CLI (tests often swallow the failure)."""
    if _blocked_cli_log is None or not _blocked_cli_log.exists():
        return
    calls: Counter[tuple[str, str]] = Counter()
    for ln in _blocked_cli_log.read_text().splitlines():
        test_id, sep, cmd = ln.partition(" :: ")
        if sep:
            # PYTEST_CURRENT_TEST looks like "tests/x.py::test_y (call)"
            calls[(test_id.rsplit(" (", 1)[0], cmd.split(" ", 1)[0])] += 1
    if calls:
        terminalreporter.section(f"blocked external CLI calls ({sum(calls.values())})")
        for (test_id, tool), n in sorted(calls.items()):
            terminalreporter.write_line(f"{n:3d}x {tool:10s} {test_id}")


@pytest.fixture(autouse=True)
def _isolate_conductor_home(monkeypatch, tmp_path_factory):
    """Keep StorageResolver and user-preset discovery off the real ~/.conductor."""
    monkeypatch.setenv(CONDUCTOR_HOME_ENV, str(tmp_path_factory.mktemp("conductor-home")))
    monkeypatch.delenv(PRESETS_DIR_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_gh_retry_backoff(monkeypatch):
    """merge.py sleeps between gh retries; with gh mocked to fail that was 20 s per test."""
    import conductor.integration.merge as merge

    monkeypatch.setattr(merge, "_GH_RETRY_DELAY_S", 0.0)


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
    for pattern in [
        "/tmp/conductor-exit-*",
        "/tmp/conductor-activity-*",
        "/tmp/conductor-speccer-exit-*",
    ]:
        for f in glob.glob(pattern):
            try:
                Path(f).unlink()
            except (OSError, FileNotFoundError):
                pass
    yield
    # Also clean up after test
    for pattern in [
        "/tmp/conductor-exit-*",
        "/tmp/conductor-activity-*",
        "/tmp/conductor-speccer-exit-*",
    ]:
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

    def mock_create_worktree(
        state, run_idx, stage_idx, storage_or_project_dir, *args, **kwargs
    ):
        run = state.runs[run_idx]
        stage = run.stages[stage_idx]
        _wt_counter[0] += 1
        wt_dir = tmp_path / "worktrees" / f"wt-{_wt_counter[0]}"
        wt_dir.mkdir(parents=True, exist_ok=True)
        stage.branch = f"conductor/{state.project_name}/{run.name}/{stage.name}"
        stage.worktree = str(wt_dir)

    monkeypatch.setattr(orch, "create_worktree", mock_create_worktree)
