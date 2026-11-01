"""Shared helpers and fixtures for the conductor CLI tests.

Fixtures are registered under their `name=` so test modules can import them
(`from tests.cli_support import repo_fixture  # noqa: F401`) without the test
arguments shadowing the import.
"""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from conductor.core import lock
from conductor.core.models import ConductorState, atomic_save
from conductor.core.storage import StorageResolver
from tests.helpers import make_run_state

NAME = "proj"
BRIEF = "## Goal\nAdd a /health endpoint that reports database connectivity.\n"


def run_main(argv: list[str]) -> tuple[str, str, int]:
    """Run `conductor <argv>`; return (stdout, stderr, exit code)."""
    from conductor.cli import main

    out, err = StringIO(), StringIO()
    code = 0
    with patch("sys.argv", ["conductor", *argv]), patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return out.getvalue(), err.getvalue(), code


def init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def seed_state(repo: Path, name: str = NAME, **fields) -> ConductorState:
    fields.setdefault("runs", [make_run_state(0, "r0")])
    state = ConductorState(project_name=name, base_branch="main", **fields)
    atomic_save(state, StorageResolver(repo).conductor_state(name))
    return state


def saved_state(repo: Path, name: str = NAME) -> ConductorState:
    path = StorageResolver(repo).conductor_state(name)
    return ConductorState.model_validate_json(path.read_text(encoding="utf-8"))


def write_brief(repo: Path, text: str = BRIEF, name: str = NAME) -> None:
    path = StorageResolver(repo).conductor_brief(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def release_held_locks() -> None:
    """Drop project locks taken by CLI commands in this (test) process."""
    for path in list(lock._held):
        lock.release_project_lock(path.parent)


class FakeTmux:
    """Stand-in for `conductor.cli._tmux`: a server holding *sessions*.

    sessions maps a session name to its panes as (dead, pid, window, command).
    """

    def __init__(self, sessions: dict[str, list[tuple[str, str, str, str]]] | None = None):
        self.sessions = sessions or {}
        self.calls: list[tuple[str, ...]] = []

    def _session(self, target: str) -> str | None:
        # "=session:" or "=session:=window"
        assert target.startswith("=") and ":" in target, f"inexact session target {target!r}"
        name = target[1:].split(":", 1)[0]
        return name if name in self.sessions else None

    @staticmethod
    def _window(target: str) -> str | None:
        window = target.split(":", 1)[1]
        return window[1:] if window.startswith("=") else None

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        cmd = args[0]
        rc, out = 0, ""
        if cmd == "has-session":
            rc = 0 if self._session(args[2]) else 1
        elif cmd == "list-panes":
            target = args[args.index("-t") + 1]
            name = self._session(target)
            window = self._window(target)
            panes = [p for p in self.sessions.get(name or "", []) if window in (None, p[2])]
            out = "".join("\t".join(p) + "\n" for p in panes)
        elif cmd == "kill-window":
            target = args[2]
            name, window = self._session(target), self._window(target)
            if name:
                self.sessions[name] = [p for p in self.sessions[name] if p[2] != window]
        elif cmd == "new-window":
            name = self._session(args[args.index("-t") + 1])
            if name:
                self.sessions[name].append(("0", "998", args[args.index("-n") + 1], "python3"))
            else:
                rc = 1
        elif cmd == "kill-session":
            name = self._session(args[2])
            if name:
                del self.sessions[name]
            else:
                rc = 1
        elif cmd == "new-session":
            name = args[args.index("-s") + 1]
            if name in self.sessions:
                rc = 1
            else:
                self.sessions[name] = [("0", "999", args[args.index("-n") + 1], "python3")]
        return subprocess.CompletedProcess(["tmux", *args], rc, out, "")

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture(name="repo")
def repo_fixture(tmp_path, monkeypatch):
    """An empty git repo; the CLI must not reach a tmux server the developer may be in."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield init_repo(tmp_path / "repo")
    # A CLI command keeps its project lock for the life of the process.
    release_held_locks()


@pytest.fixture(name="loop_calls")
def loop_calls_fixture(monkeypatch):
    """Replace the orchestrator loop; record the state and config it receives."""
    calls: list[tuple[ConductorState, object]] = []

    async def fake_loop(state, config):
        calls.append((state.model_copy(deep=True), config))
        return state

    monkeypatch.setattr("conductor.core.orchestrator.conductor_run_loop", fake_loop)
    return calls


@pytest.fixture(name="fake_tmux")
def fake_tmux_fixture(monkeypatch):
    import conductor.cli as cli

    fake = FakeTmux()
    monkeypatch.setattr(cli, "_tmux", fake)
    monkeypatch.setattr(cli, "_has_children", lambda pid: False)
    return fake


@pytest.fixture(name="execvp")
def execvp_fixture(monkeypatch):
    """Record the final `tmux attach-session` instead of exec'ing it."""
    import conductor.cli as cli

    calls = []

    def fake_execvp(file, argv):
        calls.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(cli.os, "execvp", fake_execvp)
    monkeypatch.setattr(cli, "_has_tty", lambda: True)
    return calls
