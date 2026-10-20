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
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import conductor.core.tmux as _tmux_module
from conductor.core import brain
from conductor.core.claude import resolve_model
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
    # None: use state.overnight (set from --no-overnight); a bool overrides it.
    overnight: bool | None = None


def _overnight(config: ConductorConfig, state: ConductorState) -> bool:
    """Whether speccer questions are auto-answered (state.overnight unless overridden)."""
    return state.overnight if config.overnight is None else config.overnight


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
) -> str:
    """Push a branch to origin and open a non-draft PR; return the PR URL.

    Raises RuntimeError when the push or the PR creation fails, so the
    caller never reports a PR that does not exist. A PR that already exists
    for the branch (a resumed run) counts as success.
    """
    project_dir = Path(storage.repo_root)

    proc = await asyncio.create_subprocess_exec(
        "git", "push", "-u", "origin", branch,
        cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, push_err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git push -u origin {branch} failed: {push_err.decode(errors='replace').strip()[-500:]}"
        )

    # Create PR with retry
    title = f"{run.name}: {run.description[:60]}" if run.description else run.name
    error = ""
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
            return stdout_bytes.decode().strip()
        error = stderr_bytes.decode(errors="replace").strip()
        existing = re.search(r"already exists:?\s*(https?://\S+)", error)
        if existing:
            return existing.group(1)
        if attempt < 2:
            await asyncio.sleep(5)
    raise RuntimeError(f"gh pr create failed after 3 attempts: {error[-500:]}")


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
# Exit/fail files shared with the tmux wrappers
# ---------------------------------------------------------------------------


def _ipc_dir(storage: StorageResolver, project_name: str) -> Path:
    """Per-project directory for the exit and fail files the tmux wrappers write.

    Keyed by repo and project (not a global /tmp name), so two conductor
    projects that share run and stage names cannot read each other's results,
    and a runner's exit code survives a reboot that clears /tmp.
    """
    d = storage.conductor_dir(project_name) / "ipc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _runner_exit_file(storage: StorageResolver, project_name: str, fname: str) -> Path:
    return _ipc_dir(storage, project_name) / f"conductor-exit-{fname}"


def _runner_fail_log(storage: StorageResolver, project_name: str, fname: str) -> Path:
    # TmuxManager.spawn_runner_in_window derives this name from the exit file
    # (conductor-exit-<x> -> conductor-fail-<x>.log in the same directory).
    return _ipc_dir(storage, project_name) / f"conductor-fail-{fname}.log"


def _speccer_exit_file(storage: StorageResolver, project_name: str, fname: str) -> Path:
    return _ipc_dir(storage, project_name) / f"conductor-speccer-exit-{fname}"


def _read_exit_code(exit_file: Path) -> int | None:
    """Exit code a wrapper wrote, None if there is none (yet); garbage counts as 1."""
    try:
        return int(exit_file.read_text().strip())
    except OSError:
        return None
    except ValueError:
        return 1


def _write_exit_code(exit_file: Path, code: int | None) -> None:
    """Record *code* in *exit_file*; None removes it."""
    if code is None:
        exit_file.unlink(missing_ok=True)
    else:
        exit_file.write_text(str(code))


def _last_change(paths: list[Path], since: float) -> float:
    """Latest mtime among *paths* (files or directories), at least *since*."""
    latest = since
    for path in paths:
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass
    return latest


# Blocking steps (speccer) run in a tmux window and are polled, never waited
# on blindly: a window that dies without an exit code, or goes silent, must
# not freeze the orchestrator (and every other run) for the rest of the night.
STEP_POLL_S = 5.0
STEP_STARTUP_GRACE_S = 15.0
STEP_INACTIVITY_S = 45 * 60.0  # no new pane output and no spec file changes
EXIT_TIMED_OUT = 124


