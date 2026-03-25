"""TmuxManager: spawn and manage processes in tmux windows."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

# Minimum number of PIDs expected in a healthy runner process tree
# (shell → python → claude = 3 processes)
MIN_HEALTHY_TREE_DEPTH = 3


def check_pstree_depth(pid: int, depth: int = MIN_HEALTHY_TREE_DEPTH) -> bool:
    """Check if a process tree has at least `depth` PIDs (sync).

    Returns True if pstree is not available (safe fallback).
    """
    r = subprocess.run(
        ["pstree", "-p", str(pid)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return True
    pids = re.findall(r"\((\d+)\)", r.stdout)
    return len(pids) >= depth


class TmuxManager:
    def __init__(self, session_name: str = "conductor") -> None:
        self._session_name = session_name

    def _run_tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a tmux command synchronously."""
        return subprocess.run(
            ["tmux"] + list(args),
            capture_output=True,
            text=True,
            check=check,
        )

    def session_exists(self) -> bool:
        """Check if the tmux session exists."""
        r = self._run_tmux("has-session", "-t", self._session_name, check=False)
        return r.returncode == 0

    async def ensure_session(self) -> None:
        """Create tmux session if it doesn't exist."""
        if not self.session_exists():
            self._run_tmux(
                "new-session", "-d", "-s", self._session_name,
                "-x", "200", "-y", "50",
            )

    async def spawn_in_window(self, name: str, cmd: str, *, cwd: str | None = None, detached: bool = False) -> None:
        """Spawn command in a new tmux window (non-blocking, fire-and-forget)."""
        # Kill stale window first
        self._run_tmux("kill-window", "-t", f"{self._session_name}:{name}", check=False)

        args = ["new-window", "-t", self._session_name, "-n", name]
        if detached:
            args.insert(1, "-d")
        if cwd:
            args.extend(["-c", cwd])
        args.append(cmd)
        self._run_tmux(*args)

    async def spawn_in_window_and_wait(
        self,
        name: str,
        cmd: str,
        *,
        exit_file: Path | None = None,
        cwd: str | None = None,
    ) -> int:
        """Spawn command in tmux window, wait for it to finish, return exit code.

        Uses tmux wait-for to block until the command completes.
        Writes exit code to exit_file if provided.
        """
        # Kill stale window
        self._run_tmux("kill-window", "-t", f"{self._session_name}:{name}", check=False)

        # Build the command that writes exit code and signals completion
        wait_channel = f"conductor-{name}-done"
        if exit_file:
            wrapped = f'{cmd}; echo $? > {exit_file}; tmux wait-for -S {wait_channel}'
        else:
            wrapped = f'{cmd}; tmux wait-for -S {wait_channel}'

        args = ["new-window", "-t", self._session_name, "-n", name]
        if cwd:
            args.extend(["-c", cwd])
        args.append(wrapped)
        self._run_tmux(*args)

        # Block until signal
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._run_tmux("wait-for", wait_channel, check=False),
        )

        # Read exit code from file if available
        if exit_file and exit_file.exists():
            try:
                return int(exit_file.read_text().strip())
            except (ValueError, OSError):
                return 1
        return 0

    async def spawn_runner_in_window(
        self,
        name: str,
        cmd: str,
        *,
        exit_file: Path,
        cwd: str | None = None,
    ) -> None:
        """Spawn runner command in tmux window (non-blocking). Writes exit code to file when done."""
        # Kill stale window
        self._run_tmux("kill-window", "-t", f"{self._session_name}:{name}", check=False)

        # Wrap command to capture pane output on failure and write exit code
        fail_log = str(exit_file).replace("conductor-exit-", "conductor-fail-") + ".log"
        wrapped = (
            f'{cmd}; _rc=$?; '
            f'if [ "$_rc" -ne 0 ]; then '
            f'tmux capture-pane -t "$TMUX_PANE" -p -S -50 > {fail_log} 2>/dev/null; '
            f'fi; '
            f'echo $_rc > {exit_file}'
        )

        args = ["new-window", "-t", self._session_name, "-n", name]
        if cwd:
            args.extend(["-c", cwd])
        args.append(wrapped)
        self._run_tmux(*args)

    async def is_window_alive(self, name: str) -> bool:
        """Check if a tmux window exists."""
        r = self._run_tmux(
            "list-windows", "-t", self._session_name,
            "-F", "#{window_name}",
            check=False,
        )
        if r.returncode != 0:
            return False
        return name in r.stdout.strip().splitlines()

    async def is_runner_idle(self, name: str) -> bool:
        """Check if the runner's pane is idle (no child processes under the pane shell)."""
        pid = await self.get_pane_pid(name)
        if pid is None:
            return True  # Window doesn't exist = "idle"
        # Check if the pane's shell has any child processes
        import subprocess as _sp
        r = _sp.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True,
        )
        # pgrep exits 0 if children found, 1 if none
        return r.returncode != 0

    async def get_pane_pid(self, name: str) -> int | None:
        """Get PID of the process in the tmux pane."""
        r = self._run_tmux(
            "list-panes", "-t", f"{self._session_name}:{name}",
            "-F", "#{pane_pid}",
            check=False,
        )
        if r.returncode != 0:
            return None
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    async def send_keys(self, name: str, keys: str) -> None:
        """Send keys to a tmux pane (e.g., Ctrl-C)."""
        self._run_tmux(
            "send-keys", "-t", f"{self._session_name}:{name}",
            keys, check=False,
        )

    async def kill_window(self, name: str) -> None:
        """Kill a tmux window."""
        self._run_tmux("kill-window", "-t", f"{self._session_name}:{name}", check=False)

    async def kill_session(self) -> None:
        """Kill the tmux session."""
        self._run_tmux("kill-session", "-t", self._session_name, check=False)

    async def has_active_children(self, name: str, depth: int = MIN_HEALTHY_TREE_DEPTH) -> bool:
        """Check if the pane has a healthy process tree.

        Returns False if fewer than `depth` PIDs in the tree (zombie runner).
        Falls back to True if pstree is not available.
        """
        pid = await self.get_pane_pid(name)
        if pid is None:
            return False
        return check_pstree_depth(pid, depth)

    async def capture_pane(self, name: str, lines: int = 20) -> str:
        """Capture the last N lines from a tmux pane."""
        r = self._run_tmux(
            "capture-pane", "-t", f"{self._session_name}:{name}",
            "-p", "-S", f"-{lines}",
            check=False,
        )
        return r.stdout if r.returncode == 0 else ""
