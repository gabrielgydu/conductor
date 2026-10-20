"""Single-instance lock for a conductor project (one repo + one --name).

Commands that drive or rewrite a project's state (run, go, plan, loop,
manual-test, cleanup) take an exclusive ``flock`` on
``<conductor_dir>/conductor.lock``. A second instance fails at once with a
`LockHeld` that names the holder.

The kernel drops a flock when its process exits, so a crashed orchestrator
never leaves a stale lock behind. The descriptor is not inheritable (Python's
default) and tmux windows are children of the tmux server, so speccer and
runner windows spawned by the orchestrator never hold it.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LOCK_FILE = "conductor.lock"

# Locks held by this process, by lock path. flock conflicts between two open
# file descriptions even inside one process, so re-acquiring (go -> plan ->
# run) must reuse the descriptor instead of opening the file again.
_held: dict[Path, int] = {}


class LockHeld(RuntimeError):
    def __init__(self, path: Path, holder: dict) -> None:
        self.path = path
        self.holder = holder
        super().__init__(f"{describe_holder(holder)} is already working on this project (lock: {path})")


def describe_holder(holder: dict) -> str:
    """One-line description of a lock holder, e.g. 'conductor run (pid 42, ...)'."""
    details = [f"pid {holder.get('pid', '?')}"]
    if holder.get("started_at"):
        details.append(f"since {holder['started_at']}")
    where = _tmux_location(holder)
    if where:
        details.append(f"tmux {where}")
    return f"conductor {holder.get('command') or '?'} ({', '.join(details)})"


def _lock_path(conductor_dir: Path) -> Path:
    return Path(conductor_dir).resolve() / LOCK_FILE


def _tmux_location(holder: dict) -> str:
    """'session:window' of the holder's tmux pane, or its pane id if tmux can't say.

    Looked up only when describing a holder, so taking the lock never runs tmux.
    """
    pane = holder.get("tmux_pane")
    if not pane:
        return ""
    socket = holder.get("tmux_socket")
    try:
        r = subprocess.run(
            ["tmux", *(["-S", socket] if socket else []), "display-message", "-p", "-t", pane,
             "#{session_name}:#{window_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return f"pane {pane}"
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else f"pane {pane}"


def _read_holder(path: Path) -> dict:
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # unreadable, or the holder is still writing it
    return holder if isinstance(holder, dict) else {}


def acquire_project_lock(conductor_dir: Path, command: str) -> bool:
    """Take the project lock for the rest of this process, or raise LockHeld.

    Re-acquiring a lock this process already holds is a no-op. Returns True
    only when this call took the lock, so a caller that found it already
    held (the orchestrator loop under `conductor run`) leaves it alone.
    """
    path = _lock_path(conductor_dir)
    if path in _held:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise LockHeld(path, _read_holder(path)) from None
    tmux = os.environ.get("TMUX", "")  # "<socket>,<server pid>,<session index>"
    holder = {
        "pid": os.getpid(),
        "command": command,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "tmux_socket": tmux.split(",")[0],
        "tmux_pane": os.environ.get("TMUX_PANE", "") if tmux else "",
    }
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(holder).encode("utf-8"))
    _held[path] = fd
    return True


def release_project_lock(conductor_dir: Path) -> None:
    """Release the project lock if this process holds it."""
    fd = _held.pop(_lock_path(conductor_dir), None)
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
