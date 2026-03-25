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
from dataclasses import dataclass
from datetime import datetime, timezone
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


@dataclass
class ConductorConfig:
    check_interval_s: float = 900.0
    max_iterations: int = 1000
    max_retries: int = 2
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
        wt_base = worktrees_base or (project_dir.parent / "worktrees")
    else:
        # StorageResolver convention: (state, run_idx, stage_idx, storage)
        storage = storage_or_project_dir
        project_dir = storage.repo_root
        wt_base = project_dir.parent / "worktrees"

    # Branch naming: conductor/{project}/{run}/{stage}
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
    """Spawn speccer init in tmux window and wait for completion."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
    wt = stage.worktree
    mode = stage.spec_mode
    window_name = f"run{run_idx}:{stage.name}"

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
        # The ContextWiring model has sources/targets list fields
        # Try to handle the wiring object — it may have extra attrs in subclasses
        # For now, use getattr to access bash-equivalent fields
        wiring_dict = wiring.model_dump() if hasattr(wiring, "model_dump") else {}
        source_run_i = wiring_dict.get("source_run")
        source_stage_i = wiring_dict.get("source_stage")
        source_path = wiring_dict.get("source_path")
        wiring_type = wiring_dict.get("type")

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
    exit_code = await tmux.spawn_in_window_and_wait(
        window_name, cmd, exit_file=exit_file, cwd=wt
    )

    _log(
        "SPECCER_INVOKE",
        f"speccer init {fname} (mode={mode})",
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
    await tmux.spawn_in_window_and_wait(window_name, cmd, exit_file=exit_file, cwd=wt)

    _log(
        "SPECCER_INVOKE",
        f"speccer run {fname}",
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
    await tmux.spawn_in_window_and_wait(window_name, cmd, exit_file=exit_file, cwd=wt)

    _log(
        "SPECCER_INVOKE",
        f"speccer run --continue {fname}",
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
    rc = await tmux.spawn_in_window_and_wait(window_name, cmd, exit_file=exit_file, cwd=wt)

    _log(
        "SPECCER_INVOKE",
        f"speccer generate {fname}",
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

    # Verify run.sh was actually created
    docs_dir = Path(wt) / "docs" / fname
    if not (docs_dir / "run.sh").exists():
        _log(
            "SPECCER_GENERATE_FAILED",
            f"speccer generate {fname} completed but run.sh not found",
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
    model_match = re.search(r'RALPH_MODEL\s*=\s*"([^"]+)"', text)
    model = model_match.group(1) if model_match else ""
    fix_model_match = re.search(r'RALPH_FIX_MODEL\s*=\s*"([^"]+)"', text)
    fix_model = fix_model_match.group(1) if fix_model_match else model

    cfg = RunConfig(
        feature_name=feature_name,
        project_dir=str(project_dir),
        phases=phase_configs,
        model=model,
        preset=None,  # preset quality gate loaded by name in phase_loop
        push_enabled=preset_config.push_enabled,
        fixer_enabled=preset_config.fixer_enabled and not quick,
        max_iterations=preset_config.max_iterations_per_phase,
        max_gate_retries=preset_config.max_gate_retries,
        steerable=True,
        push_remote=preset_config.push_remote,
        quick=quick,
        local_ci_enabled=preset_config.local_ci_enabled,
        local_ci_command=preset_config.local_ci_command,
        local_ci_full_command=preset_config.local_ci_full_command,
        local_ci_max_retries=preset_config.local_ci_max_retries,
        local_review_enabled=preset_config.local_review_enabled,
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

    try:
        config_path = generate_run_config(
            run_sh, fname, Path(wt), preset.config, quick=quick,
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
        return

    storage_dir = str(docs_dir)
    cmd = f"cd {wt} && {_RUNNER_BIN} run --feature {fname} --storage-dir {storage_dir}"
    await tmux.spawn_runner_in_window(window_name, cmd, exit_file=exit_file, cwd=wt)

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
        # Find activity log by glob
        import glob as _glob

        pattern = f"/tmp/ralph-activity-{fname}*"
        matches = sorted(
            _glob.glob(pattern), key=lambda p: Path(p).stat().st_mtime, reverse=True
        )
        if not matches:
            return None
        activity_log = Path(matches[0])
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

        import glob as _glob

        if stage.status == StageStatus.EXECUTING:
            pattern = f"/tmp/ralph-activity-{fname}*"
            matches = sorted(
                _glob.glob(pattern), key=lambda p: Path(p).stat().st_mtime, reverse=True
            )
            if matches:
                lines = (
                    Path(matches[0])
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
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
        context = "\n\n".join(context_parts)

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
                # Attempt to steer via ralph_steer — best-effort
                try:
                    subprocess.run(
                        ["ralph", "steer", fname, message],
                        capture_output=True,
                        timeout=10,
                    )
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


def _any_runner_exit_file_ready(state: ConductorState) -> bool:
    """Check if any executing stage has a runner exit file, meaning it finished."""
    for run in state.runs:
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
            # Post-runner validation
            from conductor.core.validation import ValidationContext, validate_and_fix  # noqa: PLC0415

            vctx = ValidationContext(
                project_dir=Path(stage.worktree)
                if stage.worktree
                else config.project_root or Path.cwd(),
                stage="post-runner",
                feature_name=fname,
            )
            vresult = await validate_and_fix(vctx, max_attempts=2)
            if vresult.passed:
                stage.status = StageStatus.DONE
                stage.completed_at = datetime.now(timezone.utc)
            else:
                stage.status = StageStatus.FAILED
                _log(
                    "VALIDATION_FAILED",
                    f"Post-runner validation failed for {fname}: {vresult.summary}",
                    log_path,
                    audit_path,
                    run=run_idx,
                    stage=stage_idx,
                )
        else:
            # Read captured pane output if available
            fail_log = Path(f"/tmp/conductor-fail-{fname}.log")
            pane_output = ""
            if fail_log.exists():
                try:
                    pane_output = fail_log.read_text(errors="replace").strip()
                except OSError:
                    pass
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
        else:
            await check_stall(
                state, run_idx, stage_idx, tmux, storage, config, log_path, audit_path
            )


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def handle_failure(
    state: ConductorState,
    run_idx: int,
    stage_idx: int,
    storage: StorageResolver,
    config: ConductorConfig,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """retries >= 2 -> blocked, else brain diagnose-failure call -> RETRY/BLOCK."""
    run = state.runs[run_idx]
    stage = run.stages[stage_idx]
    fname = run.name + stage.feature_suffix
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

    # Build failure context
    context_parts: list[str] = []

    import glob as _glob
    import json as _json

    pattern = f"/tmp/ralph-activity-{fname}*"
    matches = sorted(
        _glob.glob(pattern), key=lambda p: Path(p).stat().st_mtime, reverse=True
    )
    if matches:
        lines = (
            Path(matches[0]).read_text(encoding="utf-8", errors="replace").splitlines()
        )
        context_parts.append(
            "## Activity Log (last 100 lines)\n" + "\n".join(lines[-100:])
        )

    context_parts.append(
        "## Stage Config\n" + _json.dumps(stage.model_dump(), indent=2, default=str)
    )
    context_parts.append(f"## Retries\n{retries} of 2")
    context = "\n\n".join(context_parts)

    _log(
        "BRAIN_CALL",
        f"diagnose-failure for {fname}",
        log_path,
        audit_path,
        type="diagnose-failure",
        run=run_idx,
        stage=stage_idx,
    )

    result = await run_claude(context, model=resolve_model("opus"), max_turns=1)

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

    if current_status in (
        StageStatus.SPEC_INIT,
        StageStatus.SPEC_RUNNING,
        StageStatus.SPEC_NEEDS_INPUT,
        StageStatus.FAILED,
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
# Git push and PR creation
# ---------------------------------------------------------------------------


def build_pr_body(
    state: ConductorState,
    run_idx: int,
    storage: StorageResolver,
    conductor_dir: Path | None = None,
) -> str:
    """Build PR body with description, constitution, stages, and merge order."""
    run = state.runs[run_idx]
    cdir = conductor_dir or storage.conductor_dir(state.project_name)
    parts: list[str] = []

    # Feature description from first stage
    if run.stages:
        first_stage = run.stages[0]
        if first_stage.feature_description_file:
            desc_path = cdir / first_stage.feature_description_file
            if desc_path.exists():
                parts.append(
                    "## Description\n\n" + desc_path.read_text(encoding="utf-8")
                )

    # Constitution
    if run.constitution:
        principles = "\n".join(f"- {c}" for c in run.constitution)
        parts.append("## Constitution\n\n" + principles)

    # Stage list with branches
    stage_lines = []
    for i, stage in enumerate(run.stages):
        stage_lines.append(f"- **{stage.name}** (`{stage.branch}`)")
    parts.append("## Stages\n\n" + "\n".join(stage_lines))

    # Dependency merge order
    if run.depends_on:
        run_by_index = {r.index: r for r in state.runs}
        merge_lines = ["Merge dependencies first:"]
        for dep_idx in run.depends_on:
            dep_run = run_by_index.get(dep_idx)
            if dep_run is None:
                continue
            dep_pr = dep_run.pr_url
            if dep_pr:
                merge_lines.append(f"1. **{dep_run.name}** — {dep_pr}")
            else:
                merge_lines.append(f"1. **{dep_run.name}**")
        parts.append("## Merge Order\n\n" + "\n".join(merge_lines))

    return "\n\n".join(parts)


def push_and_create_pr(
    state: ConductorState,
    run_idx: int,
    storage: StorageResolver,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Push last stage branch and create a draft PR via gh."""
    run = state.runs[run_idx]
    if not run.stages:
        return

    last_stage = run.stages[-1]
    branch = last_stage.branch
    if not branch:
        _log(
            "GIT_PUSH_FAILED",
            f"No branch found for run {run_idx}",
            log_path,
            audit_path,
            run=run_idx,
            reason="no_branch",
        )
        return

    base_branch = state.base_branch or "main"
    project_dir = storage.repo_root

    # Push
    push_result = subprocess.run(
        ["git", "-C", str(project_dir), "push", "-u", "origin", branch],
        capture_output=True,
        text=True,
    )
    if push_result.returncode != 0:
        _log(
            "GIT_PUSH_FAILED",
            f"Push failed for {branch}: {push_result.stderr.strip()}",
            log_path,
            audit_path,
            run=run_idx,
            branch=branch,
        )
        return

    _log(
        "GIT_PUSH", f"Pushed {branch}", log_path, audit_path, run=run_idx, branch=branch
    )

    # Get repo slug
    slug_result = subprocess.run(
        ["git", "-C", str(project_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if slug_result.returncode != 0:
        _log(
            "PR_CREATE_FAILED",
            "Could not determine repo slug",
            log_path,
            audit_path,
            run=run_idx,
            reason="no_repo_slug",
        )
        return

    raw_url = slug_result.stdout.strip()
    # Strip protocol/host and .git suffix
    repo_slug = re.sub(r"^(https?://[^/]+/|git@[^:]+:)", "", raw_url)
    repo_slug = re.sub(r"\.git$", "", repo_slug)

    if not repo_slug:
        _log(
            "PR_CREATE_FAILED",
            "Could not determine repo slug",
            log_path,
            audit_path,
            run=run_idx,
            reason="no_repo_slug",
        )
        return

    body = build_pr_body(state, run_idx, storage)

    pr_result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--head",
            branch,
            "--base",
            base_branch,
            "--title",
            f"feat: {run.name}",
            "--body",
            body,
            "--draft",
        ],
        capture_output=True,
        text=True,
    )

    if pr_result.returncode == 0:
        pr_url = pr_result.stdout.strip()
        run.pr_url = pr_url
        _log(
            "PR_CREATED",
            pr_url,
            log_path,
            audit_path,
            run=run_idx,
            branch=branch,
            base=base_branch,
            pr_url=pr_url,
        )
    else:
        _log(
            "PR_CREATE_FAILED",
            f"gh pr create failed: {pr_result.stdout.strip()} {pr_result.stderr.strip()}",
            log_path,
            audit_path,
            run=run_idx,
            branch=branch,
            base=base_branch,
        )


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def activate_ready_runs(state: ConductorState) -> list[int]:
    """Activate runs whose dependencies are satisfied; block runs whose deps are blocked.

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
        push_and_create_pr(state, run_idx, storage, log_path, audit_path)
        return

    stage = run.stages[stage_idx]
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

    result = await run_claude(prompt, model=resolve_model("opus"), max_turns=1)

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

    context = "\n\n".join(context_parts)
    result = await run_claude(context, model=resolve_model("opus"), max_turns=1)

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

    exit_reason = "unknown"

    for _iteration in range(config.max_iterations):
        active_run_indices = activate_ready_runs(state)

        all_terminal = all(
            r.status in (RunStatus.DONE, RunStatus.BLOCKED) for r in state.runs
        )
        done_count = sum(1 for r in state.runs if r.status == RunStatus.DONE)

        # Integration merge trigger: check whenever all runs are terminal with 2+ done
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
                if _any_runner_exit_file_ready(state):
                    break

    else:
        exit_reason = "max_iterations"

    # Post-run processing
    await conductor_post_run(state, storage, exit_reason, log_path, audit_path)

    return state
