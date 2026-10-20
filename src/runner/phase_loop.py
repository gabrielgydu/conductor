"""Core phase loop — drives Claude through implementation phases.

For each phase:
  1. Build prompt from phase's plan file
  2. Run Claude (steerable or plain)
  3. Monitor output for promise token
  4. On token: stage, run the preset quality gate, commit
  5. Gate or commit hook fails: feed the errors back, retry (capped at
     max_gate_retries)
  6. After all phases: write LEARNINGS.md summary, STATS.json, exit_code
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from pathlib import Path
from typing import Optional

from conductor.core.presets import GateResult, Preset, load_preset
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
    git_snapshot_untracked,
    git_unstage_pre_existing,
    git_unstage_paths,
    git_current_sha,
    git_is_ancestor,
)
from runner.logging import log, success, warn, error, header, dim
from runner.prompt import build_prompt
from runner.steerable import SteerableSession
from runner.steer_inbox import clear_steer_inbox, poll_steer_inbox


# ─── Helpers ────────────────────────────────────────────────────────────────

# Pauses between retries and between phases (patched to 0 in tests).
_RETRY_PAUSE_S = 2.0
_PHASE_PAUSE_S = 3.0

# Completed phases, one JSON object per line, appended as each phase ends.
# A restart without --start-phase resumes after them (see _completed_phases).
PHASES_DONE_FILE = "PHASES-DONE.jsonl"

# Run state the runner writes into its feature dir. The orchestrator places
# that dir inside the worktree (docs/<feature>/), so these are unstaged before
# every commit: they describe the run, they are not part of the feature.
_RUNNER_FILES = (
    "activity.log",
    "STATS.json",
    "exit_code",
    "RUN-CONFIG.json",
    PHASES_DONE_FILE,
    "logs",  # also holds the .pre-untracked-phase-N snapshots
    "steer_inbox",
)


def _runner_owned_paths(project_dir: Path, feature_dir: Path, feature_name: str) -> list[str]:
    """Repo-relative paths that must never be committed by a phase.

    Besides the run state above this is docs/<feature>/LEARNINGS.md. Like the
    speccer's PRD, design and prompt files next to it (kept out of commits by
    the untracked snapshot), it is conductor working material: post-run reads
    it from the worktree and folds it into CLAUDE.md, which is what gets
    committed. Without this it was committed only when Claude happened to
    create it during a phase.
    """
    paths = [(Path("docs") / feature_name / "LEARNINGS.md").as_posix()]
    try:
        rel = feature_dir.resolve().relative_to(project_dir.resolve())
    except ValueError:
        return paths  # feature dir outside the repo (runner CLI default)
    paths.extend((rel / name).as_posix() for name in _RUNNER_FILES)
    return paths


# ─── Phase checkpoint ───────────────────────────────────────────────────────


def _record_phase_done(checkpoint: Path, phase: PhaseConfig, sha: str, committed: bool) -> None:
    record = {
        "phase": phase.number,
        "name": phase.name,
        "token": phase.token,
        "sha": sha,
        "committed": committed,
        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(checkpoint, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _write_checkpoint(checkpoint: Path, records: list[dict]) -> None:
    if not records:
        checkpoint.unlink(missing_ok=True)
        return
    checkpoint.write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")


def _completed_phases(cfg: RunConfig, project_dir: Path, checkpoint: Path) -> list[dict]:
    """Checkpoint records of the leading phases that are done on this branch.

    Every record must still describe this plan (same phase name and token)
    and its commit must still be HEAD or an ancestor of it. Otherwise the
    branch was reset or rebuilt, or the plan regenerated, since the phases
    ran, and nothing is trusted: the run starts again from the first phase.
    """
    try:
        lines = checkpoint.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: dict[int, dict] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn last line after a kill
        if isinstance(record, dict) and isinstance(record.get("phase"), int):
            records[record["phase"]] = record

    phases = {p.number: p for p in cfg.phases}
    for number, record in records.items():
        phase = phases.get(number)
        if (
            phase is None
            or record.get("token") != phase.token
            or record.get("name") != phase.name
            or not git_is_ancestor(project_dir, str(record.get("sha", "")))
        ):
            warn(f"Phase checkpoint {checkpoint.name} does not match this branch or plan "
                 f"(phase {number}) — ignoring it")
            return []

    done = []
    for phase in cfg.phases:
        if phase.number not in records:
            break
        done.append(records[phase.number])
    return done


# Gate output fed back to Claude is capped like local CI output.
_MAX_GATE_FEEDBACK = 50_000


def _gate_fix_context(gate: GateResult) -> str:
    """Turn a failed quality gate into the fix block for the next iteration."""
    failures = gate.failures or [gate.message or "quality gate failed"]
    budget = max(2_000, _MAX_GATE_FEEDBACK // len(failures))
    parts = []
    for failure in failures:
        if len(failure) > budget:
            # Keep the "<check>: exit N" line and the tail, where errors land.
            first, _, rest = failure.partition("\n")
            failure = f"{first}\n... (truncated) ...\n{rest[-budget:]}"
        parts.append(failure)
    return (
        "=== QUALITY GATE FAILED ===\n"
        + "\n\n".join(parts)
        + "\n\nThe runner re-runs these checks before every commit; the phase"
        " is not committed until they pass.\n"
    )


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
            if isinstance(event, dict):
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
    feature_dir: Path | None = None,
) -> tuple[str, str, dict | None]:
    """Run one steerable Claude invocation. Returns (raw_output, assistant_text, result_event).

    If feature_dir is provided, a steer_inbox directory is created inside it and polled
    for incoming .msg files that are forwarded to the session as steering messages.
    """
    session = await SteerableSession.launch(
        prompt,
        model=model,
        max_turns=max_turns,
        cwd=cwd,
        activity_log=activity_log,
    )

    inbox_task: asyncio.Task | None = None
    if feature_dir is not None:
        inbox = feature_dir / "steer_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        inbox_task = asyncio.ensure_future(poll_steer_inbox(inbox, session))

    try:
        assistant_text, result_event = await session.stream_events(
            idle_timeout=idle_timeout,
        )
    finally:
        if inbox_task is not None and not inbox_task.done():
            inbox_task.cancel()
            try:
                await inbox_task
            except (asyncio.CancelledError, Exception):
                pass
        await session.close()

    return assistant_text, assistant_text, result_event


# ─── Single phase execution ──────────────────────────────────────────────────


async def _run_phase(
    phase: PhaseConfig,
    cfg: RunConfig,
    project_dir: Path,
    feature_dir: Path,
    log_dir: Path,
    stats_path: Path,
    preset: Preset,
) -> tuple[bool, bool]:
    """Execute one phase. Returns (success, committed)."""

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

    header(f"PHASE {phase_num} — {phase_name}")
    log(f"Prompt: {prompt_file}")
    log(f"Token:  {phase.token}")
    if model:
        log(f"Model:  {model}")

    phase_start = time.monotonic()
    phase_complete = False
    phase_committed = False
    iteration = 0
    gate_retries = 0
    commit_retries = 0
    fix_context = ""
    fix_label = ""

    # Snapshot pre-existing untracked files so we don't commit them
    pre_untracked = git_snapshot_untracked(project_dir, untracked_snapshot)
    runner_owned = _runner_owned_paths(project_dir, feature_dir, cfg.feature_name)

    cwd_str = str(project_dir)
    max_turns = 200  # generous; Claude controls its own turn budget

    while iteration < cfg.max_iterations:
        iteration += 1
        iter_start = time.monotonic()

        if fix_context:
            log(f"--- Phase {phase_num}, iteration {iteration} ({fix_label}) ---")
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
        result_event = None
        try:
            if cfg.steerable:
                raw_output, assistant_text, result_event = await _run_steerable_iteration(
                    prompt, model, max_turns, cwd_str, activity_log,
                    feature_dir=feature_dir,
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
        if cfg.steerable and result_event:
            usage = result_event.get("usage", {})
            tokens = TokenStats(
                input=usage.get("input_tokens", 0),
                output=usage.get("output_tokens", 0),
                cache_read=usage.get("cache_read_input_tokens", 0),
                cache_write=usage.get("cache_creation_input_tokens", 0),
            )
            cost = result_event.get("total_cost_usd", 0.0)
        else:
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
            await asyncio.sleep(_RETRY_PAUSE_S)
            continue

        # ── Promise token detected ─────────────────────────────────────────
        header(f"PROMISE TOKEN DETECTED — {phase.token}")
        log("Staging changes...")

        git_stage_all(project_dir)
        git_unstage_pre_existing(project_dir, pre_untracked)
        git_unstage_paths(project_dir, runner_owned)

        if not git_has_staged_changes(project_dir):
            # No changes to commit — phase is complete with no-op. The gate is
            # not run: the tree is HEAD, which passed it when the previous
            # phase committed (or is the base branch). Re-testing it would only
            # spend this phase's retries on failures it did not introduce.
            log("No changes to commit — phase complete (no-op)")
            untracked_snapshot.unlink(missing_ok=True)
            phase_complete = True
            break

        # ── Quality gate (preset [[quality_gate.checks]]) ──────────────
        log("Running quality gate...")
        gate = await asyncio.to_thread(preset.quality_gate, project_dir)
        if not gate.passed:
            git_unstage_all(project_dir)
            gate_retries += 1
            summary = "; ".join(f.partition("\n")[0] for f in gate.failures or [gate.message])
            error(f"Quality gate failed (retry {gate_retries}/{cfg.max_gate_retries}): {summary}")
            if gate_retries >= cfg.max_gate_retries:
                error(f"PHASE {phase_num} — quality gate failed {cfg.max_gate_retries} times, giving up")
                error("Last failures:")
                error("\n".join(gate.failures or [gate.message])[-2000:])
                return False, False
            fix_context = _gate_fix_context(gate)
            fix_label = f"quality gate fix {gate_retries}/{cfg.max_gate_retries}"
            await asyncio.sleep(_RETRY_PAUSE_S)
            continue
        success("Quality gate passed")

        # ── Commit ─────────────────────────────────────────────────────
        commit_msg = f"Phase {phase_num}: {phase_name}"
        commit_ok, commit_output = git_commit(project_dir, commit_msg)

        if not commit_ok:
            # Pre-commit hook or other commit failure
            commit_retries += 1
            error(f"Commit rejected (pre-commit hook): {commit_output.strip()}")
            if commit_retries >= cfg.max_gate_retries:
                error(f"PHASE {phase_num} — commit failed {cfg.max_gate_retries} times, giving up")
                return False, False
            fix_context = (
                f"=== COMMIT REJECTED ===\n{commit_output}\n\n"
                "Fix the issues above. The commit was rejected by a pre-commit hook.\n"
                "Do NOT simply bypass the hook — fix the root cause.\n"
            )
            fix_label = f"commit fix {commit_retries}/{cfg.max_gate_retries}"
            await asyncio.sleep(_RETRY_PAUSE_S)
            continue

        success(f"Committed: {commit_msg}")
        untracked_snapshot.unlink(missing_ok=True)
        phase_committed = True

        # ── Local checks (skip in quick mode) ─────────────────────────
        if not cfg.quick and (cfg.local_ci_enabled or cfg.local_review_enabled):
            from runner.local_checks import run_local_checks  # noqa: PLC0415

            checks_ok = await run_local_checks(
                project_dir,
                cfg.feature_name,
                ci_enabled=cfg.local_ci_enabled,
                ci_command=cfg.local_ci_command,
                ci_full_command=cfg.local_ci_full_command,
                ci_max_retries=cfg.local_ci_max_retries,
                review_enabled=cfg.local_review_enabled,
                review_command=cfg.local_review_command,
                review_full_command=cfg.local_review_full_command,
                review_max_retries=cfg.local_review_max_retries,
                fix_model=cfg.fix_model,
                runner_owned=runner_owned,
            )
            if not checks_ok:
                error(f"PHASE {phase_num} FAILED — local checks failed")
                return False, phase_committed

        phase_complete = True
        break

    if not phase_complete:
        error(f"PHASE {phase_num} FAILED — max iterations ({cfg.max_iterations}) reached")
        return False, False

    phase_duration = time.monotonic() - phase_start
    success(f"PHASE {phase_num} COMPLETE after {iteration} iteration(s) ({format_duration(phase_duration)})")
    return True, phase_committed


# ─── Main entry point ────────────────────────────────────────────────────────


async def run_phase_loop(
    cfg: RunConfig,
    feature_dir: Path,
    storage_dir: Optional[Path] = None,
    start_phase: int | None = None,
) -> int:
    """Drive Claude through all phases in cfg.

    Without *start_phase*, resumes after the phases recorded as done in
    feature_dir/PHASES-DONE.jsonl (see _completed_phases); a fresh run starts
    at the first phase.

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
    log(f"Steerable: {'yes' if cfg.steerable else 'no'}")
    if cfg.model:
        log(f"Model: {cfg.model}")

    # Preflight check
    preset = load_preset(cfg.preset, project_dir)
    if not preset.preflight(project_dir):
        error("Preflight check failed — aborting")
        _write_exit_code(feature_dir, 1)
        return 1

    # Steering written for a previous runner process is stale; messages that
    # arrive from now on wait in the inbox for the next live session.
    clear_steer_inbox(feature_dir / "steer_inbox")

    # ── Resume point ───────────────────────────────────────────────────────
    # Restarts (infra/transient/logic retries, auto-resume) relaunch the
    # runner without --start-phase; replaying finished phases costs a Claude
    # session each and can sweep the interrupted phase's leftovers into an
    # earlier phase's commit.
    checkpoint = feature_dir / PHASES_DONE_FILE
    done = _completed_phases(cfg, project_dir, checkpoint)
    if start_phase is None:
        start_phase = next(
            (p.number for p in cfg.phases if p.number not in {r["phase"] for r in done}),
            max((p.number for p in cfg.phases), default=0) + 1,
        )
        if done:
            log(f"Phases {', '.join(str(r['phase']) for r in done)} already done "
                f"(per {checkpoint.name}) — resuming at phase {start_phase}")
    skipped = [r for r in done if r["phase"] < start_phase]
    _write_checkpoint(checkpoint, skipped)

    exit_code = 0
    # Skipped phases count: without their commits a resumed run whose
    # remaining phases change nothing would skip the end-of-run checks.
    any_committed = any(r.get("committed") for r in skipped)

    for phase in cfg.phases:
        if phase.number < start_phase:
            log(dim(f"Skipping phase {phase.number} (before start_phase={start_phase})"))
            continue

        phase_start = time.monotonic()
        phase_ok, phase_committed = await _run_phase(
            phase=phase,
            cfg=cfg,
            project_dir=project_dir,
            feature_dir=feature_dir,
            log_dir=log_dir,
            stats_path=stats_path,
            preset=preset,
        )
        phase_duration = time.monotonic() - phase_start
        if phase_committed:
            any_committed = True
        if phase_ok:
            _record_phase_done(checkpoint, phase, git_current_sha(project_dir), phase_committed)

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
            await asyncio.sleep(_PHASE_PAUSE_S)

    # ── End-of-run local checks (quick mode: deferred to here) ──────────
    if exit_code == 0 and cfg.quick and not any_committed:
        log("All phases were no-ops — skipping end-of-run checks")
    elif exit_code == 0 and cfg.quick and (cfg.local_ci_enabled or cfg.local_review_enabled):
        from runner.local_checks import run_local_checks  # noqa: PLC0415

        header("END-OF-RUN LOCAL CHECKS (full)")
        checks_ok = await run_local_checks(
            project_dir,
            cfg.feature_name,
            ci_enabled=cfg.local_ci_enabled,
            ci_command=cfg.local_ci_command,
            ci_full_command=cfg.local_ci_full_command,
            ci_max_retries=cfg.local_ci_max_retries,
            review_enabled=cfg.local_review_enabled,
            review_command=cfg.local_review_command,
            review_full_command=cfg.local_review_full_command,
            review_max_retries=cfg.local_review_max_retries,
            fix_model=cfg.fix_model,
            full_mode=True,
            runner_owned=_runner_owned_paths(project_dir, feature_dir, cfg.feature_name),
        )
        if not checks_ok:
            exit_code = 1

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
