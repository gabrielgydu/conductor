"""Integration test fixtures: mocks for Claude CLI, Tmux, Speccer, Runner, and git/storage helpers."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from typing import Callable

import pytest

from conductor.core.storage import StorageResolver


# ---------------------------------------------------------------------------
# MockClaudeCLI
# ---------------------------------------------------------------------------

_MOCK_CLAUDE_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json
    import os
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    tmp_path = Path(os.environ.get("MOCK_CLAUDE_TMP", "/tmp"))

    prompt = sys.stdin.read()
    args = sys.argv[1:]

    # Load response map
    responses_file = tmp_path / "claude_responses.json"
    response_map = {}
    if responses_file.exists():
        response_map = json.loads(responses_file.read_text())

    matched_response = "OK"
    exit_code = 0

    for pattern, config in response_map.items():
        if pattern in prompt:
            matched_response = config.get("response", "OK")
            exit_code = config.get("exit_code", 0)
            break

    # Write stream-json to stdout
    print(json.dumps({"type": "assistant", "content": matched_response}), flush=True)
    print(json.dumps({
        "type": "result",
        "result": {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        }
    }), flush=True)

    # Append call metadata
    calls_file = tmp_path / "claude_calls.jsonl"
    entry = json.dumps({
        "prompt": prompt,
        "args": args,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    with open(calls_file, "a") as f:
        f.write(entry + "\\n")

    sys.exit(exit_code)
""")


class MockClaudeCLI:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self._tmp_path = tmp_path
        self._responses_file = tmp_path / "claude_responses.json"
        self._calls_file = tmp_path / "claude_calls.jsonl"

        # Write mock executable
        script_path = tmp_path / "claude"
        script_path.write_text(_MOCK_CLAUDE_SCRIPT)
        script_path.chmod(
            script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )

        # Prepend tmp_path to PATH and set env var for script to find its tmp dir
        monkeypatch.setenv(
            "PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", "")
        )
        monkeypatch.setenv("MOCK_CLAUDE_TMP", str(tmp_path))

        # Initialize empty response map
        self._responses_file.write_text("{}")

    def set_response(self, pattern: str, response: str, exit_code: int = 0) -> None:
        response_map = json.loads(self._responses_file.read_text())
        response_map[pattern] = {"response": response, "exit_code": exit_code}
        self._responses_file.write_text(json.dumps(response_map))

    def set_side_effect(self, pattern: str, callable: Callable) -> None:
        # Store side effects in a separate in-memory map (callable can't be serialized)
        if not hasattr(self, "_side_effects"):
            self._side_effects: dict[str, Callable] = {}
        self._side_effects[pattern] = callable

    def get_calls(self) -> list[dict]:
        if not self._calls_file.exists():
            return []
        calls = []
        for line in self._calls_file.read_text().splitlines():
            line = line.strip()
            if line:
                calls.append(json.loads(line))
        return calls

    @property
    def call_count(self) -> int:
        return len(self.get_calls())


