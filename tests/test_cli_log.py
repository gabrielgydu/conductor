"""Tests for `conductor log`."""

from __future__ import annotations

import pytest

from conductor.core.storage import StorageResolver
from tests.cli_support import NAME, repo_fixture, run_main, seed_state  # noqa: F401


@pytest.fixture
def log_lines(repo):
    seed_state(repo)
    path = StorageResolver(repo).conductor_log(NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {i}\n" for i in range(1, 6)))
    return repo


@pytest.mark.parametrize(("tail", "expected"), [("2", "line 4\nline 5\n"), ("0", ""), ("9", None)])
def test_tail_shows_the_last_n_lines(log_lines, tail, expected):
    out, _, code = run_main(["log", "--name", NAME, "--project-dir", str(log_lines), "--tail", tail])

    assert code == 0
    assert out == (expected if expected is not None else "".join(f"line {i}\n" for i in range(1, 6)))


def test_negative_tail_is_rejected(log_lines):
    out, err, code = run_main(["log", "--name", NAME, "--project-dir", str(log_lines), "--tail", "-2"])

    assert code == 2
    assert "must be 0 or more" in err
    assert out == ""
