"""Conductor CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from conductor.core.enums import IntegrationStatus, RunStatus, StageStatus
from conductor.core.models import ConductorState, atomic_save
from conductor.core.storage import StorageResolver


def _cmd_init(args):
    """Initialize a new conductor project."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    conductor_dir = storage.conductor_dir(args.name)
    conductor_dir.mkdir(parents=True, exist_ok=True)

    brief_path = storage.conductor_brief(args.name)
    if not brief_path.exists():
        brief_path.write_text(
            f"# Feature Brief — {args.name}\n\n"
            "## Goal\n<!-- What does this feature accomplish? -->\n\n"
            "## Requirements\n<!-- Numbered list of requirements -->\n\n"
            "## Constraints\n<!-- Technical or business constraints -->\n",
            encoding="utf-8",
        )

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        state = ConductorState(
            project_name=args.name,
            base_branch=args.base_branch,
        )
        atomic_save(state, state_path)

    print(f"Initialized conductor project: {args.name}")
    print(f"  Storage: {conductor_dir}")
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
    from conductor.core.brain import _load_prompt_template

    prompt = (
        "You are planning a multi-run conductor project.\n\n"
        f"## Feature Brief\n{brief_content}\n\n"
        "## Instructions\n"
        "Analyze the brief and produce a plan as a JSON array of runs.\n"
        "Each run has: name, stages (array of {name, spec_mode}), depends_on (array of run indices), "
        "constitution (array of rules).\n\n"
        "Output the plan inside a ```conductor-state code block.\n"
        "For each run+stage, also output a description block: ```description:run-{idx}-{stage_name}\n"
    )

    print("Invoking Claude to generate plan...")
    result = asyncio.run(run_claude(
        prompt, model="claude-opus-4-6", max_turns=10, cwd=str(repo_path),
    ))

    if result.exit_code != 0:
        print(f"Error: Claude exited with code {result.exit_code}")
        sys.exit(1)

    # Extract text from stream-json
    text_parts = []
    for line in result.output.splitlines():
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

    full_text = "\n".join(text_parts)

    # Extract conductor-state JSON
    import re
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

    # Build state
    from conductor.core.models import RunState, StageState
    runs = []
    for i, run_data in enumerate(runs_json):
        stages = []
        for stage_data in run_data.get("stages", []):
            stages.append(StageState(
                name=stage_data["name"],
                spec_mode=stage_data.get("spec_mode", stage_data["name"]),
            ))
        runs.append(RunState(
            index=i,
            name=run_data["name"],
            description=run_data.get("description", ""),
            depends_on=run_data.get("depends_on", []),
            constitution=run_data.get("constitution", []),
            stages=stages,
        ))

    state = ConductorState(
        project_name=args.name,
        base_branch=args.base_branch,
        runs=runs,
    )
    state_path = storage.conductor_state(args.name)
    atomic_save(state, state_path)

    # Print summary
    print(f"\nPlan generated: {len(runs)} runs")
    for run in runs:
        deps = run.depends_on or "none"
        stages = " → ".join(s.name for s in run.stages)
        print(f"  {run.index}: {run.name} (deps: {deps}) — {stages}")
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
    """Show current state of all runs."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("No conductor project found. Run 'conductor init' first.")
        sys.exit(0)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    print(f"Project: {state.project_name}")
    print(f"Base branch: {state.base_branch}")
    print()

    fmt = "  {:<4}  {:<30}  {:<10}  {:<15}  {}"
    print(fmt.format("Run", "Name", "Status", "Stage", "Progress"))
    print(fmt.format("---", "----", "------", "-----", "--------"))

    for run in state.runs:
        stage_info = "—"
        progress = "—"
        if run.status == RunStatus.ACTIVE and run.stages:
            cs = run.current_stage
            if cs < len(run.stages):
                stage = run.stages[cs]
                stage_info = f"{stage.name} ({cs+1}/{len(run.stages)})"
                progress = stage.status.value if hasattr(stage.status, 'value') else str(stage.status)
        elif run.status == RunStatus.DONE:
            progress = "complete"
        elif run.status == RunStatus.PENDING:
            progress = f"waiting on {run.depends_on}"

        print(fmt.format(run.index, run.name, run.status.value if hasattr(run.status, 'value') else str(run.status), stage_info, progress))

    done = sum(1 for r in state.runs if r.status == RunStatus.DONE)
    print(f"\n  {done}/{len(state.runs)} runs complete")

    if state.integration:
        status = state.integration.status
        s = status.value if hasattr(status, 'value') else str(status)
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
    """Clean up worktrees and branches."""
    import subprocess

    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("No conductor project found.")
        sys.exit(0)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

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
