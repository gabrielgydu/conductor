"""Conductor CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
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
from conductor.core.tmux import forwarded_env, forwarded_vars


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


def _self_argv(*args: str) -> list[str]:
    """argv that runs this conductor again: same interpreter, same installed package.

    Works for a wheel install as well as the repo wrapper. -P keeps the
    current directory off sys.path, so a project's own `conductor` module
    can't shadow ours.
    """
    return [sys.executable, "-P", "-m", "conductor", *args]


# ─── tmux ────────────────────────────────────────────────────────────────────

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "tcsh", "csh"}


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)


def _tmux_target(session: str, window: str | None = None) -> str:
    """Exact-match tmux target for *session* (and *window*).

    A bare ``-t conductor-foo`` falls back to prefix matching, so it resolves
    to ``conductor-foobar`` when that is the only candidate, and a '.' in the
    name is read as a pane index. ``=name:`` and ``=name:=window`` match
    exactly.
    """
    target = f"={session}:"
    return f"{target}={window}" if window else target


def _has_children(pid: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-P", pid], capture_output=True).returncode == 0
    except FileNotFoundError:
        return True  # cannot tell: assume busy


def _busy_tmux_panes(session: str, window: str | None = None) -> list[str]:
    """Describe the panes of *session* (or just of its *window*) still running something.

    A pane is idle when it is dead (remain-on-exit keeps finished windows
    around) or sits at a shell prompt with no child processes.
    """
    scope = ["-t", _tmux_target(session, window)] if window else ["-s", "-t", _tmux_target(session)]
    r = _tmux(
        "list-panes", *scope,
        "-F", "#{pane_dead}\t#{pane_pid}\t#{window_name}\t#{pane_current_command}",
    )
    busy = []
    for line in r.stdout.splitlines():
        dead, pid, name, command = (line.split("\t") + ["", "", "", ""])[:4]
        if dead == "1" or (command in _SHELLS and not _has_children(pid)):
            continue
        busy.append(f"{name} ({command}, pid {pid})")
    return busy


def _refuse_live_session(session: str, busy: list[str]) -> None:
    print(f"Error: tmux session '{session}' is still running: {', '.join(busy)}")
    print(f"  Watch it:  tmux attach -t '={session}:'")
    print(f"  Stop it:   tmux kill-session -t '={session}:'")
    sys.exit(1)


def _kill_stale_tmux_session(session: str) -> bool:
    """Kill *session* if it only holds finished or idle panes. True if it was killed.

    Exits with an error instead while anything in it is still running: that is
    a live orchestrator, loop or Claude session, not a leftover.
    """
    if _tmux("has-session", "-t", _tmux_target(session)).returncode != 0:
        return False
    busy = _busy_tmux_panes(session)
    if busy:
        _refuse_live_session(session, busy)
    _tmux("kill-session", "-t", _tmux_target(session))
    return True


def _has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _launch_in_tmux(
    session: str,
    window: str,
    argv: list[str],
    log_file: Path,
    *,
    login_shell: bool = False,
    adopt_runners: bool = False,
) -> None:
    """Run *argv* in a detached tmux session, then attach or switch to it.

    Without a terminal on stdin/stdout the session is left running detached.
    With *adopt_runners*, a session whose own *window* is dead but which still
    has busy panes (runners the previous orchestrator left working) is reused:
    the new orchestrator adopts those runners. Otherwise any busy pane refuses.
    """
    shell = "bash -lc" if login_shell else "bash -c"
    command = f"{shell} {shlex.quote(shlex.join(argv))}"
    target = _tmux_target(session, window)
    log_cmd = f"cat >> {shlex.quote(str(log_file))}"

    reuse = False
    if _tmux("has-session", "-t", _tmux_target(session)).returncode == 0:
        busy = _busy_tmux_panes(session)
        if busy and adopt_runners and not _busy_tmux_panes(session, window):
            reuse = True
        elif busy:
            _refuse_live_session(session, busy)
        else:
            _tmux("kill-session", "-t", _tmux_target(session))

    # One tmux invocation, so remain-on-exit and the log pipe are in place even
    # when the command exits immediately.
    if reuse:
        _tmux("kill-window", "-t", target)  # the dead orchestrator's pane, if still there
        for name, value in forwarded_vars().items():
            _tmux("set-environment", "-t", f"={session}", name, value)
        r = _tmux(
            "new-window", "-t", _tmux_target(session), "-n", window, command,
            ";", "set-option", "-t", target, "remain-on-exit", "on",
            ";", "pipe-pane", "-t", target, log_cmd,
        )
    else:
        r = _tmux(
            "new-session", "-d", "-s", session, *forwarded_env(), "-n", window, command,
            ";", "set-option", "-t", target, "remain-on-exit", "on",
            ";", "pipe-pane", "-t", target, log_cmd,
        )
    if r.returncode != 0:
        print(f"Error: could not start tmux session '{session}': {r.stderr.strip()}")
        sys.exit(1)

    if not _has_tty():
        print(f"started in tmux session {session}; attach with: tmux attach -t {_tmux_target(session)}")
        sys.exit(0)
    if not os.environ.get("TMUX"):
        os.execvp("tmux", ["tmux", "attach-session", "-t", _tmux_target(session)])
    # Already in tmux — switch client
    subprocess.run(["tmux", "switch-client", "-t", _tmux_target(session)], check=False)
    sys.exit(0)


# Project names end up in tmux targets, branch names and storage paths.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _validate_name(name: str) -> None:
    """Exit with an error unless *name* is safe as a project/loop/manual-test name."""
    if not _NAME_RE.fullmatch(name):
        print(
            f"Error: invalid --name {name!r}: use letters, digits, '-' and '_', "
            "starting with a letter or digit.\n"
            "  Characters such as '.', ':', '/' or spaces break tmux targets, "
            "branch names and storage paths."
        )
        sys.exit(1)


def _resolve_plan_path(plan: str, repo_path: Path) -> Path:
    """Existing file for a --plan value: relative paths mean cwd, as in `go`.

    Loops used to resolve them against --project-dir, so that location is still
    tried (with a note) when nothing exists under the current directory.
    """
    path = Path(plan)
    if path.is_absolute():
        candidates = [path]
    else:
        candidates = [Path.cwd() / path, repo_path / path]
    for i, candidate in enumerate(candidates):
        if candidate.exists():
            if i:
                print(f"Note: {candidates[0]} does not exist; using {candidate}", file=sys.stderr)
            return candidate
    print(f"Error: Plan file not found: {candidates[0]}")
    sys.exit(1)


# ─── Project lock ────────────────────────────────────────────────────────────


def _lock_project(storage: StorageResolver, name: str, command: str) -> None:
    """Take the project's single-instance lock, or exit naming whoever holds it."""
    from conductor.core.lock import LockHeld, acquire_project_lock

    try:
        acquire_project_lock(storage.conductor_dir(name), command)
    except LockHeld as exc:
        print(f"Error: {exc}")
        print("Wait for it to finish, or stop it first.")
        sys.exit(1)


