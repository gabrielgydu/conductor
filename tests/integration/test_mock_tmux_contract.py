"""MockTmux stays in step with the real TmuxManager interface.

Production code gets an autospec proxy of TmuxManager that delegates to
MockTmux (see tests/integration/conftest.py), so calls are checked against the
real signatures and new TmuxManager methods need a fake before tests run.
"""

from __future__ import annotations

import pytest

import conductor.core.tmux as tmux_module
from conductor.core.tmux import TmuxManager
from tests.integration.conftest import MockTmux, tmux_proxy


@pytest.mark.asyncio
async def test_calls_are_checked_against_real_signature(mock_tmux):
    tmux = tmux_module.TmuxManager(session_name="conductor-x")
    assert tmux._session_name == "conductor-x"

    # Real options the fake does not model are accepted ...
    rc = await tmux.spawn_in_window_and_wait("w", "true", cwd="/x", log_file=None)
    assert rc == 0
    # ... while anything the real TmuxManager would reject still fails.
    with pytest.raises(TypeError):
        await tmux.spawn_in_window_and_wait("w", "true", no_such_option=1)
    with pytest.raises(TypeError):
        tmux_module.TmuxManager(name="conductor-x")


def test_every_public_method_needs_a_fake(monkeypatch):
    async def resize_window(self, name: str) -> None: ...

    monkeypatch.setattr(TmuxManager, "resize_window", resize_window, raising=False)
    with pytest.raises(AssertionError, match="no fake for TmuxManager.resize_window"):
        tmux_proxy(MockTmux())


def test_private_helpers_refuse_to_run_tmux(mock_tmux):
    tmux = tmux_module.TmuxManager(session_name="conductor-x")
    with pytest.raises(AssertionError, match="real tmux binary"):
        tmux._run_tmux("list-sessions")
