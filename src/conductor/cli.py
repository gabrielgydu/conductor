"""Conductor CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from conductor.core.enums import IntegrationStatus, RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState, ContextWiring, atomic_save, validate_dag
from conductor.core.storage import StorageResolver


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _extract_text_from_stream_json(output: str) -> str:
    """Extract assistant text content from stream-json output."""
    text_parts = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])
    return "\n".join(text_parts)


def _generate_file_listing(repo_path: Path, max_files: int = 500) -> str:
    """Generate a file listing excluding common noise directories."""
    exclude_dirs = {"node_modules", ".git", "vendor", ".conductor", "__pycache__",
                    ".venv", "venv", ".tox", ".mypy_cache", "dist", "build"}
    files = []
    for f in sorted(repo_path.rglob("*")):
        if any(d in f.parts for d in exclude_dirs):
            continue
        if f.is_file():
            files.append(str(f.relative_to(repo_path)))
            if len(files) >= max_files:
                break
    return "\n".join(files)


def _load_prompt_template(name: str) -> str:
    """Load a prompt template from src/conductor/prompts/."""
    template_dir = Path(__file__).parent / "prompts"
    path = template_dir / f"{name}-prompt.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


# ─── Commands ────────────────────────────────────────────────────────────────


def _cmd_init(args):
    """Initialize a new conductor project."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    conductor_dir = storage.conductor_dir(args.name)
    conductor_dir.mkdir(parents=True, exist_ok=True)

    # Validate preset if specified
    preset_name = getattr(args, "preset", "base") or "base"
    from conductor.core.presets import load_preset
    try:
        load_preset(preset_name)
    except ValueError:
        print(f"Error: Unknown preset: {preset_name!r}")
        sys.exit(1)

    brief_path = storage.conductor_brief(args.name)
    if not brief_path.exists():
        brief_path.write_text(
            f"# Feature Brief — {args.name}\n\n"
            "## Goal\n<!-- What does this feature accomplish? -->\n\n"
            "## Context\n<!-- Background and motivation -->\n\n"
            "## Requirements\n<!-- Numbered list of requirements -->\n\n"
            "## Constraints\n<!-- Technical or business constraints -->\n\n"
            "## Scope\n<!-- What is in/out of scope -->\n",
            encoding="utf-8",
        )

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        state = ConductorState(
            project_name=args.name,
            base_branch=args.base_branch,
            preset=preset_name,
        )
        atomic_save(state, state_path)

    print(f"Initialized conductor project: {args.name}")
    print(f"  Storage: {conductor_dir}")
    print(f"  Preset:  {preset_name}")
    print(f"  Fill in: {brief_path}")
    print(f"  Then run: conductor plan --name {args.name}")