def _unlock_project(storage: StorageResolver, name: str) -> None:
    """Hand the lock over, e.g. to the process a tmux re-exec is about to start."""
    from conductor.core.lock import release_project_lock

    release_project_lock(storage.conductor_dir(name))


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


# ─── Presets ─────────────────────────────────────────────────────────────────


def _resolve_preset(name: str | None, repo_path: Path) -> str:
    """The preset to use: *name*, or the detected one when None. Exits if it can't be loaded."""
    from conductor.core.presets import detect_preset, load_preset

    try:
        if name is None:
            name = detect_preset(repo_path)
            if name != "base":
                print(f"Auto-detected preset: {name}", file=sys.stderr)
        explicit = Path(name).expanduser()
        if explicit.suffix == ".toml" and explicit.is_file():
            # Saved in the state and loaded later from other directories
            # (the tmux re-exec, stage worktrees): relative to here only.
            name = str(explicit.resolve())
        load_preset(name, repo_path)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    return name


# ─── Commands ────────────────────────────────────────────────────────────────


def _brief_has_content(text: str) -> bool:
    """True when a brief says more than the init template's headings and comments."""
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    stripped = re.sub(r"^#+\s.*$", "", stripped, flags=re.MULTILINE)
    return len(stripped.strip()) >= 20


def _cmd_init(args, *, next_steps: bool = True):
    """Initialize a new conductor project."""
    _validate_name(args.name)
    if args.base_branch is None:
        args.base_branch = _detect_git_branch(args.project_dir)
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    explicit_preset = getattr(args, "preset", None)
    preset_name = _resolve_preset(explicit_preset, repo_path)
    conductor_dir = storage.conductor_dir(args.name)
    conductor_dir.mkdir(parents=True, exist_ok=True)

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
    else:
        state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if explicit_preset is not None and state.preset != preset_name:
            _lock_project(storage, args.name, "init")
            print(f"Switching preset: {state.preset} -> {preset_name}", file=sys.stderr)
            state.preset = preset_name
            atomic_save(state, state_path)
        elif state.preset != preset_name:
            print(
                f"Note: detected preset '{preset_name}', but this project keeps '{state.preset}'. "
                f"Pass --preset {preset_name} to switch.",
                file=sys.stderr,
            )
            preset_name = state.preset

    print(f"Initialized conductor project: {args.name}")
    print(f"  Storage: {conductor_dir}")
    print(f"  Preset:  {preset_name}")
    if next_steps:
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
        r"""<description\s+name=["']([\w\-]+)["']\s*>(.*?)</description>""",
        output_text,
        re.DOTALL,
    ):
        descriptions[desc_match.group(1)] = desc_match.group(2).strip()

    return runs_json, descriptions


# Run and stage names become branch names, worktree paths and tmux windows.
_PLAN_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_SPEC_MODES = {"fullstack", "frontend", "backend", "testing"}  # speccer --mode choices


