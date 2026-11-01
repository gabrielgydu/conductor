"""TmuxManager: spawn and manage processes in tmux windows."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from pathlib import Path

# Minimum number of processes expected in a healthy runner process tree
# (shell → python → claude = 3 processes)
MIN_HEALTHY_TREE_DEPTH = 3

# Seconds between pane checks while spawn_in_window_and_wait waits
_WAIT_POLL_SECS = 2.0


def check_pstree_depth(pid: int, depth: int = MIN_HEALTHY_TREE_DEPTH) -> bool:
    """Check if a process tree has at least `depth` processes (sync).

    Threads are not counted (-T): after its first quality gate the runner
    keeps an idle worker thread, which would make a runner with no Claude
    under it look healthy for the rest of its life.

    Returns True if pstree is not available (safe fallback).
    """
    try:
        r = subprocess.run(
            ["pstree", "-p", "-T", str(pid)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:  # psmisc not installed
        return True
    if r.returncode != 0:
        return True
    pids = re.findall(r"\((\d+)\)", r.stdout)
    return len(pids) >= depth


def session_target(session: str) -> str:
    """Exact-match ``-t`` target for *session*.

    A bare name falls back to prefix and fnmatch matching when no session has
    exactly that name, so ``conductor-foo`` would resolve to a live
    ``conductor-foobar``. For ``new-window -t`` append a colon
    (``session_target(s) + ":"``): without it tmux reads the target as a
    window, and the ``=`` only makes the window match exact.
    """
    return f"={session}"


def forwarded_vars() -> dict[str, str]:
    """The variables a window started by a running tmux server must still see.

    A command started by an already running tmux server gets the server's
    environment, not ours: without these, `CONDUCTOR_HOME=... conductor run`
    would read the state in one place and run from another. Values end up in
    tmux's argv, so only non-secret variables are forwarded.
    """
    names = sorted(k for k in os.environ if k.startswith("CONDUCTOR_")) + ["PATH", "PYTHONPATH"]
    return {name: os.environ[name] for name in names if name in os.environ}


def forwarded_env() -> list[str]:
    """`-e NAME=VALUE` options for tmux new-session/new-window."""
    opts: list[str] = []
    for name, value in forwarded_vars().items():
        opts += ["-e", f"{name}={value}"]
    return opts


def window_target(session: str, window: str) -> str:
    """Exact-match ``-t`` target for *window* in *session* (both names exact).

    ``=session:window`` alone still lets ``run0:api`` resolve to a sibling
    ``run0:api-exec`` when ``run0:api`` itself is gone.
    """
    return f"={session}:={window}"


def _record_exit_code(cmd: str, exit_file: Path) -> str:
    """Clear *exit_file* and wrap bash script *cmd* to write its exit code there."""
    exit_file.unlink(missing_ok=True)
    return f"{cmd}\n_rc=$?; echo $_rc > {shlex.quote(str(exit_file))}; exit $_rc"


def _read_exit_code(exit_file: Path) -> int | None:
    """Exit code a window wrapper wrote to *exit_file*; None if missing or garbled."""
    try:
        return int(exit_file.read_text().strip())
    except (OSError, ValueError):
        return None


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

    def _window(self, name: str) -> str:
        return window_target(self._session_name, name)

    def session_exists(self) -> bool:
        """Check if the tmux session exists."""
        r = self._run_tmux("has-session", "-t", session_target(self._session_name), check=False)
        return r.returncode == 0

    async def ensure_session(self) -> None:
        """Create tmux session if it doesn't exist.

        Windows inherit the session environment, so a new session gets this
        invocation's CONDUCTOR_*/PATH values and a reused one is updated to them.
        """
        if not self.session_exists():
            self._run_tmux(
                "new-session", "-d", "-s", self._session_name,
                "-x", "200", "-y", "50", *forwarded_env(),
            )
            return
        for name, value in forwarded_vars().items():
            self._run_tmux(
                "set-environment", "-t", session_target(self._session_name), name, value,
                check=False,
            )

    def _enable_logging(self, window_name: str, log_file: Path) -> None:
        """Tee all pane output to a log file via tmux pipe-pane."""
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._run_tmux(
            "pipe-pane", "-t", self._window(window_name),
            f"cat >> {shlex.quote(str(log_file))}",
            check=False,
        )

    def _set_remain_on_exit(self, window_name: str) -> None:
        """Keep window open after command exits."""
        self._run_tmux(
            "set-option", "-t", self._window(window_name),
            "remain-on-exit", "on",
            check=False,
        )

    def _new_window(
        self, name: str, script: str, *, cwd: str | None, detached: bool,
        log_file: Path | None,
    ) -> None:
        """Run *script* with bash in a fresh window *name*, replacing a stale one.

        The script is passed argv-style (``bash -c <script>``) so tmux execs
        bash directly. Handed over as a single string, tmux would run it with
        the server's ``default-shell`` instead (zsh, fish, ...), where an
        unquoted ``[1m]`` model ID is a failing glob and ``$PIPESTATUS``
        does not exist.
        """
        self._run_tmux("kill-window", "-t", self._window(name), check=False)

        args = ["new-window", "-t", f"{session_target(self._session_name)}:", "-n", name]
        if detached:
            args.insert(1, "-d")
        if cwd:
            args.extend(["-c", cwd])
        args.extend(["bash", "-c", script])
        self._run_tmux(*args)
        self._set_remain_on_exit(name)
        if log_file:
            self._enable_logging(name, log_file)

    async def spawn_in_window(self, name: str, cmd: str, *, cwd: str | None = None, detached: bool = False, log_file: Path | None = None) -> None:
        """Spawn bash script *cmd* in a new tmux window (non-blocking, fire-and-forget)."""
        self._new_window(name, cmd, cwd=cwd, detached=detached, log_file=log_file)

    async def spawn_in_window_and_wait(
        self,
        name: str,
        cmd: str,
        *,
        exit_file: Path | None = None,
        cwd: str | None = None,
        log_file: Path | None = None,
    ) -> int:
        """Spawn bash script *cmd* in a tmux window, wait for it to finish, return exit code.

        Polls the window until its command has exited; remain-on-exit keeps
        the dead pane and its exit status around. (``tmux wait-for`` is not
        used: its channels are global to the tmux server and latch a signal
        nobody waits for, so another project's window of the same name, or a
        window that finished while conductor was down, released the wait
        early.) Writes the exit code to *exit_file* if provided. A command
        that leaves no exit code behind (window killed, shell killed by a
        signal) counts as failed: 1.
        """
        wrapped = _record_exit_code(cmd, exit_file) if exit_file else cmd
        self._new_window(name, wrapped, cwd=cwd, detached=False, log_file=log_file)

        finished, status = self._pane_exit_status(name)
        while not finished:
            await asyncio.sleep(_WAIT_POLL_SECS)
            finished, status = self._pane_exit_status(name)

        if exit_file:
            code = _read_exit_code(exit_file)
            if code is not None:
                return code
        return status if status is not None else 1

    def _pane_exit_status(self, name: str) -> tuple[bool, int | None]:
        """(finished, exit status) of the command in window *name*.

        The status is None while the command runs, and also when it finished
        without a known status (window killed, or shell died from a signal).
        """
        r = self._run_tmux(
            "list-panes", "-t", self._window(name),
            "-F", "#{pane_dead} #{pane_dead_status}",
            check=False,
        )
        if r.returncode != 0:
            return True, None
        dead, _, status = (r.stdout.splitlines() or [""])[0].partition(" ")
        if dead != "1":
            return False, None
        return True, int(status) if status.isdigit() else None

    async def spawn_runner_in_window(
        self,
        name: str,
        cmd: str,
        *,
        exit_file: Path,
        fail_log: Path | None = None,
        cwd: str | None = None,
        log_file: Path | None = None,
    ) -> None:
        """Spawn runner bash script in tmux window (non-blocking).

        Writes the exit code to *exit_file* when done and, on a non-zero
        exit, the last 50 pane lines to *fail_log* if one is given.
        """
        capture = (
            f'if [ "$_rc" -ne 0 ]; then '
            f'tmux capture-pane -t "$TMUX_PANE" -p -S -50 > {shlex.quote(str(fail_log))} 2>/dev/null; '
            f'fi; '
            if fail_log else ""
        )
        wrapped = f'{cmd}\n_rc=$?; {capture}echo $_rc > {shlex.quote(str(exit_file))}'

        self._new_window(name, wrapped, cwd=cwd, detached=False, log_file=log_file)

    async def is_window_alive(self, name: str) -> bool:
        """Check if a tmux window exists."""
        r = self._run_tmux(
            "list-windows", "-t", session_target(self._session_name),
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
            "list-panes", "-t", self._window(name),
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
            "send-keys", "-t", self._window(name),
            keys, check=False,
        )

    async def kill_window(self, name: str) -> None:
        """Kill a tmux window."""
        self._run_tmux("kill-window", "-t", self._window(name), check=False)

    async def kill_session(self) -> None:
        """Kill the tmux session."""
        self._run_tmux("kill-session", "-t", session_target(self._session_name), check=False)

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
            "capture-pane", "-t", self._window(name),
            "-p", "-S", f"-{lines}",
            check=False,
        )
        return r.stdout if r.returncode == 0 else ""
