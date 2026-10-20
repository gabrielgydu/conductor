"""Shell fragments that launch Claude sessions in tmux windows (conductor.core.launch)."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import conductor.core.stream_filter as stream_filter
from conductor.core.launch import stream_filter_pipe

# A pane's shell without the venv on PATH and without the user-site editable
# install that made `python3 -m conductor.core.stream_filter` work by accident.
_BARE_ENV = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}


def test_stream_filter_pipe_runs_the_filter_with_conductors_interpreter():
    argv = shlex.split(stream_filter_pipe("/x/out dir/events.jsonl"))

    assert argv == [
        sys.executable, "-I", str(Path(stream_filter.__file__).resolve()), "/x/out dir/events.jsonl",
    ]


def test_stream_filter_pipe_works_without_conductor_on_the_pane_path(tmp_path):
    output_file = tmp_path / "out dir" / "events.jsonl"
    output_file.parent.mkdir()
    event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
    script = f"printf '%s\\n' {shlex.quote(json.dumps(event))} | {stream_filter_pipe(output_file)}"

    r = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=_BARE_ENV,
        capture_output=True, text=True, check=True,
    )

    assert json.loads(output_file.read_text()) == event
    assert "hello" in r.stdout