async def _run_step_in_window(
    tmux: _tmux_module.TmuxManager,
    window: str,
    cmd: str,
    *,
    exit_file: Path,
    cwd: str,
    log_file: Path,
    activity: list[Path],
    stage_state: StageState,
    log_path: Path | None,
    audit_path: Path | None,
    **audit,
) -> int | None:
    """Run *cmd* in *window* until it exits; return its exit code.

    Returns None when the window ended without writing an exit code, and
    EXIT_TIMED_OUT (after stopping the window) when neither its output
    (*log_file*) nor any of the *activity* paths changed for
    STEP_INACTIVITY_S. The wait is a polling loop, so Ctrl-C and SIGTERM
    still end conductor while a step runs.
    """
    _write_exit_code(exit_file, None)
    await tmux.spawn_runner_in_window(
        window, cmd, exit_file=exit_file, cwd=cwd, log_file=log_file
    )
    started = time.time()
    idle_checks = 0
    while True:
        rc = _read_exit_code(exit_file)
        if rc is not None:
            return rc
        now = time.time()
        if not await tmux.is_window_alive(window):
            idle_checks = 2  # window closed: nothing will write the exit code
        elif now - started > STEP_STARTUP_GRACE_S and await tmux.is_runner_idle(window):
            # No process under the pane shell: it ended, or is about to write
            # its exit code. Confirm on the next poll before giving up.
            idle_checks += 1
        else:
            idle_checks = 0
        if idle_checks >= 2:
            rc = _read_exit_code(exit_file)
            if rc is None:
                _log(
                    "FAILURE",
                    f"{window}: command ended without an exit code",
                    log_path, audit_path, window=window, **audit,
                )
            return rc
        if now - _last_change([log_file, *activity], started) > STEP_INACTIVITY_S:
            minutes = int(STEP_INACTIVITY_S // 60)
            stage_state.last_error = (
                f"{window} produced no output and changed no spec files for "
                f"{minutes} min; conductor stopped it"
            )
            await _stop_runner(
                tmux, window, exit_file, f"no activity for {minutes} min",
                log_path, audit_path, **audit,
            )
            _write_exit_code(exit_file, EXIT_TIMED_OUT)
            return EXIT_TIMED_OUT
        await asyncio.sleep(STEP_POLL_S)


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


def _worktree_branch(path: Path) -> str | None:
    """Branch checked out in the worktree at *path*, or None if it is not one."""
    if not (path / ".git").exists():
        return None
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(path),
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _registered_worktrees(project_dir: Path) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` into one dict per worktree."""
    r = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def _release_stale_worktrees(
    project_dir: Path,
    wt_path: Path,
    branch: str,
    log_path: Path | None,
    audit_path: Path | None,
) -> Path | None:
    """Clear git worktree registrations that would make ``git worktree add`` fail.

    git refuses to add a worktree at a path, or for a branch, that is still
    registered to a worktree whose directory is gone ("missing but already
    registered worktree", "'<branch>' is already used by worktree at ..."):
    the directory was deleted by hand or lived on a tmpfs that a reboot
    cleared. Only those registrations, for this path or this branch, are
    removed (other worktrees, e.g. on an unmounted disk, are left alone).

    If *branch* is checked out in a worktree that does exist at another path,
    that worktree is returned so the caller reuses it instead.
    """
    target = wt_path.resolve()
    ref = f"refs/heads/{branch}"
    for entry in _registered_worktrees(project_dir):
        path = Path(entry.get("worktree", ""))
        if path.resolve() != target and entry.get("branch") != ref:
            continue
        if path.exists():
            if entry.get("branch") == ref and path.resolve() != target:
                _log(
                    "WORKTREE_CREATE",
                    f"{branch} is already checked out at {path} — reusing it",
                    log_path,
                    audit_path,
                    branch=branch,
                    worktree=str(path),
                )
                return path
            continue
        r = subprocess.run(
            ["git", "worktree", "remove", str(path)],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
        )
        _log(
            "WORKTREE_CREATE",
            f"Dropped stale worktree registration {path} (directory missing)"
            + ("" if r.returncode == 0 else f" — failed: {r.stderr.strip()}"),
            log_path,
            audit_path,
            branch=entry.get("branch"),
            worktree=str(path),
        )
    return None


class DependencyMergeError(RuntimeError):
    """A run's dependency branches could not be merged into its first worktree."""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _merge_dependencies(wt: Path, branch: str, dep_branches: list[str]) -> list[str]:
    """Merge every dependency branch that *wt*'s HEAD does not contain yet.

    Returns the branches merged by this call. Idempotent: a branch already
    in HEAD is skipped, and a merge a crash left half-done is aborted and
    redone. On a conflict the merge is aborted (the worktree stays clean)
    and DependencyMergeError names the conflicting files.
    """
    def merging() -> bool:
        return _git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0

    if merging():
        _git(wt, "merge", "--abort")
    merged: list[str] = []
    for dep in dep_branches:
        if _git(wt, "merge-base", "--is-ancestor", dep, "HEAD").returncode == 0:
            continue
        r = _git(wt, "merge", "--no-edit", "-m", f"Merge dependency {dep} into {branch}", dep)
        if r.returncode != 0:
            conflicts = _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
            if merging():
                _git(wt, "merge", "--abort")
            if conflicts:
                raise DependencyMergeError(
                    f"dependency {dep} conflicts with {branch} in: {', '.join(conflicts)}"
                )
            raise DependencyMergeError(
                f"git merge {dep} failed in {wt}: {(r.stderr or r.stdout).strip()[-500:]}"
            )
        merged.append(dep)
    return merged


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

    # Worktree path: worktrees_base / {project}-{run}-{stage}. The project
    # prefix keeps two conductor projects on one repo (same run and stage
    # names, e.g. after starting a new project for the same feature) out of
    # each other's worktrees.
    wt_candidate = wt_base / f"{state.project_name}-{run.name}-{stage.name}"
    # A stage that already has a worktree keeps it, even if worktrees_base or
    # the naming scheme changed since it was created.
    if stage.worktree and _worktree_branch(Path(stage.worktree)) == branch:
        wt_candidate = Path(stage.worktree)

    # Determine base ref. The first stage of a run with dependencies starts
    # from the last one and merges the others in (below), so it sees the
    # work of every dependency, not just one.
    dep_branches: list[str] = []
    if stage_idx == 0 and run.depends_on:
        run_by_index = {r.index: r for r in state.runs}
        dep_branches = [
            b for d in run.depends_on
            if (b := run_by_index[d].stages[-1].branch if run_by_index[d].stages else None)
        ]
    if stage_idx > 0:
        base_ref = run.stages[stage_idx - 1].branch
    elif dep_branches:
        base_ref = dep_branches[-1]
    else:
        base_ref = state.base_branch

    # Reuse existing worktree if it's valid and on the right branch
    if wt_candidate.exists() and (wt_candidate / ".git").exists():
        current = _worktree_branch(wt_candidate)
        if current != branch:
            raise RuntimeError(
                f"Worktree {wt_candidate} exists but on wrong branch "
                f"({current}), expected {branch}"
            )
    else:
        # Remove stale leftover dir (not a valid worktree)
        if wt_candidate.exists():
            subprocess.run(["rm", "-rf", str(wt_candidate)], capture_output=True)
            if wt_candidate.exists():
                subprocess.run(["sudo", "rm", "-rf", str(wt_candidate)], capture_output=True)

        adopted = _release_stale_worktrees(
            project_dir, wt_candidate, branch, log_path, audit_path
        )
        if adopted is not None:
            wt_candidate = adopted
        else:
            branch_exists = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=str(project_dir),
                capture_output=True,
            ).returncode == 0

            wt_candidate.parent.mkdir(parents=True, exist_ok=True)
            if branch_exists:
                add_args = ["git", "worktree", "add", str(wt_candidate), branch]
            else:
                add_args = [
                    "git", "worktree", "add", "-b", branch, str(wt_candidate), base_ref,
                ]
            r = subprocess.run(
                add_args, cwd=str(project_dir), capture_output=True, text=True
            )
            if r.returncode != 0:
                raise RuntimeError(
                    f"git worktree add {wt_candidate} failed (rc={r.returncode}): "
                    f"{r.stderr.strip()}"
                )

    stage.branch = branch
    stage.worktree = str(wt_candidate)

    # Raises DependencyMergeError on a conflict, with branch and worktree
    # already recorded so the conflict can be resolved there.
    merged = _merge_dependencies(wt_candidate, branch, dep_branches)

    _log(
        "WORKTREE_CREATE",
        f"run {run_idx} stage {stage_idx}: {wt_candidate} <- {base_ref}"
        + (f" (merged {', '.join(merged)})" if merged else ""),
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        branch=branch,
        worktree=str(wt_candidate),
        base_ref=base_ref,
        merged_dependencies=merged,
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

    exit_file = _speccer_exit_file(storage, state.project_name, fname)
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
    exit_code = await _run_step_in_window(
        tmux, window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file,
        activity=[progress_file.parent], stage_state=stage,
        log_path=log_path, audit_path=audit_path, run=run_idx, stage=stage_idx,
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
        stage.last_exit_code = exit_code if exit_code is not None else 1
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

    exit_file = _speccer_exit_file(storage, state.project_name, fname)
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
    await _run_step_in_window(
        tmux, window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file,
        activity=[Path(wt) / "docs" / fname / "spec"], stage_state=stage,
        log_path=log_path, audit_path=audit_path, run=run_idx, stage=stage_idx,
    )

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

    exit_file = _speccer_exit_file(storage, state.project_name, fname)
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
    await _run_step_in_window(
        tmux, window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file,
        activity=[Path(wt) / "docs" / fname / "spec"], stage_state=stage,
        log_path=log_path, audit_path=audit_path, run=run_idx, stage=stage_idx,
    )

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

    exit_file = _speccer_exit_file(storage, state.project_name, f"{fname}-gen")
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
    rc = await _run_step_in_window(
        tmux, window_name, cmd, exit_file=exit_file, cwd=wt, log_file=log_file,
        activity=[Path(wt) / "docs" / fname], stage_state=stage,
        log_path=log_path, audit_path=audit_path, run=run_idx, stage=stage_idx,
    )

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
        stage.last_exit_code = rc if rc is not None else 1
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

    problem = _manifest_problem(run_sh)
    if problem:
        stage.last_error = f"speccer generate produced an unusable run.sh: {problem}"
        _log(
            "SPECCER_GENERATE_FAILED",
            f"speccer generate {fname}: unusable run.sh ({problem}) — regeneration needed",
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


# Consecutive speccer rounds without progress (see StageState.spec_failures)
# before the stage FAILs into handle_failure's diagnosis and retry budget.
MAX_SPEC_FAILURES = 3


def _count_spec_failure(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    reason: str,
    log_path: Path | None,
    audit_path: Path | None,
) -> bool:
    """Count a spec round that made no progress; True if the stage now FAILED.

    speccer leaves STATUS=NEEDS_INPUT and exits 1 when Claude fails, when no
    promise token comes back, or when SPEC_COMPLETE is rejected. Without a
    limit the stage re-enters NEEDS_INPUT and starts another spec session
    forever, never touching the retry budget.
    """
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    stage.spec_failures += 1
    failed = stage.spec_failures >= MAX_SPEC_FAILURES
    _log(
        "FAILURE",
        f"Run {run.name} stage {stage.name}: {reason} "
        f"({stage.spec_failures}/{MAX_SPEC_FAILURES} in a row"
        + (", stage failed)" if failed else ")"),
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        reason="spec_no_progress",
        spec_failures=stage.spec_failures,
        exit_code=stage.last_exit_code,
    )
    if failed:
        stage.status = StageStatus.FAILED
    return failed


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
    exit_file = _speccer_exit_file(storage, state.project_name, fname)

    exit_code = _read_exit_code(exit_file)

    progress_file = _progress_file_path(state, run_idx, stage_idx)
    _recoverable = {"NEEDS_INPUT", "COMPLETE", "GENERATED"}
    if exit_code is not None:
        stage.last_exit_code = exit_code

    if exit_code == 0:
        stage.spec_failures = 0
        sync_speccer_status(state, run_idx, stage_idx, storage, log_path, audit_path)
    elif exit_code is not None:
        # Non-zero exit — check PROGRESS.md for recoverable state
        if progress_file is not None and progress_file.exists():
            pstatus = _read_progress_status(progress_file)
            if pstatus == "NEEDS_INPUT" and _count_spec_failure(
                state, run_idx, stage_idx,
                f"speccer exited {exit_code} with the spec still waiting for input",
                log_path, audit_path,
            ):
                return
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


# Wall-clock limit for answering one round of speccer questions.
ANSWER_TIMEOUT_S = 1200.0
# Automatic question rounds per stage before a person has to look.
MAX_AUTO_QUESTION_ROUNDS = 8

_QUESTION_HEADING = re.compile(r"^#{2,4}[ \t]*Q(\d+)\b", re.MULTILINE | re.IGNORECASE)


def _questions_file(run: RunState, stage: StageState) -> Path | None:
    if not stage.worktree:
        return None
    fname = run.name + stage.feature_suffix
    return Path(stage.worktree) / "docs" / fname / "spec" / "QUESTIONS.md"


def _has_answers(questions_file: Path | None) -> bool:
    """Whether QUESTIONS.md holds answers (speccer's test: a line starting '> ')."""
    if questions_file is None:
        return False
    try:
        text = questions_file.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.startswith("> ") for line in text.splitlines())


def _awaits_human(run: RunState, stage: StageState, overnight: bool) -> bool:
    """Whether a SPEC_NEEDS_INPUT stage can only be moved on by a person.

    Overnight, questions are answered automatically up to
    MAX_AUTO_QUESTION_ROUNDS rounds. Otherwise the stage waits until someone
    writes answers into QUESTIONS.md.
    """
    if overnight:
        return stage.question_rounds >= MAX_AUTO_QUESTION_ROUNDS
    questions = _questions_file(run, stage)
    return questions is not None and questions.exists() and not _has_answers(questions)


def _unanswered(questions: str, answers: str) -> list[str]:
    """Numbers of the '### Qn' questions that *answers* has no heading for."""
    asked = dict.fromkeys(m.group(1) for m in _QUESTION_HEADING.finditer(questions))
    return [
        n for n in asked
        if not re.search(rf"^[\s#*>_-]*Q{n}\b", answers, re.MULTILINE | re.IGNORECASE)
    ]


async def answer_questions(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    conductor_dir: Path | None = None,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> bool:
    """Answer speccer's questions in QUESTIONS.md on the product owner's behalf.

    Returns True when speccer can continue: answers were written, or there is
    nothing to answer (speccer then reports the problem itself). Returns
    False when no usable answers came back; the stage then backs off (the
    call failed) or counts a spec failure (the answers were incomplete).
    """
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree

    questions_file = Path(wt) / "docs" / fname / "spec" / "QUESTIONS.md"
    if not questions_file.exists():
        return True
    questions = questions_file.read_text(encoding="utf-8").strip()
    if not questions:
        return True

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
        "Use the provided feature brief, constitution, description, and existing specs as context; "
        "you may read the code in the current directory to ground your answers. "
        "Answer each question directly and concisely. Do NOT start implementing — only answer the questions.\n\n"
        "Your final message is written verbatim into QUESTIONS.md as the answers, so it must "
        "contain only the answers: for every question a heading `### Qn` (its number) followed "
        "by the answer. No preamble, no closing remarks."
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

    reply = await brain.ask(
        context,
        model=resolve_model("opus"),
        timeout_s=ANSWER_TIMEOUT_S,
        max_turns=20,
        tools="Read,Grep,Glob",  # read-only: ground answers in the code
        cwd=wt,
    )
    missing = _unanswered(questions, reply.text) if reply.ok else []
    usable = reply.ok and not missing

    if usable:
        # Speccer expects answers as lines prefixed with "> " in QUESTIONS.md
        prefixed = "\n".join(f"> {line}" for line in reply.text.splitlines())
        combined = questions + "\n\n" + prefixed + "\n"
        questions_file.write_text(combined, encoding="utf-8")

    brain.log_brain_call(
        storage.brain_calls_dir(state.project_name),
        "answer-questions", run_idx, stage_idx, context, reply,
        questions=questions, written=usable, unanswered=missing,
    )
    if usable:
        return True

    if not reply.ok and reply.subtype != "error_max_turns":
        # The call itself failed (API down, CLI error, timeout): retry later.
        _schedule_backoff(
            state, run_idx, stage_idx,
            f"answering questions failed ({reply.detail})", log_path, audit_path,
        )
        return False

    reason = (
        f"auto-answers skip question(s) {', '.join('Q' + n for n in missing)}"
        if missing
        else f"auto-answer produced no final answer ({reply.detail})"
    )
    _count_spec_failure(state, run_idx, stage_idx, reason, log_path, audit_path)
    return False


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


class ManifestError(ValueError):
    """run.sh does not describe a runnable set of phases."""


@dataclass
class Manifest:
    phases: list  # list[runner.config.PhaseConfig]
    model: str
    fix_model: str


_VALUE = r"""(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([^\s"'#;)]+))"""
_ARRAY_ITEM = re.compile(
    r"^\s*(PHASES|PHASE_TOKENS|PHASE_NAMES)\[\s*(\d+)\s*\]\s*=\s*" + _VALUE
)
_ARRAY_COMPOUND = re.compile(
    r"^\s*(?:declare\s+-A\s+)?(PHASES|PHASE_TOKENS|PHASE_NAMES)\s*=\s*\((.*)\)"
)
_COMPOUND_ITEM = re.compile(r"\[\s*(\d+)\s*\]\s*=\s*" + _VALUE)
_MODEL_VAR = re.compile(
    r"^\s*(?:export\s+)?(CONDUCTOR_MODEL|CONDUCTOR_FIX_MODEL)\s*=\s*" + _VALUE
)
_DEFAULT_EXPANSION = re.compile(r"^\$\{\w+:?-([^}]*)\}$")


def _shell_value(dq: str | None, sq: str | None, bare: str | None) -> str:
    if dq is not None:
        return re.sub(r"\\(.)", r"\1", dq)
    return sq if sq is not None else (bare or "")


def parse_manifest(run_sh: Path) -> Manifest:
    """Read the phases and models a generated run.sh declares.

    Accepts the forms bash accepts for the template's arrays (double or
    single quotes, bare words, ``declare -A X=([1]=...)``, padded keys),
    ignores comments, and resolves ``${VAR:-default}`` to its default.
    Raises ManifestError for no phases, an unresolvable model value, or a
    phase prompt file that does not exist (the runner would run Claude on a
    placeholder prompt).
    """
    from runner.config import PhaseConfig  # noqa: PLC0415

    arrays: dict[str, dict[int, str]] = {"PHASES": {}, "PHASE_TOKENS": {}, "PHASE_NAMES": {}}
    models: dict[str, str] = {}
    for line in run_sh.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        if m := _ARRAY_ITEM.match(line):
            arrays[m.group(1)][int(m.group(2))] = _shell_value(*m.group(3, 4, 5))
        elif m := _ARRAY_COMPOUND.match(line):
            for item in _COMPOUND_ITEM.finditer(m.group(2)):
                arrays[m.group(1)][int(item.group(1))] = _shell_value(*item.group(2, 3, 4))
        elif m := _MODEL_VAR.match(line):
            value = _shell_value(*m.group(2, 3, 4))
            if d := _DEFAULT_EXPANSION.match(value):
                value = d.group(1)
            if "$" in value:
                raise ManifestError(f"{run_sh}: cannot resolve {m.group(1)}={value}")
            models[m.group(1)] = value

    files = arrays["PHASES"]
    if not files:
        raise ManifestError(f"No PHASES found in {run_sh}")
    prompt_dir = run_sh.parent / "prompts"
    phases = []
    missing = []
    for num in sorted(files):
        rel = files[num].removeprefix("./").removeprefix("prompts/")
        prompt = prompt_dir / rel
        if not prompt.is_file():
            missing.append(str(prompt))
        phases.append(PhaseConfig(
            number=num,
            name=arrays["PHASE_NAMES"].get(num, f"Phase {num}"),
            prompt_file=str(prompt),
            token=arrays["PHASE_TOKENS"].get(num, f"PHASE_{num}_COMPLETE"),
        ))
    if missing:
        raise ManifestError(f"{run_sh}: phase prompt file(s) missing: {', '.join(missing)}")
    model = models.get("CONDUCTOR_MODEL", "")
    return Manifest(phases, model, models.get("CONDUCTOR_FIX_MODEL", "") or model)


def _manifest_problem(run_sh: Path) -> str | None:
    """Why *run_sh* is not a usable manifest, or None if it is."""
    try:
        parse_manifest(run_sh)
    except (ManifestError, OSError) as exc:
        return str(exc)
    return None


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

    Returns path to the written RUN-CONFIG.json. Raises ManifestError (a
    ValueError) when run.sh is not a usable manifest.
    """
    from runner.config import RunConfig  # noqa: PLC0415

    manifest = parse_manifest(run_sh_path)
    phase_configs = manifest.phases
    for phase in phase_configs:
        phase.model = preset_config.phase_models.get(phase.number) or None

    # Models: the preset wins over run.sh, whose values the generate step
    # always writes as the speccer default ("opus").
    model = preset_config.model or manifest.model
    fix_model = preset_config.fix_model or manifest.fix_model or model

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
        # Off for every stage on purpose (commit 96c31cd): the preset's
        # local_ci_enabled is not honoured on the run path.
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

    exit_file = _runner_exit_file(storage, state.project_name, fname)
    exit_file.unlink(missing_ok=True)
    _runner_fail_log(storage, state.project_name, fname).unlink(missing_ok=True)

    # Generate RUN-CONFIG.json from the speccer-generated run.sh
    docs_dir = Path(wt) / "docs" / fname
    run_sh = docs_dir / "run.sh"
    preset = load_preset(state.preset, storage.repo_root)
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
            window_log = _latest_window_log(storage, state.project_name, fname)
            if window_log is not None:
                context_parts.append(
                    f"## Runner Window Output ({window_log.name}, last 60 lines)\n"
                    + _tail(window_log, 60)
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
        prompt = brain.render_prompt("diagnose-stall", "\n\n".join(context_parts))

        _log(
            "BRAIN_CALL",
            f"diagnose-stall for {fname}",
            log_path,
            audit_path,
            type="diagnose-stall",
            run=run_idx,
            stage=stage_idx,
        )

        reply = await brain.ask(
            prompt, model=resolve_model("sonnet"), timeout_s=BRAIN_DIAGNOSE_TIMEOUT_S
        )
        brain.log_brain_call(
            storage.brain_calls_dir(state.project_name),
            "diagnose-stall", run_idx, stage_idx, prompt, reply,
        )
        action, message = (
            brain.parse_action(reply.text, ("STEER", "RESET", "IGNORE"))
            if reply.ok
            else (None, "")
        )
        if action is None:
            action = "IGNORE"  # least disruptive when there is no usable verdict

        if action == "STEER":
            message = "\n".join(message.splitlines()[:5])
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


def _any_runner_exit_file_ready(
    state: ConductorState,
    storage: StorageResolver,
    session_name: str | None = None,
) -> bool:
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
        exit_file = _runner_exit_file(storage, state.project_name, fname)
        if exit_file.exists():
            return True
        # Also detect dead windows (runner crashed without writing exit file)
        if session_name:
            window_name = f"run{ri}:{stage.name}-exec"
            r = subprocess.run(
                # "=" makes tmux match names exactly, not by prefix: run0:api
                # must never resolve to its sibling run0:api-exec.
                ["tmux", "list-windows", "-t", f"={session_name}", "-F", "#{window_name}"],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and window_name not in r.stdout.strip().splitlines():
                # Window is gone — write synthetic exit file
                exit_file.write_text("1")
                return True
            # Check for zombie runner (window alive but Claude process dead)
            if r.returncode == 0 and window_name in r.stdout.strip().splitlines():
                pid_r = subprocess.run(
                    ["tmux", "list-panes", "-t", f"={session_name}:={window_name}",
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
                                exit_file.write_text("143")
                                return True
                        else:
                            _shallow_tree_counts.pop(window_key, None)
                    except (ValueError, OSError):
                        pass
    return False


def _runner_window(run_idx: int, stage: StageState) -> str:
    return f"run{run_idx}:{stage.name}-exec"


def _runner_outcome(
    state: ConductorState, run_idx: int, stage_idx: int, storage: StorageResolver
) -> int | None:
    """Exit code of the stage's current runner, or None if it has not exited.

    The wrapper's exit file is the primary record. The runner also writes
    docs/<feature>/exit_code itself, which survives the exit file being
    removed; it is never cleared between attempts, so it only counts when
    written after this attempt started.
    """
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    exit_file = _runner_exit_file(storage, state.project_name, fname)
    try:
        return int(exit_file.read_text().strip())
    except OSError:
        pass
    except ValueError:
        return 1
    if not stage.worktree or stage.started_at is None:
        return None
    durable = Path(stage.worktree) / "docs" / fname / "exit_code"
    try:
        if durable.stat().st_mtime <= stage.started_at.timestamp():
            return None
        return int(durable.read_text().strip())
    except OSError:
        return None
    except ValueError:
        return 1


def _own_pane_group(pid: int, exit_file: Path) -> bool:
    """Whether *pid* leads the process group of our runner wrapper for *exit_file*.

    A pane pid can outlive its process and be reused, so signal a group only
    when the process is its own group leader and its command line is the
    wrapper that writes this stage's exit file (Linux /proc; elsewhere the
    window is just killed).
    """
    try:
        if os.getpgid(pid) != pid or pid == os.getpgid(0):
            return False
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except (OSError, ProcessLookupError):
        return False
    return str(exit_file).encode() in cmdline


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _stop_runner(
    tmux: _tmux_module.TmuxManager,
    window: str,
    exit_file: Path,
    reason: str,
    log_path: Path | None,
    audit_path: Path | None,
    **audit,
) -> None:
    """Make sure nothing runs in a runner window before its stage leaves EXECUTING.

    A stage that is FAILED or BLOCKED while its runner keeps working gets
    its containers torn down underneath it, and a later exit is never read.
    The wrapper's process group (shell, runner, Claude session) gets SIGTERM,
    then SIGKILL after a grace period, and the window is closed; its output
    is kept in the window's log file.
    """
    if not await tmux.is_window_alive(window) or await tmux.is_runner_idle(window):
        return  # nothing runs there any more
    pid = await tmux.get_pane_pid(window)
    if pid is not None and _own_pane_group(pid, exit_file):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(20):
            if not _group_alive(pid):
                break
            await asyncio.sleep(0.5)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    await tmux.kill_window(window)
    _log(
        "RUNNER_KILLED",
        f"Stopped runner in {window}: {reason}",
        log_path,
        audit_path,
        window=window,
        **audit,
    )


def _finish_runner(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    exit_code: int,
    storage: StorageResolver,
    log_path: Path | None,
    audit_path: Path | None,
) -> None:
    """Record a runner exit on its stage: DONE for 0, FAILED otherwise."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
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
        # The runner's own quality gates passed; nothing to re-validate here.
        stage.status = StageStatus.DONE
        stage.completed_at = datetime.now(timezone.utc)
        return
    # Read captured pane output if available
    fail_log = _runner_fail_log(storage, state.project_name, fname)
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
    window_name = _runner_window(run_idx, stage)
    exit_file = _runner_exit_file(storage, state.project_name, fname)

    exit_code = _runner_outcome(state, run_idx, stage_idx, storage)
    if exit_code is not None:
        if exit_code != 0:
            # A synthetic exit (zombie) can leave the runner process alive.
            await _stop_runner(
                tmux, window_name, exit_file, f"exit {exit_code} recorded",
                log_path, audit_path, run=run_idx, stage=stage_idx,
            )
        _finish_runner(state, run_idx, stage_idx, exit_code, storage, log_path, audit_path)
        return

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

    if stage.status == StageStatus.EXECUTING:
        return
    # Declared dead, zombie or stalled. The runner may have exited between
    # the checks above: trust a recorded exit over the diagnosis.
    late_exit = _runner_outcome(state, run_idx, stage_idx, storage)
    if late_exit is not None:
        stage.status = StageStatus.EXECUTING
        stage.last_exit_code = None
        if late_exit != 0:
            await _stop_runner(
                tmux, window_name, exit_file, f"exit {late_exit} recorded",
                log_path, audit_path, run=run_idx, stage=stage_idx,
            )
        _finish_runner(state, run_idx, stage_idx, late_exit, storage, log_path, audit_path)
        return
    await _stop_runner(
        tmux, window_name, exit_file, f"stage left EXECUTING ({stage.status})",
        log_path, audit_path, run=run_idx, stage=stage_idx,
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


def _is_transient_api_error(paths_to_check: list[Path]) -> bool:
    """Check fail logs for transient API error patterns."""
    for p in paths_to_check:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _TRANSIENT_PATTERNS.search(text):
            return True
    return False


def classify_failure(
    fname: str,
    exit_code: int | None,
    stage: StageState,
    fail_logs: list[Path] | None = None,
) -> str:
    """Classify failure as 'infra', 'transient', or 'logic'.

    - Exit 137/143 (signal kills) → infra
    - Exit 1 + transient API error patterns in fail log → transient
    - Exit 1 + activity log modified within 60s → infra (mid-work crash)
    - Everything else → logic
    """
    if exit_code in (137, 143):
        return "infra"
    if exit_code == 1:
        if _is_transient_api_error(fail_logs or []):
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


# Wall-clock limit for one-turn diagnosis calls (normally ~20 s).
BRAIN_DIAGNOSE_TIMEOUT_S = 300.0

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\r")


def _tail(path: Path, lines: int) -> str:
    """Last *lines* lines of a log, with terminal escape codes removed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(_ANSI_RE.sub("", text).splitlines()[-lines:])


def _latest_window_log(
    storage: StorageResolver, project_name: str, fname: str
) -> Path | None:
    """Newest tmux pane log of this stage's speccer or runner windows."""
    logs_dir = storage.conductor_dir(project_name) / "logs"
    if not logs_dir.is_dir():
        return None
    pattern = re.compile(
        rf"^(?:speccer-(?:init|run|continue|generate)|runner)_{re.escape(fname)}"
        r"_\d{8}_\d{6}\.log$"
    )
    logs = [p for p in logs_dir.iterdir() if pattern.match(p.name)]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def _failure_context(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    retries: int,
    max_retries: int,
) -> str:
    """The evidence a failure diagnosis needs: what ran, how it ended, its output."""
    import json as _json

    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    parts: list[str] = [
        f"## Stage\nRun `{run.name}` ({run.description}), stage `{stage.name}`, "
        f"feature `{fname}`, status {stage.status}, "
        f"exit code {stage.last_exit_code if stage.last_exit_code is not None else 'unknown'}"
    ]
    if stage.last_error:
        parts.append(
            "## Orchestrator Error\nConductor itself raised while advancing this "
            f"stage:\n{stage.last_error}"
        )

    fail_log = _runner_fail_log(storage, state.project_name, fname)
    window_log = _latest_window_log(storage, state.project_name, fname)
    if fail_log.exists():
        parts.append("## Failure Output (last lines of the runner pane)\n" + _tail(fail_log, 50))
    elif window_log is not None:
        parts.append(f"## Window Output ({window_log.name}, last 80 lines)\n" + _tail(window_log, 80))

    progress = _progress_file_path(state, run_idx, stage_idx)
    if progress is not None and progress.exists():
        parts.append("## Speccer PROGRESS.md\n" + _tail(progress, 60))
    activity_log = _activity_log_path(state, run_idx, stage_idx)
    if activity_log is not None and activity_log.exists():
        parts.append("## Runner Activity Log (last 100 lines)\n" + _tail(activity_log, 100))

    parts.append(
        "## Stage Config\n" + _json.dumps(stage.model_dump(), indent=2, default=str)
    )
    parts.append(f"## Retries\n{retries} of {max_retries} used")
    return "\n\n".join(parts)


async def _back_off_and_restart(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    reason: str,
    log_path: Path | None,
    audit_path: Path | None,
) -> None:
    """Restart the stage after an exponential backoff; block after 8 hours of it."""
    if _schedule_backoff(state, run_idx, stage_idx, reason, log_path, audit_path):
        await restart_stage(
            state, run_idx, stage_idx, storage, log_path=log_path, audit_path=audit_path
        )


def _schedule_backoff(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    reason: str,
    log_path: Path | None,
    audit_path: Path | None,
) -> bool:
    """Set the stage's next exponential backoff; False (and BLOCKED) after 8 hours."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    now = datetime.now(timezone.utc)
    if stage.first_transient_failure_ts is None:
        stage.first_transient_failure_ts = now
    elapsed_hours = (now - stage.first_transient_failure_ts).total_seconds() / 3600
    if elapsed_hours > 8:
        stage.status = StageStatus.BLOCKED
        _log(
            "BLOCKED",
            f"Run {run.name} stage {stage.name}: {reason} for 8+ hours",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
            reason="transient_timeout",
            exit_code=stage.last_exit_code,
        )
        return False
    # Exponential backoff: 30s, 60s, 2m, 4m, 8m, 16m, 32m, 60m cap
    backoff_s = min(3600, 30 * (2 ** stage.transient_retries))
    stage.transient_retries += 1
    stage.backoff_until = now + timedelta(seconds=backoff_s)
    _log(
        "TRANSIENT_FAILURE",
        f"Run {run.name} stage {stage.name}: {reason}, backing off {backoff_s}s "
        f"(attempt {stage.transient_retries})",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        backoff_s=backoff_s,
        transient_retries=stage.transient_retries,
        exit_code=stage.last_exit_code,
    )
    return True


def _worktree_head(worktree: str | None) -> str | None:
    """The worktree's HEAD commit, or None if it cannot be read."""
    if not worktree:
        return None
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and sha else None


# A runner that ran this long before a transient failure saw a working API:
# the failure starts a new 8-hour window.
TRANSIENT_WINDOW_RESET_S = 2 * 3600


def _reset_budgets_after_progress(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    log_path: Path | None,
    audit_path: Path | None,
) -> None:
    """Give a failure that follows real progress fresh infra/transient budgets.

    Progress is a commit since the previous failure, not activity (every
    restarted session writes its activity log). Without a commit the caps
    stay, so a crash loop or a long outage still blocks. The logic-retry
    budget (brain-diagnosed) is never reset here.
    """
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    head = _worktree_head(stage.worktree)
    committed = bool(head and stage.last_failure_head and head != stage.last_failure_head)
    long_run = (
        stage.started_at is not None
        and stage.first_transient_failure_ts is not None
        and stage.first_transient_failure_ts < stage.started_at
        and (datetime.now(timezone.utc) - stage.started_at).total_seconds()
        > TRANSIENT_WINDOW_RESET_S
    )
    if head:
        stage.last_failure_head = head
    if not (committed or long_run):
        return
    if stage.infra_retries or stage.transient_retries or stage.first_transient_failure_ts:
        _log(
            "RETRY_BUDGET_RESET",
            f"Run {run.name} stage {stage.name}: "
            + ("new commits since the last failure" if committed else "ran for hours since")
            + ", infra/transient retry budgets reset",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
        )
    if committed:
        stage.infra_retries = 0
    stage.transient_retries = 0
    stage.first_transient_failure_ts = None


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

    failure_type = classify_failure(
        fname,
        stage.last_exit_code,
        stage,
        [_runner_fail_log(storage, state.project_name, fname)],
    )
    _reset_budgets_after_progress(state, run_idx, stage_idx, log_path, audit_path)

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
        await _back_off_and_restart(
            state, run_idx, stage_idx, storage, "API error", log_path, audit_path
        )
        return

    # Logic failures: brain diagnosis decides RETRY or BLOCK, cap at max_retries
    retries = stage.retries
    if retries >= config.max_retries:
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

    prompt = brain.render_prompt(
        "diagnose-failure",
        _failure_context(state, run_idx, stage_idx, storage, retries, config.max_retries),
    )
    _log(
        "BRAIN_CALL",
        f"diagnose-failure for {fname}",
        log_path,
        audit_path,
        type="diagnose-failure",
        run=run_idx,
        stage=stage_idx,
    )
    reply = await brain.ask(
        prompt, model=resolve_model("sonnet"), timeout_s=BRAIN_DIAGNOSE_TIMEOUT_S
    )
    brain.log_brain_call(
        storage.brain_calls_dir(state.project_name),
        "diagnose-failure", run_idx, stage_idx, prompt, reply,
    )

    if not reply.ok:
        # The diagnosis itself failed (API down, CLI error, timeout): that says
        # nothing about the stage, so wait like for an API error instead of
        # spending a retry or blocking.
        await _back_off_and_restart(
            state, run_idx, stage_idx, storage,
            f"failure diagnosis unavailable ({reply.detail})", log_path, audit_path,
        )
        return

    action, explanation = brain.parse_action(reply.text, ("RETRY", "BLOCK"))
    if action is None:
        # No verdict in the reply. Retries are capped, so default to trying again.
        action = "RETRY"
        explanation = reply.text

    _log(
        "FAILURE",
        f"Run {run.name} stage {stage.name}: {action}"
        + (f" — {explanation[:500]}" if explanation else ""),
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        retries=retries,
        max_retries=config.max_retries,
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
    stage.last_error = None
    stage.spec_failures = 0

    if current_status in (StageStatus.FAILED, StageStatus.BLOCKED):
        # Determine where to restart based on how far the stage got on disk.
        # If run.sh exists AND is valid, the spec phase completed — restart from runner.
        # Otherwise, consult PROGRESS.md to re-enter the pipeline at the correct phase.
        wt = stage.worktree
        run_sh = Path(wt) / "docs" / fname / "run.sh" if wt else None
        run_sh_valid = run_sh is not None and _manifest_problem(run_sh) is None
        if run_sh_valid:
            exit_file = _runner_exit_file(storage, state.project_name, fname)
            exit_file.unlink(missing_ok=True)
            _runner_fail_log(storage, state.project_name, fname).unlink(missing_ok=True)
            stage.status = StageStatus.GENERATED
        else:
            # run.sh missing or malformed — delete it and consult speccer progress
            # so we resume at the right spec phase instead of blindly re-running
            # `speccer generate` (which errors if the spec is still at INIT).
            if run_sh and run_sh.exists():
                run_sh.unlink()
            _speccer_exit_file(storage, state.project_name, f"{fname}-gen").unlink(missing_ok=True)
            _speccer_exit_file(storage, state.project_name, fname).unlink(missing_ok=True)

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
        exit_file = _speccer_exit_file(storage, state.project_name, fname)
        exit_file.unlink(missing_ok=True)
        stage.status = StageStatus.SPEC_INIT

    elif current_status in (StageStatus.EXECUTING, StageStatus.STALLED):
        exit_file = _runner_exit_file(storage, state.project_name, fname)
        exit_file.unlink(missing_ok=True)
        _runner_fail_log(storage, state.project_name, fname).unlink(missing_ok=True)
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


def _waiting_reason(
    state: ConductorState,
    run: RunState,
    overnight: bool,
    now: datetime,
) -> str | None:
    """Why *run* has nothing to do right now, or None if advancing it would act.

    "executing": a runner is working in its tmux window; only monitoring is due.
    "backoff":   the stage waits out a transient-failure backoff.
    "human":     speccer asked questions and nobody auto-answers them.
    """
    if run.current_stage >= len(run.stages):
        return None
    stage = run.stages[run.current_stage]
    if stage.backoff_until is not None and now < stage.backoff_until:
        return "backoff"
    if stage.status == StageStatus.EXECUTING:
        return "executing"
    if stage.status == StageStatus.SPEC_NEEDS_INPUT and _awaits_human(run, stage, overnight):
        return "human"
    return None


def _questions_waiting_message(
    state: ConductorState, run: RunState, overnight: bool
) -> str:
    """How a human answers a stage's speccer questions and lets conductor go on."""
    stage = run.stages[run.current_stage]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree or "<worktree>"
    questions = Path(wt) / "docs" / fname / "spec" / "QUESTIONS.md"
    continue_cmd = f"`{_SPECCER_BIN} run --feature {fname} --project-dir {wt} --continue`"
    if overnight:
        return (
            f"Run {run.name} stage {stage.name}: still asking questions after "
            f"{stage.question_rounds} automatic answer rounds. Review {questions}, "
            f"then run {continue_cmd} and re-run conductor."
        )
    return (
        f"Run {run.name} stage {stage.name}: answer the questions in {questions} "
        "(lines starting with '> ') and re-run conductor, or re-run it without "
        "--no-overnight to have them answered automatically."
    )


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
        try:
            create_worktree(
                state, run_idx, stage_idx, storage, log_path=log_path, audit_path=audit_path
            )
        except DependencyMergeError as exc:
            # Retrying cannot resolve a conflict between dependencies; block
            # until someone commits the merge in the worktree.
            stage.last_error = str(exc)
            stage.status = StageStatus.BLOCKED
            _log(
                "FAILURE",
                f"Run {run.name} stage {stage.name}: {exc}. Commit the merge in "
                f"{stage.worktree} by hand; the stage resumes from there on restart.",
                log_path,
                audit_path,
                run=run_idx,
                stage=stage_idx,
                reason="dependency_conflict",
            )
            atomic_save(state, storage.conductor_state(state.project_name))
            return
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
        progress_file = _progress_file_path(state, run_idx, stage_idx)
        if (
            progress_file is not None
            and progress_file.exists()
            and _read_progress_status(progress_file) in ("COMPLETE", "GENERATED")
        ):
            # Finished outside conductor (`speccer run --continue` by hand).
            sync_speccer_status(state, run_idx, stage_idx, storage, log_path, audit_path)
            atomic_save(state, storage.conductor_state(state.project_name))
            return
        overnight = _overnight(config, state)
        if _awaits_human(run, stage, overnight):
            # Nothing to do until someone answers; the main loop ends with
            # needs_input once no other run has work left.
            return
        if overnight and not _has_answers(_questions_file(run, stage)):
            answered = await answer_questions(
                state,
                run_idx,
                stage_idx,
                storage,
                log_path=log_path,
                audit_path=audit_path,
            )
            if not answered:
                atomic_save(state, storage.conductor_state(state.project_name))
                return
        if overnight:
            stage.question_rounds += 1
        await run_speccer_continue(
            state, run_idx, stage_idx, tmux, storage, log_path, audit_path
        )
        fname = run.name + stage.feature_suffix
        speccer_exit_code_handler(
            state, run_idx, stage_idx, fname, storage, log_path, audit_path
        )
        atomic_save(state, storage.conductor_state(state.project_name))
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
        if _runner_outcome(state, run_idx, stage_idx, storage) == 0:
            # The runner finished after all; its result stands.
            _finish_runner(state, run_idx, stage_idx, 0, storage, log_path, audit_path)
            atomic_save(state, storage.conductor_state(state.project_name))
            return
        # BLOCKED means nothing runs: never tear containers down under a runner.
        await _stop_runner(
            tmux,
            _runner_window(run_idx, stage),
            _runner_exit_file(storage, state.project_name, run.name + stage.feature_suffix),
            "stage blocked",
            log_path,
            audit_path,
            run=run_idx,
            stage=stage_idx,
        )
        run.status = RunStatus.BLOCKED
        _teardown_run_containers(run, preset, log_path, audit_path)
        atomic_save(state, storage.conductor_state(state.project_name))
        return


# Consecutive advance_run exceptions tolerated on an EXECUTING stage before
# the orchestrator stops trusting its monitoring of that runner.
MAX_MONITOR_ERRORS = 3


def _record_advance_error(
    state: ConductorState,
    run_idx: int,
    exc: BaseException,
    storage: StorageResolver,
    log_path: Path | None,
    audit_path: Path | None,
    *,
    consecutive: int,
) -> None:
    """Turn an exception out of advance_run into a failed stage, not a dead loop.

    The stage goes to FAILED with the error recorded, so handle_failure's
    normal retry/BLOCK budget applies (a git or CLI error raised from one run
    must not stop the orchestration of every run, and must not re-raise on
    every resume). If the stage was already FAILED or STALLED, handle_failure
    itself raised: the stage is BLOCKED directly, otherwise the loop would
    retry the same crash without ever spending a retry. An EXECUTING stage
    keeps its runner (it lives in its own tmux window and reports through its
    exit file) until monitoring has failed MAX_MONITOR_ERRORS times in a row.
    """
    run = state.runs[run_idx]
    error = f"{type(exc).__name__}: {exc}"
    tb = "".join(traceback.format_exception(exc))
    stage_idx = run.current_stage
    stage = run.stages[stage_idx] if stage_idx < len(run.stages) else None

    if stage is None:
        # Post-run step of a finished run raised. Block the run; auto-resume
        # on the next start re-activates it to repeat the post-run step.
        run.status = RunStatus.BLOCKED
        outcome = "run blocked"
    elif stage.status == StageStatus.EXECUTING and consecutive < MAX_MONITOR_ERRORS:
        outcome = f"still executing (monitor error {consecutive}/{MAX_MONITOR_ERRORS})"
    elif stage.status in (StageStatus.FAILED, StageStatus.STALLED, StageStatus.BLOCKED):
        stage.last_error = error
        stage.status = StageStatus.BLOCKED
        outcome = "stage blocked (failure handling itself raised)"
    else:
        stage.last_error = error
        stage.status = StageStatus.FAILED
        outcome = "stage failed"

    _log(
        "FAILURE",
        f"Run {run.name}: conductor error while advancing — {error} ({outcome})",
        log_path,
        audit_path,
        run=run_idx,
        stage=stage_idx,
        reason="advance_error",
        error=error,
        traceback=tb[-4000:],
    )
    try:
        atomic_save(state, storage.conductor_state(state.project_name))
    except Exception as save_exc:
        _log(
            "FAILURE",
            f"Could not save state after advance error: {save_exc}",
            log_path,
            audit_path,
        )


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


_LEARNINGS_HEADER = "## Conductor Learnings"
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "vendor", "__pycache__", "dist", "build"}
LEARNINGS_TIMEOUT_S = 600.0


def _claude_md_files(root: Path) -> list[Path]:
    """Every CLAUDE.md in the tree (root, .claude/, subdirectories), once each."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        if "CLAUDE.md" in filenames:
            found.append(Path(dirpath) / "CLAUDE.md")
    return sorted(found)


def _parse_learning_blocks(text: str) -> dict[str, str]:
    """``<<<FILE: path>>> ... <<<END>>>`` blocks, merged per path, in order."""
    blocks: dict[str, list[str]] = {}
    for path, body in re.findall(r"<<<FILE:\s*(.+?)>>>(.*?)<<<END>>>", text, re.DOTALL):
        body = body.strip()
        if body:
            blocks.setdefault(path.strip(), []).append(body)
    return {path: "\n".join(bodies) for path, bodies in blocks.items()}


def _with_learnings(existing: str, addition: str) -> str:
    """*existing* CLAUDE.md text with *addition* in its Conductor Learnings section.

    Lines already in the file are dropped, and an existing section is
    extended instead of a second section being appended.
    """
    present = {line.strip() for line in existing.splitlines() if line.strip()}
    present.add(_LEARNINGS_HEADER)
    new_lines = [ln for ln in addition.splitlines() if ln.strip() and ln.strip() not in present]
    if not new_lines:
        return existing
    lines = existing.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _LEARNINGS_HEADER)
    except StopIteration:
        body = existing.rstrip("\n")
        prefix = f"{body}\n\n" if body else ""
        return f"{prefix}{_LEARNINGS_HEADER}\n\n" + "\n".join(new_lines) + "\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = new_lines
    return "\n".join(lines) + "\n"


def _commit_paths(root: Path, rels: list[str], message: str) -> bool:
    """Commit just *rels* in the git checkout at *root*; False if git refused."""
    for cmd in (["git", "add", "--", *rels], ["git", "commit", "-q", "-m", message, "--", *rels]):
        if subprocess.run(cmd, cwd=root, capture_output=True, text=True).returncode != 0:
            return False
    return True


def _learnings_target(state: ConductorState) -> tuple[RunState, Path] | None:
    """Worktree of the most recently finished run: its branch carries the edits."""
    done = [
        r for r in state.runs
        if r.status == RunStatus.DONE and r.stages and r.stages[-1].worktree
        and Path(r.stages[-1].worktree).is_dir()
    ]
    if not done:
        return None
    min_ts = datetime.min.replace(tzinfo=timezone.utc)
    run = max(done, key=lambda r: r.stages[-1].completed_at or min_ts)
    return run, Path(run.stages[-1].worktree)


async def _review_learnings(
    state: ConductorState,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
    *,
    force: bool = False,
) -> None:
    """Fold the runs' LEARNINGS.md into the project's CLAUDE.md files.

    The edits are made in the worktree of the most recently finished run and
    committed on its branch, never in the user's own checkout. Only existing
    CLAUDE.md files inside that worktree can be edited (or a root CLAUDE.md
    created when there is none). An unchanged set of learnings is reviewed
    once, unless *force*.
    """
    import hashlib as _hashlib

    _log("POST_RUN", "Reviewing learnings for CLAUDE.md updates", log_path, audit_path)

    learnings_parts: list[str] = []
    for run in state.runs:
        for stage in run.stages:
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
    digest = _hashlib.sha256("\n\n".join(learnings_parts).encode()).hexdigest()
    if state.learnings_reviewed == digest and not force:
        _log("POST_RUN", "Learnings unchanged since the last review — skipping", log_path, audit_path)
        return
    target = _learnings_target(state)
    if target is None:
        _log("POST_RUN", "No finished run worktree to record learnings in — skipping",
             log_path, audit_path)
        return
    target_run, root = target
    root = root.resolve()

    existing = _claude_md_files(root)
    if existing:
        claudemd_section = "\n\n".join(
            f"### FILE: {p.relative_to(root)}\n{p.read_text(encoding='utf-8')}" for p in existing
        )
    else:
        claudemd_section = (
            "(No CLAUDE.md files exist yet. You may create one at the repository "
            "root: use the path CLAUDE.md.)"
        )
    prompt = brain.render_prompt(
        "review-learnings",
        "## Learnings from Completed Run(s)\n\n"
        + "\n\n".join(learnings_parts)
        + "\n\n## Existing CLAUDE.md Files\n\n"
        + claudemd_section,
    )
    reply = await brain.ask(prompt, model=resolve_model("sonnet"), timeout_s=LEARNINGS_TIMEOUT_S)
    if not reply.ok:
        _log("POST_RUN", f"Learnings review failed: {reply.detail}", log_path, audit_path)
        return

    allowed = set(existing) or {root / "CLAUDE.md"}
    changed: list[Path] = []
    for rel, addition in _parse_learning_blocks(reply.text).items():
        candidate = Path(rel)
        target_file = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if target_file not in allowed:
            _log(
                "POST_RUN",
                f"Learnings review: ignoring {rel} (not an existing CLAUDE.md in the project)",
                log_path,
                audit_path,
                file=rel,
            )
            continue
        before = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
        after = _with_learnings(before, addition)
        if after != before:
            target_file.write_text(after, encoding="utf-8")
            changed.append(target_file)

    state.learnings_reviewed = digest
    atomic_save(state, storage.conductor_state(state.project_name))
    if not changed:
        _log("POST_RUN", "Learnings review: no changes needed", log_path, audit_path)
        return

    rels = [str(p.relative_to(root)) for p in changed]
    branch = target_run.stages[-1].branch or "(detached)"
    committed = _commit_paths(root, rels, "Record conductor learnings in CLAUDE.md")
    _log(
        "POST_RUN",
        (
            f"Learnings committed on {branch} ({', '.join(rels)}) in {root}; "
            "push the branch to add them to its PR"
            if committed
            else f"Learnings written to {', '.join(rels)} in {root} but not committed"
        ),
        log_path,
        audit_path,
        files=rels,
        branch=branch,
        committed=committed,
    )


AUDIT_TIMEOUT_S = 600.0


def _audit_context(
    state: ConductorState, storage: StorageResolver, log_path: Path | None
) -> str:
    """What the overnight audit needs: outcomes, log, auto-answers, diagnoses."""
    import json as _json

    summary = {
        "project_name": state.project_name,
        "integration": state.integration.model_dump(mode="json") if state.integration else None,
        "runs": [
            {
                "name": r.name,
                "status": str(r.status),
                "current_stage": r.current_stage,
                "stages": [
                    {
                        "name": s.name,
                        "status": str(s.status),
                        "retries": s.retries,
                        "infra_retries": s.infra_retries,
                        "transient_retries": s.transient_retries,
                        "question_rounds": s.question_rounds,
                        "started_at": s.started_at.isoformat() if s.started_at else None,
                        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                        "last_error": s.last_error,
                    }
                    for s in r.stages
                ],
            }
            for r in state.runs
        ],
    }
    parts = ["## Run State\n" + _json.dumps(summary, indent=2)]

    if log_path is not None and log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        parts.append("## Conductor Log (last 150 lines)\n" + "\n".join(lines[-150:]))

    calls_dir = storage.brain_calls_dir(state.project_name)
    for kind, keep in (("answer-questions", 8), ("diagnose-*", 5)):
        files = sorted(calls_dir.glob(f"{kind}-*.json"), key=lambda p: p.stat().st_mtime)
        for call in files[-keep:]:
            try:
                data = _json.loads(call.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            question = data.get("questions")
            parts.append(
                f"## Brain call {call.name}\n"
                + (f"### Questions\n{str(question)[:3000]}\n### Answer\n" if question else "")
                + str(data.get("response", ""))[:3000]
            )

    for run in state.runs:
        for stage in run.stages:
            if not stage.worktree:
                continue
            fname = run.name + stage.feature_suffix
            docs = Path(stage.worktree) / "docs" / fname
            learnings = docs / "LEARNINGS.md"
            if learnings.is_file():
                parts.append(f"## Learnings: {fname}\n" + _tail(learnings, 80))
            activity = docs / "activity.log"
            if activity.is_file():
                parts.append(f"## Activity Log: {fname} (last 30 lines)\n" + _tail(activity, 30))
    return "\n\n".join(parts)


async def _generate_audit_report(
    state: ConductorState,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Write an audit report of the run (overnight-audit-prompt.md) to the conductor dir."""
    _log("POST_RUN", "Generating overnight audit report", log_path, audit_path)

    prompt = brain.render_prompt("overnight-audit", _audit_context(state, storage, log_path))
    reply = await brain.ask(prompt, model=resolve_model("sonnet"), timeout_s=AUDIT_TIMEOUT_S)
    if not reply.ok:
        _log("POST_RUN", f"Audit report failed: {reply.detail}", log_path, audit_path)
        return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_file = storage.conductor_dir(state.project_name) / f"OVERNIGHT-AUDIT-{ts}.md"
    report_file.write_text(reply.text + "\n", encoding="utf-8")

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
    tmux: _tmux_module.TmuxManager | None = None,
) -> None:
    """Un-block any BLOCKED runs so they resume automatically on restart.

    A blocked stage whose runner has since exited 0 is DONE, and one whose
    runner is still working is watched again; only the others restart.
    """
    any_resumed = False
    for run_idx, run in enumerate(state.runs):
        if run.status != RunStatus.BLOCKED:
            continue
        if run.current_stage >= len(run.stages):
            # Every stage is done; only the post-run step failed. Redo it.
            run.status = RunStatus.ACTIVE
            _log(
                "AUTO_RESUME",
                f"Run {run.name}: all stages done, repeating post-run step",
                log_path,
                audit_path,
                run=run_idx,
            )
            any_resumed = True
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
        si = run.current_stage
        if _runner_outcome(state, run_idx, si, storage) == 0:
            stage.status = StageStatus.EXECUTING
            _finish_runner(state, run_idx, si, 0, storage, log_path, audit_path)
            run.status = RunStatus.ACTIVE
            _log(
                "AUTO_RESUME",
                f"Run {run.name} stage {stage.name}: runner finished with exit 0 "
                "after the stage was blocked; keeping its result",
                log_path,
                audit_path,
                run=run_idx,
                stage=si,
            )
            any_resumed = True
            continue
        window = _runner_window(run_idx, stage)
        if (
            tmux is not None
            and stage.started_at is not None
            and await tmux.is_window_alive(window)
            and not await tmux.is_runner_idle(window)
        ):
            stage.status = StageStatus.EXECUTING
            run.status = RunStatus.ACTIVE
            _log(
                "AUTO_RESUME",
                f"Run {run.name} stage {stage.name}: runner still running in "
                f"{window}, watching it again",
                log_path,
                audit_path,
                run=run_idx,
                stage=si,
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
    - Round-robin advance_run for all active runs (an exception fails that
      run's stage, see _record_advance_error)
    - Sleeps when no active run can act now: runners executing, stages in
      backoff (wakes at the earliest expiry), questions waiting for a human
    - Exits with reason "needs_input" when every active run waits for a human
    - After loop: calls conductor_post_run (not for "needs_input")
    """
    project_root = config.project_root or Path.cwd()
    storage = StorageResolver(project_root)
    log_path = storage.conductor_log(state.project_name)
    audit_path = storage.conductor_audit(state.project_name)
    preset = load_preset(state.preset, project_root)

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

    overnight = _overnight(config, state)
    _log(
        "CONDUCTOR_START",
        f"{state.project_name}{' (overnight)' if overnight else ''}",
        log_path,
        audit_path,
    )

    # Auto-resume any BLOCKED runs from a previous session
    await _auto_resume_blocked_runs(state, storage, log_path, audit_path, tmux=tmux)

    exit_reason = "unknown"
    advance_errors: dict[int, int] = {}  # run index -> consecutive exceptions
    # Only iterations that did work count against max_iterations: waiting for
    # runners or backoffs is paced by sleeps and must not end an overnight run.
    busy_iterations = 0

    while True:
        if busy_iterations >= config.max_iterations:
            exit_reason = "max_iterations"
            break
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
                        f"Single-run PR: {pr_url}",
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
        for run_idx in active_run_indices:
            try:
                await advance_run(
                    state, run_idx, tmux, storage, config, preset, log_path, audit_path
                )
            except Exception as exc:
                advance_errors[run_idx] = advance_errors.get(run_idx, 0) + 1
                _record_advance_error(
                    state, run_idx, exc, storage, log_path, audit_path,
                    consecutive=advance_errors[run_idx],
                )
            else:
                advance_errors.pop(run_idx, None)

        # Pace the loop. Advance again right away only if some run can act now;
        # otherwise sleep until the next monitoring check or the earliest
        # backoff expiry, waking early when a runner writes its exit file.
        now = datetime.now(timezone.utc)
        waiting = {
            run_idx: _waiting_reason(state, state.runs[run_idx], overnight, now)
            if state.runs[run_idx].status == RunStatus.ACTIVE
            else None
            for run_idx in active_run_indices
        }
        if any(reason is None for reason in waiting.values()):
            busy_iterations += 1
            continue

        if all(reason == "human" for reason in waiting.values()):
            # Nothing runs and nothing will change until someone answers.
            for run_idx in waiting:
                _log(
                    "CONDUCTOR_EXIT",
                    _questions_waiting_message(state, state.runs[run_idx], overnight),
                    log_path,
                    audit_path,
                    reason="needs_input",
                    run=run_idx,
                )
            atomic_save(state, storage.conductor_state(state.project_name))
            exit_reason = "needs_input"
            break

        if config.check_interval_s <= 0:
            busy_iterations += 1  # no pacing configured (tests): keep the bound
            continue

        wait_s = config.check_interval_s
        backoffs = [
            state.runs[i].stages[state.runs[i].current_stage].backoff_until
            for i, reason in waiting.items()
            if reason == "backoff"
        ]
        if backoffs:
            until_backoff = (min(backoffs) - now).total_seconds()
            wait_s = min(wait_s, max(1.0, until_backoff))
        executing = "executing" in waiting.values()
        elapsed = 0.0
        while elapsed < wait_s:
            # Fast-poll exit files every 10s so runner completion is noticed
            # quickly; the full advance_run cycle only runs once per wait.
            step = min(10.0, wait_s - elapsed)
            await asyncio.sleep(step)
            elapsed += step
            if executing and _any_runner_exit_file_ready(
                state, storage, session_name=f"conductor-{state.project_name}"
            ):
                break

    if exit_reason == "needs_input":
        # Runs are paused, not finished: no learnings review or audit yet.
        return state

    # Post-run processing
    await conductor_post_run(state, storage, exit_reason, log_path, audit_path)

    return state