def _plan_problems(runs_json, descriptions: dict[str, str]) -> list[str]:
    """Everything wrong with a parsed plan that would only fail later, at run time."""
    if not isinstance(runs_json, list) or not runs_json:
        return ["the conductor-state block must be a non-empty JSON array of runs"]
    problems: list[str] = []
    run_names: set[str] = set()
    stage_counts: dict[int, int] = {}
    for i, run in enumerate(runs_json):
        if not isinstance(run, dict):
            problems.append(f"run {i} is not a JSON object")
            continue
        name = run.get("name")
        if not isinstance(name, str) or not _PLAN_NAME_RE.fullmatch(name):
            problems.append(f"run {i}: name {name!r} must be kebab-case ([a-z0-9-])")
        elif name in run_names:
            problems.append(f"run {i}: name {name!r} is used twice")
        else:
            run_names.add(name)
        if "index" in run and run["index"] != i:
            problems.append(f"run {i}: \"index\" is {run['index']!r}; runs must be numbered 0..n-1 in order")
        deps = run.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, int) and not isinstance(d, bool) for d in deps):
            problems.append(f"run {i}: depends_on must be a list of run indexes")
        constitution = run.get("constitution", [])
        if not isinstance(constitution, list) or not all(isinstance(c, str) for c in constitution):
            problems.append(f"run {i}: constitution must be a list of strings")
        if not isinstance(run.get("description", ""), str):
            problems.append(f"run {i}: description must be a string")
        stages = run.get("stages")
        if not isinstance(stages, list) or not stages:
            problems.append(f"run {i}: needs a non-empty \"stages\" list")
            continue
        stage_counts[i] = len(stages)
        stage_names: set[str] = set()
        for s, stage in enumerate(stages):
            if not isinstance(stage, dict):
                problems.append(f"run {i} stage {s} is not a JSON object")
                continue
            stage_name = stage.get("name")
            if not isinstance(stage_name, str) or not _PLAN_NAME_RE.fullmatch(stage_name):
                problems.append(f"run {i} stage {s}: name {stage_name!r} must be kebab-case ([a-z0-9-])")
                continue
            if stage_name in stage_names:
                problems.append(f"run {i}: stage name {stage_name!r} is used twice")
            stage_names.add(stage_name)
            spec_mode = stage.get("spec_mode", stage_name)
            if spec_mode not in _SPEC_MODES:
                problems.append(
                    f"run {i} stage {stage_name}: spec_mode {spec_mode!r} must be one of {sorted(_SPEC_MODES)}"
                )
            if not descriptions.get(f"run-{i}-{stage_name}"):
                problems.append(f'missing <description name="run-{i}-{stage_name}"> block')

    # Context wiring points at another stage's worktree, so that stage must be
    # done before this one starts: an earlier stage of the same run, or a stage
    # of a run this one depends on.
    for i, run in enumerate(runs_json):
        if not isinstance(run, dict) or i not in stage_counts:
            continue
        deps = run.get("depends_on", [])
        for s, stage in enumerate(run["stages"]):
            wiring = stage.get("context_wiring") if isinstance(stage, dict) else None
            if wiring is None:
                continue
            where = f"run {i} stage {s} context_wiring"
            if not isinstance(wiring, dict):
                problems.append(f"{where} must be an object or null")
            elif wiring.get("type") == "external":
                if not wiring.get("source_path"):
                    problems.append(f"{where}: external wiring needs a source_path")
            else:
                src_run, src_stage = wiring.get("source_run"), wiring.get("source_stage")
                if not (isinstance(src_run, int) and isinstance(src_stage, int) and wiring.get("source_path")):
                    problems.append(f"{where}: needs integer source_run and source_stage and a source_path")
                elif src_run not in stage_counts or not 0 <= src_stage < stage_counts[src_run]:
                    problems.append(f"{where}: run {src_run} stage {src_stage} does not exist")
                elif src_run == i and src_stage >= s:
                    problems.append(f"{where}: must point at an earlier stage of the same run")
                elif src_run != i and isinstance(deps, list) and src_run not in deps:
                    problems.append(f"{where}: run {src_run} must be in depends_on")

    if not problems:
        try:
            validate_dag([
                RunState(index=i, name=run["name"], description="", depends_on=run.get("depends_on", []))
                for i, run in enumerate(runs_json)
            ])
        except ValueError as e:
            problems.append(f"invalid dependency graph: {e}")
    return problems


def _generate_plan_once(prompt: str, repo_path: Path, run_claude) -> tuple[list, dict[str, str], list[str]]:
    """One Claude planning call. Returns (runs, descriptions, problems)."""
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
        return [], {}, [f"could not parse the plan: {e}"]
    return runs_json, descriptions, _plan_problems(runs_json, descriptions)


def _generate_valid_plan(prompt: str, repo_path: Path, run_claude) -> tuple[list, dict[str, str]]:
    """Generate a plan; if the output is malformed, ask once more with the problems listed."""
    runs_json, descriptions, problems = _generate_plan_once(prompt, repo_path, run_claude)
    if problems:
        print("Plan output has problems — asking Claude to fix them:", file=sys.stderr)
        for problem in problems:
            print(f"  • {problem}", file=sys.stderr)
        prompt += (
            "\n\n## Problems with a previous attempt\n"
            "A previous answer to this prompt could not be used. Produce the complete plan "
            "again, in the required format, without these problems:\n"
            + "".join(f"- {problem}\n" for problem in problems)
        )
        runs_json, descriptions, problems = _generate_plan_once(prompt, repo_path, run_claude)
    if problems:
        print("Error: Claude's plan is still invalid:")
        for problem in problems:
            print(f"  • {problem}")
        sys.exit(1)
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
    runs_json, descriptions = _generate_valid_plan(prompt, repo_path, run_claude)

    # Step 2: Completeness review
    review_feedback = _run_completeness_review(
        brief_content, runs_json, descriptions, repo_path, run_claude
    )

    if review_feedback is None:
        return runs_json, descriptions

    # Step 3: Re-generate once with feedback (no second review)
    print("Review found gaps — re-generating plan...", file=sys.stderr)
    return _generate_valid_plan(prompt + "\n\n" + review_feedback, repo_path, run_claude)


