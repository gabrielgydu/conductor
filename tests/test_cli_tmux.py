"""Tests for the CLI's own tmux handling: exact targets, live-session refusal, re-exec."""

from __future__ import annotations

import shlex

import pytest

import conductor.cli as cli
from conductor.core.models import ConductorState, atomic_save
from conductor.core.storage import StorageResolver
from tests.cli_support import (  # noqa: F401
    execvp_fixture,
    fake_tmux_fixture,
    FakeTmux,
    init_repo,
    release_held_locks,
    run_main,
)
from tests.helpers import make_run_state


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    # A space in the path checks that the re-exec command is quoted.
    repo = init_repo(tmp_path / "my repo")
    state = ConductorState(project_name="foo", base_branch="main", runs=[make_run_state(0, "r0")])
    atomic_save(state, StorageResolver(repo).conductor_state("foo"))
    yield repo
    release_held_locks()


def test_tmux_target_is_exact():
    assert cli._tmux_target("conductor-v1.2") == "=conductor-v1.2:"
    assert cli._tmux_target("conductor-v1.2", "conductor") == "=conductor-v1.2:=conductor"


def test_run_launches_session_with_exact_targets_and_quoted_command(project, fake_tmux, execvp):
    _, _, code = run_main(["run", "--name", "foo", "--project-dir", str(project), "--no-overnight"])

    assert code == 0
    new = next(c for c in fake_tmux.calls if c[0] == "new-session")
    assert new[new.index("-s") + 1] == "conductor-foo"
    # remain-on-exit and pipe-pane are chained into the same tmux call.
    assert list(new).count(";") == 2
    assert "=conductor-foo:=conductor" in new
    shell_cmd = new[new.index("-n") + 2]
    bash, dash_c, inner = shlex.split(shell_cmd)
    assert (bash, dash_c) == ("bash", "-c")
    argv = shlex.split(inner)
    assert argv[argv.index("--project-dir") + 1] == str(project)
    assert argv[argv.index("run") :] == [
        "run", "--inside-tmux", "--name", "foo", "--project-dir", str(project), "--no-overnight",
    ]
    assert execvp == [["tmux", "attach-session", "-t", "=conductor-foo:"]]


def test_run_refuses_to_kill_a_live_session(project, fake_tmux, execvp, capsys):
    fake_tmux.sessions["conductor-foo"] = [
        ("0", "4242", "conductor", "python3"),
        ("1", "4243", "run0:backend", "bash"),
    ]

    out, _, code = run_main(["run", "--name", "foo", "--project-dir", str(project)])

    assert code == 1
    assert "still running" in out and "conductor (python3, pid 4242)" in out
    assert "run0:backend" not in out
    assert "kill-session" not in fake_tmux.commands()
    assert "new-session" not in fake_tmux.commands()
    assert execvp == []


def test_run_replaces_a_finished_session(project, fake_tmux, execvp):
    fake_tmux.sessions["conductor-foo"] = [
        ("1", "4242", "conductor", "python3"),  # dead, kept by remain-on-exit
        ("0", "4300", "zsh", "zsh"),  # idle shell from ensure_session
    ]

    _, _, code = run_main(["run", "--name", "foo", "--project-dir", str(project)])

    assert code == 0
    assert fake_tmux.commands()[:4] == ["has-session", "list-panes", "kill-session", "new-session"]
    assert ("kill-session", "-t", "=conductor-foo:") in fake_tmux.calls


def test_shell_pane_with_children_counts_as_busy(project, fake_tmux, execvp, monkeypatch):
    fake_tmux.sessions["conductor-foo"] = [("0", "4300", "conductor", "bash")]
    monkeypatch.setattr(cli, "_has_children", lambda pid: pid == "4300")

    _, _, code = run_main(["run", "--name", "foo", "--project-dir", str(project)])

    assert code == 1
    assert "kill-session" not in fake_tmux.commands()


def test_run_does_not_touch_a_session_that_only_shares_a_prefix(project, fake_tmux, execvp):
    fake_tmux.sessions["conductor-foobar"] = [("0", "1", "conductor", "python3")]

    _, _, code = run_main(["run", "--name", "foo", "--project-dir", str(project)])

    assert code == 0
    assert "conductor-foobar" in fake_tmux.sessions
    assert "kill-session" not in fake_tmux.commands()


def test_cleanup_refuses_while_session_is_live(project, fake_tmux):
    fake_tmux.sessions["conductor-foo"] = [("0", "4242", "conductor", "python3")]

    out, _, code = run_main(["cleanup", "--name", "foo", "--project-dir", str(project), "--force"])

    assert code == 1
    assert "still running" in out
    assert "conductor-foo" in fake_tmux.sessions


def test_cleanup_kills_a_finished_session(project, fake_tmux):
    fake_tmux.sessions["conductor-foo"] = [("1", "4242", "conductor", "python3")]

    out, _, code = run_main(["cleanup", "--name", "foo", "--project-dir", str(project)])

    assert code == 0
    assert "Killed stale tmux session: conductor-foo" in out
    assert "conductor-foo" not in fake_tmux.sessions


def _reexec_argv(fake_tmux: FakeTmux) -> list[str]:
    new = next(c for c in fake_tmux.calls if c[0] == "new-session")
    return shlex.split(shlex.split(new[new.index("-n") + 2])[2])


def test_go_reexec_forwards_only_the_flags_given(project, fake_tmux, execvp):
    from conductor.core.storage import StorageResolver

    StorageResolver(project).conductor_brief("foo").write_text("## Goal\nShip the health endpoint today.\n")

    _, _, code = run_main(["go", "--name", "foo", "--project-dir", str(project), "--no-quick"])

    assert code == 0
    argv = _reexec_argv(fake_tmux)
    assert "--no-quick" in argv
    assert "--quick" not in argv
    # go has no --max-parallel default any more, so a resume keeps the saved value
    assert "--max-parallel" not in argv


def test_new_session_gets_the_callers_conductor_env_and_path(project, fake_tmux, execvp, monkeypatch):
    # A tmux server that is already running would otherwise start the command
    # with its own environment: the inner run would read another CONDUCTOR_HOME.
    monkeypatch.setenv("CONDUCTOR_PRESETS_DIR", "/presets with space")
    monkeypatch.setenv("PATH", "/opt/claude/bin:/usr/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")

    _, _, code = run_main(["run", "--name", "foo", "--project-dir", str(project)])

    assert code == 0
    new = next(c for c in fake_tmux.calls if c[0] == "new-session")
    opts = list(new[: new.index("-n")])
    forwarded = {opts[i + 1] for i, o in enumerate(opts) if o == "-e"}
    assert "CONDUCTOR_PRESETS_DIR=/presets with space" in forwarded
    assert "PATH=/opt/claude/bin:/usr/bin" in forwarded
    assert not any(v.startswith("ANTHROPIC_") for v in forwarded)  # never put secrets in argv
