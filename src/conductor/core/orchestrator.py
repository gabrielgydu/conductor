"""Conductor orchestration loop."""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import conductor.core.tmux as _tmux_module
from conductor.core.brain import brain_answer_questions
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState
from conductor.core.storage import StorageResolver


@dataclass
class ConductorConfig:
    check_interval_s: float = 900.0
    max_iterations: int = 1000
    max_retries: int = 2
    project_root: Path | None = None


def _read_progress_status(progress_file: Path) -> str | None:
    """Read STATUS: line from PROGRESS.md, return status string or None."""
    if not progress_file.exists():
        return None
    for line in progress_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("STATUS:"):
            return line[len("STATUS:"):].strip().upper()
    return None


def _append_log(log_path: Path, message: str) -> None:
    """Append a line to CONDUCTOR-LOG.md."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{message}\n")


def create_worktree(
    state: ConductorState,
    run_list_idx: int,
    stage_idx: int,
    repo_path: Path,
    worktrees_base: Path,
) -> None:
    """Create a git worktree for a stage, chaining branches correctly.

    Branch naming: conductor/<project>/<run>/<stage>
    Start point priority:
      1. Previous stage's branch (if stage_idx > 0)
      2. Last dependency's last stage branch (if depends_on is set)
      3. state.base_branch (independent first stage)

    Updates stage.branch and stage.worktree in-place.
    """
    run = state.runs[run_list_idx]
    stage = run.stages[stage_idx]

    branch = f"conductor/{state.project_name}/{run.name}/{stage.name}"

    if stage_idx > 0:
        start_point = run.stages[stage_idx - 1].branch
    elif run.depends_on:
        run_by_index = {r.index: r for r in state.runs}
        dep_run = run_by_index[run.depends_on[-1]]
        start_point = dep_run.stages[-1].branch
    else:
        start_point = state.base_branch

    worktree_path = worktrees_base / run.name / stage.name
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), start_point],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    stage.branch = branch
    stage.worktree = str(worktree_path)


def activate_ready_runs(state: ConductorState) -> list[int]:
    """Activate runs whose dependencies are satisfied; block runs whose deps are blocked.

    Returns a list of run indices that were activated in this call.
    """
    run_by_index: dict[int, object] = {run.index: run for run in state.runs}
    activated: list[int] = []

    for run in state.runs:
        if run.status != RunStatus.PENDING:
            continue

        # Check dependency statuses
        blocked = any(
            run_by_index[dep].status == RunStatus.BLOCKED  # type: ignore[union-attr]
            for dep in run.depends_on
            if dep in run_by_index
        )
        if blocked:
            run.status = RunStatus.BLOCKED
            continue

        all_done = all(
            run_by_index[dep].status == RunStatus.DONE  # type: ignore[union-attr]
            for dep in run.depends_on
            if dep in run_by_index
        )
        if all_done:
            run.status = RunStatus.ACTIVE
            if run.stages:
                run.stages[run.current_stage].status = StageStatus.SPEC_RUNNING
            activated.append(run.index)

    return activated


async def conductor_run_loop(
    state: ConductorState,
    config: ConductorConfig,
) -> ConductorState:
    """Main conductor orchestration loop.

    Drives runs through their lifecycle:
      PENDING -> SPEC_RUNNING -> SPEC_COMPLETE -> GENERATED
    Handles NEEDS_INPUT via brain, dead speccer detection, and retries.
    """
    project_root = config.project_root or Path.cwd()
    storage = StorageResolver(project_root)

    tmux = _tmux_module.TmuxManager(session_name=f"conductor-{state.project_name}")
    await tmux.ensure_session(f"conductor-{state.project_name}")

    conductor_log = storage.conductor_log(state.project_name)

    # Track window name per (run_idx, stage_idx)
    window_names: dict[tuple[int, int], str] = {}

    for _iteration in range(config.max_iterations):
        has_active_work = False

        for run_idx, run in enumerate(state.runs):
            if run.status in (RunStatus.DONE, RunStatus.BLOCKED):
                continue

            stage_idx = run.current_stage
            stage = run.stages[stage_idx]
            spec_dir = storage.spec_dir(run.name)
            progress_file = spec_dir / "PROGRESS.md"
            window_key = (run_idx, stage_idx)

            # Resolve terminal states immediately
            if stage.status == StageStatus.FAILED:
                run.status = RunStatus.BLOCKED
                _append_log(conductor_log, f"RUN_BLOCKED: {run.name} stage {stage_idx}")
                continue

            if stage.status in (StageStatus.GENERATED, StageStatus.DONE):
                run.status = RunStatus.DONE
                continue

            if stage.status == StageStatus.SPEC_COMPLETE:
                stage.status = StageStatus.GENERATED
                run.status = RunStatus.DONE
                _append_log(conductor_log, f"GENERATED: {run.name} stage {stage_idx}")
                continue

            # Active stages need processing
            has_active_work = True

            if stage.status == StageStatus.PENDING:
                run.status = RunStatus.ACTIVE
                stage.status = StageStatus.SPEC_RUNNING
                window_name = f"speccer-{run.index}-{stage_idx}"
                window_names[window_key] = window_name
                cmd = f"speccer init --feature {run.name} --spec-dir {spec_dir}"
                await tmux.spawn_in_window(window_name, cmd)
                _append_log(conductor_log, f"SPEC_INIT: {run.name} stage {stage_idx}")

            elif stage.status == StageStatus.SPEC_RUNNING:
                window_name = window_names.get(window_key, f"speccer-{run.index}-{stage_idx}")
                alive = await tmux.is_window_alive(window_name)
                status = _read_progress_status(progress_file)

                if alive:
                    if status == "NEEDS_INPUT":
                        stage.status = StageStatus.SPEC_NEEDS_INPUT
                        _append_log(conductor_log, f"SPEC_NEEDS_INPUT: {run.name} stage {stage_idx}")
                        await brain_answer_questions(state, run_idx, stage_idx, storage)
                        cmd = f"speccer run --continue --spec-dir {spec_dir}"
                        await tmux.spawn_in_window(window_name, cmd)
                        stage.status = StageStatus.SPEC_RUNNING
                        _append_log(conductor_log, f"SPEC_RESTARTED: {run.name} stage {stage_idx}")
                    # else: still running, wait for next iteration

                else:
                    # Process died — determine outcome by PROGRESS.md status
                    if status == "COMPLETE":
                        stage.status = StageStatus.SPEC_COMPLETE
                        _append_log(conductor_log, f"SPEC_COMPLETE: {run.name} stage {stage_idx}")
                    elif status == "NEEDS_INPUT":
                        # Died after writing NEEDS_INPUT — call brain, restart
                        _append_log(conductor_log, f"SPEC_NEEDS_INPUT: {run.name} stage {stage_idx}")
                        await brain_answer_questions(state, run_idx, stage_idx, storage)
                        cmd = f"speccer run --continue --spec-dir {spec_dir}"
                        await tmux.spawn_in_window(window_name, cmd)
                        stage.status = StageStatus.SPEC_RUNNING
                        _append_log(conductor_log, f"SPEC_RESTARTED: {run.name} stage {stage_idx}")
                    elif status == "FAILED":
                        # Explicit failure status — retry up to max_retries
                        if run.monitor.retry_count < config.max_retries:
                            run.monitor.retry_count += 1
                            _append_log(
                                conductor_log,
                                f"SPEC_RETRY {run.monitor.retry_count}: {run.name} stage {stage_idx}",
                            )
                            cmd = f"speccer run --spec-dir {spec_dir}"
                            await tmux.spawn_in_window(window_name, cmd)
                            stage.status = StageStatus.SPEC_RUNNING
                        else:
                            stage.status = StageStatus.FAILED
                            run.status = RunStatus.BLOCKED
                            _append_log(
                                conductor_log,
                                f"SPEC_FAILED max retries exceeded: {run.name} stage {stage_idx}",
                            )
                            has_active_work = False
                    else:
                        # Died without writing valid status (crash) — immediate failure
                        stage.status = StageStatus.FAILED
                        run.status = RunStatus.BLOCKED
                        _append_log(
                            conductor_log,
                            f"SPEC_FAILED died unexpectedly: {run.name} stage {stage_idx}",
                        )
                        has_active_work = False

        if not has_active_work:
            break

        if config.check_interval_s > 0:
            await asyncio.sleep(config.check_interval_s)

    return state
