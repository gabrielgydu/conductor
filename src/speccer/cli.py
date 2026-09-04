"""Speccer CLI — spec generation loop (INIT -> EXPLORING -> NEEDS_INPUT <-> SPECCING -> COMPLETE -> GENERATED).

The orchestrator calls speccer as:
  speccer init --feature <name> --spec-dir <path>
  speccer run --spec-dir <path>
  speccer run --continue --spec-dir <path>
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "conductor" / "prompts" / "speccer"


# ═══════════════════════════════════════════════════════════════════════════════
# Codex second-opinion snippet (appended to planning prompts)
# ═══════════════════════════════════════════════════════════════════════════════

_CODEX_SECOND_OPINION = """

## Second Opinion — Codex (non-blocking)

Before finalizing your plans or answers, get a second opinion from Codex (OpenAI o3).
Use the Agent tool with `subagent_type="codex:codex-rescue"` and ask it to review your
draft output. Incorporate its feedback where it makes good points, then finalize.

This is non-blocking: if the agent call fails, errors, or times out, proceed with your
own judgment. Do not let Codex failures delay or block your output.

**Logging requirement:** At the end of your output, include a `## Codex Second Opinion` section.
If Codex responded, summarize what it said and what you incorporated or disagreed with.
If the call failed or timed out, note that explicitly (e.g. "Codex was unavailable: <error>").
This section is important for auditability — always include it.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# ANSI helpers
# ═══════════════════════════════════════════════════════════════════════════════

_RESET   = "\033[0m"
_BOLD    = "\033[1m"
_DIM     = "\033[2m"
_RED     = "\033[31m"
_GREEN   = "\033[32m"
_YELLOW  = "\033[33m"
_CYAN    = "\033[36m"
_MAGENTA = "\033[35m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{_RESET}"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _die(msg: str, code: int = 1) -> None:
    _log(f"{_RED}{_BOLD}error:{_RESET} {msg}")
    sys.exit(code)


def _header(title: str) -> None:
    bar = "═" * 60
    _log(f"\n{_BOLD}{_CYAN}{bar}{_RESET}")
    _log(f"  {_BOLD}{title}{_RESET}")
    _log(f"{_BOLD}{_CYAN}{bar}{_RESET}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# PROGRESS.md helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _read_field(progress_file: Path, field: str, default: str = "") -> str:
    """Read a `FIELD: value` line from PROGRESS.md."""
    if not progress_file.exists():
        return default
    prefix = f"{field}:"
    for line in progress_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return default


def _write_field(progress_file: Path, field: str, value: str) -> None:
    """Replace `FIELD: ...` line in PROGRESS.md, or insert after first line if missing."""
    text = progress_file.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(field)}:.*$"
    replacement = f"{field}: {value}"
    new_text, n = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if n == 0:
        # Insert after the first line
        lines = text.splitlines(keepends=True)
        lines.insert(1, f"{replacement}\n")
        new_text = "".join(lines)
    progress_file.write_text(new_text, encoding="utf-8")


def read_status(spec_dir: Path) -> str:
    return _read_field(spec_dir / "PROGRESS.md", "STATUS", "UNKNOWN")


def read_iteration(spec_dir: Path) -> int:
    val = _read_field(spec_dir / "PROGRESS.md", "ITERATION", "0")
    try:
        return int(val)
    except ValueError:
        return 0


def read_mode(spec_dir: Path) -> str:
    return _read_field(spec_dir / "PROGRESS.md", "MODE", "fullstack")


def read_preset(spec_dir: Path) -> str:
    return _read_field(spec_dir / "PROGRESS.md", "PRESET", "")


def update_status(spec_dir: Path, status: str) -> None:
    _write_field(spec_dir / "PROGRESS.md", "STATUS", status)


def update_iteration(spec_dir: Path, iteration: int) -> None:
    _write_field(spec_dir / "PROGRESS.md", "ITERATION", str(iteration))


# ═══════════════════════════════════════════════════════════════════════════════
# Prompt building — mirrors build_spec_prompt / build_generate_prompt in bash
# ═══════════════════════════════════════════════════════════════════════════════

