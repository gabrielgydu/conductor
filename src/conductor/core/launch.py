"""Shell fragments for launching headless Claude sessions in tmux windows."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

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