@pytest.fixture
def mock_claude_cli(tmp_path, monkeypatch) -> MockClaudeCLI:
    return MockClaudeCLI(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# MockTmux
# ---------------------------------------------------------------------------


class MockTmux:
    def __init__(self) -> None:
        self._windows: dict[str, dict] = {}
        self._session_name: str | None = None
        self._spawned_commands: list[dict] = []
        self._spawn_callback: Callable | None = None

    @property
    def windows(self) -> dict:
        return self._windows

    @property
    def session_name(self) -> str | None:
        return self._session_name

    def set_window_alive(self, name: str, alive: bool) -> None:
        if name not in self._windows:
            self._windows[name] = {}
        self._windows[name]["alive"] = alive

    def set_window_pid(self, name: str, pid: int) -> None:
        if name not in self._windows:
            self._windows[name] = {}
        self._windows[name]["pid"] = pid

    def set_window_exit_code(self, name: str, code: int) -> None:
        if name not in self._windows:
            self._windows[name] = {}
        self._windows[name]["exit_code"] = code

    def get_spawned_commands(self) -> list[dict]:
        return self._spawned_commands

    def set_spawn_callback(self, callback: Callable) -> None:
        self._spawn_callback = callback

    async def ensure_session(self, name: str) -> None:
        self._session_name = name

    async def spawn_in_window(self, name: str, cmd: str) -> None:
        self._spawned_commands.append({"window": name, "cmd": cmd})
        if name not in self._windows:
            self._windows[name] = {"alive": True}
        else:
            self._windows[name]["alive"] = True
        if self._spawn_callback is not None:
            result = self._spawn_callback(name, cmd)
            if asyncio.iscoroutine(result):
                await result

    async def spawn_in_window_and_wait(self, name: str, cmd: str) -> int:
        self._spawned_commands.append({"window": name, "cmd": cmd, "waited": True})
        if name not in self._windows:
            self._windows[name] = {"alive": False, "exit_code": 0}
        if self._spawn_callback is not None:
            result = self._spawn_callback(name, cmd)
            if asyncio.iscoroutine(result):
                await result
        return self._windows.get(name, {}).get("exit_code", 0)

    async def is_window_alive(self, name: str) -> bool:
        return self._windows.get(name, {}).get("alive", False)

    async def get_pane_pid(self, name: str) -> int | None:
        return self._windows.get(name, {}).get("pid", None)

    async def kill_window(self, name: str) -> None:
        if name in self._windows:
            self._windows[name]["alive"] = False

    async def kill_session(self, name: str) -> None:
        self._session_name = None
        for key in self._windows:
            self._windows[key]["alive"] = False


@pytest.fixture
def mock_tmux(monkeypatch) -> MockTmux:
    tmux = MockTmux()
    # Patch TmuxManager wherever it might be imported
    try:
        monkeypatch.setattr("conductor.core.tmux.TmuxManager", lambda *a, **kw: tmux)
    except (AttributeError, ModuleNotFoundError):
        pass
    return tmux


# ---------------------------------------------------------------------------
# MockSpeccer
# ---------------------------------------------------------------------------


class MockSpeccer:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._final_status = "COMPLETE"
        self._needs_input = False
        self._fail_count = 0
        self._exit_code = 0
        self._invocations: list[dict] = []
        self._invoke_count = 0

    def set_lifecycle(
        self,
        final_status: str = "COMPLETE",
        needs_input: bool = False,
        fail_count: int = 0,
    ) -> None:
        self._final_status = final_status
        self._needs_input = needs_input
        self._fail_count = fail_count

    def set_exit_code(self, code: int) -> None:
        self._exit_code = code

    def get_invocations(self) -> list[dict]:
        return self._invocations

    @property
    def invocation_count(self) -> int:
        return self._invoke_count

    def _handle_spawn(
        self, window_name: str, cmd: str, storage_path: Path | None = None
    ) -> None:
        self._invoke_count += 1
        self._invocations.append(
            {"window": window_name, "cmd": cmd, "count": self._invoke_count}
        )

        # Determine status to write:
        # - First fail_count invocations: FAILED (explicit failure, triggers retry)
        # - Next invocation after fails: NEEDS_INPUT (if needs_input=True)
        # - All subsequent: final_status
        if self._invoke_count <= self._fail_count:
            status = "FAILED"
        elif self._needs_input and self._invoke_count == self._fail_count + 1:
            status = "NEEDS_INPUT"
        else:
            status = self._final_status

        # Find PROGRESS.md path from cmd or use storage_path
        progress_path = self._find_progress_path(cmd, storage_path)
        if progress_path:
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.write_text(f"STATUS: {status}\n")
            # Write QUESTIONS.md alongside for NEEDS_INPUT scenarios
            if status == "NEEDS_INPUT":
                questions_path = progress_path.parent / "QUESTIONS.md"
                questions_path.write_text(
                    "## Round 1 Questions\n\n"
                    "### Q1: Scope\n"
                    "**Question:** What scope should we cover?\n"
                    "**Answer:**\n\n"
                    "### Q2: Auth\n"
                    "**Question:** What auth method?\n"
                    "**Answer:**\n"
                )

    def _find_progress_path(self, cmd: str, storage_path: Path | None) -> Path | None:
        """Try to extract or construct a PROGRESS.md path from the command."""
        if storage_path:
            return storage_path / "PROGRESS.md"
        # Try to find path in cmd args
        parts = cmd.split()
        for i, part in enumerate(parts):
            if part.endswith("PROGRESS.md"):
                return Path(part)
            if "spec" in part and Path(part).is_dir():
                return Path(part) / "PROGRESS.md"
        return None


@pytest.fixture
def mock_speccer(tmp_path, monkeypatch) -> MockSpeccer:
    return MockSpeccer(tmp_path)


# ---------------------------------------------------------------------------
# MockRunner
# ---------------------------------------------------------------------------


class MockRunner:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._stall = False
        self._fail = False
        self._steer_handler: Callable | None = None
        self._steer_messages: list[str] = []
        self._invocations: list[dict] = []

    def write_run_sh(
        self, feature_dir: Path, stall: bool = False, fail: bool = False
    ) -> None:
        self._stall = stall
        self._fail = fail
        run_sh = feature_dir / "run.sh"
        feature_dir.mkdir(parents=True, exist_ok=True)
        content = "#!/bin/bash\n"
        if not stall:
            content += "echo 'activity' > activity.log\n"
        exit_code = 1 if fail else 0
        content += f"exit {exit_code}\n"
        run_sh.write_text(content)
        run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)

    def set_steer_handler(self, handler: Callable) -> None:
        self._steer_handler = handler

    @property
    def steer_messages_received(self) -> list[str]:
        return self._steer_messages

    def _handle_spawn(
        self, window_name: str, cmd: str, storage_path: Path | None = None
    ) -> None:
        self._invocations.append({"window": window_name, "cmd": cmd})

        if storage_path:
            if not self._stall:
                activity_log = storage_path / "activity.log"
                activity_log.parent.mkdir(parents=True, exist_ok=True)
                activity_log.write_text("running\n")
            exit_file = storage_path / "exit_code"
            exit_code = 1 if self._fail else 0
            exit_file.parent.mkdir(parents=True, exist_ok=True)
            exit_file.write_text(str(exit_code))


@pytest.fixture
def mock_runner(tmp_path) -> MockRunner:
    return MockRunner(tmp_path)


# ---------------------------------------------------------------------------
# TmpGitRepo
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return repo


# ---------------------------------------------------------------------------
# TmpStorageDir
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_storage_dir(tmp_path, monkeypatch) -> Path:
    storage_base = tmp_path / "storage"
    storage_base.mkdir(parents=True, exist_ok=True)

    def patched_init(self, repo_path: Path) -> None:
        # Use a fixed key so storage is predictable in tests
        self.repo_root = tmp_path / "repo"
        self._project_key = "test-project"
        self.base_dir = storage_base

    monkeypatch.setattr(StorageResolver, "__init__", patched_init)
    return storage_base


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------


def write_speccer_progress(path: Path, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"STATUS: {status}\n")


def write_exit_file(path: Path, exit_code: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(exit_code))


def write_activity_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