def _run_completeness_review(
    brief_content: str,
    runs_json: list,
    descriptions: dict[str, str],
    repo_path: Path,
    run_claude,
) -> str | None:
    """Run completeness review. Returns feedback string if issues found, None if passed."""
    review_template = _load_prompt_template("completeness-review")  # checked by _cmd_plan

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


def _cmd_plan(args, *, next_steps: bool = True):
    """Generate a plan using Claude."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    _lock_project(storage, args.name, "plan")

    state_path = storage.conductor_state(args.name)
    existing = (
        ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else None
    )
    # Re-planning replaces the runs, and with them the only record of their
    # worktrees, branches and integration PR. Refuse before spending a Claude call.
    if existing is not None and not getattr(args, "force", False):
        started = [
            r.name for r in existing.runs
            if r.status != RunStatus.PENDING or any(s.worktree or s.branch for s in r.stages)
        ]
        if started or existing.integration is not None:
            print(
                f"Error: '{args.name}' already has started runs "
                f"({', '.join(started) or 'integration'}); re-planning would drop their "
                "worktrees, branches and integration from state."
            )
            print(f"  Run `conductor cleanup --name {args.name}` first, then re-plan with --force.")
            sys.exit(1)
    if args.base_branch is None:
        # The base chosen at init stands; only a new project uses the checked-out branch.
        args.base_branch = existing.base_branch if existing else _detect_git_branch(args.project_dir)

    brief_path = storage.conductor_brief(args.name)
    if not brief_path.exists():
        print(
            f"Error: FEATURE-BRIEF.md not found. Run 'conductor init --name {args.name}' first."
        )
        sys.exit(1)

    brief_content = brief_path.read_text(encoding="utf-8")
    if not _brief_has_content(brief_content):
        print(
            "Error: FEATURE-BRIEF.md has no content beyond the template headings "
            f"(or under 20 characters). Fill it in before planning: {brief_path}"
        )
        sys.exit(1)

    from conductor.core.claude import run_claude

    # Build context with file listing and brief
    file_listing = _generate_file_listing(repo_path)

    preset_info = f"\n## Preset: {existing.preset}\n" if existing and existing.preset else ""

    context = (
        f"## Feature Brief\n{brief_content}\n\n"
        f"## Repository File Structure (top {min(500, len(file_listing.splitlines()))} files)\n"
        f"```\n{file_listing}\n```\n"
        f"{preset_info}"
    )

    # Both templates up front, so a broken install fails before any Claude call
    try:
        template = _load_prompt_template("plan")
        _load_prompt_template("completeness-review")
    except FileNotFoundError as exc:
        print(f"Error: {exc}. The conductor installation is incomplete; reinstall it.")
        sys.exit(1)
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

    # Write description files
    for i, run_data in enumerate(runs_json):
        for stage_data in run_data.get("stages", []):
            stage_name = stage_data["name"]
            desc_key = f"run-{i}-{stage_name}"
            desc_content = descriptions.get(desc_key, "")
            if desc_content:
                desc_file = conductor_dir / f"description-{desc_key}.md"
                desc_file.write_text(desc_content, encoding="utf-8")

    # Build and save state. Project settings (preset, overnight, quick, ...)
    # carry over; the integration belonged to the old runs.
    if existing is not None:
        state = existing.model_copy(
            update={"base_branch": args.base_branch, "runs": runs, "integration": None}
        )
    else:
        state = ConductorState(project_name=args.name, base_branch=args.base_branch, runs=runs)
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
    if next_steps:
        print(f"Review, then run: conductor run --name {args.name}")


def _cmd_go(args):
    """One-shot resumable command: init → copy brief → plan → run."""
    _validate_name(args.name)
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    _lock_project(storage, args.name, "go")
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
    existing = (
        ConductorState.model_validate_json(state_path.read_text(encoding="utf-8")) if has_state else None
    )
    has_runs = bool(existing and existing.runs)

    plan_text = None
    plan_file = getattr(args, "plan", None)
    if plan_file:
        plan_path = Path(plan_file)
        if not plan_path.is_absolute():
            plan_path = Path.cwd() / plan_path
        if not plan_path.exists():
            print(f"Error: Plan file not found: {plan_path}")
            sys.exit(1)
        plan_text = plan_path.read_text(encoding="utf-8")

    # Phase 1: Init
    if not has_state:
        _phase(1, 3, "Initializing...")
        _cmd_init(args, next_steps=False)
    else:
        _phase(1, 3, "Initializing", skip=True)

    brief_text = brief_path.read_text(encoding="utf-8") if brief_path.exists() else ""
    if has_runs:
        # A planned project resumes straight into the run; the brief has done its job.
        if plan_text is not None and plan_text != brief_text:
            print(
                f"Warning: --plan ignored: '{args.name}' is already planned ({len(existing.runs)} runs). "
                f"To re-plan, run `conductor plan --name {args.name} --force`.",
                file=sys.stderr,
            )
    elif plan_text is not None:
        # Nothing has been planned from the brief yet, so --plan replaces it.
        if plan_text != brief_text:
            if _brief_has_content(brief_text):
                print(f"  Replacing the existing brief with {plan_path}", file=sys.stderr)
            brief_path.write_text(plan_text, encoding="utf-8")
            print(f"  Copied brief from {plan_path}", file=sys.stderr)
    elif not _brief_has_content(brief_text):
        print("Error: Brief is empty and no --plan file provided.")
        print(f"Either fill in {brief_path} or pass --plan <file>.")
        sys.exit(1)

    # Phase 2: Plan
    if not has_runs:
        _phase(2, 3, "Planning...")
        _cmd_plan(args, next_steps=False)
        # go turns quick mode on for a project it plans; on resume the saved
        # setting stands unless --quick/--no-quick is given.
        if args.quick is None:
            args.quick = True
    else:
        _phase(2, 3, "Planning", skip=True)

    # Phase 3: Run
    _phase(3, 3, "Running...")
    _cmd_run(args)


def _run_overrides(args) -> dict:
    """The run/go settings given on the command line, as ConductorState fields.

    A flag that is left out keeps the value saved in state, so a resume
    without flags does not reset anything. Overnight is the exception: it is
    on unless --no-overnight is given.
    """
    overrides: dict = {"overnight": not getattr(args, "no_overnight", False)}
    if getattr(args, "quick", None) is not None:
        overrides["quick"] = args.quick
    if getattr(args, "max_parallel", None) is not None:
        overrides["max_parallel"] = args.max_parallel
    if getattr(args, "worktrees_base", None):
        overrides["worktrees_base"] = str(Path(args.worktrees_base).resolve())
    return overrides


def _run_override_flags(overrides: dict) -> list[str]:
    """`run` arguments that reproduce *overrides* in the tmux re-exec."""
    flags = []
    if not overrides["overnight"]:
        flags.append("--no-overnight")
    if "quick" in overrides:
        flags.append("--quick" if overrides["quick"] else "--no-quick")
    if "max_parallel" in overrides:
        flags.extend(["--max-parallel", str(overrides["max_parallel"])])
    if "worktrees_base" in overrides:
        flags.extend(["--worktrees-base", overrides["worktrees_base"]])
    return flags


def _cmd_run(args):
    """Execute the conductor orchestration loop."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    _lock_project(storage, args.name, "run")

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("Error: No plan found. Run 'conductor plan' first.")
        sys.exit(1)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    overrides = _run_overrides(args)
    for field, value in overrides.items():
        setattr(state, field, value)

    inside_tmux = getattr(args, "inside_tmux", False)

    if not inside_tmux:
        # Re-exec inside tmux session
        reexec = _self_argv("run", "--inside-tmux", "--name", args.name, "--project-dir", str(repo_path))
        reexec.extend(_run_override_flags(overrides))
        if getattr(args, "reintegrate", False):
            reexec.append("--reintegrate")

        _unlock_project(storage, args.name)
        _launch_in_tmux(
            f"conductor-{state.project_name}",
            "conductor",
            reexec,
            storage.tmux_log(state.project_name, "conductor-main"),
            adopt_runners=True,
        )
    else:
        # Inside tmux — run the loop directly
        from conductor.core.orchestrator import ConductorConfig, conductor_run_loop

        config = ConductorConfig(
            check_interval_s=state.check_interval_s,
            max_parallel=state.max_parallel,
            project_root=repo_path,
            overnight=state.overnight,
            reintegrate=getattr(args, "reintegrate", False),
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
        from conductor.core.orchestrator import _integration_retry_note

        integration = state.integration
        if integration.pr_url:
            pr = f"PR: {integration.pr_url}"
        elif integration.published is False:
            pr = "not published"
        else:
            pr = "no PR recorded"
        print(f"  Integration: {integration.status} (branch: {integration.branch}, {pr})")
        note = _integration_retry_note(integration)
        if note:
            print(f"  {yellow}Integration {note}{reset}")


def _cmd_log(args):
    """Show conductor log."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    log_path = storage.conductor_log(args.name)
    if not log_path.exists():
        print("No log yet. Run 'conductor run' first.")
        sys.exit(0)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    # lines[-0:] would be the whole log
    for line in lines[max(len(lines) - args.tail, 0) :]:
        print(line)


def _cmd_cleanup(args):
    """Clean up worktrees, branches, and tmux session."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("No conductor project found.")
        sys.exit(0)

    _lock_project(storage, args.name, "cleanup")
    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    # A leftover session is killed; a live one makes this exit with an error.
    session_name = f"conductor-{state.project_name}"
    if _kill_stale_tmux_session(session_name):
        print(f"Killed stale tmux session: {session_name}")

    repo = str(storage.repo_root)
    force = getattr(args, "force", False)

    def git(*cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *cmd], cwd=repo, capture_output=True, text=True)

    def branch_exists(name: str) -> bool:
        return git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0

    registered = {
        line.split(" ", 1)[1]
        for line in git("worktree", "list", "--porcelain").stdout.splitlines()
        if line.startswith("worktree ")
    }
    stage_branches = {s.branch for r in state.runs for s in r.stages if s.branch}
    # Commits reachable from a remote, the base branch or the integration
    # branch survive deleting a stage branch; anything else would be lost.
    # Conductor never deletes or rewrites the integration branch.
    safe_refs = [
        f"refs/heads/{b}"
        for b in (state.base_branch, state.integration.branch if state.integration else None)
        if b and b not in stage_branches and branch_exists(b)
    ]

    removed: list[str] = []
    deleted: list[str] = []
    kept: list[str] = []  # unsaved work, left in place on purpose
    failed: list[str] = []
    for run in state.runs:
        for stage in run.stages:
            if stage.worktree:
                wt = stage.worktree
                if wt in registered or str(Path(wt).resolve()) in registered:
                    r = git("worktree", "remove", *(["--force"] if force else []), wt)
                    if r.returncode != 0:
                        failed.append(f"worktree {wt}: {r.stderr.strip()}")
                        continue  # its branch is still checked out there
                    removed.append(wt)
                    stage.worktree = None
                elif Path(wt).exists():
                    failed.append(f"worktree {wt}: not a registered git worktree; left in place")
                else:
                    stage.worktree = None  # already gone
            # Branch names stay in state: integration and dependent runs find
            # a deleted branch's commits on origin, or by stage.head_sha.
            if stage.branch:
                if not branch_exists(stage.branch):
                    continue
                if not force:
                    r = git("rev-list", "--count", stage.branch, "--not", "--remotes", *safe_refs, "--")
                    unsaved = int(r.stdout.strip()) if r.returncode == 0 else None
                    if unsaved != 0:
                        why = f"{unsaved} commit(s)" if unsaved else "could not check its commits"
                        kept.append(f"{stage.branch} ({why} on no remote, base or integration branch)")
                        continue
                r = git("branch", "-D", stage.branch)
                if r.returncode != 0:
                    failed.append(f"branch {stage.branch}: {r.stderr.strip()}")
                    continue
                deleted.append(stage.branch)

    # Forget removed worktrees, so status/run/go don't trust stale paths.
    atomic_save(state, state_path)

    print(f"Removed {len(removed)} worktree(s), deleted {len(deleted)} branch(es).")
    if kept:
        print("Kept (use --force to delete unpushed/unmerged work):")
        for item in kept:
            print(f"  {item}")
    if failed:
        print("Failed:")
        for item in failed:
            print(f"  {item}")
        sys.exit(1)