def _cmd_plan(args):
    """Generate a plan using Claude."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    brief_path = storage.conductor_brief(args.name)
    if not brief_path.exists():
        print(f"Error: FEATURE-BRIEF.md not found. Run 'conductor init --name {args.name}' first.")
        sys.exit(1)

    brief_content = brief_path.read_text(encoding="utf-8")
    if len(brief_content.strip()) < 20:
        print("Error: FEATURE-BRIEF.md is too short. Fill it in before planning.")
        sys.exit(1)

    from conductor.core.claude import run_claude

    # Build context with file listing and brief
    file_listing = _generate_file_listing(repo_path)

    # Load existing state to get preset info
    state_path = storage.conductor_state(args.name)
    preset_info = ""
    if state_path.exists():
        existing_state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if hasattr(existing_state, "preset") and existing_state.preset:
            preset_info = f"\n## Preset: {existing_state.preset}\n"

    context = (
        f"## Feature Brief\n{brief_content}\n\n"
        f"## Repository File Structure (top {min(500, len(file_listing.splitlines()))} files)\n"
        f"```\n{file_listing}\n```\n"
        f"{preset_info}"
    )

    # Load plan prompt template and substitute context
    try:
        template = _load_prompt_template("plan")
    except FileNotFoundError:
        # Fallback if template not found
        template = (
            "You are planning a multi-run conductor project.\n\n"
            "{CONTEXT}\n\n"
            "Output the plan inside a ```conductor-state code block.\n"
            "For each run+stage, also output a description block: ```description:run-{idx}-{stage_name}\n"
        )
    prompt = template.replace("{CONTEXT}", context)

    print("Invoking Claude to generate plan...")
    result = asyncio.run(run_claude(
        prompt, model="claude-opus-4-6", max_turns=10, cwd=str(repo_path),
    ))

    if result.exit_code != 0:
        print(f"Error: Claude exited with code {result.exit_code}")
        sys.exit(1)

    full_text = _extract_text_from_stream_json(result.output)

    # Extract conductor-state JSON
    m = re.search(r"```conductor-state\s*\n(.*?)```", full_text, re.DOTALL)
    if not m:
        print("Error: No conductor-state block in Claude response")
        print("Response text:", full_text[:500])
        sys.exit(1)

    try:
        runs_json = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in conductor-state block: {e}")
        sys.exit(1)

    # Extract description blocks
    descriptions: dict[str, str] = {}
    for desc_match in re.finditer(r"```description:([\w\-]+)\s*\n(.*?)```", full_text, re.DOTALL):
        key = desc_match.group(1)
        descriptions[key] = desc_match.group(2).strip()

    # Build state
    runs = []
    conductor_dir = storage.conductor_dir(args.name)
    for i, run_data in enumerate(runs_json):
        stages = []
        for stage_data in run_data.get("stages", []):
            stage_name = stage_data["name"]
            spec_mode = stage_data.get("spec_mode", stage_name)

            # Parse context wiring
            cw_data = stage_data.get("context_wiring")
            context_wiring = None
            if cw_data and isinstance(cw_data, dict):
                context_wiring = ContextWiring(
                    sources=[json.dumps(cw_data)],
                )

            # Determine feature suffix
            suffix = f"-{stage_name}" if stage_name != "backend" else ""

            stages.append(StageState(
                name=stage_name,
                spec_mode=spec_mode,
                context_wiring=context_wiring,
                feature_suffix=suffix if hasattr(StageState, "feature_suffix") else None,
            ))

        constitution = run_data.get("constitution", [])

        runs.append(RunState(
            index=i,
            name=run_data["name"],
            description=run_data.get("description", ""),
            depends_on=run_data.get("depends_on", []),
            constitution=constitution,
            stages=stages,
        ))

    # Validate DAG
    try:
        validate_dag(runs)
    except ValueError as e:
        print(f"Error: Invalid dependency graph: {e}")
        sys.exit(1)

    # Write description files
    for i, run_data in enumerate(runs_json):
        for stage_data in run_data.get("stages", []):
            stage_name = stage_data["name"]
            desc_key = f"run-{i}-{stage_name}"
            desc_content = descriptions.get(desc_key, "")
            if desc_content:
                desc_file = conductor_dir / f"description-{desc_key}.md"
                desc_file.write_text(desc_content, encoding="utf-8")

    # Build and save state
    state = ConductorState(
        project_name=args.name,
        base_branch=args.base_branch,
        runs=runs,
    )
    # Preserve preset from existing state
    if state_path.exists():
        existing = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if hasattr(existing, "preset"):
            state.preset = existing.preset
        if hasattr(existing, "overnight"):
            state.overnight = existing.overnight

    atomic_save(state, state_path)

    # Print summary table
    print(f"\nPlan generated: {len(runs)} runs")
    print(f"{'─' * 60}")
    for run in runs:
        deps = ", ".join(str(d) for d in run.depends_on) if run.depends_on else "none"
        stages = " → ".join(s.name for s in run.stages)
        print(f"  {run.index}: {run.name}")
        print(f"     deps: {deps}")
        print(f"     stages: {stages}")
        if run.constitution:
            for c in run.constitution[:2]:
                print(f"     • {c}")
    print(f"{'─' * 60}")
    print(f"\nState saved to: {state_path}")
    print(f"Review, then run: conductor run --name {args.name}")


def _cmd_run(args):
    """Execute the conductor orchestration loop."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("Error: No plan found. Run 'conductor plan' first.")
        sys.exit(1)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    # Set overnight flag
    if hasattr(state, "overnight"):
        state.overnight = getattr(args, "overnight", False)

    inside_tmux = getattr(args, "inside_tmux", False)

    if not inside_tmux:
        # Re-exec inside tmux session
        from conductor.core.tmux import TmuxManager
        tmux = TmuxManager(session_name=f"conductor-{state.project_name}")

        if tmux.session_exists():
            # Kill stale session
            tmux._run_tmux("kill-session", "-t", f"conductor-{state.project_name}", check=False)

        # Build re-exec command
        conductor_bin = sys.argv[0]
        reexec_args = f"run --inside-tmux --name {args.name} --project-dir {repo_path}"
        if getattr(args, "overnight", False):
            reexec_args += " --overnight"

        # Create detached session
        tmux._run_tmux(
            "new-session", "-d", "-s", f"conductor-{state.project_name}",
            "-n", "conductor",
            f"bash -c '{conductor_bin} {reexec_args}'",
        )

        tmux_env = os.environ.get("TMUX", "")
        if not tmux_env:
            # Not in tmux — attach
            os.execvp("tmux", ["tmux", "attach-session", "-t", f"conductor-{state.project_name}"])
        else:
            # Already in tmux — switch client
            subprocess.run(
                ["tmux", "switch-client", "-t", f"conductor-{state.project_name}"],
                check=False,
            )
            sys.exit(0)
    else:
        # Inside tmux — run the loop directly
        from conductor.core.orchestrator import ConductorConfig, conductor_run_loop

        config = ConductorConfig(
            check_interval_s=state.check_interval_s,
            project_root=repo_path,
        )

        print(f"Starting conductor run: {state.project_name}")
        result_state = asyncio.run(conductor_run_loop(state, config))
        atomic_save(result_state, state_path)

        done = sum(1 for r in result_state.runs if r.status == RunStatus.DONE)
        print(f"\nRun complete: {done}/{len(result_state.runs)} runs done")


