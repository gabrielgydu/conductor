"""Tests for conductor.core.stream_filter — the pipe between claude and loop/manual-test."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conductor.core import stream_filter
from conductor.core.claude import progress_on_event
from conductor.core.stream_filter import describe_result

STREAM_JSON_FIXTURES = Path(__file__).parent / "fixtures" / "stream_json"
FILTER = Path(stream_filter.__file__)


def load_stream_json(name: str) -> bytes:
    return (STREAM_JSON_FIXTURES / f"{name}.jsonl").read_bytes()


def run_filter(tmp_path: Path, stdin: bytes, **popen_kwargs) -> tuple[subprocess.CompletedProcess, bytes]:
    """Run the filter by file path in isolated mode, as a plain script needing only the stdlib."""
    out = tmp_path / "out.jsonl"
    proc = subprocess.run(
        [sys.executable, "-I", str(FILTER), str(out)],
        input=stdin,
        capture_output=True,
        timeout=10,
        check=False,
        env={"PYTHONIOENCODING": "utf-8:strict"},
        **popen_kwargs,
    )
    return proc, out.read_bytes()


def test_copies_the_stream_and_prints_it(tmp_path):
    stream = load_stream_json("success")
    proc, saved = run_filter(tmp_path, stream)
    assert proc.returncode == 0
    assert saved == stream
    shown = proc.stdout.decode()
    assert "Read: /work/project/hello.txt" in shown
    assert "success" in shown and "(2 turns, $0.0015, 3.0s)" in shown


def test_runs_as_a_module_without_warnings(tmp_path):
    """Loop mode and manual-test run `python3 -m conductor.core.stream_filter`.

    runpy imports the conductor.core package first; if that already loaded
    the filter, a RuntimeWarning lands in every session's tmux pane.
    """
    out = tmp_path / "out.jsonl"
    stream = load_stream_json("success")
    proc = subprocess.run(
        [sys.executable, "-m", "conductor.core.stream_filter", str(out)],
        input=stream,
        capture_output=True,
        timeout=20,
        check=False,
        env={"PYTHONPATH": str(FILTER.parents[2]), "PYTHONIOENCODING": "utf-8:strict"},
    )
    assert proc.returncode == 0
    assert proc.stderr == b""
    assert out.read_bytes() == stream
    assert "(2 turns, $0.0015, 3.0s)" in proc.stdout.decode()


def test_unprintable_event_does_not_lose_the_result_event(tmp_path):
    """A shape the printer chokes on used to kill the filter, losing everything after it."""
    stream = (
        b'{"type":"assistant","message":null}\n'
        + b'{"type":"user","message":{"content":null}}\n'
        + load_stream_json("error_max_turns")
    )
    proc, saved = run_filter(tmp_path, stream)
    assert proc.returncode == 0
    assert saved == stream
    assert b"cannot display a 'assistant' event" in proc.stderr
    assert "Reached maximum number of turns (1)" in proc.stdout.decode()


def test_undecodable_bytes_are_copied_verbatim(tmp_path):
    stream = b'{"type":"assistant","message":{"content":[]}}\xff\xfe\n' + load_stream_json("success")
    proc, saved = run_filter(tmp_path, stream)
    assert proc.returncode == 0
    assert saved == stream


def test_closed_terminal_does_not_stop_the_copy(tmp_path):
    """With nothing reading the display, keep saving the stream."""
    out = tmp_path / "out.jsonl"
    stream = load_stream_json("success") * 50
    filt = subprocess.Popen(
        [sys.executable, "-I", str(FILTER), str(out)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    filt.stdout.close()  # the reader goes away before the first event
    filt.stdin.write(stream)
    filt.stdin.close()
    filt.wait(timeout=10)
    assert out.read_bytes() == stream


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("success", ("success", " (2 turns, $0.0015, 3.0s)", "")),
        ("error_max_turns", ("error_max_turns", " (2 turns, $0.0002, 1.8s)", "Reached maximum number of turns (1)")),
        # unknown model: the CLI reports subtype "success" with is_error set
        (
            "model_not_found",
            (
                "error",
                " (1 turn, $0.0000, 0.7s)",
                (
                    "There's an issue with the selected model (claude-nonexistent-model-0). It may not exist "
                    "or you may not have access to it. Run --model to pick a different model."
                ),
            ),
        ),
    ],
)
def test_describe_result(fixture, expected):
    event = json.loads(load_stream_json(fixture).splitlines()[-1])
    assert describe_result(event) == expected


def test_describe_result_minimal_event():
    assert describe_result({"type": "result"}) == ("", "", "")


def test_progress_printer_shows_error_results(capsys):
    event = json.loads(load_stream_json("model_not_found").splitlines()[-1])
    progress_on_event(event)
    err = capsys.readouterr().err
    assert "error (1 turn, $0.0000, 0.7s)" in err
    assert "There's an issue with the selected model" in err
