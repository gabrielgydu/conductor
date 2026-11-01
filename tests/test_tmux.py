"""TmuxManager: the argv it hands to tmux and the bash scripts it wraps commands in.

tmux itself is never run here: ``_run_tmux`` is replaced by a recorder. The
wrapper scripts are executed with bash (exactly as tmux would exec them) only
where they do not call back into tmux.
"""

from __future__ import annotations

import subprocess
from typing import Callable

import pytest

import conductor.core.tmux as tmux_module
from conductor.core.tmux import TmuxManager, session_target, window_target


class TmuxRecorder:
    """Records every tmux argv and reports success.

    ``list-panes`` answers come from ``pane_states`` in order (a
    ``"#{pane_dead} #{pane_dead_status}"`` line, or None for "can't find
    window"); ``on_dead`` runs right before the first dead-pane answer.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.pane_states: list[str | None] = []
        self.on_dead: Callable[[], object] | None = None

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[0] == "list-panes" and self.pane_states:
            state = self.pane_states.pop(0)
            if state is None:
                return subprocess.CompletedProcess(["tmux", *args], 1, "", "can't find window")
            if state.startswith("1") and self.on_dead is not None:
                self.on_dead()
                self.on_dead = None
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout=f"{state}\n", stderr="")
        return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

    def commands(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def recorder(monkeypatch) -> TmuxRecorder:
    rec = TmuxRecorder()
    monkeypatch.setattr(TmuxManager, "_run_tmux", lambda _self, *a, **kw: rec.run(*a, **kw))
    monkeypatch.setattr(tmux_module, "_WAIT_POLL_SECS", 0)
    return rec


def _script(new_window: list[str]) -> str:
    """The bash script of a recorded new-window call (argv ends ``bash -c <script>``)."""
    assert new_window[-3:-1] == ["bash", "-c"], new_window
    return new_window[-1]


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a wrapper the way tmux would (kept out of the async tests' bodies)."""
    kwargs.setdefault("check", True)
    return subprocess.run(argv, **kwargs)


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


async def test_every_tmux_target_is_exact(recorder, tmp_path):
    """Bare names prefix-match: conductor-foo would hit conductor-foobar, run0:api run0:api-exec."""
    tmux = TmuxManager("conductor-foo")
    tmux.session_exists()
    await tmux.ensure_session()
    await tmux.spawn_in_window("run0:api", "true", log_file=tmp_path / "w.log")
    await tmux.spawn_runner_in_window("run0:api", "true", exit_file=tmp_path / "conductor-exit-a")
    await tmux.is_window_alive("run0:api")
    await tmux.is_runner_idle("run0:api")
    await tmux.has_active_children("run0:api")
    await tmux.send_keys("run0:api", "C-c")
    await tmux.capture_pane("run0:api")
    await tmux.kill_window("run0:api")
    await tmux.kill_session()

    targets = {(c[0], c[c.index("-t") + 1]) for c in recorder.calls if "-t" in c}
    assert targets == {
        ("has-session", "=conductor-foo"),
        ("kill-session", "=conductor-foo"),
        ("set-environment", "=conductor-foo"),  # a session that already exists
        ("list-windows", "=conductor-foo"),
        # without the colon tmux reads a new-window target as a window name
        ("new-window", "=conductor-foo:"),
        ("kill-window", "=conductor-foo:=run0:api"),
        ("set-option", "=conductor-foo:=run0:api"),
        ("pipe-pane", "=conductor-foo:=run0:api"),
        ("list-panes", "=conductor-foo:=run0:api"),
        ("send-keys", "=conductor-foo:=run0:api"),
        ("capture-pane", "=conductor-foo:=run0:api"),
    }


def test_target_helpers():
    assert session_target("conductor-loop-x") == "=conductor-loop-x"
    assert window_target("conductor-x", "run0:backend") == "=conductor-x:=run0:backend"


# -- spawn_in_window_and_wait ------------------------------------------------


async def test_wait_returns_only_after_the_command_exits(recorder, tmp_path):
    """A latched or foreign wait-for signal used to end the wait early and read as 0."""
    exit_file = tmp_path / "speccer exit"
    exit_file.write_text("0")  # stale result from an earlier spawn
    recorder.pane_states = ["0 ", "0 ", "1 4"]
    recorder.on_dead = lambda: _run(
        ["bash", "-c", _script(recorder.commands("new-window")[0])], check=False,
    )

    rc = await TmuxManager("conductor-x").spawn_in_window_and_wait(
        "run0:backend", "(exit 4)", exit_file=exit_file,
    )

    assert rc == 4
    assert exit_file.read_text().strip() == "4"
    assert len(recorder.commands("list-panes")) == 3
    assert not recorder.commands("wait-for")


async def test_wait_reports_failure_when_window_vanishes(recorder, tmp_path):
    exit_file = tmp_path / "exit"
    exit_file.write_text("0")  # stale: must not be mistaken for this command's result
    recorder.pane_states = ["0 ", None]

    rc = await TmuxManager("conductor-x").spawn_in_window_and_wait("w", "sleep 60", exit_file=exit_file)

    assert rc == 1


async def test_wait_without_exit_file_uses_dead_pane_status(recorder):
    recorder.pane_states = ["1 3"]

    assert await TmuxManager("conductor-x").spawn_in_window_and_wait("w", "exit 3") == 3


def test_pstree_depth_without_pstree_is_the_documented_fallback(monkeypatch, tmp_path):
    """Without psmisc, subprocess raised FileNotFoundError into the loop's and orchestrator's monitors."""
    monkeypatch.setenv("PATH", str(tmp_path))

    assert tmux_module.check_pstree_depth(1) is True


async def test_ensure_session_new_session_carries_the_forwarded_env(recorder, monkeypatch):
    # Windows of a session made here (--inside-tmux) must see the caller's
    # CONDUCTOR_HOME/PRESETS_DIR, not the tmux server's environment.
    monkeypatch.setenv("CONDUCTOR_HOME", "/x y")
    monkeypatch.setenv("PATH", "/opt/bin:/usr/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setattr(TmuxManager, "session_exists", lambda _self: False)

    await TmuxManager("conductor-foo").ensure_session()

    (new,) = recorder.commands("new-session")
    forwarded = {new[i + 1] for i, o in enumerate(new) if o == "-e"}
    assert "CONDUCTOR_HOME=/x y" in forwarded
    assert "PATH=/opt/bin:/usr/bin" in forwarded
    assert not any(v.startswith("ANTHROPIC_") for v in forwarded)
    assert recorder.commands("set-environment") == []


async def test_ensure_session_updates_the_environment_of_an_existing_session(recorder, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HOME", "/x y")
    monkeypatch.setenv("PATH", "/opt/bin:/usr/bin")

    await TmuxManager("conductor-foo").ensure_session()

    assert recorder.commands("new-session") == []
    env = {c[3]: c[4] for c in recorder.commands("set-environment")}
    assert env["CONDUCTOR_HOME"] == "/x y"
    assert env["PATH"] == "/opt/bin:/usr/bin"
    assert all(c[1:3] == ["-t", "=conductor-foo"] for c in recorder.commands("set-environment"))
