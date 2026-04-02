"""File-based steering inbox — polls a directory for .msg files and forwards them to a steerable session."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from runner.steerable import SteerableSession

logger = logging.getLogger("runner.steer_inbox")


async def poll_steer_inbox(
    inbox: Path,
    session: SteerableSession,
    interval: float = 2.0,
) -> None:
    """Poll inbox directory for .msg files, send contents to session, delete after sending.

    Clears stale .msg files on startup. Runs until cancelled.
    Handles FileNotFoundError gracefully if inbox dir does not exist yet.
    Files are sorted by name so timestamp-prefixed filenames are processed in order.
    """
    # Clear any stale messages that pre-date this session
    try:
        for stale in sorted(inbox.glob("*.msg")):
            stale.unlink(missing_ok=True)
    except FileNotFoundError:
        pass

    while True:
        await asyncio.sleep(interval)
        try:
            msg_files = sorted(inbox.glob("*.msg"))
        except FileNotFoundError:
            continue

        for msg_file in msg_files:
            try:
                content = msg_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Raced with another consumer — skip
                continue

            try:
                await session.send(content)
                logger.info("Steered session from inbox file: %s", msg_file.name)
            except RuntimeError as exc:
                logger.warning("Could not send steering message: %s", exc)
                # Session is closed — no point continuing
                return

            msg_file.unlink(missing_ok=True)
