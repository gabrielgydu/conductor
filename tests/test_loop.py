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
import subprocess
import sys
from pathlib import Path

import pytest

from conductor.core.loop import LoopState, _loop_main, parse_checklist
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
        self.windows: list[str] = []

    async def ensure_session(self) -> None:
        pass

    async def spawn_in_window(self, name, cmd, *, cwd=None, detached=False, log_file=None) -> None:
        self.windows.append(name)
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
