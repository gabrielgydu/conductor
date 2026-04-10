"""Conductor orchestration loop — full advance_run state machine."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import conductor.core.tmux as _tmux_module
from conductor.core.brain import brain_answer_questions, brain_diagnose_runner
from conductor.core.claude import resolve_model, run_claude
from conductor.core.enums import RunStatus, StageStatus
from conductor.core.logging import live_log
from conductor.core.models import ConductorState, RunState, StageState, atomic_save
from conductor.core.presets import Preset, PresetConfig, load_preset
from conductor.core.storage import StorageResolver

# Resolve paths to our own speccer/runner wrappers (sibling to the conductor package root)
# so we never accidentally invoke the old bash versions via PATH.
_PACKAGE_ROOT = (
    Path(__file__).resolve().parents[3]
)  # src/conductor/core/orchestrator.py -> repo root
_SPECCER_BIN = str(_PACKAGE_ROOT / "speccer")
_RUNNER_BIN = str(_PACKAGE_ROOT / "runner")

# Tracks consecutive shallow-pstree observations per tmux window name.
# Only declare zombie after ZOMBIE_SHALLOW_THRESHOLD consecutive shallow checks,
# to avoid false positives during the brief gap when the runner restarts Claude
# between phase iterations.
_shallow_tree_counts: dict[str, int] = {}
ZOMBIE_SHALLOW_THRESHOLD = 3


@dataclass
class ConductorConfig:
    check_interval_s: float = 120.0
    max_iterations: int = 1000
    max_retries: int = 2
    max_parallel: int = 1
    project_root: Path | None = None
    overnight: bool = True


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log(
    event: str, message: str, log_path: Path | None, audit_path: Path | None, **kw
) -> None:
    live_log(
        event, message, audit_data=kw or None, log_path=log_path, audit_path=audit_path
    )


async def _push_and_pr_for_branch(
    branch: str,
    base_branch: str,
    project_name: str,
    run: "RunState",
    storage: "StorageResolver",
) -> str | None:
    """Push a branch to origin and create a non-draft PR. Returns PR URL or None."""
    import logging

    logger = logging.getLogger(__name__)
    project_dir = Path(storage.repo_root)

    # Push
    proc = await asyncio.create_subprocess_exec(
        "git", "push", "-u", "origin", branch,
        cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Create PR with retry
    pr_url: str | None = None
    title = f"{run.name}: {run.description[:60]}" if run.description else run.name
    for attempt in range(3):
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create",
            "--title", title,
            "--body", f"Conductor single-run output for **{project_name}**.",
            "--head", branch,
            "--base", base_branch,
            cwd=str(project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        if proc.returncode == 0:
            pr_url = stdout_bytes.decode().strip()
            break
        logger.warning(
            "gh pr create attempt %d failed: %s",
            attempt + 1,
            stderr_bytes.decode().strip(),
        )
        if attempt < 2:
            await asyncio.sleep(5)

    return pr_url


# ---------------------------------------------------------------------------
# Progress-file helpers
# ---------------------------------------------------------------------------


def _read_progress_status(progress_file: Path) -> str | None:
    """Read STATUS: line from PROGRESS.md, return status string or None."""
    if not progress_file.exists():
        return None
    for line in progress_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("STATUS:"):
            return line[len("STATUS:") :].strip().upper()
    return None


def _progress_file_path(
    state: ConductorState, run_idx: int, stage_idx: int
) -> Path | None:
    """Return path to PROGRESS.md for a given run/stage, or None if worktree is not set."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    wt = stage.worktree
    if not wt:
        return None
    fname = run.name + stage.feature_suffix
    return Path(wt) / "docs" / fname / "spec" / "PROGRESS.md"


def _activity_log_path(state: ConductorState, run_idx: int, stage_idx: int) -> Path | None:
    """Return path to activity.log for a given run/stage, or None if worktree is not set."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    wt = stage.worktree
    if not wt:
        return None
    fname = run.name + stage.feature_suffix
    return Path(wt) / "docs" / fname / "activity.log"


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------



def create_worktree(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage_or_project_dir,
    worktrees_base: Path | None = None,
    *,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Create (or recreate) a git worktree for a stage, chaining branches correctly.

    Accepts two calling conventions:
      - create_worktree(state, run_idx, stage_idx, storage, log_path=..., audit_path=...)
        where storage is a StorageResolver (internal orchestrator use)
      - create_worktree(state, run_idx, stage_idx, project_dir, worktrees_base)
        where project_dir and worktrees_base are plain Path objects (test use)
    """
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]

    # Detect calling convention
    if isinstance(storage_or_project_dir, Path):
        # Direct path convention: (state, run_idx, stage_idx, project_dir, worktrees_base)
        project_dir = storage_or_project_dir
        wt_base = worktrees_base or (
            Path(state.worktrees_base) if state.worktrees_base else project_dir.parent / "worktrees"
        )
    else:
        # StorageResolver convention: (state, run_idx, stage_idx, storage)
        storage = storage_or_project_dir
        project_dir = storage.repo_root
        wt_base = (
            Path(state.worktrees_base) if state.worktrees_base else project_dir.parent / "worktrees"
        )

    # Branch naming: conductor/{project}/{run}/{stage} to avoid ref collisions
    # with the base branch (which may share the project_name prefix)
    branch = f"conductor/{state.project_name}/{run.name}/{stage.name}"

    # Worktree path: worktrees_base / {run}-{stage}
    wt_name = f"{run.name}-{stage.name}"
    wt_candidate = wt_base / wt_name

    # Determine base ref
    if stage_idx > 0:
        base_ref = run.stages[stage_idx - 1].branch
    elif run.depends_on:
        run_by_index = {r.index: r for r in state.runs}
        dep_run = run_by_index[run.depends_on[-1]]
        base_ref = dep_run.stages[-1].branch
    else:
        base_ref = state.base_branch

    # Reuse existing worktree if it's valid and on the right branch
    wt_ready = False
    if wt_candidate.exists() and (wt_candidate / ".git").exists():
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(wt_candidate),
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == branch:
            wt_ready = True
        else:
            raise RuntimeError(
                f"Worktree {wt_candidate} exists but on wrong branch "
                f"({result.stdout.strip()}), expected {branch}"
            )
    else:
        # Remove stale leftover dir (not a valid worktree)
        if wt_candidate.exists():
            subprocess.run(["rm", "-rf", str(wt_candidate)], capture_output=True)
            if wt_candidate.exists():
                subprocess.run(["sudo", "rm", "-rf", str(wt_candidate)], capture_output=True)

        branch_exists = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(project_dir),
            capture_output=True,
        ).returncode == 0

        wt_candidate.parent.mkdir(parents=True, exist_ok=True)
        if branch_exists:
            subprocess.run(
                ["git", "worktree", "add", str(wt_candidate), branch],
                cwd=str(project_dir), check=True, capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(wt_candidate), base_ref],
                cwd=str(project_dir), check=True, capture_output=True,
            )
        wt_ready = True

    stage.branch = branch
    stage.worktree = str(wt_candidate)

    _log(
        "WORKTREE_CREATE",
        f"run {run_idx} stage {stage_idx}: {wt_candidate} <- {base_ref}",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        branch=branch,
        worktree=str(wt_candidate),
        base_ref=base_ref,
    )


# ---------------------------------------------------------------------------
# Speccer integration helpers
# ---------------------------------------------------------------------------