def _cmd_learnings(args):
    """Review learnings from completed runs and update/create CLAUDE.md."""
    import asyncio

    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("Error: No state found. Run 'conductor plan' first.")
        sys.exit(1)

    _lock_project(storage, args.name, "learnings")
    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    from conductor.core.orchestrator import _review_learnings

    log_path = storage.conductor_log(args.name)
    audit_path = storage.conductor_audit(args.name)
    asyncio.run(_review_learnings(state, storage, log_path, audit_path))


def _cmd_loop(args):
    """Start or resume a conductor loop."""
    _validate_name(args.name)
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    _lock_project(storage, args.name, "loop")

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
        plan_path = _resolve_plan_path(args.plan, repo_path)

        plan_content = plan_path.read_text(encoding="utf-8")

        from conductor.core.loop import LoopState, parse_checklist, decompose_plan

        # Before decomposing, so a broken preset doesn't cost a Claude call
        preset_name = _resolve_preset(getattr(args, "preset", None), repo_path)

        # Try parsing as checklist first; if no tasks found, use Claude to decompose
        tasks = parse_checklist(plan_content)
        checklist_text = None
        if not tasks:
            print("No checklist found in plan — decomposing via Claude...", file=sys.stderr)
            tasks, checklist_text = decompose_plan(plan_content)

        if not tasks:
            print("Error: Could not extract tasks from plan.")
            sys.exit(1)

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
                _preset = _load_preset(preset_name, repo_path)
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
        reexec = _self_argv(
            "loop", "--inside-tmux", "--name", args.name,
            "--project-dir", str(repo_path), "--plan", state.plan_file,
        )
        if state.preset:
            reexec.extend(["--preset", state.preset])
        if getattr(args, "no_worktree", False):
            reexec.append("--no-worktree")
        if state.model:
            reexec.extend(["--model", state.model])

        _unlock_project(storage, args.name)
        _launch_in_tmux(
            f"conductor-loop-{state.name}",
            "loop",
            reexec,
            storage.tmux_log(state.name, "loop-main"),
        )
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


