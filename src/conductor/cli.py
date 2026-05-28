"""Conductor CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from conductor.core.enums import IntegrationStatus, RunStatus, StageStatus
from conductor.core.models import (
    ConductorState,
    RunState,
    StageState,
    ContextWiring,
    atomic_save,
    validate_dag,
)
from conductor.core.storage import StorageResolver


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _detect_git_branch(project_dir: str | None) -> str:
    """Detect current git branch in project_dir, fallback to 'master'."""
    cwd = project_dir or "."
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=cwd, check=True,
        )
        return result.stdout.strip() or "master"
    except Exception:
        return "master"

_DIM = "\033[2m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _isatty() -> bool:
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _ts() -> str:
    """HH:MM:SS timestamp, dimmed when outputting to a terminal."""
    from datetime import datetime, timezone

    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return f"{_DIM}{t}{_RESET}" if _isatty() else t


def _plan_progress_callback(event: dict) -> None:
    """Print live progress from Claude stream-json events during plan generation."""
    tty = _isatty()
    etype = event.get("type", "")
    ts = _ts()

    if etype == "system":
        if not getattr(_plan_progress_callback, "_session_logged", False):
            _plan_progress_callback._session_logged = True
            sys.stderr.write(f"{ts} ● Claude session started\n")
            sys.stderr.flush()
        return

    if etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name = block.get("name", "?")
                inp = block.get("input", {})
                detail = ""
                if "file_path" in inp:
                    detail = f" {inp['file_path']}"
                elif "pattern" in inp:
                    detail = f" {inp['pattern']}"
                elif "command" in inp:
                    detail = f" {inp['command'][:80]}"
                if tty:
                    sys.stderr.write(f"{ts} {_DIM}⚙ {tool_name}{detail}{_RESET}\n")
                else:
                    sys.stderr.write(f"{ts} ⚙ {tool_name}{detail}\n")
                sys.stderr.flush()
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    first_line = text.split("\n")[0][:120]
                    if tty:
                        sys.stderr.write(f"{ts} {_CYAN}▸ {first_line}{_RESET}\n")
                    else:
                        sys.stderr.write(f"{ts} ▸ {first_line}\n")
                    sys.stderr.flush()
    elif etype == "result":
        cost = event.get("total_cost_usd")
        duration_ms = event.get("duration_ms")
        usage = event.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        tokens_in = usage.get("input_tokens", 0) + usage.get(
            "cache_read_input_tokens", 0
        )
        tokens_out = usage.get("output_tokens", 0)
        parts = []
        if tokens_in or tokens_out:
            parts.append(f"{tokens_in}in/{tokens_out}out tokens")
        if cost:
            parts.append(f"${cost:.2f}")
        if duration_ms:
            parts.append(f"{duration_ms / 1000:.0f}s")
        if parts:
            summary = ", ".join(parts)
            if tty:
                sys.stderr.write(f"{ts} {_YELLOW}✓ Done: {summary}{_RESET}\n")
            else:
                sys.stderr.write(f"{ts} ✓ Done: {summary}\n")
            sys.stderr.flush()


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
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])
    return "\n".join(text_parts)


def _generate_file_listing(repo_path: Path, max_files: int = 500) -> str:
    """Generate a file listing excluding common noise directories."""
    exclude_dirs = {
        "node_modules",
        ".git",
        "vendor",
        ".conductor",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        "dist",
        "build",
    }
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
    if args.base_branch is None:
        args.base_branch = _detect_git_branch(args.project_dir)
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    conductor_dir = storage.conductor_dir(args.name)
    conductor_dir.mkdir(parents=True, exist_ok=True)

    # Detect or validate preset
    from conductor.core.presets import detect_preset, load_preset

    preset_name = getattr(args, "preset", None)
    if preset_name is None or preset_name == "base":
        detected = detect_preset(repo_path)
        if detected != "base":
            preset_name = detected
            print(f"Auto-detected preset: {preset_name}", file=sys.stderr)
        else:
            preset_name = preset_name or "base"

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


def _parse_plan_output(output_text: str) -> tuple[list, dict[str, str]]:
    """Parse conductor-state JSON and description blocks from Claude output."""
    m = re.search(r"```conductor-state\s*\n(.*?)```", output_text, re.DOTALL)
    if not m:
        raise ValueError(f"No conductor-state block in response: {output_text[:500]}")

    runs_json = json.loads(m.group(1))

    descriptions: dict[str, str] = {}
    for desc_match in re.finditer(
        r'<description\s+name="([\w\-]+)">\s*\n(.*?)</description>',
        output_text,
        re.DOTALL,
    ):
        descriptions[desc_match.group(1)] = desc_match.group(2).strip()

    return runs_json, descriptions


def _generate_and_review_plan(
    prompt: str,
    brief_content: str,
    repo_path: Path,
    run_claude,
) -> tuple[list, dict[str, str]]:
    """Generate plan, review for completeness, re-generate if needed (max 1 retry)."""
    # Step 1: Generate plan
    print("Invoking Claude to generate plan...", file=sys.stderr)
    _plan_progress_callback._session_logged = False
    result = asyncio.run(
        run_claude(
            prompt,
            model="claude-opus-4-8",
            max_turns=10,
            cwd=str(repo_path),
            on_event=_plan_progress_callback,
        )
    )

    if result.exit_code != 0:
        print(f"Error: Claude exited with code {result.exit_code}")
        sys.exit(1)

    full_text = _extract_text_from_stream_json(result.output)

    try:
        runs_json, descriptions = _parse_plan_output(full_text)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Step 2: Completeness review
    review_feedback = _run_completeness_review(
        brief_content, runs_json, descriptions, repo_path, run_claude
    )

    if review_feedback is None:
        return runs_json, descriptions

    # Step 3: Re-generate once with feedback (no second review)
    print("Review found gaps — re-generating plan...", file=sys.stderr)
    prompt = prompt + "\n\n" + review_feedback

    _plan_progress_callback._session_logged = False
    result = asyncio.run(
        run_claude(
            prompt,
            model="claude-opus-4-8",
            max_turns=10,
            cwd=str(repo_path),
            on_event=_plan_progress_callback,
        )
    )

    if result.exit_code != 0:
        print(f"Error: Claude exited with code {result.exit_code}")
        sys.exit(1)

    full_text = _extract_text_from_stream_json(result.output)

    try:
        runs_json, descriptions = _parse_plan_output(full_text)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Error: {e}")
        sys.exit(1)

    return runs_json, descriptions


def _run_completeness_review(
    brief_content: str,
    runs_json: list,
    descriptions: dict[str, str],
    repo_path: Path,
    run_claude,
) -> str | None:
    """Run completeness review. Returns feedback string if issues found, None if passed."""
    try:
        review_template = _load_prompt_template("completeness-review")
    except FileNotFoundError:
        print("Warning: completeness-review-prompt.md not found, skipping review.", file=sys.stderr)
        return None

    # Format descriptions for the review
    desc_text = ""
    for key, content in sorted(descriptions.items()):
        desc_text += f"### {key}\n{content}\n\n"

    review_prompt = (
        review_template
        .replace("{BRIEF}", brief_content)
        .replace("{PLAN_JSON}", json.dumps(runs_json, indent=2))
        .replace("{DESCRIPTIONS}", desc_text)
    )

    print("Reviewing plan for completeness...", file=sys.stderr)
    result = asyncio.run(
        run_claude(
            review_prompt,
            model="claude-sonnet-4-6",
            max_turns=1,
            cwd=str(repo_path),
        )
    )

    if result.exit_code != 0:
        print("Warning: completeness review failed, proceeding with plan.", file=sys.stderr)
        return None

    review_text = _extract_text_from_stream_json(result.output)

    # Parse review result
    rm = re.search(r"```completeness-review\s*\n(.*?)```", review_text, re.DOTALL)
    if not rm:
        print("Warning: could not parse review output, proceeding with plan.", file=sys.stderr)
        return None

    try:
        review = json.loads(rm.group(1))
    except json.JSONDecodeError:
        print("Warning: invalid JSON in review output, proceeding with plan.", file=sys.stderr)
        return None

    verdict = review.get("verdict", "pass")
    issues = review.get("issues", [])
    missing_runs = review.get("missing_runs", [])

    if verdict == "pass":
        print("Completeness review: passed.", file=sys.stderr)
        return None

    # Format feedback for re-generation
    feedback_parts = [
        "## Completeness Review Feedback\n",
        "A reviewer found the following gaps in your plan. "
        "You MUST address ALL of these issues in the revised plan.\n",
    ]

    if issues:
        feedback_parts.append("### Issues with existing runs\n")
        for issue in issues:
            feedback_parts.append(
                f"- **Run {issue.get('run_index', '?')} / {issue.get('stage', '?')}**: "
                f"{issue.get('issue', '')} → {issue.get('fix', '')}\n"
            )

    if missing_runs:
        feedback_parts.append("\n### Missing runs\n")
        for mr in missing_runs:
            feedback_parts.append(
                f"- **{mr.get('name', '?')}**: {mr.get('reason', '')}\n"
            )

    feedback = "\n".join(feedback_parts)

    # Print to stderr so user can see
    print(f"\nCompleteness review: {len(issues)} issues, {len(missing_runs)} missing runs.", file=sys.stderr)
    for issue in issues:
        print(f"  • {issue.get('issue', '')}", file=sys.stderr)
    for mr in missing_runs:
        print(f"  + Missing: {mr.get('name', '')} — {mr.get('reason', '')}", file=sys.stderr)

    return feedback


def _cmd_plan(args):
    """Generate a plan using Claude."""
    if args.base_branch is None:
        args.base_branch = _detect_git_branch(args.project_dir)
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    brief_path = storage.conductor_brief(args.name)
    if not brief_path.exists():
        print(
            f"Error: FEATURE-BRIEF.md not found. Run 'conductor init --name {args.name}' first."
        )
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
        existing_state = ConductorState.model_validate_json(
            state_path.read_text(encoding="utf-8")
        )
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
            'For each run+stage, also output a description block: <description name="run-{idx}-{stage_name}">...\n'
        )
    prompt = template.replace("{CONTEXT}", context)

    runs_json, descriptions = _generate_and_review_plan(
        prompt, brief_content, repo_path, run_claude
    )

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

            desc_key = f"run-{i}-{stage_name}"
            desc_file_rel = (
                f"description-{desc_key}.md" if desc_key in descriptions else None
            )

            stages.append(
                StageState(
                    name=stage_name,
                    spec_mode=spec_mode,
                    context_wiring=context_wiring,
                    feature_suffix=suffix,
                    feature_description_file=desc_file_rel,
                )
            )

        constitution = run_data.get("constitution", [])

        runs.append(
            RunState(
                index=i,
                name=run_data["name"],
                description=run_data.get("description", ""),
                depends_on=run_data.get("depends_on", []),
                constitution=constitution,
                stages=stages,
            )
        )

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
        existing = ConductorState.model_validate_json(
            state_path.read_text(encoding="utf-8")
        )
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


def _cmd_go(args):
    """One-shot resumable command: init → copy brief → plan → run."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    state_path = storage.conductor_state(args.name)
    brief_path = storage.conductor_brief(args.name)

    tty = _isatty()
    def _phase(num: int, total: int, label: str, skip: bool = False):
        action = "skipping" if skip else label
        msg = f"==> Phase {num}/{total}: {action}"
        if tty:
            msg = f"{_CYAN}{msg}{_RESET}"
        print(msg, file=sys.stderr)

    has_state = state_path.exists()
    def _brief_has_content(path: Path) -> bool:
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8")
        # Strip HTML comments, markdown headings, and whitespace to detect placeholder-only briefs
        stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        stripped = re.sub(r"^#+\s.*$", "", stripped, flags=re.MULTILINE)
        return len(stripped.strip()) > 20

    brief_populated = _brief_has_content(brief_path)
    has_runs = False
    if has_state:
        existing = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))
        has_runs = bool(existing.runs)

    # Phase 1: Init
    if not has_state:
        _phase(1, 3, "Initializing...")
        _cmd_init(args)
    else:
        _phase(1, 3, "Initializing", skip=True)

    # Copy brief from --plan if needed
    if not brief_populated:
        plan_file = getattr(args, "plan", None)
        if not plan_file:
            print("Error: Brief is empty and no --plan file provided.")
            print(f"Either fill in {brief_path} or pass --plan <file>.")
            sys.exit(1)
        plan_path = Path(plan_file)
        if not plan_path.is_absolute():
            plan_path = Path.cwd() / plan_path
        if not plan_path.exists():
            print(f"Error: Plan file not found: {plan_path}")
            sys.exit(1)
        brief_path.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  Copied brief from {plan_path}", file=sys.stderr)

    # Phase 2: Plan
    if not has_runs:
        _phase(2, 3, "Planning...")
        _cmd_plan(args)
    else:
        _phase(2, 3, "Planning", skip=True)

    # Phase 3: Run
    # Handle --no-quick override (go defaults quick=True)
    if getattr(args, "no_quick", False):
        args.quick = False
    _phase(3, 3, "Running...")
    _cmd_run(args)


