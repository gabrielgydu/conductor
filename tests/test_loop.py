"""Loop mode end to end: the real _loop_main against a real git repo.

tmux is replaced by ``BashTmux``, which runs each window's script to
completion with bash, exactly as the real TmuxManager would have tmux exec
it. ``claude`` on PATH is a fake that replays scripted sessions (see
``FakeClaude``), so the real session script, stream_filter and git commits
all run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from conductor.core.loop import (
    LoopState, _loop_main, check_output_for_signal, parse_checklist, run_loop_in_tmux,
)
from conductor.core.storage import StorageResolver

_FAKE_CLAUDE_PY = r'''
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_CLAUDE_DIR"])
n = len(list(state.glob("call-*.json")))
sessions = json.loads((state / "sessions.json").read_text())
session = sessions[min(n, len(sessions) - 1)]
(state / f"call-{n}.json").write_text(json.dumps(
    {"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": sys.stdin.read()}
))
for rel, content in session.get("write", {}).items():
    Path(rel).write_text(content)
if session.get("stderr"):
    print(session["stderr"], file=sys.stderr)
for event in session.get("events", []):
    print(json.dumps(event), flush=True)
sys.exit(session.get("exit", 0))
'''


def say(text: str, **extra) -> dict:
    """A stream-json assistant event carrying *text*."""
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, **extra}


class FakeClaude:
    """``claude`` on PATH that replays ``sessions`` in order (the last one repeats).

    A session is a dict: ``events`` (stream-json lines to print), ``write``
    ({relative path: content} written in its cwd), ``stderr`` and ``exit``.
    """

    def __init__(self, root: Path) -> None:
        self.dir = root / "fake-claude"
        self.bindir = self.dir / "bin"
        self.bindir.mkdir(parents=True)
        script = self.dir / "fake_claude.py"
        script.write_text(_FAKE_CLAUDE_PY)
        shim = self.bindir / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{script}" "$@"\n')
        shim.chmod(0o755)
        self.script([{"events": [say("ok")]}])

    def script(self, sessions: list[dict]) -> None:
        (self.dir / "sessions.json").write_text(json.dumps(sessions))

    @property
    def calls(self) -> list[dict]:
        files = sorted(self.dir.glob("call-*.json"), key=lambda p: int(p.stem.split("-")[1]))
        return [json.loads(p.read_text()) for p in files]


def _run_window(script: str, cwd: str | None, log_file: Path | None) -> None:
    log_file = log_file or Path("/dev/null")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as log:
        subprocess.run(["bash", "-c", script], cwd=cwd, stdout=log, stderr=log)


class BashTmux:
    """Stands in for TmuxManager: runs each window's script to completion with bash."""

    def __init__(self, session_name: str = "conductor") -> None:
        self.session_name = session_name
        self.scripts: list[str] = []

    async def ensure_session(self) -> None:
        pass

    async def spawn_in_window(self, name, cmd, *, cwd=None, detached=False, log_file=None) -> None:
        self.scripts.append(cmd)
        _run_window(cmd, cwd, log_file)

    async def is_window_alive(self, name: str) -> bool:
        return True

    async def get_pane_pid(self, name: str) -> int | None:
        return None

    async def kill_window(self, name: str) -> None:
        pass


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    """A git repo whose path contains a space, with no hooks and no signing."""
    repo = tmp_path / "my project"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in [
        ("user.name", "Test"), ("user.email", "test@example.com"),
        ("commit.gpgsign", "false"), ("core.hooksPath", str(tmp_path / "hooks")),
    ]:
        _git(repo, "config", key, value)
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def claude(tmp_path, monkeypatch) -> FakeClaude:
    fake = FakeClaude(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(fake.dir))
    monkeypatch.setenv("PATH", f"{fake.bindir}{os.pathsep}{os.environ['PATH']}")
    return fake


@pytest.fixture
def tmux(monkeypatch) -> list[BashTmux]:
    """Every BashTmux the loop creates."""
    created: list[BashTmux] = []

    def factory(*args, **kwargs) -> BashTmux:
        created.append(BashTmux(*args, **kwargs))
        return created[-1]

    monkeypatch.setattr("conductor.core.tmux.TmuxManager", factory)
    return created


def lint_hook(repo: Path) -> None:
    """Install a pre-commit hook that rejects any notes.txt containing TODO."""
    hooks = Path(_git(repo, "config", "core.hooksPath").strip())
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "if grep -q TODO notes.txt 2>/dev/null; then\n"
        "  echo 'LINT FAILED: TODO left in notes.txt' >&2; exit 1\n"
        "fi\n"
    )
    hook.chmod(0o755)


def make_state(repo: Path, checklist: str, **kwargs) -> LoopState:
    plan = repo / "plan.md"
    plan.write_text(checklist)
    _git(repo, "add", "plan.md")
    _git(repo, "commit", "-q", "-m", "plan")
    return LoopState(
        name="fix", base_branch="main", plan_file=str(plan),
        tasks=parse_checklist(checklist), **kwargs,
    )