def _cmd_status(args):
    """Show current state of all runs with colored output."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("No conductor project found. Run 'conductor init' first.")
        sys.exit(0)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    # ANSI colors
    isatty = sys.stdout.isatty()
    green = "\033[32m" if isatty else ""
    red = "\033[31m" if isatty else ""
    yellow = "\033[33m" if isatty else ""
    cyan = "\033[36m" if isatty else ""
    dim = "\033[2m" if isatty else ""
    bold = "\033[1m" if isatty else ""
    reset = "\033[0m" if isatty else ""

    print(f"{bold}Project:{reset} {state.project_name}")
    print(f"{bold}Base branch:{reset} {state.base_branch}")
    if hasattr(state, "preset") and state.preset:
        print(f"{bold}Preset:{reset} {state.preset}")
    print()

    fmt = "  {:<4}  {:<30}  {:<12}  {:<20}  {}"
    print(fmt.format("Run", "Name", "Status", "Stage", "Progress"))
    print(fmt.format("───", "────", "──────", "─────", "────────"))

    status_colors = {
        "done": green,
        "active": cyan,
        "blocked": red,
        "pending": dim,
    }

    for run in state.runs:
        stage_info = "—"
        progress = "—"
        status_val = run.status.value if hasattr(run.status, "value") else str(run.status)
        color = status_colors.get(status_val, "")

        if run.status == RunStatus.ACTIVE and run.stages:
            cs = run.current_stage
            if cs < len(run.stages):
                stage = run.stages[cs]
                stage_info = f"{stage.name} ({cs+1}/{len(run.stages)})"
                stage_status = stage.status.value if hasattr(stage.status, "value") else str(stage.status)
                progress = stage_status
        elif run.status == RunStatus.DONE:
            progress = "✓ complete"
        elif run.status == RunStatus.BLOCKED:
            progress = "⊘ blocked"
            retries = run.monitor.retry_count if run.monitor else 0
            if retries:
                progress += f" (retries: {retries})"
        elif run.status == RunStatus.PENDING:
            if run.depends_on:
                progress = f"waiting on {run.depends_on}"
            else:
                progress = "ready"

        colored_status = f"{color}{status_val}{reset}"
        print(f"  {run.index:<4}  {run.name:<30}  {colored_status:<12}  {stage_info:<20}  {progress}")

    done = sum(1 for r in state.runs if r.status == RunStatus.DONE)
    blocked = sum(1 for r in state.runs if r.status == RunStatus.BLOCKED)
    active = sum(1 for r in state.runs if r.status == RunStatus.ACTIVE)

    print(f"\n  {green}{done}{reset} done, {cyan}{active}{reset} active, {red}{blocked}{reset} blocked / {len(state.runs)} total")

    if state.integration:
        status = state.integration.status
        s = status.value if hasattr(status, "value") else str(status)
        print(f"  Integration: {s} (branch: {state.integration.branch})")


def _cmd_log(args):
    """Show conductor log."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    log_path = storage.conductor_log(args.name)
    if not log_path.exists():
        print("No log yet. Run 'conductor run' first.")
        sys.exit(0)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.tail:]:
        print(line)