def _cmd_run(args):
    """Execute the conductor orchestration loop."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("Error: No plan found. Run 'conductor plan' first.")
        sys.exit(1)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    # Set overnight flag (default ON, --no-overnight disables)
    if hasattr(state, "overnight"):
        state.overnight = not getattr(args, "no_overnight", False)

    # Set quick flag
    if getattr(args, "quick", False):
        state.quick = True

    # Set max_parallel from CLI
    cli_max_parallel = getattr(args, "max_parallel", None)
    if cli_max_parallel is not None:
        state.max_parallel = cli_max_parallel

    # Set worktrees_base from CLI (overrides preset default)
    cli_wt_base = getattr(args, "worktrees_base", None)
    if cli_wt_base:
        state.worktrees_base = str(Path(cli_wt_base).resolve())

    inside_tmux = getattr(args, "inside_tmux", False)

    if not inside_tmux:
        # Re-exec inside tmux session
        from conductor.core.tmux import TmuxManager

        tmux = TmuxManager(session_name=f"conductor-{state.project_name}")

        if tmux.session_exists():
            # Kill stale session
            tmux._run_tmux(
                "kill-session", "-t", f"conductor-{state.project_name}", check=False
            )

        # Build re-exec command — resolve to the wrapper script so it works inside tmux
        conductor_bin = str(Path(__file__).resolve().parents[2] / "conductor")
        reexec_args = f"run --inside-tmux --name {args.name} --project-dir {repo_path}"
        if getattr(args, "no_overnight", False):
            reexec_args += " --no-overnight"
        if getattr(args, "quick", False):
            reexec_args += " --quick"
        if cli_max_parallel is not None:
            reexec_args += f" --max-parallel {state.max_parallel}"
        if cli_wt_base:
            reexec_args += f" --worktrees-base {state.worktrees_base}"

        # Create detached session
        session_name = f"conductor-{state.project_name}"
        tmux._run_tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-n",
            "conductor",
            f"bash -c '{conductor_bin} {reexec_args}'",
        )
        # Keep window open after exit + log all output
        tmux._run_tmux(
            "set-option", "-t", f"{session_name}:conductor",
            "remain-on-exit", "on", check=False,
        )
        log_file = storage.tmux_log(state.project_name, "conductor-main")
        tmux._run_tmux(
            "pipe-pane", "-t", f"{session_name}:conductor",
            f"cat >> {log_file}", check=False,
        )

        tmux_env = os.environ.get("TMUX", "")
        if not tmux_env:
            # Not in tmux — attach
            os.execvp(
                "tmux",
                ["tmux", "attach-session", "-t", session_name],
            )
        else:
            # Already in tmux — switch client
            subprocess.run(
                ["tmux", "switch-client", "-t", session_name],
                check=False,
            )
            sys.exit(0)
    else:
        # Inside tmux — run the loop directly
        from conductor.core.orchestrator import ConductorConfig, conductor_run_loop

        config = ConductorConfig(
            check_interval_s=state.check_interval_s,
            max_parallel=state.max_parallel,
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
        status_val = (
            run.status.value if hasattr(run.status, "value") else str(run.status)
        )
        color = status_colors.get(status_val, "")

        if run.status == RunStatus.ACTIVE and run.stages:
            cs = run.current_stage
            if cs < len(run.stages):
                stage = run.stages[cs]
                stage_info = f"{stage.name} ({cs + 1}/{len(run.stages)})"
                stage_status = (
                    stage.status.value
                    if hasattr(stage.status, "value")
                    else str(stage.status)
                )
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
        print(
            f"  {run.index:<4}  {run.name:<30}  {colored_status:<12}  {stage_info:<20}  {progress}"
        )

    done = sum(1 for r in state.runs if r.status == RunStatus.DONE)
    blocked = sum(1 for r in state.runs if r.status == RunStatus.BLOCKED)
    active = sum(1 for r in state.runs if r.status == RunStatus.ACTIVE)

    print(
        f"\n  {green}{done}{reset} done, {cyan}{active}{reset} active, {red}{blocked}{reset} blocked / {len(state.runs)} total"
    )

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
    for line in lines[-args.tail :]:
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
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name], capture_output=True
        )
        print(f"Killed tmux session: {session_name}")

    wt_count = 0
    branch_count = 0

    for run in state.runs:
        for stage in run.stages:
            if stage.worktree and Path(stage.worktree).exists():
                subprocess.run(
                    ["git", "worktree", "remove", stage.worktree],
                    cwd=str(repo_path),
                    capture_output=True,
                )
                wt_count += 1
            if stage.branch:
                result = subprocess.run(
                    ["git", "branch", "-D", stage.branch],
                    cwd=str(repo_path),
                    capture_output=True,
                )
                if result.returncode == 0:
                    branch_count += 1

    subprocess.run(
        ["git", "worktree", "prune"], cwd=str(repo_path), capture_output=True
    )

    print(f"Cleaned up: {wt_count} worktrees, {branch_count} branches")


def _cmd_learnings(args):
    """Review learnings from completed runs and update/create CLAUDE.md."""
    import asyncio

    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("Error: No state found. Run 'conductor plan' first.")
        sys.exit(1)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    from conductor.core.orchestrator import _review_learnings

    log_path = storage.conductor_log(args.name)
    audit_path = storage.conductor_audit(args.name)
    asyncio.run(_review_learnings(state, storage, log_path, audit_path))


def _cmd_loop(args):
    """Start or resume a conductor loop."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_dir(args.name) / "LOOP-STATE.json"
    inside_tmux = getattr(args, "inside_tmux", False)

    if state_path.exists() and not getattr(args, "reset", False):
        # Resume existing loop
        from conductor.core.loop import LoopState
        state = LoopState.model_validate_json(state_path.read_text(encoding="utf-8"))
        print(f"Resuming loop: {state.name} ({sum(1 for t in state.tasks if t.status == 'completed')}/{len(state.tasks)} done)")
    else:
        # Initialize new loop
        if not args.plan:
            print("Error: --plan is required for new loops.")
            sys.exit(1)
        plan_path = Path(args.plan)
        if not plan_path.is_absolute():
            plan_path = repo_path / plan_path
        if not plan_path.exists():
            print(f"Error: Plan file not found: {plan_path}")
            sys.exit(1)

        plan_content = plan_path.read_text(encoding="utf-8")

        from conductor.core.loop import LoopState, parse_checklist, decompose_plan
        from conductor.core.presets import detect_preset

        # Try parsing as checklist first; if no tasks found, use Claude to decompose
        tasks = parse_checklist(plan_content)
        checklist_text = None
        if not tasks:
            print("No checklist found in plan — decomposing via Claude...", file=sys.stderr)
            tasks, checklist_text = decompose_plan(plan_content)

        if not tasks:
            print("Error: Could not extract tasks from plan.")
            sys.exit(1)

        preset_name = getattr(args, "preset", None)
        if not preset_name:
            preset_name = detect_preset(repo_path)

        base_branch = getattr(args, "base_branch", None) or _detect_git_branch(args.project_dir)

        state = LoopState(
            name=args.name,
            base_branch=base_branch,
            plan_file=str(plan_path),
            preset=preset_name,
            tasks=tasks,
            created_at=datetime.now(timezone.utc),
            model=getattr(args, "model", None),
        )

        # Create worktree if requested
        if not getattr(args, "no_worktree", False):
            from conductor.core.loop import create_loop_worktree
            from conductor.core.presets import load_preset as _load_preset
            cli_wt_base = getattr(args, "worktrees_base", None)
            wt_base = None
            if cli_wt_base:
                wt_base = Path(cli_wt_base).resolve()
            elif preset_name:
                _preset = _load_preset(preset_name)
                if _preset and _preset.config.worktrees_base:
                    wt_base = Path(_preset.config.worktrees_base)
            branch_name = f"loop-{args.name}"
            wt = create_loop_worktree(repo_path, branch_name, base_branch, worktrees_base=wt_base)
            state.worktree = str(wt)
            state.branch = branch_name
            print(f"Created worktree: {wt}")

        conductor_dir = storage.conductor_dir(args.name)
        conductor_dir.mkdir(parents=True, exist_ok=True)

        # Save generated checklist if Claude decomposed the plan
        if checklist_text:
            checklist_path = conductor_dir / "CHECKLIST.md"
            checklist_path.write_text(checklist_text, encoding="utf-8")
            print(f"  Checklist saved: {checklist_path}", file=sys.stderr)

        from conductor.core.models import atomic_save
        atomic_save(state, state_path)

        print(f"Initialized loop: {args.name}")
        print(f"  Tasks: {len(tasks)}")
        for t in tasks:
            print(f"    {t.index}: {t.name}")
        print(f"  Preset: {preset_name}")
        print(f"  State: {state_path}")

    if not inside_tmux:
        # Re-exec inside tmux
        from conductor.core.tmux import TmuxManager
        session_name = f"conductor-loop-{state.name}"
        tmux = TmuxManager(session_name=session_name)

        if tmux.session_exists():
            tmux._run_tmux("kill-session", "-t", session_name, check=False)

        conductor_bin = str(Path(__file__).resolve().parents[2] / "conductor")
        reexec_args = f"loop --inside-tmux --name {args.name} --project-dir {repo_path} --plan {state.plan_file}"
        if state.preset:
            reexec_args += f" --preset {state.preset}"
        if getattr(args, "no_worktree", False):
            reexec_args += " --no-worktree"
        if state.model:
            reexec_args += f" --model {state.model}"

        tmux._run_tmux(
            "new-session", "-d", "-s", session_name, "-n", "loop",
            f"bash -c '{conductor_bin} {reexec_args}'",
        )
        # Keep window open after exit + log all output
        tmux._run_tmux(
            "set-option", "-t", f"{session_name}:loop",
            "remain-on-exit", "on", check=False,
        )
        log_file = storage.tmux_log(state.name, "loop-main")
        tmux._run_tmux(
            "pipe-pane", "-t", f"{session_name}:loop",
            f"cat >> {log_file}", check=False,
        )

        tmux_env = os.environ.get("TMUX", "")
        if not tmux_env:
            os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])
        else:
            subprocess.run(["tmux", "switch-client", "-t", session_name], check=False)
            sys.exit(0)
    else:
        # Inside tmux — run the loop directly
        from conductor.core.loop import run_loop_in_tmux
        run_loop_in_tmux(state, repo_path, storage)


