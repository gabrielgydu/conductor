"""Integration test fixtures: mocks for Claude CLI, Tmux, Speccer, Runner, and git/storage helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator
from unittest.mock import AsyncMock, create_autospec

import pytest

from conductor.core.storage import StorageResolver
from conductor.core.tmux import MIN_HEALTHY_TREE_DEPTH, TmuxManager


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

    # Write stream-json to stdout in the format the orchestrator expects:
    # {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
    print(json.dumps({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": matched_response}]
        }
    }), flush=True)
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
    """In-memory fake of ``conductor.core.tmux.TmuxManager``.

    Production code never receives this object directly: the ``mock_tmux``
    fixture patches ``TmuxManager`` with a factory returning
    :func:`tmux_proxy`, a ``create_autospec(TmuxManager)`` that checks every
    call against the *real* signature and then delegates here. Keyword-only
    options the fake does not model (``log_file``, ``detached``, ...) land in
    ``**_opts``, so a new optional parameter on ``TmuxManager`` does not break
    the fake, while a call the real class would reject still raises TypeError.
    """

    def __init__(self) -> None:
        self._windows: dict[str, dict] = {}
        self._session_name: str | None = None
        self._spawned_commands: list[dict] = []
        self._spawn_callback: Callable | None = None
        self._files_written: set[Path] = set()

    @property
    def windows(self) -> dict:
        return self._windows

    @property
    def session_name(self) -> str | None:
        return self._session_name

    # -- test controls -------------------------------------------------------

    def _window(self, name: str) -> dict:
        return self._windows.setdefault(name, {})

    def set_window_alive(self, name: str, alive: bool) -> None:
        self._window(name)["alive"] = alive

    def set_window_pid(self, name: str, pid: int) -> None:
        self._window(name)["pid"] = pid

    def set_window_exit_code(self, name: str, code: int) -> None:
        self._window(name)["exit_code"] = code

    def set_window_children(self, name: str, healthy: bool) -> None:
        """Answer for ``has_active_children`` (real: pstree depth of the pane)."""
        self._window(name)["children"] = healthy

    def finish_window(self, name: str, exit_code: int = 0) -> None:
        """Simulate the command running in *name* exiting with *exit_code*.

        Mirrors the real wrappers: the shell has no children any more and the
        exit code lands in the exit file given to ``spawn_runner_in_window``.
        """
        window = self._window(name)
        window.update(alive=False, children=False, exit_code=exit_code)
        if window.get("exit_file") is not None:
            self._write(window["exit_file"], str(exit_code))

    def get_spawned_commands(self) -> list[dict]:
        return self._spawned_commands

    def set_spawn_callback(self, callback: Callable) -> None:
        self._spawn_callback = callback

    def cleanup(self) -> None:
        """Delete the exit files this fake wrote."""
        for path in self._files_written:
            path.unlink(missing_ok=True)
        self._files_written.clear()

    def _write(self, path: Path, text: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self._files_written.add(path)

    async def _spawned(
        self, name: str, cmd: str, exit_file: Path | None = None, **record
    ) -> None:
        self._spawned_commands.append({"window": name, "cmd": cmd, **record})
        window = self._window(name)
        window.pop("exit_code", None)  # each command starts without a result
        window.update(alive=True, children=True, exit_file=exit_file)
        if self._spawn_callback is not None:
            result = self._spawn_callback(name, cmd)
            if asyncio.iscoroutine(result):
                await result

    # -- TmuxManager interface ----------------------------------------------

    def session_exists(self) -> bool:
        return self._session_name is not None

    async def ensure_session(self) -> None:
        pass

    async def spawn_in_window(self, name: str, cmd: str, **_opts) -> None:
        await self._spawned(name, cmd)

    async def spawn_in_window_and_wait(
        self, name: str, cmd: str, *, exit_file: Path | None = None, **_opts
    ) -> int:
        """Blocking spawn: the command has finished by the time this returns."""
        await self._spawned(name, cmd, exit_file=exit_file, waited=True)
        exit_code = self._window(name).get("exit_code", 0)
        self.finish_window(name, exit_code)
        return exit_code

    async def spawn_runner_in_window(
        self, name: str, cmd: str, *, exit_file: Path, **_opts
    ) -> None:
        """Non-blocking spawn: tests call ``finish_window`` to end the runner."""
        await self._spawned(name, cmd, exit_file=Path(exit_file), runner=True)

    async def is_window_alive(self, name: str) -> bool:
        return self._windows.get(name, {}).get("alive", False)

    async def is_runner_idle(self, name: str) -> bool:
        return not self._windows.get(name, {}).get("alive", False)

    async def get_pane_pid(self, name: str) -> int | None:
        return self._windows.get(name, {}).get("pid", None)

    async def has_active_children(
        self, name: str, depth: int = MIN_HEALTHY_TREE_DEPTH
    ) -> bool:
        window = self._windows.get(name, {})
        return window.get("children", window.get("alive", False))

    async def send_keys(self, name: str, keys: str) -> None:
        pass

    async def kill_window(self, name: str) -> None:
        if name in self._windows:
            self._windows[name]["alive"] = False

    async def kill_session(self) -> None:
        self._session_name = None
        for window in self._windows.values():
            window["alive"] = False

    async def capture_pane(self, name: str, lines: int = 20) -> str:
        return ""


def _refuse_real_tmux(name: str) -> Callable:
    def refuse(*args, **kwargs):
        raise AssertionError(f"TmuxManager.{name} would run the real tmux binary in a test")

    return refuse


def tmux_proxy(fake: MockTmux) -> TmuxManager:
    """Wrap *fake* in an autospec of ``TmuxManager`` (real signatures enforced).

    Every public ``TmuxManager`` method needs a fake - this fails loudly when
    the real class grows one - and private helpers that shell out to ``tmux``
    raise instead of returning a silent MagicMock.
    """
    proxy = create_autospec(TmuxManager, instance=True)
    for name, member in inspect.getmembers(TmuxManager, inspect.isfunction):
        if name.startswith("__"):
            continue
        if name.startswith("_"):
            behaviour = _refuse_real_tmux(name)
        else:
            behaviour = getattr(fake, name, None)
            if behaviour is None:
                raise AssertionError(
                    f"MockTmux has no fake for TmuxManager.{name}"
                    f"{inspect.signature(member)} - add one in tests/integration/conftest.py"
                )
        getattr(proxy, name).side_effect = behaviour
    proxy._session_name = fake.session_name
    return proxy


@pytest.fixture
def mock_tmux(monkeypatch) -> Iterator[MockTmux]:
    """Fake tmux; code under test gets a signature-checked proxy of it."""
    tmux = MockTmux()
    real_init = inspect.signature(TmuxManager)

    def factory(*args, **kwargs):
        bound = real_init.bind(*args, **kwargs)  # TypeError on drift, like the real class
        bound.apply_defaults()
        tmux._session_name = bound.arguments["session_name"]
        return tmux_proxy(tmux)

    monkeypatch.setattr("conductor.core.tmux.TmuxManager", factory)
    yield tmux
    tmux.cleanup()


@pytest.fixture(autouse=True)
def mock_conductor_post_run(monkeypatch):
    """Prevent conductor_post_run from hitting real Claude API in all integration tests."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "conductor.core.orchestrator.conductor_post_run",
        AsyncMock(return_value=None),
    )