def _cmd_cleanup(args):
    """Clean up worktrees, branches, and tmux session."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("No conductor project found.")
        sys.exit(0)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    # Check for running tmux session
    session_name = f"conductor-{state.project_name}"
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if result.returncode == 0:
        if not getattr(args, "force", False):
            print(f"Warning: tmux session '{session_name}' is still running.")
            print("Use --force to kill it, or stop the run first.")
            sys.exit(1)
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
        print(f"Killed tmux session: {session_name}")

    wt_count = 0
    branch_count = 0

    for run in state.runs:
        for stage in run.stages:
            if stage.worktree and Path(stage.worktree).exists():
                subprocess.run(
                    ["git", "worktree", "remove", stage.worktree],
                    cwd=str(repo_path), capture_output=True,
                )
                wt_count += 1
            if stage.branch:
                result = subprocess.run(
                    ["git", "branch", "-D", stage.branch],
                    cwd=str(repo_path), capture_output=True,
                )
                if result.returncode == 0:
                    branch_count += 1

    subprocess.run(["git", "worktree", "prune"], cwd=str(repo_path), capture_output=True)

    print(f"Cleaned up: {wt_count} worktrees, {branch_count} branches")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(prog="conductor", description="Conductor orchestration tool")
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")

    # Common args for most commands
    def _add_common(p):
        p.add_argument("--name", required=True, help="Project name")
        p.add_argument("--project-dir", default=None, help="Project root directory")

    p_init = subparsers.add_parser("init", help="Initialize a new conductor project")
    _add_common(p_init)
    p_init.add_argument("--base-branch", default="master")
    p_init.add_argument("--preset", default="base")

    p_plan = subparsers.add_parser("plan", help="Generate a plan via Claude")
    _add_common(p_plan)
    p_plan.add_argument("--base-branch", default="master")

    p_run = subparsers.add_parser("run", help="Execute the orchestration loop")
    _add_common(p_run)
    p_run.add_argument("--overnight", action="store_true")
    p_run.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)

    p_status = subparsers.add_parser("status", help="Show run status")
    _add_common(p_status)

    p_log = subparsers.add_parser("log", help="Show conductor log")
    _add_common(p_log)
    p_log.add_argument("--tail", type=int, default=50)

    p_cleanup = subparsers.add_parser("cleanup", help="Clean up worktrees and branches")
    _add_common(p_cleanup)
    p_cleanup.add_argument("--force", action="store_true")

    handlers = {
        "init": _cmd_init,
        "plan": _cmd_plan,
        "run": _cmd_run,
        "status": _cmd_status,
        "log": _cmd_log,
        "cleanup": _cmd_cleanup,
    }

    args = parser.parse_args()

    if args.subcommand is None:
        parser.print_help()
        sys.exit(1)

    handlers[args.subcommand](args)


if __name__ == "__main__":
    main()