def _cmd_manual_test(args):
    """Start, resume, or initialize a manual-test run."""
    _validate_name(args.name)
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    _lock_project(storage, args.name, "manual-test")
    conductor_dir = storage.conductor_dir(args.name)

    from conductor.core.manual_test import (
        format_manual_status,
        initialize_manual_state,
        load_manual_state,
        manual_state_path,
        run_manual_test_in_tmux,
    )

    state_path = manual_state_path(conductor_dir)
    inside_tmux = getattr(args, "inside_tmux", False)

    if state_path.exists() and not getattr(args, "reset", False):
        state = load_manual_state(conductor_dir)
        done = sum(1 for s in state.scenarios if s.status in {"passed", "failed", "blocked", "policy_violation"})
        print(f"Resuming manual-test: {state.name} ({done}/{len(state.scenarios)} scenarios terminal)")
    else:
        if not args.plan:
            print("Error: --plan is required for new manual-test runs.")
            sys.exit(1)

        plan_path = _resolve_plan_path(args.plan, repo_path)

        preset_name = _resolve_preset(getattr(args, "preset", None), repo_path)
        try:
            state = initialize_manual_state(
                name=args.name,
                project_dir=repo_path,
                plan_file=plan_path,
                preset=preset_name,
                model=getattr(args, "model", None),
                max_turns=getattr(args, "max_turns", 200),
                conductor_dir=conductor_dir,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}")
            sys.exit(1)

        print(f"Initialized manual-test: {state.name}")
        print(f"  Scenarios: {len(state.scenarios)}")
        print(f"  Preset: {preset_name}")
        print(f"  State: {state_path}")

    if getattr(args, "init_only", False):
        print()
        print(format_manual_status(state))
        return

    if not inside_tmux:
        reexec = _self_argv("manual-test", "--inside-tmux", "--name", args.name, "--project-dir", str(repo_path))
        if state.plan_file:
            reexec.extend(["--plan", state.plan_file])
        if state.preset:
            reexec.extend(["--preset", state.preset])
        if state.model:
            reexec.extend(["--model", state.model])
        reexec.extend(["--max-turns", str(state.max_turns)])

        _unlock_project(storage, args.name)
        _launch_in_tmux(
            f"conductor-manual-test-{state.name}",
            "manual-test",
            reexec,
            storage.tmux_log(state.name, "manual-test-main"),
            login_shell=True,
        )
    else:
        run_manual_test_in_tmux(state, repo_path, storage)