@pytest.fixture
def isolated_run_loop(monkeypatch) -> SimpleNamespace:
    """Isolate ``conductor_run_loop`` from the network, real tools and other tests.

    Otherwise a single DONE run does ``git push`` + ``gh pr create`` (three
    attempts, 5 s sleeps), two or more DONE runs start a real integration
    merge (a worktree under /tmp, ``gh``, ``npx playwright``, ``claude``), and
    the module-level zombie counter leaks between tests that reuse window names.
    """
    import conductor.core.orchestrator as orch
    import conductor.integration.merge as merge
    from conductor.core.enums import IntegrationStatus, RunStatus
    from conductor.core.models import IntegrationState

    async def fake_merge(state, storage):
        return IntegrationState(
            status=IntegrationStatus.DONE,
            branch=f"integration/{state.project_name}",
            merged_runs=[r.index for r in state.runs if r.status == RunStatus.DONE],
        )

    mocks = SimpleNamespace(
        push_and_pr=AsyncMock(return_value=None),
        integration_merge=AsyncMock(side_effect=fake_merge),
    )
    monkeypatch.setattr(orch, "_push_and_pr_for_branch", mocks.push_and_pr)
    monkeypatch.setattr(merge, "run_integration_merge", mocks.integration_merge)
    monkeypatch.setattr(orch, "_shallow_tree_counts", {})
    return mocks


@pytest.fixture(autouse=True)
def patch_orchestrator_create_worktree(monkeypatch, tmp_path):
    """Patch create_worktree to avoid real git ops in tests."""
    import conductor.core.orchestrator as orch

    _wt_counter = [0]

    def mock_create_worktree(
        state, run_idx, stage_idx, storage_or_project_dir, *args, **kwargs
    ):
        run = state.runs[run_idx]
        stage = run.stages[stage_idx]
        _wt_counter[0] += 1
        wt_dir = tmp_path / "worktrees" / f"wt-{_wt_counter[0]}"
        wt_dir.mkdir(parents=True, exist_ok=True)
        stage.branch = f"conductor/{state.project_name}/{run.name}/{stage.name}"
        stage.worktree = str(wt_dir)

    monkeypatch.setattr(orch, "create_worktree", mock_create_worktree)


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
        subcommand, options = _parse_speccer_cmd(cmd)
        self._invocations.append(
            {"window": window_name, "cmd": cmd, "count": self._invoke_count}
        )
        progress_path = self._find_progress_path(options, storage_path)

        if subcommand == "init":
            # Init only sets the spec dir up: INIT status, not counted as a run.
            if progress_path:
                write_speccer_progress(progress_path, "INIT")
            return
        if subcommand == "generate":
            # Real `speccer generate` leaves docs/<feature>/run.sh with PHASES arrays.
            if progress_path:
                write_generated_run_sh(progress_path.parent.parent)
            return

        self._invoke_count += 1
        invoke = self._invoke_count

        # Status written by `speccer run` (with or without --continue):
        # - First fail_count runs: FAILED (explicit failure, triggers retry)
        # - Next run after the failures: NEEDS_INPUT (if needs_input=True)
        # - All subsequent runs: final_status
        if invoke <= self._fail_count:
            status = "FAILED"
        elif self._needs_input and invoke == self._fail_count + 1:
            status = "NEEDS_INPUT"
        else:
            status = self._final_status

        if progress_path:
            write_speccer_progress(progress_path, status)
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

    def _find_progress_path(
        self, options: dict[str, str], storage_path: Path | None
    ) -> Path | None:
        """PROGRESS.md the real speccer would write: <project-dir>/docs/<feature>/spec/."""
        if storage_path:
            return storage_path / "PROGRESS.md"
        if "--project-dir" in options and "--feature" in options:
            return (
                Path(options["--project-dir"]) / "docs" / options["--feature"]
                / "spec" / "PROGRESS.md"
            )
        return None


