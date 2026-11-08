"""Shell fragments for launching headless Claude sessions in tmux windows."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from conductor.core.model_catalog import effort_args

_STREAM_FILTER = Path(__file__).resolve().with_name("stream_filter.py")


def stream_filter_pipe(output_file: Path | str) -> str:
    """Shell command to pipe ``claude --output-format stream-json`` into.

    It pretty-prints the events to the pane and saves the raw stream to
    *output_file*. stream_filter.py runs by path under the interpreter that
    runs conductor (the wrapper's venv python), not whatever ``python3`` comes
    first on the tmux server's PATH, which need not have conductor installed.
    ``-I`` keeps the script's directory (conductor/core, with its own
    logging.py) off sys.path; the filter needs only the stdlib.
    """
    python = sys.executable or "python3"
    return shlex.join([python, "-I", str(_STREAM_FILTER), str(output_file)])


def claude_session_script(
    *,
    cwd: Path | str,
    prompt_file: Path | str,
    output_file: Path | str,
    exit_file: Path | str,
    model: str,
    max_turns: int,
    stderr_file: Path | str | None = None,
) -> str:
    """Bash script for one headless Claude session, for TmuxManager to spawn.

    Feeds *prompt_file* to ``claude -p``, pipes its stream-json through
    :func:`stream_filter_pipe` and writes Claude's own exit status (not the
    filter's) to *exit_file*. Claude's stderr goes to *stderr_file*, or is
    discarded. Every value is shell-quoted: model IDs such as
    ``claude-opus-4-8[1m]`` are glob patterns. ``$PIPESTATUS`` needs bash,
    which is what TmuxManager runs window scripts with.
    """
    claude = shlex.join([
        "claude", "-p", "-", "--dangerously-skip-permissions",
        "--max-turns", str(max_turns), "--model", model, *effort_args(),
        "--output-format", "stream-json", "--verbose",
    ])
    stderr = shlex.quote(str(stderr_file)) if stderr_file else "/dev/null"
    return (
        f"cd {shlex.quote(str(cwd))} && "
        f"{claude} < {shlex.quote(str(prompt_file))} 2> {stderr} "
        f"| {stream_filter_pipe(output_file)}; "
        f"echo ${{PIPESTATUS[0]}} > {shlex.quote(str(exit_file))}"
    )
