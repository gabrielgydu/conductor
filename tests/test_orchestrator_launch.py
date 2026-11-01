"""How the orchestrator starts speccer and runner windows: the command, its paths and bookkeeping."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import conductor.core.orchestrator as orch
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, ContextWiring, RunState, StageState
from conductor.core.storage import StorageResolver

FNAME = "run-0"
RUN_SH = 'PHASES[1]="01-backend.md"\nPHASE_TOKENS[1]="DONE"\nPHASE_NAMES[1]="Backend"\n'


def _finish_at_once(name, cmd, *, exit_file, **_kw) -> None:
    """spawn_runner_in_window whose command exits 0 right away (speccer steps wait for it)."""
    exit_file.write_text("0")


def launch_env(tmp_path: Path, repo_name: str = "repo", wt_parent: str = "worktrees"):
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
    tmux.spawn_runner_in_window = AsyncMock(side_effect=_finish_at_once)
    return state, StorageResolver(repo), tmux, wt


async def test_runner_wrapper_gets_the_fail_log_the_orchestrator_reads(tmp_path):
    """The wrapper derived it by replacing 'conductor-exit-' anywhere in the exit
    file's path, including a project key made from a repo named like that."""
    state, storage, tmux, _ = launch_env(tmp_path, repo_name="conductor-exit-test")

    await orch.start_runner(state, 0, 0, tmux, storage)

    kwargs = tmux.spawn_runner_in_window.await_args.kwargs
    assert kwargs["fail_log"] == orch._runner_fail_log(storage, "proj", FNAME)
    assert kwargs["fail_log"].parent == kwargs["exit_file"].parent


async def test_runner_start_time_is_set_before_the_spawn(tmp_path):
    """An exit file counts only when newer than started_at, which was set after the spawn."""
    state, storage, tmux, _ = launch_env(tmp_path)
    stage = state.runs[0].stages[0]
    at_spawn = []
    tmux.spawn_runner_in_window.side_effect = lambda *a, **kw: at_spawn.append(stage.started_at)

    await orch.start_runner(state, 0, 0, tmux, storage)

    assert at_spawn == [stage.started_at] and stage.started_at is not None


# -- paths with spaces --------------------------------------------------------

_ARGV_RECORDER = '#!/bin/bash\nprintf "%s\\n" "$PWD" "$@" > "$ARGV_OUT"\n'


def _run_window_command(tmp_path: Path, tmux) -> list[str]:
    """Run the spawned window's command with bash, as tmux would; return [cwd, *argv[1:]]."""
    args, kwargs = tmux.spawn_runner_in_window.await_args
    out = tmp_path / "argv.txt"
    r = subprocess.run(
        ["bash", "-c", args[1]], cwd=kwargs["cwd"], capture_output=True, text=True, check=False,
        env={**os.environ, "ARGV_OUT": str(out)},
    )
    assert r.returncode == 0, r.stderr
    return out.read_text().splitlines()


def _record_argv_instead_of_python(tmp_path: Path, monkeypatch) -> None:
    recorder = tmp_path / "py bin" / "python"
    recorder.parent.mkdir()
    recorder.write_text(_ARGV_RECORDER)
    recorder.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(recorder))


async def test_runner_command_survives_spaces_in_paths(tmp_path, monkeypatch):
    """`cd /home/u/My Projects/... && ...` failed with 'cd: too many arguments' for every stage."""
    _record_argv_instead_of_python(tmp_path, monkeypatch)
    state, storage, tmux, wt = launch_env(tmp_path, wt_parent="My Projects/worktrees")

    await orch.start_runner(state, 0, 0, tmux, storage)

    assert _run_window_command(tmp_path, tmux) == [
        str(wt), "-P", "-m", "runner", "run", "--feature", FNAME, "--storage-dir", str(wt / "docs" / FNAME),
    ]


async def test_speccer_command_survives_spaces_in_paths(tmp_path, monkeypatch):
    _record_argv_instead_of_python(tmp_path, monkeypatch)
    state, storage, tmux, wt = launch_env(tmp_path, wt_parent="My Projects/worktrees")
    context = tmp_path / "Shared Specs" / "api.md"
    context.parent.mkdir()
    context.write_text("api\n")
    stage = state.runs[0].stages[0]
    stage.context_wiring = ContextWiring(
        sources=[json.dumps({"type": "external", "source_path": str(context)})]
    )

    await orch.run_speccer_init(state, 0, 0, tmux, storage)

    argv = _run_window_command(tmp_path, tmux)
    assert argv[:5] == [str(wt), "-P", "-m", "speccer", "init"]
    assert argv[argv.index("--project-dir") + 1] == str(wt)
    assert argv[argv.index("--spec-context") + 1] == str(context)