def _parse_speccer_cmd(cmd: str) -> tuple[str | None, dict[str, str]]:
    """Split ``cd <wt> && <bin>/speccer <subcommand> --opt value ...``."""
    parts = cmd.split("&&")[-1].split()
    subcommand = None
    options: dict[str, str] = {}
    for i, part in enumerate(parts):
        if subcommand is None and Path(part).name == "speccer" and i + 1 < len(parts):
            subcommand = parts[i + 1]
        elif part.startswith("--"):
            nxt = parts[i + 1] if i + 1 < len(parts) else ""
            options[part] = "" if nxt.startswith("--") else nxt
    return subcommand, options


def write_generated_run_sh(feature_docs_dir: Path, phases: int = 2) -> Path:
    """Write a run.sh shaped like `speccer generate` output (PHASES arrays)."""
    lines = ["#!/bin/bash", 'CONDUCTOR_MODEL="sonnet"']
    for n in range(1, phases + 1):
        lines += [
            f'PHASES[{n}]="phase-{n}.md"',
            f'PHASE_NAMES[{n}]="Phase {n}"',
            f'PHASE_TOKENS[{n}]="PHASE_{n}_COMPLETE"',
        ]
    run_sh = feature_docs_dir / "run.sh"
    run_sh.parent.mkdir(parents=True, exist_ok=True)
    run_sh.write_text("\n".join(lines) + "\n")
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)
    return run_sh


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
        """Write the run.sh that `speccer generate` would leave in docs/<feature>/."""
        self._stall = stall
        self._fail = fail
        write_generated_run_sh(feature_dir)

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
# RealMergeRepo
# ---------------------------------------------------------------------------


class RealMergeRepo:
    """A real git repo with helpers for creating feature branches."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def create_branch(self, name: str, files: dict[str, str]) -> None:
        """Create a branch from main with the given file contents."""
        subprocess.run(
            ["git", "-C", str(self.path), "checkout", "main"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.path), "checkout", "-b", name],
            check=True,
            capture_output=True,
        )
        for filename, content in files.items():
            file_path = self.path / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
            subprocess.run(
                ["git", "-C", str(self.path), "add", filename],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["git", "-C", str(self.path), "commit", "-m", f"changes on {name}"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.path), "checkout", "main"],
            check=True,
            capture_output=True,
        )


@pytest.fixture
def real_merge_repo(tmp_path) -> RealMergeRepo:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
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
    return RealMergeRepo(repo)


@pytest.fixture
def hermetic_merge(monkeypatch):
    """Keep run_integration_merge's real-git tests away from Claude, npx and gh sleeps.

    Post-merge validation (smoke via ``npx playwright`` + Claude self-healing) and
    integration-test generation are replaced with passing stubs, the PR retry
    delay is zeroed, and every ``run_claude`` binding on the merge path fails the
    test loudly instead of starting a real (paid) Claude session.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import conductor.core.validation as validation_module
    import conductor.integration.e2e as e2e_module
    import conductor.integration.merge as merge_module
    from conductor.core.models import E2ETestState
    from conductor.core.validation import ValidationResult

    async def _no_real_claude(*args, **kwargs):
        # pytest.fail raises a BaseException, so `except Exception` in e2e.py
        # cannot swallow it.
        pytest.fail("real Claude CLI must not be invoked from merge tests")

    for module in (merge_module, validation_module, e2e_module):
        monkeypatch.setattr(module, "run_claude", _no_real_claude)

    validate = AsyncMock(return_value=ValidationResult(passed=True, summary="stubbed"))
    e2e = AsyncMock(return_value=E2ETestState(skipped=1))
    monkeypatch.setattr(merge_module, "validate_and_fix", validate)
    monkeypatch.setattr(e2e_module, "run_integration_testing", e2e)
    monkeypatch.setattr(merge_module, "_GH_RETRY_DELAY_S", 0)
    return SimpleNamespace(validate=validate, e2e=e2e)


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
