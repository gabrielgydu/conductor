"""Tests for `conductor run` / `conductor go` flag handling (orchestrator loop mocked)."""

from __future__ import annotations

import subprocess
from io import StringIO
from unittest.mock import patch

import pytest

from conductor.core.models import ConductorState, atomic_save
from conductor.core.storage import StorageResolver
from tests.helpers import make_run_state

NAME = "proj"


def _run_main(argv: list[str]) -> tuple[str, str, int]:
    from conductor.cli import main

    out, err = StringIO(), StringIO()
    code = 0
    with patch("sys.argv", ["conductor", *argv]), patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return out.getvalue(), err.getvalue(), code


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    # The developer may run the suite inside tmux; nothing here may talk to it.
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return repo


def _seed_state(repo, **fields) -> ConductorState:
    state = ConductorState(project_name=NAME, base_branch="main", runs=[make_run_state(0, "r0")], **fields)
    atomic_save(state, StorageResolver(repo).conductor_state(NAME))
    return state


def _saved_state(repo) -> ConductorState:
    path = StorageResolver(repo).conductor_state(NAME)
    return ConductorState.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.fixture
def loop_calls(monkeypatch):
    """Replace the orchestrator loop; record the state and config it receives."""
    calls: list[tuple[ConductorState, object]] = []

    async def fake_loop(state, config):
        calls.append((state.model_copy(deep=True), config))
        return state

    monkeypatch.setattr("conductor.core.orchestrator.conductor_run_loop", fake_loop)
    return calls


def _inside(repo, *extra: str) -> list[str]:
    return ["run", "--inside-tmux", "--name", NAME, "--project-dir", str(repo), *extra]


# ── overnight ─────────────────────────────────────────────────────────────────


def test_run_no_overnight_reaches_orchestrator_config(repo, loop_calls):
    _seed_state(repo)

    _, _, code = _run_main(_inside(repo, "--no-overnight"))

    assert code == 0
    state, config = loop_calls[0]
    assert state.overnight is False
    # The loop reads `config.overnight or state.overnight`; both must be off.
    assert config.overnight is False


def test_run_defaults_to_overnight(repo, loop_calls):
    _seed_state(repo, overnight=False)

    _, _, code = _run_main(_inside(repo))

    assert code == 0
    state, config = loop_calls[0]
    assert state.overnight is True
    assert config.overnight is True
