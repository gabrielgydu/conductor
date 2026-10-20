"""runner.steer_inbox: deliver steering written between sessions; survive bad entries."""

from __future__ import annotations

import asyncio

from runner.steer_inbox import clear_steer_inbox, poll_steer_inbox


class _Session:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


async def _poll_briefly(inbox, session) -> None:
    task = asyncio.ensure_future(poll_steer_inbox(inbox, session, interval=0.01))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not list(inbox.glob("*.msg")) or session.sent:
            break
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_message_written_between_sessions_is_delivered(tmp_path):
    # The orchestrator wrote this while no session was live (e.g. during the
    # commit after the previous iteration); the next session's poller used to
    # delete it unread.
    inbox = tmp_path / "steer_inbox"
    inbox.mkdir()
    (inbox / "1.000000.msg").write_text("focus on the failing test")
    session = _Session()

    await _poll_briefly(inbox, session)

    assert session.sent == ["focus on the failing test"]
    assert not list(inbox.glob("*.msg"))


async def test_unreadable_entry_does_not_stop_the_poller(tmp_path):
    inbox = tmp_path / "steer_inbox"
    inbox.mkdir()
    (inbox / "1.msg").mkdir()  # sorts first; reading it raises IsADirectoryError
    (inbox / "2.msg").write_text("still delivered")
    session = _Session()

    await _poll_briefly(inbox, session)

    assert session.sent == ["still delivered"]


def test_clear_removes_stale_messages_only(tmp_path):
    inbox = tmp_path / "steer_inbox"
    inbox.mkdir()
    (inbox / "old.msg").write_text("stale")
    (inbox / "new.msg.tmp").write_text("being written")

    clear_steer_inbox(inbox)
    clear_steer_inbox(tmp_path / "missing")  # no inbox yet: no error

    assert sorted(p.name for p in inbox.iterdir()) == ["new.msg.tmp"]