def _cmd_manual_test_status(args):
    """Show manual-test status."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    conductor_dir = storage.conductor_dir(args.name)

    from conductor.core.manual_test import (
        format_manual_status,
        load_manual_state,
        manual_state_path,
    )

    if not manual_state_path(conductor_dir).exists():
        print("No manual-test found. Run 'conductor manual-test' first.")
        sys.exit(0)

    state = load_manual_state(conductor_dir)
    print(format_manual_status(state))


def _cmd_manual_test_report(args):
    """Generate and print the manual-test report."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)
    conductor_dir = storage.conductor_dir(args.name)

    from conductor.core.manual_test import (
        generate_manual_report,
        load_manual_state,
        manual_state_path,
        write_manual_report,
    )

    if not manual_state_path(conductor_dir).exists():
        print("No manual-test found. Run 'conductor manual-test' first.")
        sys.exit(0)

    state = load_manual_state(conductor_dir)
    path = write_manual_report(state, conductor_dir)
    print(f"Report written: {path}\n")
    print(generate_manual_report(state), end="")


def _cmd_validate(args):
    """Run validation checks against the project."""
    repo_path = Path(args.project_dir or ".").resolve()
    storage = StorageResolver(repo_path)

    state_path = storage.conductor_state(args.name)
    if not state_path.exists():
        print("Error: No state found. Run 'conductor plan' first.")
        sys.exit(1)

    state = ConductorState.model_validate_json(state_path.read_text(encoding="utf-8"))

    from conductor.core.presets import load_preset
    from conductor.core.validation import (
        ValidationContext,
        detect_checks,
        run_validation,
        validate_and_fix,
    )
    from conductor.integration.merge import integration_worktree_path

    # Load the preset from the repo root, as a run does; a typo must not turn
    # into a green "0/0 checks passed".
    try:
        preset = load_preset(state.preset, storage.repo_root)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    # Determine project directory: the integration worktree while it exists
    # (a merge in progress or interrupted), otherwise the given checkout
    project_dir = repo_path
    if state.integration and state.integration.branch:
        wt_path = integration_worktree_path(state, storage.repo_root)
        if wt_path.exists():
            project_dir = wt_path

    vctx = ValidationContext(
        project_dir=project_dir,
        stage="integration" if (args.smoke or args.integration) else "post-run",
        feature_name=state.project_name,
        state=state,
        preset=preset,
    )
    # Tool and preset checks always; smoke and the integration specs only on
    # request (detect_checks adds both for the integration stage).
    on_request = {"smoke": args.smoke, "integration-tests": args.integration}
    checks = [c for c in asyncio.run(detect_checks(vctx)) if c not in on_request]
    checks += [name for name, wanted in on_request.items() if wanted]

    if args.fix:
        result = asyncio.run(
            validate_and_fix(vctx, max_attempts=args.max_attempts, checks=checks)
        )
    else:
        result = asyncio.run(run_validation(vctx, checks=checks))

    # Print results
    isatty = sys.stdout.isatty()
    green = "\033[32m" if isatty else ""
    red = "\033[31m" if isatty else ""
    reset = "\033[0m" if isatty else ""

    if not result.checks:
        print(
            "No validation checks found: no frontend/package.json, vendor/bin/phpstan,"
            " vendor/bin/phpunit or [[validation.checks]] in the preset"
        )
        return

    for check in result.checks:
        icon = f"{green}✓{reset}" if check.passed else f"{red}✗{reset}"
        skipped = check.passed and check.output.startswith("skipped:")
        note = f" — {check.output.strip()}" if skipped else ""
        print(f"  {icon} {check.name} ({check.duration_s:.1f}s){note}")
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