async def run_speccer_init(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Spawn speccer init in tmux window and wait for completion.

    Idempotent: if the spec dir was already initialized (PROGRESS.md present),
    skip the speccer invocation so resume flows can safely re-enter the
    PENDING branch.
    """
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    mode = stage.spec_mode
    window_name = f"run{run_idx}:{stage.name}"

    progress_file = Path(wt) / "docs" / fname / "spec" / "PROGRESS.md"
    if progress_file.exists():
        _log(
            "SPECCER_INVOKE",
            f"speccer init {fname} (mode={mode}) skipped — already initialized",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            command="speccer init",
            mode=mode,
            skipped=True,
        )
        return

    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.unlink(missing_ok=True)

    args = [
        _SPECCER_BIN,
        "init",
        "--feature",
        fname,
        "--project-dir",
        wt,
        "--mode",
        mode,
        "--constitution",
    ]

    # Optional preset
    if state.preset:
        args += ["--preset", state.preset]

    # Context wiring
    wiring = stage.context_wiring
    if wiring is not None:
        import json as _json  # noqa: PLC0415

        try:
            wiring_data = _json.loads(wiring.sources[0])
        except (_json.JSONDecodeError, IndexError):
            wiring_data = {}

        source_run_i = wiring_data.get("source_run")
        source_stage_i = wiring_data.get("source_stage")
        source_path = wiring_data.get("source_path")
        wiring_type = wiring_data.get("type")

        if wiring_type == "external" and source_path:
            if not Path(source_path).exists():
                _log(
                    "SPECCER_INVOKE",
                    f"Warning: external context path not found: {source_path}",
                    log_path,
                    audit_path,
                )
            args += ["--spec-context", source_path]
        elif source_run_i is not None and source_stage_i is not None and source_path:
            run_by_index = {r.index: r for r in state.runs}
            if source_run_i in run_by_index:
                src_run = run_by_index[source_run_i]
                src_stage = src_run.stages[source_stage_i]
                src_wt = src_stage.worktree
                src_fname = src_run.name + src_stage.feature_suffix
                full_ctx = str(Path(src_wt) / "docs" / src_fname / source_path)
                if not Path(full_ctx).exists():
                    _log(
                        "SPECCER_INVOKE",
                        f"Warning: context wiring source not found: {full_ctx}",
                        log_path,
                        audit_path,
                    )
                if wiring_type == "backend-context":
                    args += ["--backend-context", full_ctx]
                else:
                    args += ["--spec-context", full_ctx]

    cmd = f"cd {wt} && {' '.join(args)}"
    _log(
        "SPECCER_INVOKE",
        f"speccer init {fname} (mode={mode}) [window: {window_name}]",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        command="speccer init",
        mode=mode,
    )
    log_file = storage.tmux_log(state.project_name, f"speccer-init_{fname}")
    exit_code = await tmux.spawn_in_window_and_wait(
        window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file,
    )

    _log(
        "SPECCER_INVOKE",
        f"speccer init {fname} (mode={mode}) done",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        command="speccer init",
        mode=mode,
    )

    if exit_code != 0:
        _log(
            "FAILURE",
            f"speccer init exited {exit_code} for {fname}",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            exit_code=exit_code,
        )
        stage.status = StageStatus.FAILED


async def run_speccer_run(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Spawn speccer run in tmux window and wait for completion."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    window_name = f"run{run_idx}:{stage.name}"

    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.unlink(missing_ok=True)

    cmd = f"cd {wt} && {_SPECCER_BIN} run --feature {fname} --project-dir {wt}"
    _log(
        "SPECCER_INVOKE",
        f"speccer run {fname} [window: {window_name}]",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
    )
    log_file = storage.tmux_log(state.project_name, f"speccer-run_{fname}")
    await tmux.spawn_in_window_and_wait(window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file)

    _log(
        "SPECCER_INVOKE",
        f"speccer run {fname} done",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        command="speccer run",
    )


async def run_speccer_continue(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Spawn speccer run --continue in tmux window and wait for completion."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    window_name = f"run{run_idx}:{stage.name}"

    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.unlink(missing_ok=True)

    cmd = (
        f"cd {wt} && {_SPECCER_BIN} run --feature {fname} --project-dir {wt} --continue"
    )
    _log(
        "SPECCER_INVOKE",
        f"speccer run --continue {fname} [window: {window_name}]",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
    )
    log_file = storage.tmux_log(state.project_name, f"speccer-continue_{fname}")
    await tmux.spawn_in_window_and_wait(window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file)

    _log(
        "SPECCER_INVOKE",
        f"speccer run --continue {fname} done",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        command="speccer run --continue",
    )


async def run_speccer_generate(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Spawn speccer generate in tmux window and wait for completion."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    window_name = f"run{run_idx}:{stage.name}-gen"

    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}-gen")
    exit_file.unlink(missing_ok=True)

    cmd = f"cd {wt} && {_SPECCER_BIN} generate --feature {fname} --project-dir {wt}"
    _log(
        "SPECCER_INVOKE",
        f"speccer generate {fname} [window: {window_name}]",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
    )
    log_file = storage.tmux_log(state.project_name, f"speccer-generate_{fname}")
    rc = await tmux.spawn_in_window_and_wait(window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file)

    _log(
        "SPECCER_INVOKE",
        f"speccer generate {fname} done",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        command="speccer generate",
    )

    if rc != 0:
        _log(
            "SPECCER_GENERATE_FAILED",
            f"speccer generate {fname} exited with code {rc}",
            log_path, audit_path, run=run_idx, stage=stage_idx,
        )
        stage.status = StageStatus.FAILED
        return

    # Verify run.sh was actually created and contains PHASES
    docs_dir = Path(wt) / "docs" / fname
    run_sh = docs_dir / "run.sh"
    if not run_sh.exists():
        _log(
            "SPECCER_GENERATE_FAILED",
            f"speccer generate {fname} completed but run.sh not found",
            log_path, audit_path, run=run_idx, stage=stage_idx,
        )
        stage.status = StageStatus.FAILED
        return

    run_sh_text = run_sh.read_text(encoding="utf-8")
    if "PHASES[" not in run_sh_text:
        _log(
            "SPECCER_GENERATE_FAILED",
            f"speccer generate {fname} produced run.sh without PHASES arrays — "
            f"regeneration needed",
            log_path, audit_path, run=run_idx, stage=stage_idx,
        )
        stage.status = StageStatus.FAILED
        return


def sync_speccer_status(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Read PROGRESS.md STATUS and map to StageStatus on the stage object."""
    stage = state.runs[run_idx].stages[stage_idx]
    progress_file = _progress_file_path(state, run_idx, stage_idx)

    if progress_file is None or not progress_file.exists():
        stage.status = StageStatus.FAILED
        return

    speccer_status = _read_progress_status(progress_file)

    status_map = {
        "NEEDS_INPUT": StageStatus.SPEC_NEEDS_INPUT,
        "COMPLETE": StageStatus.SPEC_COMPLETE,
        "GENERATED": StageStatus.GENERATED,
        "INIT": StageStatus.SPEC_RUNNING,
        "EXPLORING": StageStatus.SPEC_RUNNING,
        "SPECCING": StageStatus.SPEC_RUNNING,
    }

    if speccer_status in status_map:
        stage.status = status_map[speccer_status]
    else:
        _log(
            "SPECCER_STATUS_SYNC",
            f"Unknown speccer status: {speccer_status}",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            speccer_status=speccer_status,
        )
        stage.status = StageStatus.FAILED

    _log(
        "SPECCER_STATUS_SYNC",
        f"run {run_idx} stage {stage_idx}: {speccer_status} -> {stage.status}",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        speccer_status=speccer_status,
    )


def pre_reset_speccer_status(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
) -> None:
    """Reset EXPLORING->INIT and SPECCING->NEEDS_INPUT in PROGRESS.md before re-run."""
    progress_file = _progress_file_path(state, run_idx, stage_idx)
    if progress_file is None or not progress_file.exists():
        return

    content = progress_file.read_text(encoding="utf-8")
    speccer_status = _read_progress_status(progress_file)

    if speccer_status == "EXPLORING":
        content = re.sub(r"^STATUS:.*", "STATUS: INIT", content, flags=re.MULTILINE)
        progress_file.write_text(content, encoding="utf-8")
    elif speccer_status == "SPECCING":
        content = re.sub(
            r"^STATUS:.*", "STATUS: NEEDS_INPUT", content, flags=re.MULTILINE
        )
        progress_file.write_text(content, encoding="utf-8")


def speccer_exit_code_handler(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    fname: str,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Check speccer exit file and PROGRESS.md to set the correct stage status."""
    stage = state.runs[run_idx].stages[stage_idx]
    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")

    exit_code: int | None = None
    if exit_file.exists():
        try:
            exit_code = int(exit_file.read_text().strip())
        except (ValueError, OSError):
            exit_code = None

    progress_file = _progress_file_path(state, run_idx, stage_idx)
    _recoverable = {"NEEDS_INPUT", "COMPLETE", "GENERATED"}

    if exit_code == 0:
        sync_speccer_status(state, run_idx, stage_idx, storage, log_path, audit_path)
    elif exit_code is not None:
        # Non-zero exit — check PROGRESS.md for recoverable state
        if progress_file is not None and progress_file.exists():
            pstatus = _read_progress_status(progress_file)
            if pstatus in _recoverable:
                sync_speccer_status(
                    state, run_idx, stage_idx, storage, log_path, audit_path
                )
                return
        _log(
            "FAILURE",
            f"Speccer exited {exit_code} for {fname}",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            exit_code=exit_code,
        )
        stage.status = StageStatus.FAILED
    else:
        # No exit file — crash before write
        if progress_file is not None and progress_file.exists():
            pstatus = _read_progress_status(progress_file)
            if pstatus in _recoverable:
                sync_speccer_status(
                    state, run_idx, stage_idx, storage, log_path, audit_path
                )
                return
        stage.status = StageStatus.FAILED


def write_feature_description(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    conductor_dir: Path | None = None,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Copy the feature description file to the worktree spec dir."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    desc_file_rel = stage.feature_description_file

    if not desc_file_rel:
        return

    cdir = conductor_dir or storage.conductor_dir(state.project_name)
    source = cdir / desc_file_rel

    if not source.exists():
        _log(
            "FILE_WRITE",
            f"Warning: description file missing: {desc_file_rel}",
            log_path,
            audit_path,
        )
        return

    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    dest = Path(wt) / "docs" / fname / "spec" / "FEATURE-DESCRIPTION.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(dest))

    _log(
        "FILE_WRITE",
        f"Feature description for {fname} ({dest.stat().st_size} bytes)",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        file="FEATURE-DESCRIPTION.md",
    )


def write_constitution(
    state: ConductorState,
    run_idx: int,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Write CONSTITUTION.md to the current stage's worktree spec dir."""
    run = state.runs[run_idx]
    constitution = run.constitution
    if not constitution:
        return

    stage_idx = run.current_stage
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    const_file = Path(wt) / "docs" / fname / "spec" / "CONSTITUTION.md"
    const_file.parent.mkdir(parents=True, exist_ok=True)

    principles = "\n".join(f"- {item}" for item in constitution)
    content = (
        "# Project Constitution\n\n"
        "Immutable principles that ALL specs and implementations must respect.\n\n"
        "## Immutable Principles\n\n"
        f"{principles}\n"
    )
    const_file.write_text(content, encoding="utf-8")

    _log(
        "FILE_WRITE",
        f"Constitution for {fname}",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        file="CONSTITUTION.md",
    )


async def answer_questions(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    conductor_dir: Path | None = None,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Build context for speccer questions and call brain to answer them."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree

    questions_file = Path(wt) / "docs" / fname / "spec" / "QUESTIONS.md"
    if not questions_file.exists():
        return
    questions = questions_file.read_text(encoding="utf-8").strip()
    if not questions:
        return

    # Build context
    cdir = conductor_dir or storage.conductor_dir(state.project_name)
    brief_file = storage.conductor_brief(state.project_name)
    brief = brief_file.read_text(encoding="utf-8") if brief_file.exists() else ""

    description = ""
    if stage.feature_description_file:
        desc_path = cdir / stage.feature_description_file
        if desc_path.exists():
            description = desc_path.read_text(encoding="utf-8")

    # Gather existing domain specs
    specs_parts: list[str] = []
    domains_dir = Path(wt) / "docs" / fname / "spec" / "domains"
    if domains_dir.exists():
        for spec_file in sorted(domains_dir.glob("*.md")):
            specs_parts.append(spec_file.read_text(encoding="utf-8"))
    specs = "\n\n".join(specs_parts)

    system_prompt = (
        "You are answering clarification questions on behalf of a product owner. "
        "A spec-generation tool has produced questions that need answers before it can continue. "
        "Use the provided feature brief, constitution, description, and existing specs as context. "
        "Answer each question directly and concisely. Do NOT start implementing — only answer the questions."
    )

    context = (
        f"{system_prompt}\n\n"
        f"## Questions to Answer\n{questions}\n\n"
        f"## Feature Brief\n{brief}\n\n"
        f"## Constitution\n{chr(10).join('- ' + c for c in run.constitution)}\n\n"
        f"## Feature Description (this stage)\n{description}\n\n"
        f"## Existing Domain Specs\n{specs}"
    )

    _log(
        "BRAIN_CALL",
        f"answer-questions for {fname}",
        log_path,
        audit_path,
        type="answer-questions",
        run=run_idx,
        stage=stage_idx,
    )

    result = await run_claude(
        context,
        model=resolve_model("opus"),
        max_turns=10,
    )

    # Extract text from stream-json output
    import json as _json

    answer_text = ""
    for line in result.output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        answer_text = block.get("text", "")
                        break
            elif isinstance(content, str):
                answer_text = content
            if answer_text:
                break

    if answer_text:
        # Speccer expects answers as lines prefixed with "> " in QUESTIONS.md
        prefixed = "\n".join(f"> {line}" for line in answer_text.splitlines())
        combined = questions + "\n\n" + prefixed + "\n"
        questions_file.write_text(combined, encoding="utf-8")

    # Log brain call
    brain_calls_dir = storage.brain_calls_dir(state.project_name)
    import time as _time

    ts = int(_time.time() * 1000)
    log_file = brain_calls_dir / f"answer-questions-{run_idx}-{stage_idx}-{ts}.json"
    log_file.write_text(
        _json.dumps(
            {
                "action": "answer-questions",
                "run_idx": run_idx,
                "stage_idx": stage_idx,
                "questions": questions,
                "response": answer_text,
                "tokens": result.tokens_used,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


def generate_run_config(
    run_sh_path: Path,
    feature_name: str,
    project_dir: Path,
    preset_config: PresetConfig,
    *,
    quick: bool = False,
    is_final_stage: bool = False,
) -> Path:
    """Parse a generated run.sh and write RUN-CONFIG.json for the Python runner.

    Returns path to the written RUN-CONFIG.json.
    """
    from runner.config import PhaseConfig, RunConfig  # noqa: PLC0415

    text = run_sh_path.read_text(encoding="utf-8")

    # Extract phases, tokens, names from bash associative arrays
    phases_re = re.compile(r'PHASES\[(\d+)\]\s*=\s*"([^"]+)"')
    tokens_re = re.compile(r'PHASE_TOKENS\[(\d+)\]\s*=\s*"([^"]+)"')
    names_re = re.compile(r'PHASE_NAMES\[(\d+)\]\s*=\s*"([^"]+)"')

    phase_files = dict(phases_re.findall(text))
    phase_tokens = dict(tokens_re.findall(text))
    phase_names = dict(names_re.findall(text))

    if not phase_files:
        raise ValueError(f"No PHASES found in {run_sh_path}")

    prompt_dir = run_sh_path.parent / "prompts"
    phase_configs = []
    for num_str in sorted(phase_files.keys(), key=int):
        num = int(num_str)
        prompt_file = str(prompt_dir / phase_files[num_str])
        phase_configs.append(PhaseConfig(
            number=num,
            name=phase_names.get(num_str, f"Phase {num}"),
            prompt_file=prompt_file,
            token=phase_tokens.get(num_str, f"PHASE_{num}_COMPLETE"),
        ))

    # Extract model if present
    model_match = re.search(r'CONDUCTOR_MODEL\s*=\s*"([^"]+)"', text)
    model = model_match.group(1) if model_match else ""
    fix_model_match = re.search(r'CONDUCTOR_FIX_MODEL\s*=\s*"([^"]+)"', text)
    fix_model = fix_model_match.group(1) if fix_model_match else model

    cfg = RunConfig(
        feature_name=feature_name,
        project_dir=str(project_dir),
        phases=phase_configs,
        model=model,
        preset=None,  # preset quality gate loaded by name in phase_loop
        fixer_enabled=preset_config.fixer_enabled and not quick,
        max_iterations=preset_config.max_iterations_per_phase,
        max_gate_retries=preset_config.max_gate_retries,
        steerable=True,
        quick=quick,
        local_ci_enabled=False,
        local_ci_command=preset_config.local_ci_command,
        local_ci_full_command=preset_config.local_ci_full_command,
        local_ci_max_retries=preset_config.local_ci_max_retries,
        local_review_enabled=preset_config.local_review_enabled and is_final_stage,
        local_review_command=preset_config.local_review_command,
        local_review_full_command=preset_config.local_review_full_command,
        local_review_max_retries=preset_config.local_review_max_retries,
        fix_model=fix_model,
    )

    config_path = run_sh_path.parent / "RUN-CONFIG.json"
    cfg.save(config_path)
    return config_path


async def start_runner(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Spawn Python runner in tmux window (non-blocking)."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    window_name = f"run{run_idx}:{stage.name}-exec"

    exit_file = Path(f"/tmp/conductor-exit-{fname}")
    exit_file.unlink(missing_ok=True)
    Path(f"/tmp/conductor-fail-{fname}.log").unlink(missing_ok=True)

    # Generate RUN-CONFIG.json from the speccer-generated run.sh
    docs_dir = Path(wt) / "docs" / fname
    run_sh = docs_dir / "run.sh"
    preset = load_preset(state.preset)
    quick = getattr(state, "quick", False)

    is_final_stage = stage_idx == len(run.stages) - 1

    try:
        config_path = generate_run_config(
            run_sh, fname, Path(wt), preset.config, quick=quick,
            is_final_stage=is_final_stage,
        )
        # Also set the preset name so phase_loop can load it for quality gates
        import json  # noqa: PLC0415
        data = json.loads(config_path.read_text("utf-8"))
        data["preset"] = state.preset
        config_path.write_text(json.dumps(data, indent=2), "utf-8")
    except Exception as exc:
        _log(
            "RUNNER_CONFIG_ERROR",
            f"Failed to generate RUN-CONFIG.json for {fname}: {exc}",
            log_path, audit_path, run=run_idx, stage=stage_idx,
        )
        stage.status = StageStatus.FAILED
        stage.last_exit_code = 1
        return

    storage_dir = str(docs_dir)
    cmd = f"cd {wt} && {_RUNNER_BIN} run --feature {fname} --storage-dir {storage_dir}"
    log_file = storage.tmux_log(state.project_name, f"runner_{fname}")
    await tmux.spawn_runner_in_window(window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file)

    stage.started_at = datetime.now(timezone.utc)

    _log(
        "RUNNER_START",
        f"Runner for {fname} [window: {window_name}]",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        worktree=wt,
        tmux_window=window_name,
    )


def compute_progress_hash(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
) -> str | None:
    """Hash last 20 lines of the activity log for the executing stage."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix

    if stage.status in (StageStatus.SPEC_RUNNING, StageStatus.SPEC_INIT):
        progress_file = _progress_file_path(state, run_idx, stage_idx)
        if progress_file is None or not progress_file.exists():
            return None
        return hashlib.md5(progress_file.read_bytes()).hexdigest()

    if stage.status == StageStatus.EXECUTING:
        activity_log = _activity_log_path(state, run_idx, stage_idx)
        if activity_log is None or not activity_log.exists():
            return None
        lines = activity_log.read_text(encoding="utf-8", errors="replace").splitlines()
        last20 = "\n".join(lines[-20:])
        return hashlib.md5(last20.encode()).hexdigest()

    return None


async def check_stall(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    config: ConductorConfig,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """5-minute grace period, hash last 20 lines, stall_count, diagnose-stall brain call."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix

    # Grace period — skip stall checks for first 5 minutes
    if stage.started_at is not None:
        now = datetime.now(timezone.utc)
        elapsed = (now - stage.started_at).total_seconds()
        if elapsed < 300:
            return

    current_hash = compute_progress_hash(state, run_idx, stage_idx, storage)
    if current_hash is None:
        return

    prev_hash = run.monitor.last_progress_hash
    stall_count = run.monitor.stall_count

    if current_hash == prev_hash:
        stall_count += 1
    else:
        stall_count = 0

    # Heartbeat: if Claude process is alive, cap stall_count at 1 (don't escalate)
    window_name = f"run{run_idx}:{stage.name}-exec"
    if stall_count > 1 and await tmux.has_active_children(window_name):
        run.monitor.last_heartbeat_ts = datetime.now(timezone.utc)
        stall_count = 1

    run.monitor.last_progress_hash = current_hash
    run.monitor.stall_count = stall_count
    run.monitor.last_check_ts = datetime.now(timezone.utc)

    _log(
        "STALL_CHECK",
        f"run {run_idx}: hash={current_hash[:8]} prev={str(prev_hash)[:8]} stall={stall_count}",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        progress_hash=current_hash,
        stall_count=stall_count,
        threshold=2,
    )

    if stall_count >= 5:
        stage.status = StageStatus.FAILED
        _log(
            "STALL_CHECK",
            f"Run {run_idx} auto-failed after {stall_count} stall cycles",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            stall_count=stall_count,
        )
        return

    if stall_count >= 2:
        # Build context for brain call
        context_parts: list[str] = []

        if stage.status == StageStatus.EXECUTING:
            activity_log = _activity_log_path(state, run_idx, stage_idx)
            if activity_log is not None and activity_log.exists():
                lines = activity_log.read_text(encoding="utf-8", errors="replace").splitlines()
                context_parts.append(
                    "## Activity Log (last 100 lines)\n" + "\n".join(lines[-100:])
                )
        else:
            progress_file = _progress_file_path(state, run_idx, stage_idx)
            if progress_file is not None and progress_file.exists():
                context_parts.append(
                    "## PROGRESS.md\n" + progress_file.read_text(encoding="utf-8")
                )

        import json as _json

        context_parts.append(
            "## Stage Config\n" + _json.dumps(stage.model_dump(), indent=2, default=str)
        )
        stall_seconds = stall_count * int(config.check_interval_s)
        context_parts.append(
            f"## Stall Duration\nStalled for {stall_seconds}s ({stall_count} consecutive checks)"
        )
        no_tools = (
            "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
            "Tool calls will be REJECTED and will waste your only turn.\n\n"
        )
        context = no_tools + "\n\n".join(context_parts)

        _log(
            "BRAIN_CALL",
            f"diagnose-stall for {fname}",
            log_path,
            audit_path,
            type="diagnose-stall",
            run=run_idx,
            stage=stage_idx,
        )

        result = await run_claude(context, model=resolve_model("sonnet"), max_turns=1)

        # Parse ACTION: STEER/RESET/IGNORE from response
        response_text = ""
        for line in result.output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                content = event.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            response_text = block.get("text", "")
                            break
                elif isinstance(content, str):
                    response_text = content
                if response_text:
                    break

        action_match = re.search(r"ACTION:\s*(\w+)", response_text)
        action = action_match.group(1).upper() if action_match else "IGNORE"

        if action == "STEER":
            # Extract message lines after ACTION: STEER
            lines_after = (
                response_text.split("ACTION: STEER", 1)[-1].strip().splitlines()[:5]
            )
            message = "\n".join(lines_after)
            if message and stage.status == StageStatus.EXECUTING:
                # Write steering message to runner's steer_inbox (file-based IPC)
                try:
                    worktree = stage.worktree
                    if worktree:
                        inbox = Path(worktree) / "docs" / fname / "steer_inbox"
                        inbox.mkdir(parents=True, exist_ok=True)
                        msg_file = inbox / f"{time.time():.6f}.msg"
                        tmp_file = msg_file.with_suffix(".msg.tmp")
                        tmp_file.write_text(message, encoding="utf-8")
                        tmp_file.rename(msg_file)  # atomic on same filesystem
                    _log(
                        "RUNNER_STEER",
                        f"Steered {fname}",
                        log_path,
                        audit_path,
                        run=run_idx,
                        stage=stage_idx,
                    )
                except Exception:
                    pass
        elif action == "RESET":
            stage.status = StageStatus.FAILED


def _any_runner_exit_file_ready(state: ConductorState, session_name: str | None = None) -> bool:
    """Check if any executing stage has a runner exit file or dead window."""
    for ri, run in enumerate(state.runs):
        if run.status != RunStatus.ACTIVE:
            continue
        si = run.current_stage
        if si >= len(run.stages):
            continue
        stage = run.stages[si]
        if stage.status != StageStatus.EXECUTING:
            continue
        fname = run.name + stage.feature_suffix
        if Path(f"/tmp/conductor-exit-{fname}").exists():
            return True
        # Also detect dead windows (runner crashed without writing exit file)
        if session_name:
            window_name = f"run{ri}:{stage.name}-exec"
            r = subprocess.run(
                ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and window_name not in r.stdout.strip().splitlines():
                # Window is gone — write synthetic exit file
                Path(f"/tmp/conductor-exit-{fname}").write_text("1")
                return True
            # Check for zombie runner (window alive but Claude process dead)
            if r.returncode == 0 and window_name in r.stdout.strip().splitlines():
                pid_r = subprocess.run(
                    ["tmux", "list-panes", "-t", f"{session_name}:{window_name}",
                     "-F", "#{pane_pid}"],
                    capture_output=True, text=True,
                )
                if pid_r.returncode == 0 and pid_r.stdout.strip():
                    try:
                        pane_pid = int(pid_r.stdout.strip())
                        window_key = f"{session_name}:{window_name}"
                        if not _tmux_module.check_pstree_depth(pane_pid):
                            _shallow_tree_counts[window_key] = (
                                _shallow_tree_counts.get(window_key, 0) + 1
                            )
                            if _shallow_tree_counts[window_key] >= ZOMBIE_SHALLOW_THRESHOLD:
                                _shallow_tree_counts.pop(window_key, None)
                                Path(f"/tmp/conductor-exit-{fname}").write_text("143")
                                return True
                        else:
                            _shallow_tree_counts.pop(window_key, None)
                    except (ValueError, OSError):
                        pass
    return False


async def monitor_runner(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    config: ConductorConfig,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Check exit file, idle check, stall check for the executing runner."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    exit_file = Path(f"/tmp/conductor-exit-{fname}")
    window_name = f"run{run_idx}:{stage.name}-exec"

    if exit_file.exists():
        try:
            exit_code = int(exit_file.read_text().strip())
        except (ValueError, OSError):
            exit_code = 1

        if exit_code == 0:
            _log(
                "RUNNER_EXIT",
                f"Runner {fname} exit 0",
                log_path,
                audit_path,
                run=run_idx,
                stage=stage_idx,
                exit_code=0,
            )
            # Runner already ran local CI before exiting 0 — skip redundant validation
            stage.status = StageStatus.DONE
            stage.completed_at = datetime.now(timezone.utc)
        else:
            # Read captured pane output if available
            fail_log = Path(f"/tmp/conductor-fail-{fname}.log")
            pane_output = ""
            if fail_log.exists():
                try:
                    pane_output = fail_log.read_text(errors="replace").strip()
                except OSError:
                    pass
            stage.last_exit_code = exit_code
            stage.status = StageStatus.FAILED
            _log(
                "RUNNER_EXIT",
                f"Runner {fname} exit {exit_code}"
                + (f"\n--- last output ---\n{pane_output}" if pane_output else ""),
                log_path,
                audit_path,
                run=run_idx,
                stage=stage_idx,
                exit_code=exit_code,
            )
    else:
        # Still running — check if window went idle (died without exit file)
        # Grace period: don't check idle until runner has been running for at least 30s
        elapsed = (
            (datetime.now(timezone.utc) - stage.started_at).total_seconds()
            if stage.started_at
            else 0
        )
        if elapsed > 30:
            is_idle = await tmux.is_runner_idle(window_name)
            if is_idle:
                stage.status = StageStatus.FAILED
                _log(
                    "RUNNER_DEAD",
                    f"Runner {fname} died without exit file (window idle)",
                    log_path,
                    audit_path,
                    run=run_idx,
                    stage=stage_idx,
                    window=window_name,
                )
            elif elapsed > 60 and not await tmux.has_active_children(window_name):
                session_name = tmux._session_name
                window_key = f"{session_name}:{window_name}"
                _shallow_tree_counts[window_key] = (
                    _shallow_tree_counts.get(window_key, 0) + 1
                )
                if _shallow_tree_counts[window_key] >= ZOMBIE_SHALLOW_THRESHOLD:
                    _shallow_tree_counts.pop(window_key, None)
                    stage.last_exit_code = 143
                    stage.status = StageStatus.FAILED
                    _log(
                        "RUNNER_ZOMBIE",
                        f"Runner {fname} alive but Claude process dead (no children, "
                        f"{ZOMBIE_SHALLOW_THRESHOLD} consecutive checks)",
                        log_path,
                        audit_path,
                        run=run_idx,
                        stage=stage_idx,
                        window=window_name,
                    )
                else:
                    _log(
                        "RUNNER_SHALLOW_TREE",
                        f"Runner {fname} shallow pstree (check "
                        f"{_shallow_tree_counts[window_key]}/{ZOMBIE_SHALLOW_THRESHOLD}), "
                        "waiting before declaring zombie",
                        log_path,
                        audit_path,
                        run=run_idx,
                        stage=stage_idx,
                        window=window_name,
                    )
                    await check_stall(
                        state, run_idx, stage_idx, tmux, storage, config, log_path, audit_path
                    )
            else:
                # Tree is healthy — reset any accumulated shallow count
                window_key = f"{tmux._session_name}:{window_name}"
                _shallow_tree_counts.pop(window_key, None)
                await check_stall(
                    state, run_idx, stage_idx, tmux, storage, config, log_path, audit_path
                )


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


_TRANSIENT_PATTERNS = re.compile(
    r"api_error|overloaded_error|internal server error|server_error|rate_limit"
    r"|529|500\s*error|capacity|connection error|timeout|econnreset"
    r"|APIStatusError|APIConnectionError|APITimeoutError",
    re.IGNORECASE,
)


def _is_transient_api_error(fname: str) -> bool:
    """Check fail logs for transient API error patterns."""
    paths_to_check = [
        Path(f"/tmp/conductor-fail-{fname}.log"),
    ]
    for p in paths_to_check:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _TRANSIENT_PATTERNS.search(text):
            return True
    return False


def classify_failure(fname: str, exit_code: int | None, stage: StageState) -> str:
    """Classify failure as 'infra', 'transient', or 'logic'.

    - Exit 137/143 (signal kills) → infra
    - Exit 1 + transient API error patterns in fail log → transient
    - Exit 1 + activity log modified within 60s → infra (mid-work crash)
    - Everything else → logic
    """
    if exit_code in (137, 143):
        return "infra"
    if exit_code == 1:
        if _is_transient_api_error(fname):
            return "transient"
        if stage.worktree:
            activity_log = Path(stage.worktree) / "docs" / fname / "activity.log"
            try:
                mtime = activity_log.stat().st_mtime
                if time.time() - mtime < 60:
                    return "infra"
            except OSError:
                pass
    return "logic"


async def handle_failure(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    config: ConductorConfig,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Classify failure, auto-retry infra failures, brain call for logic failures."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix

    failure_type = classify_failure(fname, stage.last_exit_code, stage)

    # Infra failures: auto-retry without brain call, separate budget (cap 5)
    if failure_type == "infra":
        if stage.infra_retries >= 5:
            stage.status = StageStatus.BLOCKED
            _log(
                "BLOCKED",
                f"Run {run.name} stage {stage.name}: max infra retries ({stage.infra_retries})",
                log_path,
                audit_path,
                run=run_idx,
                stage=stage_idx,
                reason="max_infra_retries",
                exit_code=stage.last_exit_code,
            )
            return
        stage.infra_retries += 1
        _log(
            "INFRA_RETRY",
            f"Run {run.name} stage {stage.name}: infra failure (exit {stage.last_exit_code}), "
            f"auto-retry {stage.infra_retries}/5",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            infra_retries=stage.infra_retries,
            exit_code=stage.last_exit_code,
        )
        await restart_stage(
            state, run_idx, stage_idx, storage, log_path=log_path, audit_path=audit_path
        )
        return

    # Transient API failures: exponential backoff with 8-hour window
    if failure_type == "transient":
        now = datetime.now(timezone.utc)
        if stage.first_transient_failure_ts is None:
            stage.first_transient_failure_ts = now
        elapsed_hours = (now - stage.first_transient_failure_ts).total_seconds() / 3600
        if elapsed_hours > 8:
            stage.status = StageStatus.BLOCKED
            _log(
                "BLOCKED",
                f"Run {run.name} stage {stage.name}: API unavailable for 8+ hours",
                log_path,
                audit_path,
                run=run_idx,
                stage=stage_idx,
                reason="transient_timeout",
                exit_code=stage.last_exit_code,
            )
            return
        # Exponential backoff: 30s, 60s, 2m, 4m, 8m, 16m, 32m, 60m cap
        backoff_s = min(3600, 30 * (2 ** stage.transient_retries))
        stage.transient_retries += 1
        stage.backoff_until = now + timedelta(seconds=backoff_s)
        _log(
            "TRANSIENT_FAILURE",
            f"Run {run.name} stage {stage.name}: API error, backing off {backoff_s}s "
            f"(attempt {stage.transient_retries})",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            backoff_s=backoff_s,
            transient_retries=stage.transient_retries,
            exit_code=stage.last_exit_code,
        )
        await restart_stage(
            state, run_idx, stage_idx, storage, log_path=log_path, audit_path=audit_path
        )
        return

    # Logic failures: existing brain call flow, cap at 2
    retries = stage.retries
    if retries >= 2:
        stage.status = StageStatus.BLOCKED
        _log(
            "BLOCKED",
            f"Run {run.name} stage {stage.name}: max retries ({retries})",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            reason="max_retries",
        )
        return

    # Build failure context for brain call
    context_parts: list[str] = []

    import json as _json

    activity_log = _activity_log_path(state, run_idx, stage_idx)
    if activity_log is not None and activity_log.exists():
        lines = activity_log.read_text(encoding="utf-8", errors="replace").splitlines()
        context_parts.append(
            "## Activity Log (last 100 lines)\n" + "\n".join(lines[-100:])
        )

    context_parts.append(
        "## Stage Config\n" + _json.dumps(stage.model_dump(), indent=2, default=str)
    )
    context_parts.append(f"## Retries\n{retries} of 2")
    no_tools = (
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
        "Tool calls will be REJECTED and will waste your only turn.\n\n"
    )
    context = no_tools + "\n\n".join(context_parts)

    _log(
        "BRAIN_CALL",
        f"diagnose-failure for {fname}",
        log_path,
        audit_path,
        type="diagnose-failure",
        run=run_idx,
        stage=stage_idx,
    )

    result = await run_claude(context, model=resolve_model("sonnet"), max_turns=1)

    response_text = ""
    for line in result.output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text = block.get("text", "")
                        break
            elif isinstance(content, str):
                response_text = content
            if response_text:
                break

    action_match = re.search(r"ACTION:\s*(\w+)", response_text)
    action = action_match.group(1).upper() if action_match else "BLOCK"

    _log(
        "FAILURE",
        f"Run {run.name} stage {stage.name}: {action}",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        retries=retries,
        max_retries=2,
        action=action,
    )

    if action == "RETRY":
        stage.retries += 1
        run.monitor.retry_count += 1
        await restart_stage(
            state, run_idx, stage_idx, storage, log_path=log_path, audit_path=audit_path
        )
    else:
        stage.status = StageStatus.BLOCKED


async def restart_stage(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Reset stage status to re-enter the pipeline at the right point."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    current_status = stage.status
    stage.last_exit_code = None

    if current_status in (StageStatus.FAILED, StageStatus.BLOCKED):
        # Determine where to restart based on how far the stage got on disk.
        # If run.sh exists AND is valid, the spec phase completed — restart from runner.
        # Otherwise, consult PROGRESS.md to re-enter the pipeline at the correct phase.
        wt = stage.worktree
        run_sh = Path(wt) / "docs" / fname / "run.sh" if wt else None
        run_sh_valid = (
            run_sh is not None
            and run_sh.exists()
            and "PHASES[" in run_sh.read_text(encoding="utf-8")
        )
        if run_sh_valid:
            exit_file = Path(f"/tmp/conductor-exit-{fname}")
            exit_file.unlink(missing_ok=True)
            Path(f"/tmp/conductor-fail-{fname}.log").unlink(missing_ok=True)
            stage.status = StageStatus.GENERATED
        else:
            # run.sh missing or malformed — delete it and consult speccer progress
            # so we resume at the right spec phase instead of blindly re-running
            # `speccer generate` (which errors if the spec is still at INIT).
            if run_sh and run_sh.exists():
                run_sh.unlink()
            Path(f"/tmp/conductor-speccer-exit-{fname}-gen").unlink(missing_ok=True)
            Path(f"/tmp/conductor-speccer-exit-{fname}").unlink(missing_ok=True)

            progress_file = _progress_file_path(state, run_idx, stage_idx)
            speccer_status = (
                _read_progress_status(progress_file)
                if progress_file is not None and progress_file.exists()
                else None
            )

            if speccer_status in ("COMPLETE", "GENERATED"):
                stage.status = StageStatus.SPEC_COMPLETE
            elif speccer_status == "NEEDS_INPUT":
                stage.status = StageStatus.SPEC_NEEDS_INPUT
            elif speccer_status in ("INIT", "EXPLORING", "SPECCING"):
                pre_reset_speccer_status(state, run_idx, stage_idx, storage)
                # Re-enter the pipeline at PENDING so the natural startup
                # sequence (create_worktree → speccer init → write feature
                # description → write constitution) runs. run_speccer_init
                # is idempotent against an already-initialized spec dir.
                stage.status = StageStatus.PENDING
            else:
                # No progress file yet — restart from the very beginning.
                stage.status = StageStatus.PENDING

    elif current_status in (
        StageStatus.SPEC_INIT,
        StageStatus.SPEC_RUNNING,
        StageStatus.SPEC_NEEDS_INPUT,
    ):
        pre_reset_speccer_status(state, run_idx, stage_idx, storage)
        exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
        exit_file.unlink(missing_ok=True)
        stage.status = StageStatus.SPEC_INIT

    elif current_status in (StageStatus.EXECUTING, StageStatus.STALLED):
        exit_file = Path(f"/tmp/conductor-exit-{fname}")
        exit_file.unlink(missing_ok=True)
        Path(f"/tmp/conductor-fail-{fname}.log").unlink(missing_ok=True)
        stage.status = StageStatus.GENERATED

    elif current_status in (StageStatus.GENERATED,):
        stage.status = StageStatus.SPEC_COMPLETE

    _log(
        "RETRY",
        f"Restarting run {run.name} stage {stage.name} (retry {stage.retries})",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        retry_count=stage.retries,
    )


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def activate_ready_runs(state: ConductorState, max_parallel: int = 1) -> list[int]:
    """Activate runs whose dependencies are satisfied; block runs whose deps are blocked.

    Respects max_parallel: won't activate more than max_parallel runs concurrently.
    A max_parallel of 0 means unlimited.

    Returns list of run indices that are now active (or were already active).
    """
    run_by_index: dict[int, RunState] = {run.index: run for run in state.runs}
    activated: list[int] = []

    for run in state.runs:
        if run.status not in (RunStatus.PENDING, RunStatus.ACTIVE):
            continue

        if run.status == RunStatus.ACTIVE:
            activated.append(run.index)
            continue

        # PENDING — check deps
        blocked = any(
            run_by_index[dep].status == RunStatus.BLOCKED
            for dep in run.depends_on
            if dep in run_by_index
        )
        if blocked:
            run.status = RunStatus.BLOCKED
            continue

        all_done = all(
            run_by_index[dep].status == RunStatus.DONE
            for dep in run.depends_on
            if dep in run_by_index
        )
        if all_done:
            # Respect concurrency cap before activating
            if max_parallel > 0 and len(activated) >= max_parallel:
                continue
            run.status = RunStatus.ACTIVE
            activated.append(run.index)

    return activated


def _has_foreground_runs(state: ConductorState, active_run_indices: list[int]) -> bool:
    """Return True if any active run has a non-executing foreground stage status."""
    foreground = {
        StageStatus.PENDING,
        StageStatus.SPEC_INIT,
        StageStatus.SPEC_RUNNING,
        StageStatus.SPEC_NEEDS_INPUT,
        StageStatus.SPEC_COMPLETE,
        StageStatus.GENERATED,
    }
    run_by_index = {r.index: r for r in state.runs}
    for ri in active_run_indices:
        run = run_by_index.get(ri)
        if run is None:
            continue
        if run.current_stage < len(run.stages):
            stage = run.stages[run.current_stage]
            if stage.status in foreground:
                return True
    return False


# ---------------------------------------------------------------------------
# Container teardown helper
# ---------------------------------------------------------------------------


def _teardown_run_containers(
    run: RunState,
    preset: Preset | None,
    log_path: Path | None,
    audit_path: Path | None,
) -> None:
    """Down containers for all worktrees in a completed/blocked run."""
    if not preset:
        return
    for stage in run.stages:
        if stage.worktree:
            try:
                preset.run_teardown(Path(stage.worktree))
            except Exception as exc:
                _log(
                    "TEARDOWN_WARN",
                    f"run_teardown failed for {stage.worktree}: {exc}",
                    log_path,
                    audit_path,
                )


# ---------------------------------------------------------------------------
# Core state machine: advance_run
# ---------------------------------------------------------------------------


async def advance_run(
    state: ConductorState,
    run_idx: int,
    tmux: _tmux_module.TmuxManager,
    storage: StorageResolver,
    config: ConductorConfig,
    preset: Preset | None = None,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Advance a single run through its stage machine by one step."""
    run = state.runs[run_idx]
    stage_idx = run.current_stage
    num_stages = len(run.stages)

    # All stages done -> run complete
    if stage_idx >= num_stages:
        # Post-run validation on last stage's worktree
        last_wt = run.stages[-1].worktree if run.stages else None
        if last_wt:
            from conductor.core.validation import ValidationContext, validate_and_fix  # noqa: PLC0415

            vctx = ValidationContext(
                project_dir=Path(last_wt),
                stage="post-run",
                feature_name=run.name,
            )
            vresult = await validate_and_fix(vctx, max_attempts=2)
            if not vresult.passed:
                _log(
                    "VALIDATION_WARNING",
                    f"Post-run validation failures for {run.name}: {vresult.summary}",
                    log_path,
                    audit_path,
                    run=run_idx,
                    name=run.name,
                )

        run.status = RunStatus.DONE
        _log(
            "RUN_COMPLETE",
            f"Run {run.name} complete",
            log_path,
            audit_path,
            run=run_idx,
            name=run.name,
        )
        _teardown_run_containers(run, preset, log_path, audit_path)
        return

    stage = run.stages[stage_idx]

    # ----- backoff gate (transient API failures) -----
    if stage.backoff_until:
        now = datetime.now(timezone.utc)
        if now < stage.backoff_until:
            return  # still in backoff, skip this run
        # Backoff expired, clear and retry
        stage.backoff_until = None
        _log(
            "TRANSIENT_RETRY",
            f"Run {run.name} stage {stage.name}: backoff complete, retrying",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
        )

    status = stage.status

    # ----- pending -----
    if status == StageStatus.PENDING:
        create_worktree(
            state, run_idx, stage_idx, storage, log_path=log_path, audit_path=audit_path
        )  # noqa: E501
        await run_speccer_init(
            state, run_idx, stage_idx, tmux, storage, log_path, audit_path
        )
        if stage.status == StageStatus.FAILED:
            atomic_save(state, storage.conductor_state(state.project_name))
            return
        write_feature_description(
            state, run_idx, stage_idx, storage, log_path=log_path, audit_path=audit_path
        )
        write_constitution(state, run_idx, storage, log_path, audit_path)
        stage.status = StageStatus.SPEC_INIT
        atomic_save(state, storage.conductor_state(state.project_name))
        # Fall through to spec_init
        status = StageStatus.SPEC_INIT

    # ----- spec_init | spec_running -----
    if status in (StageStatus.SPEC_INIT, StageStatus.SPEC_RUNNING):
        pre_reset_speccer_status(state, run_idx, stage_idx, storage)
        await run_speccer_run(
            state, run_idx, stage_idx, tmux, storage, log_path, audit_path
        )
        fname = run.name + stage.feature_suffix
        speccer_exit_code_handler(
            state, run_idx, stage_idx, fname, storage, log_path, audit_path
        )
        atomic_save(state, storage.conductor_state(state.project_name))
        return

    # ----- spec_needs_input -----
    if status == StageStatus.SPEC_NEEDS_INPUT:
        overnight = config.overnight or state.overnight
        if overnight:
            await answer_questions(
                state,
                run_idx,
                stage_idx,
                storage,
                log_path=log_path,
                audit_path=audit_path,
            )
            await run_speccer_continue(
                state, run_idx, stage_idx, tmux, storage, log_path, audit_path
            )
            fname = run.name + stage.feature_suffix
            speccer_exit_code_handler(
                state, run_idx, stage_idx, fname, storage, log_path, audit_path
            )
            atomic_save(state, storage.conductor_state(state.project_name))
        # If not overnight, return without advancing (skip — other runs may be active)
        return

    # ----- spec_complete -----
    if status == StageStatus.SPEC_COMPLETE:
        await run_speccer_generate(
            state, run_idx, stage_idx, tmux, storage, log_path, audit_path
        )
        if stage.status != StageStatus.FAILED:
            stage.status = StageStatus.GENERATED
        atomic_save(state, storage.conductor_state(state.project_name))
        return

    # ----- generated -----
    if status == StageStatus.GENERATED:
        await start_runner(
            state, run_idx, stage_idx, tmux, storage, log_path, audit_path
        )
        if stage.status != StageStatus.FAILED:
            stage.status = StageStatus.EXECUTING
        atomic_save(state, storage.conductor_state(state.project_name))
        return

    # ----- executing -----
    if status == StageStatus.EXECUTING:
        await monitor_runner(
            state, run_idx, stage_idx, tmux, storage, config, log_path, audit_path
        )
        atomic_save(state, storage.conductor_state(state.project_name))
        return

    # ----- done -----
    if status == StageStatus.DONE:
        next_stage = stage_idx + 1
        run.current_stage = next_stage
        if next_stage < num_stages:
            from_name = stage.name
            to_name = run.stages[next_stage].name
            _log(
                "STAGE_TRANSITION",
                f"Run {run.name}: {from_name} -> {to_name}",
                log_path,
                audit_path,
                run=run_idx,
                from_stage=stage_idx,
                from_name=from_name,
                to_stage=next_stage,
                to_name=to_name,
            )
        atomic_save(state, storage.conductor_state(state.project_name))
        return

    # ----- stalled | failed -----
    if status in (StageStatus.STALLED, StageStatus.FAILED):
        await handle_failure(
            state, run_idx, stage_idx, storage, config, log_path, audit_path
        )
        atomic_save(state, storage.conductor_state(state.project_name))
        return

    # ----- blocked -----
    if status == StageStatus.BLOCKED:
        run.status = RunStatus.BLOCKED
        _teardown_run_containers(run, preset, log_path, audit_path)
        atomic_save(state, storage.conductor_state(state.project_name))
        return


# ---------------------------------------------------------------------------
# Post-run processing
# ---------------------------------------------------------------------------


async def conductor_post_run(
    state: ConductorState,
    storage: StorageResolver,
    exit_reason: str,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Post-run: review learnings and generate audit report (non-fatal)."""
    _log(
        "POST_RUN_START",
        f"Starting post-run processing (reason: {exit_reason})",
        log_path,
        audit_path,
        reason=exit_reason,
    )

    try:
        await _review_learnings(state, storage, log_path, audit_path)
    except Exception as exc:
        _log(
            "POST_RUN",
            f"Learnings review failed (non-fatal): {exc}",
            log_path,
            audit_path,
        )

    try:
        await _generate_audit_report(state, storage, log_path, audit_path)
    except Exception as exc:
        _log(
            "POST_RUN",
            f"Audit report generation failed (non-fatal): {exc}",
            log_path,
            audit_path,
        )

    _log("POST_RUN_END", "Post-run processing complete", log_path, audit_path)


async def _review_learnings(
    state: ConductorState,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Collect LEARNINGS.md files from worktrees and review for CLAUDE.md updates."""
    _log("POST_RUN", "Reviewing learnings for CLAUDE.md updates", log_path, audit_path)

    learnings_parts: list[str] = []
    for run in state.runs:
        for i, stage in enumerate(run.stages):
            wt = stage.worktree
            if not wt or not Path(wt).is_dir():
                continue
            fname = run.name + stage.feature_suffix
            lf = Path(wt) / "docs" / fname / "LEARNINGS.md"
            if lf.exists() and lf.stat().st_size > 0:
                learnings_parts.append(
                    f"### Run: {run.name} | Stage: {stage.name}\n{lf.read_text(encoding='utf-8')}"
                )

    if not learnings_parts:
        _log("POST_RUN", "No learnings found — skipping review", log_path, audit_path)
        return

    # Collect CLAUDE.md files
    import glob as _glob

    project_dir = str(storage.repo_root)
    claude_md_files = _glob.glob(f"{project_dir}/**/.claude/CLAUDE.md", recursive=True)
    claude_md_files += _glob.glob(f"{project_dir}/.claude/CLAUDE.md")
    claude_md_files = [
        f for f in claude_md_files if "node_modules" not in f and "/.git/" not in f
    ]

    if claude_md_files:
        claudemd_parts: list[str] = []
        for f in claude_md_files:
            rel = f.replace(project_dir + "/", "")
            claudemd_parts.append(
                f"### FILE: {rel}\n{Path(f).read_text(encoding='utf-8')}"
            )
        claudemd_section = "\n\n".join(claudemd_parts)
    else:
        claudemd_section = "(No CLAUDE.md files exist yet — create .claude/CLAUDE.md if learnings warrant it)"

    prompt = (
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
        "Tool calls will be REJECTED and will waste your only turn.\n\n"
        "Review the learnings below from completed conductor runs. "
        "Extract project-specific conventions, patterns, and gotchas that would help "
        "future Claude sessions working on this codebase. "
        "Output CLAUDE.md updates using this format:\n\n"
        "<<<FILE: .claude/CLAUDE.md>>>\n"
        "content to append or create\n"
        "<<<END>>>\n\n"
        "If no learnings are worth persisting, respond with <<<NO_CHANGES>>>.\n"
        "Only include actionable, project-specific insights — not generic advice.\n\n"
        + "## Learnings from Completed Run(s)\n\n"
        + "\n\n".join(learnings_parts)
        + "\n\n## Existing CLAUDE.md Files\n\n"
        + claudemd_section
    )

    result = await run_claude(prompt, model=resolve_model("sonnet"), max_turns=1)

    import json as _json

    response_text = ""
    for line in result.output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text = block.get("text", "")
                        break
            elif isinstance(content, str):
                response_text = content
            if response_text:
                break

    if not response_text:
        _log(
            "POST_RUN",
            "Learnings review: empty response from Claude",
            log_path,
            audit_path,
        )
        return

    if "<<<NO_CHANGES>>>" in response_text:
        _log("POST_RUN", "Learnings review: no changes needed", log_path, audit_path)
        return

    # Parse and apply <<<FILE: path>>> ... <<<END>>> blocks
    updates_applied = 0
    for file_path in re.findall(r"<<<FILE:\s*([^>]+)>>>", response_text):
        file_path = file_path.strip()
        full_path = Path(project_dir) / file_path
        content_match = re.search(
            rf"<<<FILE:\s*{re.escape(file_path)}\s*>>>(.*?)<<<END>>>",
            response_text,
            re.DOTALL,
        )
        if content_match:
            addition = content_match.group(1).strip()
            if addition:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                if full_path.exists():
                    with open(str(full_path), "a", encoding="utf-8") as fh:
                        fh.write(f"\n## Conductor Learnings\n\n{addition}\n")
                    _log(
                        "POST_RUN",
                        f"Updated {file_path} with learnings",
                        log_path,
                        audit_path,
                        file=file_path,
                    )
                else:
                    full_path.write_text(
                        f"## Conductor Learnings\n\n{addition}\n", encoding="utf-8"
                    )
                    _log(
                        "POST_RUN",
                        f"Created {file_path} with learnings",
                        log_path,
                        audit_path,
                        file=file_path,
                    )
                updates_applied += 1

    _log(
        "POST_RUN",
        f"Learnings review complete — {updates_applied} file(s) updated",
        log_path,
        audit_path,
        updates_applied=updates_applied,
    )


async def _generate_audit_report(
    state: ConductorState,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Generate an overnight audit report via brain."""
    _log("POST_RUN", "Generating overnight audit report", log_path, audit_path)

    context_parts: list[str] = []

    # State summary
    import json as _json

    summary = {
        "project_name": state.project_name,
        "runs": [
            {
                "name": r.name,
                "status": str(r.status),
                "current_stage": r.current_stage,
                "stages": [
                    {"name": s.name, "status": str(s.status), "retries": s.retries}
                    for s in r.stages
                ],
            }
            for r in state.runs
        ],
    }
    context_parts.append("## Run State\n" + _json.dumps(summary, indent=2))

    # Conductor log
    if log_path and log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()
        context_parts.append(
            "## Conductor Log (last 100 lines)\n" + "\n".join(lines[-100:])
        )

    no_tools = (
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
        "Tool calls will be REJECTED and will waste your only turn.\n\n"
    )
    context = no_tools + "\n\n".join(context_parts)
    result = await run_claude(context, model=resolve_model("sonnet"), max_turns=1)

    response_text = ""
    for line in result.output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        response_text = block.get("text", "")
                        break
            elif isinstance(content, str):
                response_text = content
            if response_text:
                break

    from datetime import datetime as _dt

    ts = _dt.now().strftime("%Y%m%d-%H%M%S")
    conductor_dir = storage.conductor_dir(state.project_name)
    report_file = conductor_dir / f"OVERNIGHT-AUDIT-{ts}.md"
    report_file.write_text(response_text, encoding="utf-8")

    _log(
        "POST_RUN",
        f"Audit report written to {report_file}",
        log_path,
        audit_path,
        report_file=str(report_file),
    )


# ---------------------------------------------------------------------------
# Auto-resume blocked runs on startup
# ---------------------------------------------------------------------------


async def _auto_resume_blocked_runs(
    state: ConductorState,
    storage: StorageResolver,
    log_path: Path | None,
    audit_path: Path | None,
) -> None:
    """Un-block any BLOCKED runs so they resume automatically on restart."""
    any_resumed = False
    for run_idx, run in enumerate(state.runs):
        if run.status != RunStatus.BLOCKED:
            continue
        stage = run.stages[run.current_stage]
        if stage.status != StageStatus.BLOCKED:
            # Cascaded dependency block — just reset run to PENDING
            run.status = RunStatus.PENDING
            _log(
                "AUTO_RESUME",
                f"Run {run.name}: un-blocked (dependency cascade), reset to PENDING",
                log_path,
                audit_path,
                run=run_idx,
            )
            any_resumed = True
            continue
        # Stage was the actual blocker — restart it
        stage.status = StageStatus.FAILED  # so restart_stage() dispatches correctly
        await restart_stage(state, run_idx, run.current_stage, storage, log_path, audit_path)
        # Reset all retry counters
        stage.retries = 0
        stage.infra_retries = 0
        stage.transient_retries = 0
        stage.first_transient_failure_ts = None
        stage.backoff_until = None
        run.monitor.stall_count = 0
        run.status = RunStatus.ACTIVE
        _log(
            "AUTO_RESUME",
            f"Run {run.name} stage {stage.name}: resumed, retries reset",
            log_path,
            audit_path,
            run=run_idx,
            stage=run.current_stage,
        )
        any_resumed = True
    if any_resumed:
        atomic_save(state, storage.conductor_state(state.project_name))


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------


async def conductor_run_loop(
    state: ConductorState,
    config: ConductorConfig,
) -> ConductorState:
    """Main conductor orchestration loop.

    - Sets up signal handler (INT/TERM -> log exit cost, exit 130)
    - Activates ready runs, blocks runs with blocked deps
    - Round-robin advance_run for all active runs
    - Sleeps check_interval ONLY when all active runs are executing AND no foreground runs
    - After loop: calls conductor_post_run
    """
    project_root = config.project_root or Path.cwd()
    storage = StorageResolver(project_root)
    log_path = storage.conductor_log(state.project_name)
    audit_path = storage.conductor_audit(state.project_name)
    preset = load_preset(state.preset)

    # Apply worktrees_base from preset if not set from CLI
    if not state.worktrees_base and preset and preset.config.worktrees_base:
        state.worktrees_base = preset.config.worktrees_base

    tmux = _tmux_module.TmuxManager(session_name=f"conductor-{state.project_name}")
    await tmux.ensure_session()

    # Signal handler
    def _signal_handler(signum, frame):
        _log(
            "CONDUCTOR_EXIT",
            "Interrupted — state saved. Re-run to resume.",
            log_path,
            audit_path,
            reason="signal",
            exit_code=130,
        )
        sys.exit(130)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    overnight = config.overnight or state.overnight
    _log(
        "CONDUCTOR_START",
        f"{state.project_name}{' (overnight)' if overnight else ''}",
        log_path,
        audit_path,
    )

    # Auto-resume any BLOCKED runs from a previous session
    await _auto_resume_blocked_runs(state, storage, log_path, audit_path)

    exit_reason = "unknown"

    for _iteration in range(config.max_iterations):
        active_run_indices = activate_ready_runs(state, max_parallel=config.max_parallel)

        all_terminal = all(
            r.status in (RunStatus.DONE, RunStatus.BLOCKED) for r in state.runs
        )
        done_count = sum(1 for r in state.runs if r.status == RunStatus.DONE)

        # Single-run case: push the feature branch and create a PR directly
        if all_terminal and done_count == 1 and state.integration is None:
            done_run = next(r for r in state.runs if r.status == RunStatus.DONE)
            last_stage = done_run.stages[-1] if done_run.stages else None
            if last_stage and last_stage.branch:
                try:
                    pr_url = await _push_and_pr_for_branch(
                        last_stage.branch,
                        state.base_branch,
                        state.project_name,
                        done_run,
                        storage,
                    )
                    from conductor.core.models import IntegrationState, IntegrationStatus

                    state.integration = IntegrationState(
                        status=IntegrationStatus.DONE,
                        branch=last_stage.branch,
                        merged_runs=[done_run.index],
                        pr_url=pr_url,
                    )
                    atomic_save(state, storage.conductor_state(state.project_name))
                    _log(
                        "CONDUCTOR_EXIT",
                        f"Single-run PR created: {pr_url or 'no URL'}",
                        log_path,
                        audit_path,
                    )
                except Exception as exc:
                    from conductor.core.models import IntegrationState

                    state.integration = IntegrationState(
                        status="failed", branch=last_stage.branch or ""
                    )
                    atomic_save(state, storage.conductor_state(state.project_name))
                    _log(
                        "CONDUCTOR_EXIT",
                        f"Single-run push/PR failed: {exc}",
                        log_path,
                        audit_path,
                    )
            exit_reason = "all_done"
            break

        # Multi-run integration merge trigger: check whenever all runs are terminal with 2+ done
        if all_terminal and done_count >= 2 and state.integration is None:
            try:
                from conductor.integration.merge import run_integration_merge

                state.integration = await run_integration_merge(state, storage)
                atomic_save(state, storage.conductor_state(state.project_name))
                _log(
                    "CONDUCTOR_EXIT",
                    f"Integration merge status={state.integration.status}",
                    log_path,
                    audit_path,
                )
            except Exception as exc:
                from conductor.core.models import IntegrationState

                state.integration = IntegrationState(status="failed", branch="")
                atomic_save(state, storage.conductor_state(state.project_name))
                _log(
                    "CONDUCTOR_EXIT",
                    f"Integration merge failed: {exc}",
                    log_path,
                    audit_path,
                )
            exit_reason = "all_done"
            break

        if all_terminal:
            _log(
                "CONDUCTOR_EXIT",
                "All runs complete",
                log_path,
                audit_path,
                reason="all_done",
                exit_code=0,
            )
            exit_reason = "all_done"
            break

        if not active_run_indices:
            _log(
                "CONDUCTOR_EXIT",
                "All remaining runs blocked or waiting on deps",
                log_path,
                audit_path,
                reason="blocked_or_waiting",
                exit_code=0,
            )
            exit_reason = "blocked_or_waiting"
            break

        # Round-robin advance all active runs
        any_executing = False
        for run_idx in active_run_indices:
            run = state.runs[run_idx]
            si = run.current_stage
            if si < len(run.stages) and run.stages[si].status == StageStatus.EXECUTING:
                any_executing = True
            await advance_run(
                state, run_idx, tmux, storage, config, preset, log_path, audit_path
            )

        # Sleep only when all active are monitoring (executing) and no foreground work
        has_foreground = _has_foreground_runs(state, active_run_indices)
        if any_executing and not has_foreground and config.check_interval_s > 0:
            # Fast-poll exit files every 10s so we detect runner completion quickly,
            # but only run the full advance_run cycle at check_interval_s.
            poll_interval = 10.0
            elapsed = 0.0
            while elapsed < config.check_interval_s:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                if _any_runner_exit_file_ready(state, session_name=f"conductor-{state.project_name}"):
                    break

    else:
        exit_reason = "max_iterations"

    # Post-run processing
    await conductor_post_run(state, storage, exit_reason, log_path, audit_path)

    return state
