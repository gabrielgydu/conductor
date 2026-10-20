"""Shell fragments that launch Claude sessions in tmux windows (conductor.core.launch)."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import conductor.core.stream_filter as stream_filter
from conductor.core.launch import claude_session_script, stream_filter_pipe

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


# -- claude_session_script ---------------------------------------------------

_FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > argv.txt
cat > prompt.txt
echo "fake claude stderr" >&2
printf '%s\\n' '{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}'
exit 3
"""


@pytest.fixture
def fake_claude(tmp_path) -> dict[str, str]:
    """Environment whose `claude` records its argv and stdin in its cwd, then exits 3."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.write_text(_FAKE_CLAUDE)
    claude.chmod(0o755)
    return {**_BARE_ENV, "PATH": f"{bindir}:{_BARE_ENV['PATH']}"}


def _session(tmp_path: Path, **overrides) -> tuple[str, dict[str, Path]]:
    work = tmp_path / "my repo"  # space: an unquoted `cd` would break
    work.mkdir(exist_ok=True)
    files = {
        "cwd": work,
        "prompt_file": tmp_path / "prompt file.md",
        "output_file": tmp_path / "output file.jsonl",
        "exit_file": tmp_path / "exit file",
        "stderr_file": tmp_path / "stderr file",
    }
    files["prompt_file"].write_text("do the task")
    kwargs = {**files, "model": "claude-opus-4-8[1m]", "max_turns": 7, **overrides}
    return claude_session_script(**kwargs), files


def test_claude_session_script_passes_values_literally(tmp_path, fake_claude):
    script, files = _session(tmp_path)
    # Unquoted, bash would glob claude-opus-4-8[1m] into this file name (zsh fails outright).
    (files["cwd"] / "claude-opus-4-8m").touch()

    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=fake_claude, check=True)

    argv = (files["cwd"] / "argv.txt").read_text().splitlines()
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8[1m]"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert (files["cwd"] / "prompt.txt").read_text() == "do the task"
    assert "hi" in files["output_file"].read_text()
    assert files["stderr_file"].read_text() == "fake claude stderr\n"


def test_claude_session_script_records_claudes_exit_status(tmp_path, fake_claude):
    """The filter at the end of the pipe exits 0; the exit file must hold Claude's 3."""
    script, files = _session(tmp_path)

    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=fake_claude, check=True)

    assert files["exit_file"].read_text().strip() == "3"


def test_claude_session_script_survives_a_missing_cwd(tmp_path, fake_claude):
    script, files = _session(tmp_path, cwd=tmp_path / "gone")

    subprocess.run(["bash", "-c", script], cwd=tmp_path, env=fake_claude, check=True)

    assert files["exit_file"].read_text().strip() == "1"
    assert not files["output_file"].exists()


def test_claude_session_script_discards_stderr_by_default(tmp_path, fake_claude):
    script, files = _session(tmp_path, stderr_file=None)

    r = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=fake_claude,
        capture_output=True, text=True, check=True,
    )

    assert "fake claude stderr" not in r.stderr