def _read_file(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _gather_domain_specs(spec_dir: Path) -> str:
    domains_dir = spec_dir / "domains"
    if not domains_dir.is_dir():
        return ""
    parts: list[str] = []
    for f in sorted(domains_dir.glob("*.md")):
        parts.append(f"--- {f.name} ---")
        parts.append(f.read_text(encoding="utf-8"))
        parts.append("")
    return "\n".join(parts)


def _has_domains(spec_dir: Path) -> bool:
    domains_dir = spec_dir / "domains"
    if not domains_dir.is_dir():
        return False
    return any(domains_dir.glob("*.md"))


def _process_template(
    template_path: Path,
    variables: dict[str, str],
    conditions: dict[str, bool],
    injections: dict[str, str],
) -> str:
    """Process a template file.

    Supports:
      {VARIABLE}          — scalar substitution
      {IF CONDITION}      — conditional block start
      {ENDIF CONDITION}   — conditional block end
      {INJECT:NAME}       — multi-line content injection
    """
    lines = template_path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    skip_stack: list[str] = []  # stack of condition names currently being skipped

    for line in lines:
        # {IF CONDITION}
        m = re.fullmatch(r"\{IF ([A-Z_]+)\}", line.strip())
        if m:
            cond = m.group(1)
            if skip_stack:
                # Already inside a skipped block — push sentinel to track nesting
                skip_stack.append(f"__nested__{cond}")
            elif not conditions.get(cond, False):
                skip_stack.append(cond)
            continue

        # {ENDIF CONDITION}
        m = re.fullmatch(r"\{ENDIF ([A-Z_]+)\}", line.strip())
        if m:
            cond = m.group(1)
            if skip_stack:
                top = skip_stack[-1]
                # Pop if it matches either the real condition or its nested sentinel
                if top == cond or top == f"__nested__{cond}":
                    skip_stack.pop()
            continue

        # Skip lines inside an excluded block
        if skip_stack:
            continue

        # {INJECT:NAME}
        m = re.fullmatch(r"\{INJECT:([A-Z_]+)\}", line.strip())
        if m:
            name = m.group(1)
            content = injections.get(name, "")
            if content:
                output.append(content)
            continue

        # Variable substitution
        for var, val in variables.items():
            line = line.replace("{" + var + "}", val)

        output.append(line)

    return "\n".join(output)


def build_spec_prompt(
    spec_dir: Path,
    feature_name: str,
    mode: str,
    status: str,
    iteration: int,
) -> str:
    """Build the spec iteration prompt by processing the appropriate template."""
    template_map = {
        "frontend": _PROMPTS_DIR / "frontend-spec-prompt.md",
        "backend":  _PROMPTS_DIR / "backend-spec-prompt.md",
        "testing":  _PROMPTS_DIR / "testing-spec-prompt.md",
    }
    template_file = template_map.get(mode, _PROMPTS_DIR / "spec-prompt.md")

    if not template_file.exists():
        _die(f"Prompt template not found: {template_file}")

    # Conditions
    is_init        = status == "INIT"
    is_needs_input = status == "NEEDS_INPUT"
    is_resume      = not is_init and not is_needs_input
    has_domains    = _has_domains(spec_dir)
    has_constitution    = (spec_dir / "CONSTITUTION.md").exists()
    has_backend_context = (spec_dir / "BACKEND-CONTEXT.md").exists() and (spec_dir / "BACKEND-CONTEXT.md").stat().st_size > 0
    has_spec_context    = (spec_dir / "SPEC-CONTEXT.md").exists() and (spec_dir / "SPEC-CONTEXT.md").stat().st_size > 0

    conditions = {
        "INIT":                is_init,
        "NEEDS_INPUT":         is_needs_input,
        "RESUME":              is_resume,
        "HAS_DOMAINS":         has_domains,
        "HAS_CONSTITUTION":    has_constitution,
        "HAS_BACKEND_CONTEXT": has_backend_context,
        "HAS_SPEC_CONTEXT":    has_spec_context,
    }

    # Injections
    injections: dict[str, str] = {
        "FEATURE_DESCRIPTION": _read_file(spec_dir / "FEATURE-DESCRIPTION.md"),
        "PROGRESS":            _read_file(spec_dir / "PROGRESS.md"),
        "HANDOFF":             _read_file(spec_dir / "HANDOFF.md"),
        "QUESTIONS":           _read_file(spec_dir / "QUESTIONS.md"),
        "FEATURE_TREE":        _read_file(spec_dir / "FEATURE-TREE.md"),
        "DOMAIN_SPECS":        _gather_domain_specs(spec_dir),
        "CONSTITUTION":        _read_file(spec_dir / "CONSTITUTION.md") if has_constitution else "",
        "BACKEND_CONTEXT":     _read_file(spec_dir / "BACKEND-CONTEXT.md") if has_backend_context else "",
        "SPEC_CONTEXT":        _read_file(spec_dir / "SPEC-CONTEXT.md") if has_spec_context else "",
    }

    # project_dir is parent of parent of spec_dir (spec_dir = .../features/<name>/spec)
    project_dir = str(spec_dir.parent.parent.parent)

    variables = {
        "FEATURE_NAME": feature_name,
        "PROJECT_DIR":  project_dir,
        "SPEC_DIR":     str(spec_dir),
        "ITERATION":    str(iteration),
        "STATUS":       status,
    }

    prompt = _process_template(template_file, variables, conditions, injections)
    return prompt + _CODEX_SECOND_OPINION


def build_generate_prompt(
    spec_dir: Path,
    feature_name: str,
    mode: str,
    split_prs: bool,
    preset_name: str,
) -> str:
    """Build the generate prompt."""
    template_map = {
        "frontend": _PROMPTS_DIR / "frontend-generate-prompt.md",
        "backend":  _PROMPTS_DIR / "backend-generate-prompt.md",
        "testing":  _PROMPTS_DIR / "testing-generate-prompt.md",
    }
    template_file = template_map.get(mode, _PROMPTS_DIR / "generate-prompt.md")

    if not template_file.exists():
        _die(f"Generate prompt template not found: {template_file}")

    has_constitution    = (spec_dir / "CONSTITUTION.md").exists()
    has_backend_context = (spec_dir / "BACKEND-CONTEXT.md").exists() and (spec_dir / "BACKEND-CONTEXT.md").stat().st_size > 0
    has_spec_context    = (spec_dir / "SPEC-CONTEXT.md").exists() and (spec_dir / "SPEC-CONTEXT.md").stat().st_size > 0

    conditions = {
        "SPLIT_PRS":           split_prs,
        "SINGLE_PR":           not split_prs,
        "HAS_CONSTITUTION":    has_constitution,
        "HAS_BACKEND_CONTEXT": has_backend_context,
        "HAS_SPEC_CONTEXT":    has_spec_context,
    }

    # docs_dir = parent of spec_dir (.../features/<name>)
    docs_dir = spec_dir.parent
    project_dir = str(docs_dir.parent.parent)

    model_default = "opus"

    injections: dict[str, str] = {
        "FEATURE_DESCRIPTION": _read_file(spec_dir / "FEATURE-DESCRIPTION.md"),
        "PROGRESS":            _read_file(spec_dir / "PROGRESS.md"),
        "FEATURE_TREE":        _read_file(spec_dir / "FEATURE-TREE.md"),
        "DOMAIN_SPECS":        _gather_domain_specs(spec_dir),
        "CONSTITUTION":        _read_file(spec_dir / "CONSTITUTION.md") if has_constitution else "",
        "BACKEND_CONTEXT":     _read_file(spec_dir / "BACKEND-CONTEXT.md") if has_backend_context else "",
        "SPEC_CONTEXT":        _read_file(spec_dir / "SPEC-CONTEXT.md") if has_spec_context else "",
    }

    variables = {
        "FEATURE_NAME": feature_name,
        "PROJECT_DIR":  project_dir,
        "DOCS_DIR":     str(docs_dir),
        "SPEC_DIR":     str(spec_dir),
        "CONDUCTOR_DIR": str(_PROMPTS_DIR),
        "PRESET":       preset_name,
        "MODEL":        model_default,
    }

    prompt = _process_template(template_file, variables, conditions, injections)
    return prompt + _CODEX_SECOND_OPINION


# ═══════════════════════════════════════════════════════════════════════════════
# Text extraction from Claude stream-json output
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_text_from_output(output: str) -> str:
    """Extract assistant text content from stream-json output."""
    import json
    parts: list[str] = []
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
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            parts.append(text)
            elif isinstance(content, str):
                parts.append(content)
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Promise token detection
# ═══════════════════════════════════════════════════════════════════════════════

_TOKEN_COMPLETE    = "<promise>SPEC_COMPLETE</promise>"
_TOKEN_NEEDS_INPUT = "<promise>SPEC_NEEDS_INPUT</promise>"


def _detect_promise(text: str) -> str | None:
    """Return 'COMPLETE', 'NEEDS_INPUT', or None."""
    if _TOKEN_COMPLETE in text:
        return "COMPLETE"
    if _TOKEN_NEEDS_INPUT in text:
        return "NEEDS_INPUT"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Subcommands
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_init(
    feature_name: str,
    spec_dir: Path,
    mode: str = "backend",
    preset: str = "",
    constitution: bool = False,
    backend_context_path: str | None = None,
    spec_context_path: str | None = None,
) -> None:
    if spec_dir.exists():
        _die(f"Spec directory already exists: {spec_dir}")

    _header(f"Initializing spec: {feature_name}")

    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "domains").mkdir(parents=True, exist_ok=True)
    (spec_dir / "questions-archive").mkdir(parents=True, exist_ok=True)

    # Write PROGRESS.md
    progress_content = (
        f"STATUS: INIT\n"
        f"MODE: {mode}\n"
        f"PRESET: {preset}\n"
        f"ITERATION: 0\n"
        f"\n"
        f"## Domain Progress\n"
        f"\n"
        f"| # | Domain | Status | File |\n"
        f"|---|--------|--------|------|\n"
    )
    (spec_dir / "PROGRESS.md").write_text(progress_content, encoding="utf-8")

    # Touch standard files
    for fname in ("FEATURE-DESCRIPTION.md", "HANDOFF.md", "QUESTIONS.md", "FEATURE-TREE.md"):
        (spec_dir / fname).touch()

    # Frontend mode: BACKEND-CONTEXT.md
    if mode == "frontend":
        bc_path = spec_dir / "BACKEND-CONTEXT.md"
        if backend_context_path:
            bp = Path(backend_context_path)
            if bp.is_dir():
                lines: list[str] = []
                for f in sorted(bp.glob("*.md")):
                    lines.append(f"--- {f.name} ---")
                    lines.append(f.read_text(encoding="utf-8"))
                    lines.append("")
                bc_path.write_text("\n".join(lines), encoding="utf-8")
                _log(f"{_GREEN}Populated backend context from:{_RESET} {backend_context_path}")
            elif bp.is_file():
                import shutil
                shutil.copy(bp, bc_path)
                _log(f"{_GREEN}Copied backend context from:{_RESET} {backend_context_path}")
            else:
                _die(f"Backend context path not found: {backend_context_path}")
        else:
            bc_path.touch()
            _log(f"{_YELLOW}Created empty BACKEND-CONTEXT.md{_RESET} — populate it with backend API specs")

    # Testing mode: SPEC-CONTEXT.md
    if mode == "testing":
        sc_path = spec_dir / "SPEC-CONTEXT.md"
        if spec_context_path:
            sp = Path(spec_context_path)
            if sp.is_dir():
                lines = []
                for f in sorted(sp.glob("*.md")):
                    lines.append(f"--- {f.name} ---")
                    lines.append(f.read_text(encoding="utf-8"))
                    lines.append("")
                sc_path.write_text("\n".join(lines), encoding="utf-8")
                _log(f"{_GREEN}Populated spec context from:{_RESET} {spec_context_path}")
            elif sp.is_file():
                import shutil
                shutil.copy(sp, sc_path)
                _log(f"{_GREEN}Copied spec context from:{_RESET} {spec_context_path}")
            else:
                _die(f"Spec context path not found: {spec_context_path}")
        else:
            sc_path.touch()
            _log(f"{_YELLOW}Created empty SPEC-CONTEXT.md{_RESET} — populate it with feature domain specs")

    # Constitution
    if constitution:
        constitution_content = """\
# Project Constitution

Immutable principles that ALL specs and implementations must respect. The spec agent will verify compliance and flag violations.

## Immutable Principles

<!-- Add principles that must never be violated. Examples: -->
<!-- - All user data must be encrypted at rest -->
<!-- - No breaking changes to the public API -->
<!-- - All endpoints require authentication -->

## Technology Constraints

<!-- Hard constraints on technology choices. Examples: -->
<!-- - Backend: <language/framework and version> -->
<!-- - No new JavaScript dependencies over 50KB gzipped -->
<!-- - Database: PostgreSQL only (no MySQL-specific features) -->

## Quality Gates

<!-- Minimum quality standards. Examples: -->
<!-- - All new code must have >80% test coverage -->
<!-- - Static analysis (e.g. PHPStan, mypy, tsc) must pass with zero errors -->
<!-- - All API endpoints must have OpenAPI documentation -->
"""
        (spec_dir / "CONSTITUTION.md").write_text(constitution_content, encoding="utf-8")
        _log(f"{_GREEN}Created constitution:{_RESET} {spec_dir}/CONSTITUTION.md")

    _log(f"{_GREEN}Created spec directory:{_RESET} {spec_dir}")
    if mode != "fullstack":
        _log(f"Mode: {_CYAN}{mode}{_RESET}")
    _log("")
    _log(f"{_YELLOW}Next steps:{_RESET}")
    step = 1
    if constitution:
        _log(f"  {step}. Edit your constitution:")
        _log(f"     {_CYAN}{spec_dir}/CONSTITUTION.md{_RESET}")
        step += 1
    if mode == "frontend":
        _log(f"  {step}. Review/edit backend context:")
        _log(f"     {_CYAN}{spec_dir}/BACKEND-CONTEXT.md{_RESET}")
        step += 1
    if mode == "testing":
        _log(f"  {step}. Review/edit spec context:")
        _log(f"     {_CYAN}{spec_dir}/SPEC-CONTEXT.md{_RESET}")
        step += 1
    _log(f"  {step}. Write your feature description in:")
    _log(f"     {_CYAN}{spec_dir}/FEATURE-DESCRIPTION.md{_RESET}")
    step += 1
    _log(f"  {step}. Run the spec loop:")
    _log(f"     {_CYAN}speccer run --spec-dir {spec_dir}{_RESET}")
    print("")


