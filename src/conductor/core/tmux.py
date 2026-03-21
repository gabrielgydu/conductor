"""TmuxManager: spawn and manage processes in tmux windows."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path


class TmuxManager:
    def __init__(self, session_name: str = "conductor") -> None:
        self._session_name = session_name

    async def ensure_session(self, name: str) -> None:
        """Create tmux session if it doesn't exist."""
        # Real implementation: tmux new-session -d -s name
        pass

    async def spawn_in_window(self, name: str, cmd: str) -> None:
        """Spawn command in a tmux window (non-blocking)."""
        # Real implementation: tmux new-window -t session:name -n name cmd
        pass

    async def spawn_in_window_and_wait(self, name: str, cmd: str) -> int:
        """Spawn command in a tmux window and wait for exit, return exit code."""
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await proc.communicate()
        return proc.returncode or 0

    async def is_window_alive(self, name: str) -> bool:
        """Check if a tmux window/pane is alive (process still running)."""
        # Real implementation: check tmux has-session / list-panes
        return False

    async def get_pane_pid(self, name: str) -> int | None:
        """Get PID of the process in the tmux pane."""
        return None

    async def kill_window(self, name: str) -> None:
        """Kill a tmux window."""
        pass

    async def kill_session(self, name: str) -> None:
        """Kill a tmux session."""
        pass