def _cmd_loop_status(args):
    """Show loop status."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_dir(args.name) / "LOOP-STATE.json"
    if not state_path.exists():
        print("No loop found. Run 'conductor loop' first.")
        sys.exit(0)

    from conductor.core.loop import LoopState
    state = LoopState.model_validate_json(state_path.read_text(encoding="utf-8"))

    isatty = sys.stdout.isatty()
    green = "\033[32m" if isatty else ""
    red = "\033[31m" if isatty else ""
    yellow = "\033[33m" if isatty else ""
    cyan = "\033[36m" if isatty else ""
    dim = "\033[2m" if isatty else ""
    bold = "\033[1m" if isatty else ""
    reset = "\033[0m" if isatty else ""

    print(f"{bold}Loop:{reset} {state.name}")
    print(f"{bold}Status:{reset} {state.status}")
    print(f"{bold}Sessions:{reset} {state.session_count}")
    if state.worktree:
        print(f"{bold}Worktree:{reset} {state.worktree}")
    print()

    for t in state.tasks:
        if t.status == "completed":
            icon = f"{green}[x]{reset}"
            extra = f" {dim}({t.commit}){reset}" if t.commit else ""
        elif t.status == "in_progress":
            icon = f"{cyan}[>]{reset}"
            extra = f" {yellow}(attempt {t.attempts}){reset}"
        elif t.status == "failed":
            icon = f"{red}[!]{reset}"
            extra = f" {red}(failed after {t.attempts} attempts){reset}"
        else:
            icon = f"{dim}[ ]{reset}"
            extra = ""
        print(f"  {icon} {t.index}: {t.name}{extra}")

    completed = sum(1 for t in state.tasks if t.status == "completed")
    failed = sum(1 for t in state.tasks if t.status == "failed")
    print(f"\n  {green}{completed}{reset} done, {red}{failed}{reset} failed / {len(state.tasks)} total")


def _cmd_validate(args):
    """Run validation checks against the project."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("Error: No state found. Run 'conductor plan' first.")
        sys.exit(1)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    from conductor.core.validation import (
        ValidationContext,
        validate_and_fix,
        run_validation,
    )

    # Determine project directory: integration worktree or repo root
    project_dir = repo_path
    if state.integration and state.integration.branch:
        if state.worktrees_base:
            wt_path = Path(state.worktrees_base) / f"integration-{state.project_name}"
        else:
            wt_path = Path("/tmp") / f"conductor-integration-{state.project_name}"
        if wt_path.exists():
            project_dir = wt_path

    stage = "integration" if (args.smoke or args.integration) else "post-run"

    vctx = ValidationContext(
        project_dir=project_dir,
        stage=stage,
        feature_name=state.project_name,
        state=state if args.integration else None,
    )

    if args.fix:
        max_att = args.max_attempts if hasattr(args, "max_attempts") else 3
        result = asyncio.run(validate_and_fix(vctx, max_attempts=max_att))
    else:
        result = asyncio.run(run_validation(vctx))

    # Print results
    isatty = sys.stdout.isatty()
    green = "\033[32m" if isatty else ""
    red = "\033[31m" if isatty else ""
    reset = "\033[0m" if isatty else ""

    for check in result.checks:
        icon = f"{green}✓{reset}" if check.passed else f"{red}✗{reset}"
        print(f"  {icon} {check.name} ({check.duration_s:.1f}s)")
        if not check.passed:
            # Indent error output
            for line in check.output.strip().splitlines()[-10:]:
                print(f"    {line}")

    print()
    if result.passed:
        print(f"{green}All checks passed{reset}: {result.summary}")
    else:
        print(f"{red}Validation failed{reset}: {result.summary}")
        sys.exit(1)


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="conductor", description="Conductor orchestration tool"
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")

    # Common args for most commands
    def _add_common(p):
        p.add_argument("--name", required=True, help="Project name")
        p.add_argument("--project-dir", default=None, help="Project root directory")

    p_init = subparsers.add_parser("init", help="Initialize a new conductor project")
    _add_common(p_init)
    p_init.add_argument("--base-branch", default=None)
    p_init.add_argument("--preset", default="base")

    p_plan = subparsers.add_parser("plan", help="Generate a plan via Claude")
    _add_common(p_plan)
    p_plan.add_argument("--base-branch", default=None)

    p_go = subparsers.add_parser("go", help="One-shot resumable: init → plan → run")
    _add_common(p_go)
    p_go.add_argument("--plan", default=None, help="Path to brief/plan file (required on first run)")
    p_go.add_argument("--preset", default="base", help="Preset name (default: base, auto-detected)")
    p_go.add_argument("--base-branch", default=None, help="Base branch (auto-detected if omitted)")
    p_go.add_argument("--no-overnight", action="store_true", help="Disable auto-answering speccer questions")
    p_go.add_argument("--quick", action="store_true", default=True, help="Quality gate only between phases; full CI+review at end (default: on)")
    p_go.add_argument("--no-quick", action="store_true", help="Disable quick mode (run full CI+review between phases)")
    p_go.add_argument("--max-parallel", type=int, default=1, help="Max parallel runs (default: 1)")
    p_go.add_argument("--worktrees-base", default=None, help="Base directory for worktrees")
    p_go.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)

    p_run = subparsers.add_parser("run", help="Execute the orchestration loop")
    _add_common(p_run)
    p_run.add_argument("--no-overnight", action="store_true", help="Disable auto-answering speccer questions")
    p_run.add_argument("--quick", action="store_true", help="Quality gate only between phases; full CI+review at end of run")
    p_run.add_argument("--max-parallel", type=int, default=None, help="Max parallel runs (default: 1, 0=unlimited)")
    p_run.add_argument("--worktrees-base", default=None, help="Base directory for worktrees (default: from preset or <project>/../worktrees)")
    p_run.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)

    p_status = subparsers.add_parser("status", help="Show run status")
    _add_common(p_status)

    p_log = subparsers.add_parser("log", help="Show conductor log")
    _add_common(p_log)
    p_log.add_argument("--tail", type=int, default=50)

    p_cleanup = subparsers.add_parser("cleanup", help="Clean up worktrees and branches")
    _add_common(p_cleanup)
    p_cleanup.add_argument("--force", action="store_true")

    p_learnings = subparsers.add_parser(
        "learnings", help="Review learnings and update CLAUDE.md"
    )
    _add_common(p_learnings)

    p_loop = subparsers.add_parser("loop", help="Run a persistent prompt loop against a plan file")
    _add_common(p_loop)
    p_loop.add_argument("--plan", required=False, help="Path to plan markdown file")
    p_loop.add_argument("--preset", default=None, help="Preset name (auto-detected if omitted)")
    p_loop.add_argument("--base-branch", default=None, help="Base branch (auto-detected if omitted)")
    p_loop.add_argument("--model", default=None, help="Claude model to use")
    p_loop.add_argument("--no-worktree", action="store_true", help="Work in project dir instead of creating a worktree")
    p_loop.add_argument("--worktrees-base", default=None, help="Base directory for worktrees (default: from preset or <project>/../<name>-<branch>)")
    p_loop.add_argument("--reset", action="store_true", help="Reset and re-initialize the loop")
    p_loop.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)

    p_loop_status = subparsers.add_parser("loop-status", help="Show loop progress")
    _add_common(p_loop_status)

    p_validate = subparsers.add_parser("validate", help="Run validation checks")
    _add_common(p_validate)
    p_validate.add_argument(
        "--fix", action="store_true", help="Enable self-healing loop"
    )
    p_validate.add_argument("--smoke", action="store_true", help="Include smoke tests")
    p_validate.add_argument(
        "--integration", action="store_true", help="Include integration tests"
    )
    p_validate.add_argument(
        "--max-attempts", type=int, default=3, help="Max self-healing attempts (default: 3)"
    )

    handlers = {
        "init": _cmd_init,
        "plan": _cmd_plan,
        "go": _cmd_go,
        "run": _cmd_run,
        "status": _cmd_status,
        "log": _cmd_log,
        "cleanup": _cmd_cleanup,
        "learnings": _cmd_learnings,
        "loop": _cmd_loop,
        "loop-status": _cmd_loop_status,
        "validate": _cmd_validate,
    }

    args = parser.parse_args()

    if args.subcommand is None:
        parser.print_help()
        sys.exit(1)

    handlers[args.subcommand](args)


if __name__ == "__main__":
    main()