async def _cmd_run_async(
    spec_dir: Path,
    cont: bool,
) -> None:
    """Async implementation of the run subcommand."""
    from conductor.core.claude import run_claude  # type: ignore[import]

    if not spec_dir.is_dir():
        _die(f"Spec directory not found: {spec_dir}. Run 'init' first.")

    feature_name = spec_dir.parent.name
    mode = read_mode(spec_dir)
    preset = read_preset(spec_dir)

    status    = read_status(spec_dir)
    iteration = read_iteration(spec_dir)

    # ── Validate state ──────────────────────────────────────────────────────
    if status == "INIT":
        desc_file = spec_dir / "FEATURE-DESCRIPTION.md"
        if not desc_file.exists() or desc_file.stat().st_size == 0:
            _die("FEATURE-DESCRIPTION.md is empty. Write your feature description first.")
        _log(f"First spec iteration for {_CYAN}{feature_name}{_RESET}")

    elif status == "NEEDS_INPUT":
        if not cont:
            _log(f"{_YELLOW}Status is NEEDS_INPUT. Answer questions in:{_RESET}")
            _log(f"  {_CYAN}{spec_dir}/QUESTIONS.md{_RESET}")
            _log("")
            _log(f"Then run: {_CYAN}speccer run --continue --spec-dir {spec_dir}{_RESET}")
            sys.exit(0)
        # Validate answers exist
        questions_file = spec_dir / "QUESTIONS.md"
        if not questions_file.exists() or not any(
            line.startswith("> ")
            for line in questions_file.read_text(encoding="utf-8").splitlines()
        ):
            _die("No answers found in QUESTIONS.md. Add answers as lines starting with '> '")
        # Archive questions round
        archive_dir = spec_dir / "questions-archive"
        archive_dir.mkdir(exist_ok=True)
        round_file = archive_dir / f"round-{iteration}.md"
        import shutil
        shutil.copy(questions_file, round_file)
        _log(f"Archived questions to {_CYAN}questions-archive/round-{iteration}.md{_RESET}")
        _log("Continuing after user answers...")

    elif status == "COMPLETE":
        _log(f"{_GREEN}Spec is already complete.{_RESET} Run generate to produce artifacts:")
        _log(f"  {_CYAN}speccer generate --spec-dir {spec_dir}{_RESET}")
        sys.exit(0)

    elif status == "GENERATED":
        _die(f"Spec already generated. Check {spec_dir.parent}/ for artifacts.")

    elif status in ("EXPLORING", "SPECCING"):
        # Previous run may have crashed — non-interactive: auto-reset
        recover_to = "INIT" if status == "EXPLORING" else "NEEDS_INPUT"
        _log(f"{_YELLOW}Status is {status} — previous run may have crashed. Auto-resetting to {recover_to}.{_RESET}")
        update_status(spec_dir, recover_to)
        status = recover_to
        if status == "INIT":
            desc_file = spec_dir / "FEATURE-DESCRIPTION.md"
            if not desc_file.exists() or desc_file.stat().st_size == 0:
                _die("FEATURE-DESCRIPTION.md is empty. Write your feature description first.")
        elif status == "NEEDS_INPUT":
            if not cont:
                _log(f"{_YELLOW}Status is NEEDS_INPUT. Answer questions in:{_RESET}")
                _log(f"  {_CYAN}{spec_dir}/QUESTIONS.md{_RESET}")
                _log("")
                _log(f"Then run: {_CYAN}speccer run --continue --spec-dir {spec_dir}{_RESET}")
                sys.exit(0)
            questions_file = spec_dir / "QUESTIONS.md"
            if not questions_file.exists() or not any(
                line.startswith("> ")
                for line in questions_file.read_text(encoding="utf-8").splitlines()
            ):
                _die("No answers found in QUESTIONS.md. Add answers as lines starting with '> '")
            archive_dir = spec_dir / "questions-archive"
            archive_dir.mkdir(exist_ok=True)
            round_file = archive_dir / f"round-{iteration}.md"
            import shutil
            shutil.copy(questions_file, round_file)
            _log(f"Archived questions to {_CYAN}questions-archive/round-{iteration}.md{_RESET}")
            _log("Continuing after user answers...")

    else:
        _die(f"Unknown status: {status}")

    # ── Increment iteration ─────────────────────────────────────────────────
    iteration += 1
    update_iteration(spec_dir, iteration)

    # Set transient status
    pre_status = status
    transient = "EXPLORING" if status == "INIT" else "SPECCING"
    update_status(spec_dir, transient)

    _header(f"Spec Loop — {feature_name} (iteration {iteration})")
    _log(f"Previous status: {_CYAN}{pre_status}{_RESET}")
    _log(f"Spec dir: {_CYAN}{spec_dir}{_RESET}")

    # ── Build prompt ────────────────────────────────────────────────────────
    prompt = build_spec_prompt(
        spec_dir=spec_dir,
        feature_name=feature_name,
        mode=mode,
        status=pre_status,
        iteration=iteration,
    )

    _log(f"Running Claude (max 200 turns)...")
    print("")

    result = await run_claude(
        prompt,
        model="claude-opus-4-8",
        max_turns=200,
        cwd=str(spec_dir),
    )

    _log("")
    _log(f"Claude finished in {result.duration:.1f}s (exit_code={result.exit_code})")

    if result.exit_code != 0:
        update_status(spec_dir, pre_status)
        _die(
            f"Claude exited with code {result.exit_code}. "
            f"Status reverted to {pre_status}."
        )

    # ── Detect promise token ─────────────────────────────────────────────────
    claude_text = _extract_text_from_output(result.output)

    promise = _detect_promise(claude_text)
    # Fallback: also check raw output in case stream format differs
    if promise is None:
        promise = _detect_promise(result.output)

    if promise == "COMPLETE":
        # Gate: reject SPEC_COMPLETE if any [NEEDS CLARIFICATION] markers remain
        unresolved: list[str] = []
        domains_dir = spec_dir / "domains"
        if domains_dir.is_dir():
            for f in domains_dir.glob("*.md"):
                content = f.read_text(encoding="utf-8")
                if "[NEEDS CLARIFICATION" in content:
                    unresolved.append(f.name)

        if unresolved:
            update_status(spec_dir, "NEEDS_INPUT")
            _log(f"{_RED}{_BOLD}SPEC_COMPLETE rejected — unresolved clarification markers{_RESET}")
            _log("")
            _log(f"The following domain specs contain {_YELLOW}[NEEDS CLARIFICATION]{_RESET} markers:")
            for fname in unresolved:
                _log(f"  {_CYAN}{fname}{_RESET}")
            _log("")
            _log(f"Resolve all markers, then run: {_CYAN}speccer run --continue --spec-dir {spec_dir}{_RESET}")
            sys.exit(1)

        update_status(spec_dir, "COMPLETE")
        _log(f"{_GREEN}{_BOLD}SPEC COMPLETE after {iteration} iterations{_RESET}")
        print("")
        _log("Auto-invoking generate...")
        print("")
        await _cmd_generate_async(spec_dir=spec_dir, split_prs=False)

    elif promise == "NEEDS_INPUT":
        update_status(spec_dir, "NEEDS_INPUT")
        print("")
        _log(f"{_YELLOW}{_BOLD}Questions ready for you{_RESET}")
        _log("")
        _log("  1. Review and answer questions in:")
        _log(f"     {_CYAN}{spec_dir}/QUESTIONS.md{_RESET}")
        _log("")
        _log(f"  2. Add your answers as lines starting with {_CYAN}> {_RESET}")
        _log("")
        _log("  3. Continue the spec loop:")
        _log(f"     {_CYAN}speccer run --continue --spec-dir {spec_dir}{_RESET}")
        print("")

    else:
        # No token — revert status
        update_status(spec_dir, pre_status)
        _log(f"{_RED}{_BOLD}No promise token detected{_RESET}")
        _log("Claude finished without emitting SPEC_NEEDS_INPUT or SPEC_COMPLETE.")
        _log(f"Status reverted to {pre_status}. You may need to run again.")
        sys.exit(1)


