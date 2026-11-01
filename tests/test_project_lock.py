"""Tests for the single-instance project lock and how the CLI uses it."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import conductor.cli as cli
from conductor.core import lock
from conductor.core.lock import LockHeld, acquire_project_lock, release_project_lock
from conductor.core.storage import StorageResolver
from tests.cli_support import (  # noqa: F401
    NAME,
    FakeTmux,
    loop_calls_fixture,
    release_held_locks,
    repo_fixture,
    run_main,
    seed_state,
    write_brief,
)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _no_tmux_and_release_locks(monkeypatch):
    # acquire_project_lock asks tmux where it runs when $TMUX is set.
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield
    release_held_locks()


@pytest.fixture
def other_holder():
    """Start another process that holds the lock for a directory until the test ends."""
    procs = []

    def start(conductor_dir: Path, command: str = "run") -> subprocess.Popen:
        code = textwrap.dedent(f"""
            import sys
            from pathlib import Path
            from conductor.core.lock import acquire_project_lock
            acquire_project_lock(Path({str(conductor_dir)!r}), {command!r})
            print("locked", flush=True)
            sys.stdin.read()
        """)
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        env.pop("TMUX", None)
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env
        )
        assert proc.stdout.readline().strip() == "locked"
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_second_process_is_refused_and_told_who_holds_the_lock(tmp_path, other_holder):
    holder = other_holder(tmp_path / "proj", "go")

    with pytest.raises(LockHeld) as excinfo:
        acquire_project_lock(tmp_path / "proj", "run")

    assert excinfo.value.holder["pid"] == holder.pid
    assert f"conductor go (pid {holder.pid}" in str(excinfo.value)


def test_lock_is_released_when_the_holder_exits(tmp_path, other_holder):
    holder = other_holder(tmp_path / "proj")
    holder.stdin.close()
    holder.wait(timeout=10)

    acquire_project_lock(tmp_path / "proj", "run")


def test_reacquiring_in_the_same_process_is_a_no_op(tmp_path):
    acquire_project_lock(tmp_path / "proj", "go")
    acquire_project_lock(tmp_path / "proj", "run")  # go -> run must not deadlock
    release_project_lock(tmp_path / "proj")

    # Released for real: an independent open file description can take it.
    fd = os.open(tmp_path / "proj" / lock.LOCK_FILE, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_run_refuses_while_another_instance_holds_the_project(repo, loop_calls, other_holder):
    seed_state(repo)
    holder = other_holder(StorageResolver(repo).conductor_dir(NAME))

    out, _, code = run_main(["run", "--inside-tmux", "--name", NAME, "--project-dir", str(repo)])

    assert code == 1
    assert f"conductor run (pid {holder.pid}" in out
    assert loop_calls == []


def test_plan_refuses_while_a_run_holds_the_project(repo, other_holder, monkeypatch):
    seed_state(repo)
    write_brief(repo)
    other_holder(StorageResolver(repo).conductor_dir(NAME))
    monkeypatch.setattr(cli, "_generate_and_review_plan", lambda *a: pytest.fail("planned anyway"))

    out, _, code = run_main(["plan", "--name", NAME, "--project-dir", str(repo)])

    assert code == 1
    assert "already working on this project" in out


def test_learnings_refuses_while_a_run_holds_the_project(repo, other_holder, monkeypatch):
    seed_state(repo)
    other_holder(StorageResolver(repo).conductor_dir(NAME))
    monkeypatch.setattr(
        "conductor.core.orchestrator._review_learnings", lambda *a, **k: pytest.fail("reviewed anyway")
    )

    out, _, code = run_main(["learnings", "--name", NAME, "--project-dir", str(repo)])

    assert code == 1
    assert "already working on this project" in out


def test_go_hands_the_lock_to_the_tmux_process(repo, monkeypatch):
    seed_state(repo)
    write_brief(repo)
    conductor_dir = StorageResolver(repo).conductor_dir(NAME)
    lock_free_at_launch = []

    class CheckingTmux(FakeTmux):
        def __call__(self, *args):
            if args[0] == "new-session":
                fd = os.open(conductor_dir / lock.LOCK_FILE, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_free_at_launch.append(True)
                except BlockingIOError:
                    lock_free_at_launch.append(False)
                finally:
                    os.close(fd)
            return super().__call__(*args)

    monkeypatch.setattr(cli, "_tmux", CheckingTmux())
    monkeypatch.setattr(cli.os, "execvp", lambda *a: (_ for _ in ()).throw(SystemExit(0)))

    _, _, code = run_main(["go", "--name", NAME, "--project-dir", str(repo)])

    assert code == 0
    assert lock_free_at_launch == [True]


def test_holder_description_names_its_tmux_window(tmp_path, monkeypatch):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "conductor-proj:conductor\n", "")

    monkeypatch.setattr(lock.subprocess, "run", fake_run)
    holder = {"pid": 7, "command": "run", "tmux_socket": "/tmp/tmux-1000/default", "tmux_pane": "%5"}

    assert lock.describe_holder(holder) == "conductor run (pid 7, tmux conductor-proj:conductor)"
    assert seen[0][:3] == ["tmux", "-S", "/tmp/tmux-1000/default"]

    monkeypatch.setattr(lock.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "gone"))
    assert lock.describe_holder(holder) == "conductor run (pid 7, tmux pane %5)"
