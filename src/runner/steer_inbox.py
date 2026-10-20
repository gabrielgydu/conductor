"""File-based steering inbox — polls a directory for .msg files and forwards them to a steerable session."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from runner.steerable import SteerableSession

logger = logging.getLogger("runner.steer_inbox")


def clear_steer_inbox(inbox: Path) -> None:
    """Delete messages left from before this runner started.

    Called once per runner start, not per session: the orchestrator may write
    a message while no session is live (between iterations, during a commit
    or the quality gate), and the next session must still receive it.
    """
    try:
        for stale in inbox.glob("*.msg"):
            stale.unlink(missing_ok=True)
    except OSError:
        pass


async def poll_steer_inbox(
    inbox: Path,
    session: SteerableSession,
    interval: float = 2.0,
) -> None:
    """Poll inbox directory for .msg files, send contents to session, delete after sending.

    Runs until cancelled. Messages already waiting when it starts are
    delivered too. Handles a missing inbox dir and unreadable entries.
    Files are sorted by name so timestamp-prefixed filenames are processed in order.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            msg_files = sorted(inbox.glob("*.msg"))
        except OSError:
            continue

        for msg_file in msg_files:
            try:
                content = msg_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Raced with another consumer — skip
                continue
            except OSError as exc:
                logger.warning("Skipping unreadable steering message %s: %s", msg_file.name, exc)
                continue

            try:
                await session.send(content)
                logger.info("Steered session from inbox file: %s", msg_file.name)
            except RuntimeError as exc:
                logger.warning("Could not send steering message: %s", exc)
                # Session is closed — no point continuing
                return

            msg_file.unlink(missing_ok=True)