def cmd_run(spec_dir: Path, cont: bool) -> None:
    asyncio.run(_cmd_run_async(spec_dir=spec_dir, cont=cont))


async def _cmd_generate_async(spec_dir: Path, split_prs: bool) -> None:
    """Async implementation of the generate subcommand."""
    from conductor.core.claude import run_claude  # type: ignore[import]

    if not spec_dir.is_dir():
        _die(f"Spec directory not found: {spec_dir}")

    feature_name = spec_dir.parent.name
    mode   = read_mode(spec_dir)
    preset = read_preset(spec_dir)
    status = read_status(spec_dir)

    if status not in ("COMPLETE", "GENERATED"):
        _die(f"Spec must be COMPLETE before generating. Current status: {status}")

    if status == "GENERATED":
        _log(f"{_YELLOW}Already generated. Re-generating will overwrite existing artifacts.{_RESET}")

    # Verify domain specs exist
    domains_dir = spec_dir / "domains"
    if not domains_dir.is_dir() or not any(domains_dir.glob("*.md")):
        _die(f"No domain spec files found in {spec_dir}/domains/")

    docs_dir = spec_dir.parent

    _header(f"Generating Artifacts — {feature_name}")
    if split_prs:
        _log(f"Mode: {_CYAN}Split PRs{_RESET}")
    else:
        _log(f"Mode: {_CYAN}Single sequence{_RESET}")

    prompt = build_generate_prompt(
        spec_dir=spec_dir,
        feature_name=feature_name,
        mode=mode,
        split_prs=split_prs,
        preset_name=preset,
    )

    _log(f"Running Claude (max 200 turns)...")
    print("")

    result = await run_claude(
        prompt,
        model="claude-opus-4-8",
        max_turns=200,
        cwd=str(docs_dir),
    )

    _log("")
    _log(f"Claude finished in {result.duration:.1f}s (exit_code={result.exit_code})")

    if result.exit_code != 0:
        _die(f"Claude exited with code {result.exit_code} during generate.")

    # ── Verify expected outputs ─────────────────────────────────────────────
    missing: list[str] = []
    if mode == "testing":
        for name in ("TEST-PLAN.md", "TEST-ARCHITECTURE.md"):
            if not (docs_dir / name).exists():
                missing.append(name)
    else:
        for name in ("PRD.md", "TECHNICAL-DESIGN.md"):
            if not (docs_dir / name).exists():
                missing.append(name)

    if mode == "backend":
        if not (docs_dir / "API-CONTRACTS.md").exists():
            missing.append("API-CONTRACTS.md")

    if not (docs_dir / "IMPLEMENTATION-PLAN.md").exists():
        missing.append("IMPLEMENTATION-PLAN.md")

    if not (docs_dir / "run.sh").exists():
        missing.append("run.sh")

    prompts_dir = docs_dir / "prompts"
    if not prompts_dir.is_dir() or not any(prompts_dir.glob("*.md")):
        missing.append("prompts/*.md")

    if missing:
        _log(f"{_YELLOW}Some expected files were not generated:{_RESET}")
        for f in missing:
            _log(f"  {_RED}missing{_RESET} {f}")
        sys.exit(1)
    else:
        update_status(spec_dir, "GENERATED")
        _log(f"{_GREEN}{_BOLD}All artifacts generated{_RESET}")
        _log("")
        _log(f"Generated files in {_CYAN}{docs_dir}/{_RESET}:")
        if mode == "testing":
            _log("  TEST-PLAN.md")
            _log("  TEST-ARCHITECTURE.md")
        else:
            _log("  PRD.md")
            _log("  TECHNICAL-DESIGN.md")
        if mode == "backend":
            _log("  API-CONTRACTS.md")
        _log("  IMPLEMENTATION-PLAN.md")
        _log("  run.sh")
        for f in sorted(prompts_dir.glob("*.md")):
            _log(f"  prompts/{f.name}")
        print("")
        _log(f"Run implementation: {_CYAN}{docs_dir}/run.sh{_RESET}")
        _log(f"Dry run first:      {_CYAN}{docs_dir}/run.sh --dry-run{_RESET}")
    print("")


