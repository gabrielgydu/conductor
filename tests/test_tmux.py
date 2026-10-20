"""TmuxManager: the argv it hands to tmux and the bash scripts it wraps commands in.

tmux itself is never run here: ``_run_tmux`` is replaced by a recorder. The
wrapper scripts are executed with bash (exactly as tmux would exec them) only
where they do not call back into tmux.
"""

from __future__ import annotations

import subprocess

import pytest

from conductor.core.tmux import TmuxManager


class TmuxRecorder:
    """Records every tmux argv and reports success."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

    def commands(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def recorder(monkeypatch) -> TmuxRecorder:
    rec = TmuxRecorder()
    monkeypatch.setattr(TmuxManager, "_run_tmux", lambda _self, *a, **kw: rec.run(*a, **kw))
    return rec


def _script(new_window: list[str]) -> str:
    """The bash script of a recorded new-window call (argv ends ``bash -c <script>``)."""
    assert new_window[-3:-1] == ["bash", "-c"], new_window
    return new_window[-1]


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a wrapper the way tmux would (kept out of the async tests' bodies)."""
    return subprocess.run(argv, check=True, **kwargs)


async def test_spawn_in_window_execs_bash_argv_style(recorder):
    """A single command string would be run by the user's default-shell (zsh: [1m] is a glob)."""
    cmd = "claude --model claude-opus-4-8[1m] | cat; echo ${PIPESTATUS[0]} > out"
    await TmuxManager("conductor-x").spawn_in_window("w", cmd, cwd="/repo", detached=True)

    (new_window,) = recorder.commands("new-window")
    assert new_window[-3:] == ["bash", "-c", cmd]
    assert new_window[:2] == ["new-window", "-d"]
    assert new_window[new_window.index("-c") + 1] == "/repo"


async def test_every_spawn_method_execs_bash(recorder, tmp_path):
    tmux = TmuxManager("conductor-x")
    await tmux.spawn_runner_in_window("r", "true", exit_file=tmp_path / "conductor-exit-r")
    await tmux.spawn_in_window("s", "true")

    windows = recorder.commands("new-window")
    assert len(windows) == 2
    for new_window in windows:
        _script(new_window)


async def test_runner_wrapper_quotes_exit_file(recorder, tmp_path):
    exit_file = tmp_path / "dir with space" / "conductor-exit-feat"
    exit_file.parent.mkdir()
    await TmuxManager("conductor-x").spawn_runner_in_window("r", "true", exit_file=exit_file)

    script = _script(recorder.commands("new-window")[0])
    _run(["bash", "-c", script], cwd=tmp_path)
    assert exit_file.read_text().strip() == "0"


async def test_pipe_pane_quotes_log_file(recorder, tmp_path):
    log_file = tmp_path / "logs dir" / "w.log"
    await TmuxManager("conductor-x").spawn_in_window("w", "true", log_file=log_file)

    (pipe_pane,) = recorder.commands("pipe-pane")
    # pipe-pane runs its command with /bin/sh -c
    _run(["sh", "-c", pipe_pane[-1]], input="hello\n", text=True)
    assert log_file.read_text() == "hello\n"