_REINTEGRATE_HELP = (
    "Attempt the integration once more when all runs are finished, even if "
    "it is not due (e.g. after a failure that is not retried automatically)"
)


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
    p_init.add_argument("--preset", default=None, help="Preset name or .toml path (default: auto-detected, else base)")

    p_plan = subparsers.add_parser("plan", help="Generate a plan via Claude")
    _add_common(p_plan)
    p_plan.add_argument("--base-branch", default=None, help="Base branch (default: the project's, else the checked-out branch)")
    p_plan.add_argument(
        "--force", action="store_true",
        help="Re-plan even though runs have started (their worktrees and branches drop out of state)",
    )

    p_go = subparsers.add_parser("go", help="One-shot resumable: init → plan → run")
    _add_common(p_go)
    p_go.add_argument("--plan", default=None, help="Path to brief/plan file (required on first run)")
    p_go.add_argument("--preset", default=None, help="Preset name or .toml path (default: auto-detected, else base)")
    p_go.add_argument("--base-branch", default=None, help="Base branch (auto-detected if omitted)")
    p_go.add_argument("--no-overnight", action="store_true", help="Disable auto-answering speccer questions")
    p_go.add_argument("--overnight", action="store_true", help=argparse.SUPPRESS)  # deprecated no-op
    p_go.add_argument(
        "--quick", action=argparse.BooleanOptionalAction, default=None,
        help="Quality gate only between phases; full CI+review at end "
        "(default: on when go plans the project, else the saved setting)",
    )
    p_go.add_argument("--max-parallel", type=int, default=None, help="Max parallel runs (default: the saved setting, 1 for a new project)")
    p_go.add_argument("--worktrees-base", default=None, help="Base directory for worktrees")
    p_go.add_argument("--reintegrate", action="store_true", help=_REINTEGRATE_HELP)
    p_go.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)

    p_run = subparsers.add_parser("run", help="Execute the orchestration loop")
    _add_common(p_run)
    p_run.add_argument("--no-overnight", action="store_true", help="Disable auto-answering speccer questions")
    p_run.add_argument("--overnight", action="store_true", help=argparse.SUPPRESS)  # deprecated no-op
    p_run.add_argument(
        "--quick", action=argparse.BooleanOptionalAction, default=None,
        help="Quality gate only between phases; full CI+review at end of run (default: the saved setting, off for a new project)",
    )
    p_run.add_argument("--max-parallel", type=int, default=None, help="Max parallel runs (default: the saved setting, 1 for a new project; 0=unlimited)")
    p_run.add_argument("--worktrees-base", default=None, help="Base directory for worktrees (default: from preset or <project>/../worktrees)")
    p_run.add_argument("--reintegrate", action="store_true", help=_REINTEGRATE_HELP)
    p_run.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)

    p_status = subparsers.add_parser("status", help="Show run status")
    _add_common(p_status)

    p_log = subparsers.add_parser("log", help="Show conductor log")
    _add_common(p_log)
    p_log.add_argument("--tail", type=_non_negative_int, default=50, help="Number of lines to show (default: 50)")

    p_cleanup = subparsers.add_parser("cleanup", help="Clean up worktrees and branches")
    _add_common(p_cleanup)
    p_cleanup.add_argument(
        "--force", action="store_true",
        help="Also delete worktrees with uncommitted changes and branches whose commits "
        "are on no remote, the base branch or the integration branch",
    )

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

    p_manual_test = subparsers.add_parser("manual-test", help="Run manual browser QA scenarios from a plan")
    _add_common(p_manual_test)
    p_manual_test.add_argument("--plan", required=False, help="Path to manual-test plan markdown file")
    p_manual_test.add_argument("--preset", default=None, help="Preset name (auto-detected if omitted)")
    p_manual_test.add_argument("--model", default=None, help="Claude model to use")
    p_manual_test.add_argument("--max-turns", type=int, default=200, help="Max Claude turns per generation")
    p_manual_test.add_argument("--init-only", action="store_true", help="Initialize state without launching Claude")
    p_manual_test.add_argument("--reset", action="store_true", help="Reset and re-initialize manual-test state")
    p_manual_test.add_argument("--inside-tmux", action="store_true", help=argparse.SUPPRESS)

    p_manual_test_status = subparsers.add_parser("manual-test-status", help="Show manual-test progress")
    _add_common(p_manual_test_status)

    p_manual_test_report = subparsers.add_parser("manual-test-report", help="Generate manual-test report")
    _add_common(p_manual_test_report)

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
        "manual-test": _cmd_manual_test,
        "manual-test-status": _cmd_manual_test_status,
        "manual-test-report": _cmd_manual_test_report,
        "validate": _cmd_validate,
    }

    args = parser.parse_args()

    if args.subcommand is None:
        parser.print_help()
        sys.exit(1)

    if getattr(args, "overnight", False):
        # Older docs passed --overnight; it was replaced by the default-on mode.
        print(
            "Note: --overnight is deprecated and has no effect: overnight mode is "
            "the default (pass --no-overnight to answer speccer questions yourself).",
            file=sys.stderr,
        )

    handlers[args.subcommand](args)


if __name__ == "__main__":
    main()