def cmd_generate(spec_dir: Path, split_prs: bool) -> None:
    asyncio.run(_cmd_generate_async(spec_dir=spec_dir, split_prs=split_prs))


def cmd_status(spec_dir: Path) -> None:
    if not spec_dir.is_dir():
        _die(f"Spec directory not found: {spec_dir}")

    progress_file = spec_dir / "PROGRESS.md"
    if not progress_file.exists():
        _die(f"No PROGRESS.md found in {spec_dir}")

    feature_name = spec_dir.parent.name
    _header(f"Spec Status — {feature_name}")

    status    = read_status(spec_dir)
    iteration = read_iteration(spec_dir)

    status_color_map = {
        "INIT":       _DIM,
        "EXPLORING":  _YELLOW,
        "SPECCING":   _YELLOW,
        "NEEDS_INPUT": _MAGENTA,
        "COMPLETE":   _GREEN,
        "GENERATED":  _CYAN,
    }
    sc = status_color_map.get(status, _RED)

    print(f"  Status:    {sc}{_BOLD}{status}{_RESET}")
    print(f"  Iteration: {_BOLD}{iteration}{_RESET}")
    print(f"  Spec dir:  {_DIM}{spec_dir}{_RESET}")
    print("")

    # Print domain table from PROGRESS.md
    lines = progress_file.read_text(encoding="utf-8").splitlines()
    in_table = False
    has_data = False
    for line in lines:
        if line.startswith("|"):
            # Skip separator row
            if re.match(r"^\|\s*-+", line):
                continue
            if not in_table:
                in_table = True
                continue  # skip header row
            has_data = True
            line_lower = line.lower()
            if "complete" in line_lower:
                print(f"  {_GREEN}{line}{_RESET}")
            elif "in_progress" in line_lower or "speccing" in line_lower:
                print(f"  {_YELLOW}{line}{_RESET}")
            elif "pending" in line_lower:
                print(f"  {_DIM}{line}{_RESET}")
            else:
                print(f"  {line}")
        else:
            in_table = False

    if has_data:
        print("")

    next_action_map = {
        "INIT":       f"Next: Write {_CYAN}{spec_dir}/FEATURE-DESCRIPTION.md{_RESET}, then run",
        "NEEDS_INPUT": f"Next: Answer questions in {_CYAN}{spec_dir}/QUESTIONS.md{_RESET}, then run --continue",
        "COMPLETE":   f"Next: {_CYAN}speccer generate --spec-dir {spec_dir}{_RESET}",
        "GENERATED":  f"Done. Artifacts in {_CYAN}{spec_dir.parent}/{_RESET}",
        "EXPLORING":  f"Claude is running (or crashed). Check {spec_dir}/",
        "SPECCING":   f"Claude is running (or crashed). Check {spec_dir}/",
    }
    hint = next_action_map.get(status, "")
    if hint:
        _log(hint)


