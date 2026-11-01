"""How the orchestrator starts runner windows: the command, its paths and bookkeeping."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import conductor.core.orchestrator as orch
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
from conductor.core.storage import StorageResolver

FNAME = "run-0"
RUN_SH = 'PHASES[1]="01-backend.md"\nPHASE_TOKENS[1]="DONE"\nPHASE_NAMES[1]="Backend"\n'


def _env(tmp_path: Path, repo_name: str = "repo", wt_parent: str = "worktrees"):
    """A GENERATED stage whose worktree holds a speccer-generated run.sh."""
    repo = tmp_path / repo_name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    wt = tmp_path / wt_parent / "proj-run-0-stage-0"
    docs = wt / "docs" / FNAME
    (docs / "prompts").mkdir(parents=True)
    (docs / "prompts" / "01-backend.md").write_text("do it\n")
    (docs / "run.sh").write_text(RUN_SH)
    stage = StageState(name="stage-0", spec_mode="full", status=StageStatus.GENERATED, worktree=str(wt))
    run = RunState(index=0, name=FNAME, description="t", stages=[stage], status=RunStatus.ACTIVE)
    state = ConductorState(project_name="proj", base_branch="main", runs=[run])
    tmux = MagicMock()
    tmux.spawn_runner_in_window = AsyncMock()
    return state, StorageResolver(repo), tmux, wt


async def test_runner_wrapper_gets_the_fail_log_the_orchestrator_reads(tmp_path):
    """The wrapper derived it by replacing 'conductor-exit-' anywhere in the exit
    file's path, including a project key made from a repo named like that."""
    state, storage, tmux, _ = _env(tmp_path, repo_name="conductor-exit-test")

    await orch.start_runner(state, 0, 0, tmux, storage)

    kwargs = tmux.spawn_runner_in_window.await_args.kwargs
    assert kwargs["fail_log"] == orch._runner_fail_log(storage, "proj", FNAME)
    assert kwargs["fail_log"].parent == kwargs["exit_file"].parent