async def run_loop(state: LoopState, repo: Path) -> tuple[LoopState, list[dict]]:
    """Run the loop to the end; return the saved state and the audit events."""
    storage = StorageResolver(repo)
    await _loop_main(state, repo, storage)
    loop_dir = storage.conductor_dir(state.name)
    saved = LoopState.model_validate_json((loop_dir / "LOOP-STATE.json").read_text())
    audit = loop_dir / "LOOP-AUDIT.jsonl"
    events = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
    return saved, events


ONE_TASK = "- [ ] **Add notes** — create notes.txt\n"


async def test_default_model_reaches_claude_verbatim(repo, claude, tmux):
    """opus resolves to claude-opus-4-8[1m]: a zsh glob, and under bash one that matches files."""
    (repo / "claude-opus-4-8m").write_text("glob bait\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bait")
    claude.script([{"events": [say("done\n<task-completed/>")], "write": {"notes.txt": "n\n"}}])

    saved, events = await run_loop(make_state(repo, ONE_TASK, model="opus"), repo)

    (call,) = claude.calls
    assert call["argv"][call["argv"].index("--model") + 1] == "claude-opus-4-8[1m]"
    assert Path(call["cwd"]) == repo
    assert "create notes.txt" in call["prompt"]
    assert saved.tasks[0].status == "completed"
    session_end = [e for e in events if e.get("event") == "SESSION_END"]
    assert [e["exit_code"] for e in session_end] == [0]


async def test_session_files_live_in_the_loop_dir(repo, claude, tmux):
    """/tmp/conductor-loop-*-<name>-<idx> collided across projects using one loop name."""
    claude.script([{
        "events": [say("<task-completed/>")], "stderr": "cli warning", "write": {"notes.txt": "n\n"},
    }])

    _, events = await run_loop(make_state(repo, ONE_TASK), repo)

    session_dir = StorageResolver(repo).conductor_dir("fix") / "session"
    words = list(shlex.shlex(tmux[0].scripts[0], posix=True, punctuation_chars=True))
    for name in ("task-0-prompt.md", "task-0-output.jsonl", "task-0-exit", "task-0-stderr.log"):
        assert str(session_dir / name) in words
    assert "/tmp/conductor-loop-" not in tmux[0].scripts[0]
    (session_end,) = [e for e in events if e["event"] == "SESSION_END"]
    assert session_end["stderr_tail"] == "cli warning"
    assert list(session_dir.iterdir()) == []  # removed after the session


# -- rejected commits ----------------------------------------------------------


async def test_rejected_commit_is_fed_back_instead_of_crashing(repo, claude, tmux):
    """A pre-commit hook rejecting the completed task's commit used to kill the loop."""
    claude.script([
        {"events": [say("<task-completed/>")], "write": {"notes.txt": "TODO\n"}},
        {"events": [say("<task-completed/>")], "write": {"notes.txt": "done\n"}},
    ])
    state = make_state(repo, ONE_TASK)
    lint_hook(repo)

    saved, events = await run_loop(state, repo)

    assert "LINT FAILED: TODO left in notes.txt" in claude.calls[1]["prompt"]
    assert "Commit Rejected" not in claude.calls[0]["prompt"]
    task = saved.tasks[0]
    assert (task.status, task.attempts, task.commit_error) == ("completed", 2, None)
    assert _git(repo, "show", "HEAD:notes.txt") == "done\n"
    assert [e["event"] for e in events].count("GATE_FAILED") == 1
    assert saved.status == "completed"


async def test_rejected_wip_commit_leaves_work_for_the_next_session(repo, claude, tmux):
    claude.script([
        {"events": [say("ran out of turns")], "write": {"notes.txt": "TODO\n"}},
        {"events": [say("<task-completed/>")], "write": {"notes.txt": "done\n"}},
    ])
    state = make_state(repo, ONE_TASK)
    lint_hook(repo)

    saved, _ = await run_loop(state, repo)

    assert "LINT FAILED" in claude.calls[1]["prompt"]
    assert saved.tasks[0].status == "completed"
    assert "WIP" not in _git(repo, "log", "--format=%s")


async def test_wip_commit_clears_an_earlier_rejection(repo, claude, tmux):
    """Once the work is committed, later prompts must not quote the stale hook output."""
    claude.script([
        {"events": [say("<task-completed/>")], "write": {"notes.txt": "TODO\n"}},
        {"events": [say("ran out of turns")], "write": {"notes.txt": "fixed\n"}},
        {"events": [say("<task-completed/>")], "write": {"notes.txt": "done\n"}},
    ])
    state = make_state(repo, ONE_TASK)
    lint_hook(repo)

    saved, _ = await run_loop(state, repo)

    assert "LINT FAILED" in claude.calls[1]["prompt"]
    assert "Commit Rejected" not in claude.calls[2]["prompt"]
    assert _git(repo, "log", "--format=%s").splitlines()[:2] == ["Add notes", "WIP: Add notes"]
    assert (saved.tasks[0].status, saved.tasks[0].commit_error) == ("completed", None)