def cmd_stats(spec_dir: Path) -> None:
    """Display stats from STATS.json for the spec and build phases."""
    import json as _json

    if not spec_dir.is_dir():
        _die(f"Spec directory not found: {spec_dir}")

    feature_name = spec_dir.parent.name
    _header(f"Stats — {feature_name}")

    # Spec stats
    spec_stats = spec_dir / "STATS.json"
    if spec_stats.exists():
        try:
            data = _json.loads(spec_stats.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else data.get("entries", [])
            if entries:
                total_cost = sum(e.get("cost_usd", 0) for e in entries)
                total_input = sum(e.get("tokens", {}).get("input", 0) for e in entries)
                total_output = sum(e.get("tokens", {}).get("output", 0) for e in entries)
                total_duration = sum(e.get("duration_s", 0) for e in entries)
                print(f"  {_BOLD}Spec Stats{_RESET} ({len(entries)} calls)")
                print(f"    Input: {total_input:,}  Output: {total_output:,}")
                print(f"    Duration: {total_duration:.0f}s")
                print(f"    Cost: {_GREEN}${total_cost:.4f}{_RESET}")
                print()
        except (_json.JSONDecodeError, OSError):
            pass
    else:
        _log(f"{_DIM}No spec stats found{_RESET}")

    # Build stats
    build_stats = spec_dir.parent / "STATS.json"
    if build_stats.exists():
        try:
            data = _json.loads(build_stats.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else data.get("entries", [])
            if entries:
                total_cost = sum(e.get("cost_usd", 0) for e in entries)
                total_input = sum(e.get("tokens", {}).get("input", 0) for e in entries)
                total_output = sum(e.get("tokens", {}).get("output", 0) for e in entries)
                total_duration = sum(e.get("duration_s", 0) for e in entries)
                print(f"  {_BOLD}Build Stats{_RESET} ({len(entries)} iterations)")
                print(f"    Input: {total_input:,}  Output: {total_output:,}")
                print(f"    Duration: {total_duration:.0f}s")
                print(f"    Cost: {_GREEN}${total_cost:.4f}{_RESET}")
                print()
        except (_json.JSONDecodeError, OSError):
            pass
    else:
        _log(f"{_DIM}No build stats found{_RESET}")


def cmd_tree(spec_dir: Path) -> None:
    if not spec_dir.is_dir():
        _die(f"Spec directory not found: {spec_dir}")

    tree_file = spec_dir / "FEATURE-TREE.md"
    if not tree_file.exists():
        _die(f"No FEATURE-TREE.md found in {spec_dir}")

    if tree_file.stat().st_size == 0:
        _die("FEATURE-TREE.md is empty. Run at least one spec iteration first.")

    feature_name = spec_dir.parent.name
    _header(f"Feature Tree — {feature_name}")

    for line in tree_file.read_text(encoding="utf-8").splitlines():
        priority_fmt = ""
        if "[P1]" in line:
            priority_fmt = _BOLD
        elif "[P3]" in line:
            priority_fmt = _DIM

        if re.search(r"\[COMPLETE(D)?\]", line):
            print(f"  {priority_fmt}{_GREEN}{line}{_RESET}")
        elif "[IN_PROGRESS]" in line:
            print(f"  {priority_fmt}{_YELLOW}{line}{_RESET}")
        elif "[PENDING]" in line:
            print(f"  {priority_fmt}{_DIM}{line}{_RESET}")
        elif re.match(r"^#+", line):
            print(f"  {priority_fmt}{_BOLD}{_CYAN}{line}{_RESET}")
        else:
            print(f"  {priority_fmt}{line}{_RESET}")
    print("")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI argument parsing
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_args(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="speccer",
        description="Iterative feature specification tool",
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")

    # ── init ──────────────────────────────────────────────────────────────────
    p_init = subparsers.add_parser("init", help="Initialize a new spec workspace")
    p_init.add_argument("--feature", required=True, help="Feature name")
    p_init.add_argument(
        "--spec-dir",
        dest="spec_dir",
        help="Explicit spec directory path (takes precedence over project-dir + feature)",
    )
    p_init.add_argument("--project-dir", dest="project_dir", default=None)
    p_init.add_argument(
        "--mode",
        default="backend",
        choices=["fullstack", "frontend", "backend", "testing"],
    )
    p_init.add_argument("--preset", default="")
    p_init.add_argument("--constitution", action="store_true", default=False)
    p_init.add_argument("--backend-context", dest="backend_context", default=None)
    p_init.add_argument("--spec-context", dest="spec_context", default=None)

    # ── run ───────────────────────────────────────────────────────────────────
    p_run = subparsers.add_parser("run", help="Run one spec iteration")
    p_run.add_argument("--continue", dest="cont", action="store_true", default=False)
    p_run.add_argument(
        "--spec-dir",
        dest="spec_dir",
        help="Explicit spec directory path",
    )
    p_run.add_argument("--feature", default=None)
    p_run.add_argument("--project-dir", dest="project_dir", default=None)

    # ── generate ──────────────────────────────────────────────────────────────
    p_gen = subparsers.add_parser("generate", help="Generate implementation artifacts")
    p_gen.add_argument(
        "--spec-dir",
        dest="spec_dir",
        help="Explicit spec directory path",
    )
    p_gen.add_argument("--feature", default=None)
    p_gen.add_argument("--project-dir", dest="project_dir", default=None)
    p_gen.add_argument("--split-prs", dest="split_prs", action="store_true", default=False)

    # ── status ────────────────────────────────────────────────────────────────
    p_status = subparsers.add_parser("status", help="Show current spec status")
    p_status.add_argument(
        "--spec-dir",
        dest="spec_dir",
        help="Explicit spec directory path",
    )
    p_status.add_argument("--feature", default=None)
    p_status.add_argument("--project-dir", dest="project_dir", default=None)

    # ── stats ─────────────────────────────────────────────────────────────────
    p_stats = subparsers.add_parser("stats", help="Show spec and build stats")
    p_stats.add_argument(
        "--spec-dir",
        dest="spec_dir",
        help="Explicit spec directory path",
    )
    p_stats.add_argument("--feature", default=None)
    p_stats.add_argument("--project-dir", dest="project_dir", default=None)

    # ── tree ──────────────────────────────────────────────────────────────────
    p_tree = subparsers.add_parser("tree", help="Display feature decomposition tree")
    p_tree.add_argument(
        "--spec-dir",
        dest="spec_dir",
        help="Explicit spec directory path",
    )
    p_tree.add_argument("--feature", default=None)
    p_tree.add_argument("--project-dir", dest="project_dir", default=None)

    return parser.parse_args(argv)


def _resolve_spec_dir(args) -> Path:
    """Resolve spec_dir from --spec-dir, or from --feature + --project-dir."""
    if hasattr(args, "spec_dir") and args.spec_dir:
        return Path(args.spec_dir).resolve()

    # Fall back to feature + project-dir
    feature = getattr(args, "feature", None)
    project_dir = getattr(args, "project_dir", None) or str(Path.cwd())
    project_path = Path(project_dir).resolve()

    if not feature:
        # Auto-detect if only one feature spec exists
        features_root = project_path / "docs"
        if features_root.is_dir():
            found = [
                d.parent.name
                for d in features_root.glob("*/spec")
                if d.is_dir()
            ]
            if len(found) == 1:
                feature = found[0]
            elif len(found) > 1:
                _die(f"Multiple features found: {found}. Use --feature or --spec-dir.")
            else:
                _die("No features found. Use --feature or --spec-dir.")
        else:
            _die("Cannot auto-detect feature. Use --feature or --spec-dir.")

    return (project_path / "docs" / feature / "spec").resolve()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = _parse_args()

    if args.subcommand is None:
        _parse_args(["--help"])
        sys.exit(1)

    if args.subcommand == "init":
        # For init, spec_dir may be provided directly or derived
        if hasattr(args, "spec_dir") and args.spec_dir:
            spec_dir = Path(args.spec_dir).resolve()
        else:
            project_dir = getattr(args, "project_dir", None) or str(Path.cwd())
            spec_dir = (Path(project_dir).resolve() / "docs" / args.feature / "spec").resolve()

        cmd_init(
            feature_name=args.feature,
            spec_dir=spec_dir,
            mode=args.mode,
            preset=args.preset,
            constitution=args.constitution,
            backend_context_path=args.backend_context,
            spec_context_path=args.spec_context,
        )

    elif args.subcommand == "run":
        spec_dir = _resolve_spec_dir(args)
        cmd_run(spec_dir=spec_dir, cont=args.cont)

    elif args.subcommand == "generate":
        spec_dir = _resolve_spec_dir(args)
        split_prs = getattr(args, "split_prs", False)
        cmd_generate(spec_dir=spec_dir, split_prs=split_prs)

    elif args.subcommand == "status":
        spec_dir = _resolve_spec_dir(args)
        cmd_status(spec_dir=spec_dir)

    elif args.subcommand == "stats":
        spec_dir = _resolve_spec_dir(args)
        cmd_stats(spec_dir=spec_dir)

    elif args.subcommand == "tree":
        spec_dir = _resolve_spec_dir(args)
        cmd_tree(spec_dir=spec_dir)

    else:
        _die(f"Unknown subcommand: {args.subcommand}")


if __name__ == "__main__":
    main()
