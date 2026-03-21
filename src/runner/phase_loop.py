"""Core phase loop — drives Claude through implementation phases.

Port of the main phase loop in ralph/lib/runner.sh:ralph_main().

For each phase:
  1. Build prompt from phase's plan file
  2. Run Claude (steerable or plain)
  3. Monitor output for promise token
  4. On token: run quality_gate from preset
  5. Gate fails: feed errors back, retry (capped at max_gate_retries)
  6. Gate passes: commit, optionally push
  7. After all phases: write LEARNINGS.md summary, STATS.json, exit_code
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from pathlib import Path
from typing import Optional

from conductor.core.claude import run_claude, run_claude_steerable
from conductor.core.presets import load_preset, GateResult
from conductor.core.stats import (
    StatsEntry,
    TokenStats,
    extract_stats,
    calculate_cost,
    get_pricing,
    record_stats,
    resolve_model,
    format_duration,
    format_tokens,
)

from runner.activity import parse_stream_json_text, append_event_to_activity_log
from runner.config import RunConfig, PhaseConfig
from runner.git_ops import (
    git_stage_all,
    git_unstage_all,
    git_has_staged_changes,
    git_commit,
    git_push,
    git_snapshot_untracked,
    git_unstage_pre_existing,
    git_current_sha,
)
from runner.logging import log, info, success, warn, error, header, dim, bold
from runner.prompt import build_prompt
from runner.steerable import SteerableSession


# ─── Helpers ────────────────────────────────────────────────────────────────


def _promise_tag(token: str) -> str:
    return f"<promise>{token}</promise>"


def _contains_promise(text: str, token: str) -> bool:
    return _promise_tag(token) in text


def _resolve_model(cfg: RunConfig, phase: PhaseConfig) -> str | None:
    """Resolve effective model: phase override > run-level > None."""
    raw = phase.model or cfg.model
    if raw:
        return resolve_model(raw)
    return None


def _write_exit_code(feature_dir: Path, code: int) -> None:
    feature_dir.mkdir(parents=True, exist_ok=True)
    (feature_dir / "exit_code").write_text(str(code), "utf-8")


def _write_learnings_summary(
    project_dir: Path,
    feature_name: str,
    phase_results: list[dict],
) -> None:
    """Append a run summary to docs/<feature>/LEARNINGS.md."""
    learnings_dir = project_dir / "docs" / feature_name
    learnings_dir.mkdir(parents=True, exist_ok=True)
    learnings_file = learnings_dir / "LEARNINGS.md"

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"\n## Runner Run — {ts}\n"]
    for pr in phase_results:
        status = "PASSED" if pr["success"] else "FAILED"
        lines.append(
            f"- Phase {pr['phase_num']} ({pr['phase_name']}): {status}"
            f" — {pr['iterations']} iteration(s), {format_duration(pr['duration_s'])}"
        )
    lines.append("")

    with open(learnings_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─── Gate evaluation ────────────────────────────────────────────────────────


def _run_quality_gate(
    cfg: RunConfig,
    cwd: Path,
) -> tuple[bool, str]:
    """Run the preset quality gate. Returns (passed, failure_context)."""
    preset = load_preset(cfg.preset)
    result: GateResult = preset.quality_gate(cwd)
    if result.passed:
        return True, ""

    parts = [result.message] if result.message else []
    parts.extend(result.failures)
    context = "\n".join(parts)
    return False, context


# ─── Plain (non-steerable) phase iteration ──────────────────────────────────


async def _run_plain_iteration(
    prompt: str,
    model: str | None,
    max_turns: int,
    cwd: str,
    activity_log: Path,
) -> tuple[str, str]:
    """Run one plain Claude invocation. Returns (full_output_text, assistant_text)."""
    from conductor.core.claude import run_claude as _run_claude

    result = await _run_claude(
        prompt,
        model=model,
        max_turns=max_turns,
        cwd=cwd,
    )

    # Write events to activity log
    for line in result.output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            append_event_to_activity_log(activity_log, event)
        except (json.JSONDecodeError, ValueError):
            pass

    assistant_text = parse_stream_json_text(result.output)
    return result.output, assistant_text


# ─── Steerable phase iteration ───────────────────────────────────────────────


async def _run_steerable_iteration(
    prompt: str,
    model: str | None,
    max_turns: int,
    cwd: str,
    activity_log: Path,
    idle_timeout: float = 600.0,
) -> tuple[str, str]:
    """Run one steerable Claude invocation. Returns (raw_output, assistant_text)."""
    session = await SteerableSession.launch(
        prompt,
        model=model,
        max_turns=max_turns,
        cwd=cwd,
        activity_log=activity_log,
    )
    try:
        assistant_text, _result_event = await session.stream_events(
            idle_timeout=idle_timeout,
        )
    finally:
        await session.close()

    # For steerable, we don't have a raw stream-json buffer; return text directly.
    # We still return a consistent tuple — raw_output is the same as assistant_text here.
    return assistant_text, assistant_text


# ─── Single phase execution ──────────────────────────────────────────────────


async def _run_phase(
    phase: PhaseConfig,
    cfg: RunConfig,
    project_dir: Path,
    feature_dir: Path,
    log_dir: Path,
    stats_path: Path,
) -> bool:
    """Execute one phase. Returns True on success."""

    phase_num = phase.number
    phase_count = len(cfg.phases)
    phase_name = phase.name
    model = _resolve_model(cfg, phase)

    learnings_file = project_dir / "docs" / cfg.feature_name / "LEARNINGS.md"
    prompt_file = Path(phase.prompt_file)
    if not prompt_file.is_absolute():
        prompt_file = project_dir / phase.prompt_file

    activity_log = feature_dir / "activity.log"
    untracked_snapshot = log_dir / f".pre-untracked-phase-{phase_num}"

    preset = load_preset(cfg.preset)

    header(f"PHASE {phase_num} — {phase_name}")
    log(f"Prompt: {prompt_file}")
    log(f"Token:  {phase.token}")
    if model:
        log(f"Model:  {model}")

    phase_start = time.monotonic()
    phase_complete = False
    iteration = 0
    gate_retries = 0
    fix_context = ""

    # Snapshot pre-existing untracked files so we don't commit them
    pre_untracked = git_snapshot_untracked(project_dir, untracked_snapshot)

    cwd_str = str(project_dir)
    max_turns = 200  # generous; Claude controls its own turn budget

    while iteration < cfg.max_iterations:
        iteration += 1
        iter_start = time.monotonic()

        if fix_context:
            log(f"--- Phase {phase_num}, iteration {iteration} (GATE FIX retry {gate_retries}/{cfg.max_gate_retries}) ---")
        else:
            log(f"--- Phase {phase_num}, iteration {iteration} / {cfg.max_iterations} ---")

        prompt = build_prompt(
            prompt_file=prompt_file,
            promise_token=phase.token,
            phase_num=phase_num,
            phase_count=phase_count,
            project_dir=project_dir,
            feature_name=cfg.feature_name,
            fix_context=fix_context,
            learnings_file=learnings_file,
            prompt_extra=preset.build_prompt_extra(project_dir),
        )

        # ── Invoke Claude ──────────────────────────────────────────────────
        try:
            if cfg.steerable:
                raw_output, assistant_text = await _run_steerable_iteration(
                    prompt, model, max_turns, cwd_str, activity_log
                )
            else:
                raw_output, assistant_text = await _run_plain_iteration(
                    prompt, model, max_turns, cwd_str, activity_log
                )
        except Exception as exc:
            error(f"Claude invocation failed: {exc}")
            # Don't give up — log and retry on next iteration
            fix_context = ""
            continue

        iter_duration = time.monotonic() - iter_start

        # ── Record stats ───────────────────────────────────────────────────
        tokens = extract_stats(raw_output) if not cfg.steerable else TokenStats()
        pricing = get_pricing(model or "")
        cost = calculate_cost(tokens, pricing)

        entry = StatsEntry(
            type="phase",
            iteration=iteration,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            duration_s=round(iter_duration, 2),
            tokens=tokens,
            cost_usd=round(cost, 6),
            phase=str(phase_num),
            model=model or "",
        )
        try:
            record_stats(stats_path, entry)
        except Exception:
            pass  # stats are non-critical

        # ── Check for promise token ────────────────────────────────────────
        if not _contains_promise(assistant_text, phase.token):
            # No token — restart iteration
            fix_context = ""
            log(f"Iteration {iteration} done — no promise token, restarting...")
            await asyncio.sleep(2)
            continue

        # ── Promise token detected ─────────────────────────────────────────
        log(bold("Promise token detected — staging & running quality gate..."))

        git_stage_all(project_dir)
        git_unstage_pre_existing(project_dir, pre_untracked)

        if not git_has_staged_changes(project_dir):
            # No changes to commit — phase is complete with no-op
            log("No changes to commit — phase complete (no-op)")
            untracked_snapshot.unlink(missing_ok=True)
            phase_complete = True
            break

        gate_passed, gate_failures = _run_quality_gate(cfg, project_dir)

        if gate_passed:
            # ── Commit ─────────────────────────────────────────────────────
            commit_msg = f"Phase {phase_num}: {phase_name}"
            commit_ok, commit_output = git_commit(project_dir, commit_msg)

            if not commit_ok:
                # Pre-commit hook or other commit failure
                gate_retries += 1
                error(f"Commit rejected (pre-commit hook): {commit_output.strip()}")
                if gate_retries >= cfg.max_gate_retries:
                    error(f"PHASE {phase_num} — commit failed {cfg.max_gate_retries} times, giving up")
                    return False
                fix_context = (
                    f"=== COMMIT REJECTED ===\n{commit_output}\n\n"
                    "Fix the issues above. The commit was rejected by a pre-commit hook.\n"
                    "Do NOT simply bypass the hook — fix the root cause.\n"
                )
                await asyncio.sleep(2)
                continue

            success(f"Committed: {commit_msg}")
            untracked_snapshot.unlink(missing_ok=True)

            # ── Push ───────────────────────────────────────────────────────
            if cfg.push_enabled:
                git_push(project_dir, cfg.push_remote)

            phase_complete = True
            break

        else:
            # Gate failed — unstage and retry
            git_unstage_all(project_dir)
            gate_retries += 1
            error(f"Quality gate failed (retry {gate_retries}/{cfg.max_gate_retries})")

            if gate_retries >= cfg.max_gate_retries:
                error(f"PHASE {phase_num} — quality gate failed {cfg.max_gate_retries} times, giving up")
                error("Last failures:")
                error(gate_failures[:2000])
                return False

            fix_context = gate_failures
            log("Restarting iteration with failure context...")
            await asyncio.sleep(2)
            continue

    if not phase_complete:
        error(f"PHASE {phase_num} FAILED — max iterations ({cfg.max_iterations}) reached")
        return False

    phase_duration = time.monotonic() - phase_start
    success(f"PHASE {phase_num} COMPLETE after {iteration} iteration(s) ({format_duration(phase_duration)})")
    return True


# ─── Main entry point ────────────────────────────────────────────────────────


async def run_phase_loop(
    cfg: RunConfig,
    feature_dir: Path,
    storage_dir: Optional[Path] = None,
    start_phase: int = 1,
) -> int:
    """Drive Claude through all phases in cfg.

    Writes exit_code (0/1) to feature_dir when done.
    Returns the exit code.
    """
    project_dir = Path(cfg.project_dir)
    log_dir = feature_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Stats file lives in the feature dir
    stats_path = feature_dir / "STATS.json"

    total_start = time.monotonic()
    phase_results: list[dict] = []

    header(f"{cfg.feature_name.upper()} — Runner")
    log(f"Working directory: {project_dir}")
    log(f"Phases: {len(cfg.phases)}")
    log(f"Preset: {cfg.preset or 'base'}")
    log(f"Push: {'enabled' if cfg.push_enabled else 'disabled'}")
    log(f"Steerable: {'yes' if cfg.steerable else 'no'}")
    if cfg.model:
        log(f"Model: {cfg.model}")

    # Preflight check
    preset = load_preset(cfg.preset)
    if not preset.preflight(project_dir):
        error("Preflight check failed — aborting")
        _write_exit_code(feature_dir, 1)
        return 1

    exit_code = 0

    for phase in cfg.phases:
        if phase.number < start_phase:
            log(dim(f"Skipping phase {phase.number} (before start_phase={start_phase})"))
            continue

        phase_start = time.monotonic()
        phase_ok = await _run_phase(
            phase=phase,
            cfg=cfg,
            project_dir=project_dir,
            feature_dir=feature_dir,
            log_dir=log_dir,
            stats_path=stats_path,
        )
        phase_duration = time.monotonic() - phase_start

        phase_results.append(
            {
                "phase_num": phase.number,
                "phase_name": phase.name,
                "success": phase_ok,
                "iterations": 0,  # approximate — iterations tracked inside _run_phase
                "duration_s": phase_duration,
            }
        )

        if not phase_ok:
            exit_code = 1
            error(f"Phase {phase.number} failed — stopping")
            break

        # Brief pause between phases
        if phase.number < len(cfg.phases):
            await asyncio.sleep(3)

    # ── Teardown ───────────────────────────────────────────────────────────
    try:
        preset.stage_teardown(project_dir)
    except Exception as exc:
        warn(f"Stage teardown raised: {exc}")

    # ── Summary ────────────────────────────────────────────────────────────
    total_duration = time.monotonic() - total_start
    header(f"{cfg.feature_name.upper()} COMPLETE" if exit_code == 0 else f"{cfg.feature_name.upper()} FAILED")
    log(f"Total time: {format_duration(total_duration)}")

    # Print stats summary
    if stats_path.exists():
        try:
            entries = json.loads(stats_path.read_text("utf-8"))
            if isinstance(entries, list) and entries:
                total_input = sum(e.get("tokens", {}).get("input", 0) for e in entries)
                total_output = sum(e.get("tokens", {}).get("output", 0) for e in entries)
                total_cost = sum(e.get("cost_usd", 0) for e in entries)
                log(f"Stats: {len(entries)} iterations")
                log(f"  Input: {format_tokens(total_input)}  Output: {format_tokens(total_output)}")
                log(f"  Total cost: ${total_cost:.4f}")
        except Exception:
            pass

    # ── Write LEARNINGS summary ────────────────────────────────────────────
    try:
        _write_learnings_summary(project_dir, cfg.feature_name, phase_results)
    except Exception as exc:
        warn(f"Could not write LEARNINGS summary: {exc}")

    # ── Write exit_code (MUST be last) ─────────────────────────────────────
    _write_exit_code(feature_dir, exit_code)
    log(f"exit_code written: {exit_code}")

    return exit_code