async def test_commit_that_keeps_failing_fails_the_task(repo, claude, tmux):
    claude.script([{"events": [say("<task-completed/>")], "write": {"notes.txt": "TODO\n"}}])
    state = make_state(repo, ONE_TASK + "- [ ] **Second** — another task\n")
    lint_hook(repo)

    saved, events = await run_loop(state, repo)

    assert saved.tasks[0].status == "failed"
    assert saved.tasks[0].attempts == 3
    assert "TASK_FAILED" in [e["event"] for e in events]
    assert len(claude.calls) >= 4  # the loop moved on to the second task


# -- sessions without a signal, final status --------------------------------

TWO_TASKS = ONE_TASK + "- [ ] **Second** — another task\n"


async def test_task_without_signal_fails_after_three_sessions(repo, claude, tmux):
    """No-signal sessions used to retry the same task until 3*N sessions, then report 'completed'."""
    claude.script([
        {"events": [say("working")], "write": {"notes.txt": "1\n"}},
        {"events": [say("working")], "write": {"notes.txt": "2\n"}},
        {"events": [say("working")], "write": {"notes.txt": "3\n"}},
        {"events": [say("<task-completed/>")], "write": {"second.txt": "x\n"}},
    ])

    saved, events = await run_loop(make_state(repo, TWO_TASKS), repo)

    assert [(t.status, t.attempts) for t in saved.tasks] == [("failed", 3), ("completed", 1)]
    assert saved.status == "failed"
    assert _git(repo, "log", "--format=%s").splitlines()[1:4] == ["WIP: Add notes"] * 3
    (done,) = [e for e in events if e["event"] == "LOOP_DONE"]
    assert "1/2 completed, 1 failed" in done["message"]


async def test_loop_stops_when_claude_never_runs(repo, claude, tmux):
    """An expired login fails every session instantly; that must not burn or fail the tasks."""
    claude.script([{"stderr": "Invalid API key · Please run /login", "exit": 1}])

    saved, events = await run_loop(make_state(repo, TWO_TASKS), repo)

    assert len(claude.calls) == 2
    assert [(t.status, t.attempts) for t in saved.tasks] == [("in_progress", 0), ("pending", 0)]
    assert saved.status == "failed"
    (failure,) = [e for e in events if e["event"] == "FAILURE"]
    assert "Invalid API key" in failure["message"]


async def test_api_error_session_counts_as_claude_not_running(repo, claude, tmux):
    """The CLI reports an unknown model as a synthetic assistant message plus an error result."""
    error = "There's an issue with the selected model (claude-nope). It may not exist."
    claude.script([{"events": [
        say(error, is_api_error_message=True, error="model_not_found"),
        {"type": "result", "subtype": "success", "is_error": True, "result": error},
    ], "exit": 1}])

    saved, events = await run_loop(make_state(repo, ONE_TASK, model="claude-nope"), repo)

    assert len(claude.calls) == 2
    assert saved.tasks[0].attempts == 0
    (failure,) = [e for e in events if e["event"] == "FAILURE"]
    assert error in failure["message"]


async def test_one_empty_session_is_retried(repo, claude, tmux):
    claude.script([
        {"exit": 1},
        {"events": [say("<task-completed/>")], "write": {"notes.txt": "n\n"}},
    ])

    saved, _ = await run_loop(make_state(repo, ONE_TASK), repo)

    assert (saved.status, saved.tasks[0].status, saved.tasks[0].attempts) == ("completed", "completed", 1)


def test_crashed_loop_is_not_left_running(repo, claude, tmux, monkeypatch):
    async def boom(self, *args, **kwargs):
        raise RuntimeError("tmux went away")

    monkeypatch.setattr(BashTmux, "spawn_in_window", boom)
    state = make_state(repo, ONE_TASK)
    storage = StorageResolver(repo)

    with pytest.raises(RuntimeError, match="tmux went away"):
        run_loop_in_tmux(state, repo, storage)

    saved = LoopState.model_validate_json(
        (storage.conductor_dir("fix") / "LOOP-STATE.json").read_text()
    )
    assert saved.status == "failed"


# -- signal detection ----------------------------------------------------------


def _output(*texts: str) -> str:
    return "\n".join(json.dumps(say(t)) for t in texts)


@pytest.mark.parametrize(("texts", "expected"), [
    (["All done.\n<task-completed/>"], "completed"),
    (["<task-completed />"], "completed"),
    (["  <task-failed/>  "], "failed"),
    (["Tests still fail, so I will not output <task-completed/> for this task.\n\n<task-failed/>"], "failed"),
    (["Plan: fix it, then output <task-completed/>", "Ran out of turns."], None),
    (["<task-completed/>", "On reflection the tests fail.\n<task-failed/>"], "failed"),
    (["<task-failed/>", "Fixed it after all.", "<task-completed/>"], "completed"),
    (["`<task-completed/>`"], None),
    ([], None),
])
def test_check_output_for_signal(texts, expected):
    assert check_output_for_signal(_output(*texts)) == expected
