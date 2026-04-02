"""Post-run processing: learnings review and audit report generation."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from conductor.core.claude import run_claude
from conductor.core.enums import IntegrationStatus
from conductor.core.logging import live_log
from conductor.core.models import ConductorState
from conductor.integration.e2e import run_integration_testing
from conductor.integration.merge import run_integration_merge

def _find_codex_companion() -> Path | None:
    """Discover codex-companion.mjs from installed_plugins.json."""
    plugins_file = Path.home() / ".claude/plugins/installed_plugins.json"
    if not plugins_file.exists():
        return None
    try:
        data = json.loads(plugins_file.read_text(encoding="utf-8"))
        for key, entries in data.get("plugins", {}).items():
            if not key.startswith("codex@"):
                continue
            for entry in entries:
                candidate = Path(entry["installPath"]) / "scripts" / "codex-companion.mjs"
                if candidate.exists():
                    return candidate
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _load_prompt_template(name: str) -> str:
    """Load a prompt template from src/conductor/prompts/."""
    template_dir = Path(__file__).parent / "prompts"
    path = template_dir / f"{name}-prompt.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _collect_all_learnings(state: ConductorState, project_dir: Path) -> str:
    """Collect LEARNINGS.md from all worktrees."""
    parts = []
    for run in state.runs:
        for stage in run.stages:
            wt = stage.worktree
            if not wt or not Path(wt).is_dir():
                continue
            suffix = stage.feature_suffix or ""
            fname = f"{run.name}{suffix}"
            lf = Path(wt) / "docs" / fname / "LEARNINGS.md"
            if lf.exists() and lf.stat().st_size > 0:
                parts.append(f"### Run: {run.name} | Stage: {stage.name}")
                parts.append(lf.read_text(encoding="utf-8"))
                parts.append("")
    return "\n".join(parts)


def _collect_claudemd_files(project_dir: Path) -> str:
    """Collect all CLAUDE.md files from the project."""
    parts = []
    for f in sorted(project_dir.rglob(".claude/CLAUDE.md")):
        if "node_modules" in f.parts or ".git" in f.parts:
            continue
        rel = f.relative_to(project_dir)
        parts.append(f"### FILE: {rel}")
        parts.append(f.read_text(encoding="utf-8"))
        parts.append("")
    return "\n".join(parts)


def _extract_text_from_stream_json(output: str) -> str:
    """Extract assistant text blocks from stream-json output."""
    text_parts = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block["text"])
    return "\n".join(text_parts)


async def review_learnings(
    state: ConductorState,
    project_dir: Path,
    storage=None,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> int:
    """Review learnings and update CLAUDE.md files. Returns count of updated files."""
    live_log("PLAN", "Reviewing learnings for CLAUDE.md updates",
             log_path=log_path, audit_path=audit_path)

    learnings = _collect_all_learnings(state, project_dir)
    if not learnings.strip():
        live_log("PLAN", "No learnings found — skipping review",
                 log_path=log_path, audit_path=audit_path)
        return 0

    claudemd_files = _collect_claudemd_files(project_dir)
    if not claudemd_files.strip():
        claudemd_files = "(No CLAUDE.md files exist yet — create .claude/CLAUDE.md if learnings warrant it)"

    context = (
        f"## Learnings from Completed Run(s)\n\n{learnings}\n"
        f"## Existing CLAUDE.md Files\n\n{claudemd_files}\n"
    )

    template = _load_prompt_template("review-learnings")
    prompt = template.replace("{CONTEXT}", context)

    result = await run_claude(prompt, model="claude-sonnet-4-6", max_turns=1)
    response_text = _extract_text_from_stream_json(result.output)

    if "<<<NO_CHANGES>>>" in response_text:
        live_log("PLAN", "Learnings review: no changes needed",
                 log_path=log_path, audit_path=audit_path)
        return 0

    # Parse <<<FILE: path>>>...<<<END>>> blocks
    updates_applied = 0
    file_blocks = re.findall(
        r"<<<FILE:\s*(.+?)>>>(.*?)<<<END>>>",
        response_text,
        re.DOTALL,
    )

    for file_path_str, content in file_blocks:
        file_path_str = file_path_str.strip()
        full_path = project_dir / file_path_str
        content = content.strip()
        if not content:
            continue
        full_path.parent.mkdir(parents=True, exist_ok=True)
        if full_path.exists():
            with open(full_path, "a", encoding="utf-8") as f:
                f.write(f"\n## Conductor Learnings\n\n{content}\n")
            live_log("PLAN", f"Updated {file_path_str} with learnings",
                     log_path=log_path, audit_path=audit_path)
        else:
            full_path.write_text(f"## Conductor Learnings\n\n{content}\n", encoding="utf-8")
            live_log("PLAN", f"Created {file_path_str} with learnings",
                     log_path=log_path, audit_path=audit_path)
        updates_applied += 1

    if updates_applied > 0:
        subprocess.run(
            ["git", "add", "-A", "--", "*/.claude/CLAUDE.md", ".claude/CLAUDE.md"],
            cwd=project_dir, capture_output=True,
        )
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_dir, capture_output=True,
        )
        if diff_result.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "chore: add conductor learnings to CLAUDE.md"],
                cwd=project_dir, capture_output=True, text=True,
            )
            live_log("PLAN", "Committed CLAUDE.md updates",
                     log_path=log_path, audit_path=audit_path)

    live_log("PLAN", f"Learnings review complete — {updates_applied} file(s) updated",
             log_path=log_path, audit_path=audit_path)
    return updates_applied


def _collect_audit_context(
    state: ConductorState,
    project_dir: Path,
    storage,
) -> str:
    """Build rich context for the audit report."""
    parts = []

    # State summary
    state_summary = state.model_dump_json(indent=2)
    parts.append(f"## Run State\n{state_summary}\n")

    # Conductor log (last 100 lines)
    if storage is not None and hasattr(storage, "conductor_log"):
        log_path = storage.conductor_log(state.project_name)
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            parts.append("## Conductor Log (last 100 lines)\n" + "\n".join(lines[-100:]) + "\n")

    # Brain calls — last 5 answer-questions + last 3 diagnose
    if storage is not None and hasattr(storage, "brain_calls_dir"):
        brain_dir = storage.brain_calls_dir(state.project_name)
        if brain_dir.exists():
            aq_files = sorted(
                brain_dir.glob("answer-questions-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:5]
            for call_file in aq_files:
                try:
                    raw = call_file.read_text(encoding="utf-8")
                    # Files may be stream-json (NDJSON) or a single JSON object
                    text = _extract_text_from_stream_json(raw)
                    if not text:
                        # Try as plain JSON with a "response" key
                        data = json.loads(raw)
                        text = data.get("response", "")
                    if text:
                        parts.append(f"## Auto-Answered Questions ({call_file.name})\n{text[:2000]}\n")
                except (json.JSONDecodeError, OSError):
                    pass

            diag_files = sorted(
                brain_dir.glob("diagnose-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:3]
            for call_file in diag_files:
                try:
                    raw = call_file.read_text(encoding="utf-8")
                    text = _extract_text_from_stream_json(raw)
                    if not text:
                        data = json.loads(raw)
                        text = data.get("response", "")
                    if text:
                        parts.append(f"## Diagnosis ({call_file.name})\n{text[:2000]}\n")
                except (json.JSONDecodeError, OSError):
                    pass

    # Learnings
    learnings = _collect_all_learnings(state, project_dir)
    if learnings.strip():
        parts.append(f"## Learnings\n{learnings}\n")

    # Activity logs (last 30 lines from each)
    for run in state.runs:
        for stage in run.stages:
            wt = stage.worktree
            if not wt or not Path(wt).is_dir():
                continue
            suffix = stage.feature_suffix or ""
            fname = f"{run.name}{suffix}"
            activity_log = Path(wt) / "docs" / fname / "activity.log"
            try:
                lines = activity_log.read_text(encoding="utf-8").splitlines()
                parts.append(
                    f"## Activity Log: {fname} (last 30 lines)\n"
                    + "\n".join(lines[-30:]) + "\n"
                )
            except OSError:
                pass

    # FIXME/skip markers
    fixme_parts = []
    for run in state.runs:
        for stage in run.stages:
            wt = stage.worktree
            if not wt or not Path(wt).is_dir():
                continue
            try:
                grep_result = subprocess.run(
                    [
                        "grep", "-rn",
                        r"test\.fixme\|\.skip\|FIXME\|\.todo",
                        "--include=*.spec.ts", "--include=*.test.ts", "--include=*.spec.js",
                        "--exclude-dir=node_modules", "--exclude-dir=vendor", "--exclude-dir=.git",
                        wt,
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if grep_result.stdout.strip():
                    fixme_parts.append(f"### {wt}\n{grep_result.stdout.strip()[:2000]}\n")
            except (subprocess.TimeoutExpired, OSError):
                pass
    if fixme_parts:
        parts.append("## FIXME/Skip/Todo Markers\n" + "\n".join(fixme_parts) + "\n")

    # Test inventory
    test_inventory_parts = []
    for run in state.runs:
        for stage in run.stages:
            wt = stage.worktree
            if not wt or not Path(wt).is_dir():
                continue
            suffix = stage.feature_suffix or ""
            fname = f"{run.name}{suffix}"
            try:
                grep_result = subprocess.run(
                    [
                        "grep", "-rn",
                        r"describe\|it(\|test(\|test\.fixme",
                        "--include=*.spec.ts", "--include=*.test.ts", "--include=*.spec.js",
                        "--exclude-dir=node_modules", "--exclude-dir=vendor", "--exclude-dir=.git",
                        wt,
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if grep_result.stdout.strip():
                    test_inventory_parts.append(
                        f"### {fname}\n{grep_result.stdout.strip()[:2000]}\n"
                    )
            except (subprocess.TimeoutExpired, OSError):
                pass
    if test_inventory_parts:
        parts.append(
            "## Test Inventory (test names and descriptions)\n"
            + "\n".join(test_inventory_parts) + "\n"
        )

    return "\n".join(parts)


async def generate_audit_report(
    state: ConductorState,
    project_dir: Path,
    storage=None,
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> Path | None:
    """Generate overnight audit report. Returns path to report file."""
    live_log("PLAN", "Generating overnight audit report",
             log_path=log_path, audit_path=audit_path)

    context = _collect_audit_context(state, project_dir, storage)
    template = _load_prompt_template("overnight-audit")
    prompt = template.replace("{CONTEXT}", context)

    result = await run_claude(prompt, model="claude-sonnet-4-6", max_turns=1)
    report_text = _extract_text_from_stream_json(result.output)

    if not report_text.strip():
        live_log("PLAN", "Audit report generation returned empty response",
                 log_path=log_path, audit_path=audit_path)
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    if storage is not None and hasattr(storage, "conductor_dir"):
        conductor_dir = storage.conductor_dir(state.project_name)
    else:
        conductor_dir = project_dir / ".conductor"
    conductor_dir.mkdir(parents=True, exist_ok=True)

    report_file = conductor_dir / f"OVERNIGHT-AUDIT-{timestamp}.md"
    report_file.write_text(report_text, encoding="utf-8")

    live_log("PLAN", f"Audit report written to {report_file}",
             log_path=log_path, audit_path=audit_path)
    return report_file


async def codex_final_review(
    state: ConductorState,
    project_dir: Path,
    storage=None,
) -> None:
    """Run Codex review on each worktree's changes. Non-blocking — any failure is swallowed."""
    codex_companion = _find_codex_companion()
    if not codex_companion or not shutil.which("node"):
        live_log("PLAN", "Codex companion not available — skipping final review")
        return

    # Resolve log directory for codex review artifacts
    codex_log_dir: Path | None = None
    if storage is not None and hasattr(storage, "brain_calls_dir"):
        codex_log_dir = storage.brain_calls_dir(state.project_name)
        codex_log_dir.mkdir(parents=True, exist_ok=True)

    worktrees: list[tuple[str, str]] = []  # (name, path)
    for run in state.runs:
        for stage in run.stages:
            wt = stage.worktree
            if wt and Path(wt).is_dir():
                worktrees.append((run.name, wt))

    if not worktrees:
        live_log("PLAN", "No worktrees to review — skipping Codex final review")
        return

    for name, wt_path in worktrees:
        live_log("PLAN", f"Codex reviewing {name} ({wt_path})")
        ts = int(time.time() * 1000)
        status = "unknown"
        review_text = ""
        error_detail = ""

        try:
            proc = await asyncio.create_subprocess_exec(
                "node", str(codex_companion), "review", "--wait", "--scope", "branch",
                cwd=wt_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            review_text = stdout.decode("utf-8", errors="replace")
            error_detail = stderr.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                status = "error"
                live_log("PLAN", f"Codex review for {name} exited {proc.returncode}: {error_detail[:200]}")
            elif review_text.strip():
                status = "ok"
                # Save review alongside the worktree's conductor artifacts
                conductor_dir = Path(wt_path) / ".conductor"
                conductor_dir.mkdir(parents=True, exist_ok=True)
                review_file = conductor_dir / f"CODEX-REVIEW-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md"
                review_file.write_text(review_text, encoding="utf-8")
                live_log("PLAN", f"Codex review saved: {review_file}")
            else:
                status = "empty"
                live_log("PLAN", f"Codex review for {name} returned empty")
        except asyncio.TimeoutError:
            status = "timeout"
            error_detail = "Timed out after 300s"
            live_log("PLAN", f"Codex review for {name} timed out (300s)")
        except Exception as exc:
            status = "error"
            error_detail = str(exc)
            live_log("PLAN", f"Codex review for {name} failed: {exc}")

        # Always log the result to brain_calls_dir for auditability
        if codex_log_dir:
            log_file = codex_log_dir / f"codex-review-{name}-{ts}.json"
            log_file.write_text(
                json.dumps(
                    {
                        "action": "codex-final-review",
                        "feature": name,
                        "worktree": wt_path,
                        "status": status,
                        "review": review_text[:5000] if review_text else "",
                        "error": error_detail[:2000] if error_detail else "",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )


async def conductor_post_run(
    state: ConductorState,
    project_dir: Path,
    storage=None,
    exit_reason: str = "unknown",
    log_path: Path | None = None,
    audit_path: Path | None = None,
) -> None:
    """Run all post-run processing (learnings + audit). Non-fatal."""
    live_log("PLAN", f"Starting post-run processing (reason: {exit_reason})",
             log_path=log_path, audit_path=audit_path)

    try:
        await review_learnings(state, project_dir, storage, log_path, audit_path)
    except Exception as exc:
        live_log("PLAN", f"Learnings review failed (non-fatal): {exc}",
                 log_path=log_path, audit_path=audit_path)

    try:
        await generate_audit_report(state, project_dir, storage, log_path, audit_path)
    except Exception as exc:
        live_log("PLAN", f"Audit report generation failed (non-fatal): {exc}",
                 log_path=log_path, audit_path=audit_path)

    live_log("PLAN", "Post-run processing complete",
             log_path=log_path, audit_path=audit_path)


async def post_run_processing(
    state: ConductorState,
    storage,
) -> ConductorState:
    """Run the full post-run pipeline: learnings → merge → e2e → audit.

    E2E failures do not prevent audit from running.
    Returns updated ConductorState with integration results.
    """
    project_dir = Path(storage.repo_root)

    # 1. Learnings review
    await review_learnings(state, project_dir, storage)

    # 2. Integration merge
    integration_state = await run_integration_merge(state, storage)
    state.integration = integration_state

    # 3. Integration E2E testing (only if merge succeeded)
    if integration_state.status == IntegrationStatus.DONE:
        try:
            e2e_state = await run_integration_testing(state, storage)
            state.integration.e2e = e2e_state
        except Exception:
            pass

    # 4. Codex final review (non-blocking)
    try:
        await codex_final_review(state, project_dir, storage)
    except Exception as exc:
        live_log("PLAN", f"Codex final review failed (non-blocking): {exc}")

    # 5. Audit report (always runs)
    await generate_audit_report(state, project_dir, storage)

    return state
