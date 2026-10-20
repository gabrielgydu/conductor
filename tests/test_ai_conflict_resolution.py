"""Tests for resolve_conflicts_with_claude."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch

from conductor.integration.merge import resolve_conflicts_with_claude
from conductor.core.claude import ClaudeResult


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", message).returncode == 0


def _conflicted_repo(tmp_path: Path, base: dict, ours: dict, theirs: dict) -> Path:
    """A repo in the middle of merging branch theirs into ours, with conflicts.

    Each dict maps a path to its content; None deletes the file on that side.
    """
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "ours", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")

    def apply(files: dict) -> None:
        for path, content in files.items():
            target = repo / path
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

    apply(base)
    _commit_all(repo, "base")
    _git(repo, "checkout", "-q", "-b", "theirs")
    apply(theirs)
    _commit_all(repo, "theirs")
    _git(repo, "checkout", "-q", "ours")
    apply(ours)
    _commit_all(repo, "ours")
    assert _git(repo, "merge", "--no-edit", "theirs").returncode != 0
    return repo


@pytest.fixture
def content_conflict(tmp_path) -> Path:
    """src/foo.py: both sides rewrote line 2."""
    return _conflicted_repo(
        tmp_path,
        base={"src/foo.py": "a\nb\nc\n"},
        ours={"src/foo.py": "a\nOURS\nc\n"},
        theirs={"src/foo.py": "a\nTHEIRS\nc\n"},
    )


def _claude_writes(repo: Path, files: dict, exit_code: int = 0):
    """run_claude stand-in that leaves *files* (None deletes) in the repo."""

    async def fake(prompt, **kwargs):
        for path, content in files.items():
            if content is None:
                (repo / path).unlink()
            else:
                (repo / path).write_text(content)
        return _make_claude_result(exit_code)

    return fake


def _make_claude_result(exit_code=0):
    return ClaudeResult(
        exit_code=exit_code,
        output="",
        tokens_used=None,
        cost=None,
        duration=0.1,
    )


@pytest.mark.asyncio
async def test_empty_conflicting_files(tmp_path):
    with patch(
        "conductor.integration.merge.run_claude", new_callable=AsyncMock
    ) as mock_claude:
        result = await resolve_conflicts_with_claude([], "some description", tmp_path)
    assert result is True
    mock_claude.assert_not_called()


@pytest.mark.asyncio
async def test_successful_resolution(content_conflict):
    fake = _claude_writes(content_conflict, {"src/foo.py": "a\nOURS\nTHEIRS\nc\n"})
    with patch("conductor.integration.merge.run_claude", side_effect=fake):
        result = await resolve_conflicts_with_claude(
            ["src/foo.py"], "run description", content_conflict
        )

    assert result is True
    # The resolution is staged, ready for the merge commit.
    assert _git(content_conflict, "ls-files", "--unmerged").stdout == ""
    assert _git(content_conflict, "diff", "--cached", "--name-only").stdout == "src/foo.py\n"


@pytest.mark.asyncio
async def test_failed_resolution_markers_remain(content_conflict):
    # Claude runs but doesn't fix the markers
    with patch(
        "conductor.integration.merge.run_claude",
        new_callable=AsyncMock,
        return_value=_make_claude_result(0),
    ):
        result = await resolve_conflicts_with_claude(
            ["src/foo.py"], "run description", content_conflict
        )

    assert result is False


@pytest.mark.asyncio
async def test_markers_left_after_removing_only_the_opening_one(content_conflict):
    """'=======' and '>>>>>>> theirs' at a line start are markers too."""
    leftover = "a\nOURS\n=======\nTHEIRS\n>>>>>>> theirs\nc\n"
    fake = _claude_writes(content_conflict, {"src/foo.py": leftover})
    with patch("conductor.integration.merge.run_claude", side_effect=fake):
        result = await resolve_conflicts_with_claude(["src/foo.py"], "desc", content_conflict)

    assert result is False


@pytest.mark.asyncio
async def test_marker_lines_already_in_the_file_do_not_count(tmp_path):
    """A fixture that legitimately contains marker lines can still be resolved."""
    fixture = "<<<<<<< HEAD\nsample\n=======\n>>>>>>> branch\n"
    repo = _conflicted_repo(
        tmp_path,
        base={"fixture.txt": fixture + "tail\n"},
        ours={"fixture.txt": fixture + "OURS\n"},
        theirs={"fixture.txt": fixture + "THEIRS\n"},
    )
    fake = _claude_writes(repo, {"fixture.txt": fixture + "OURS\nTHEIRS\n"})
    with patch("conductor.integration.merge.run_claude", side_effect=fake):
        result = await resolve_conflicts_with_claude(["fixture.txt"], "desc", repo)

    assert result is True


@pytest.mark.asyncio
async def test_deleting_a_modify_delete_file_resolves_it(tmp_path):
    repo = _conflicted_repo(
        tmp_path,
        base={"keep.txt": "k\n", "old.txt": "old\n"},
        ours={"old.txt": "old, edited\n"},
        theirs={"old.txt": None},
    )
    fake = _claude_writes(repo, {"old.txt": None})
    with patch("conductor.integration.merge.run_claude", side_effect=fake) as claude:
        result = await resolve_conflicts_with_claude(["old.txt"], "desc", repo)

    assert result is True
    assert "old, edited" in claude.await_args.args[0]
    assert _git(repo, "diff", "--cached", "--name-status").stdout == "D\told.txt\n"


@pytest.mark.asyncio
async def test_claude_error_returns_false(tmp_path):
    conflict_file = tmp_path / "foo.py"
    conflict_file.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")

    with patch(
        "conductor.integration.merge.run_claude",
        new_callable=AsyncMock,
        return_value=_make_claude_result(1),
    ):
        result = await resolve_conflicts_with_claude(
            ["foo.py"], "run description", tmp_path
        )

    assert result is False


@pytest.mark.asyncio
async def test_prompt_includes_all_files(tmp_path):
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.py"
    file_a.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\n")
    file_b.write_text("<<<<<<< HEAD\nc\n=======\nd\n>>>>>>> br\n")

    captured = {}

    async def fake_claude(prompt, *, model, max_turns, cwd):
        captured["prompt"] = prompt
        # Resolve files so we pass marker check
        file_a.write_text("resolved a")
        file_b.write_text("resolved b")
        return _make_claude_result(0)

    with patch("conductor.integration.merge.run_claude", side_effect=fake_claude):
        await resolve_conflicts_with_claude(["a.py", "b.py"], "description", tmp_path)

    assert "a.py" in captured["prompt"]
    assert "b.py" in captured["prompt"]


@pytest.mark.asyncio
async def test_prompt_includes_run_description(tmp_path):
    conflict_file = tmp_path / "foo.py"
    conflict_file.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\n")

    captured = {}

    async def fake_claude(prompt, *, model, max_turns, cwd):
        captured["prompt"] = prompt
        conflict_file.write_text("resolved")
        return _make_claude_result(0)

    with patch("conductor.integration.merge.run_claude", side_effect=fake_claude):
        await resolve_conflicts_with_claude(
            ["foo.py"], "MY UNIQUE DESCRIPTION", tmp_path
        )

    assert "MY UNIQUE DESCRIPTION" in captured["prompt"]


@pytest.mark.asyncio
async def test_uses_sonnet_model(tmp_path):
    conflict_file = tmp_path / "foo.py"
    conflict_file.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\n")

    captured = {}

    async def fake_claude(prompt, *, model, max_turns, cwd):
        captured["model"] = model
        conflict_file.write_text("resolved")
        return _make_claude_result(0)

    with patch("conductor.integration.merge.run_claude", side_effect=fake_claude):
        await resolve_conflicts_with_claude(["foo.py"], "desc", tmp_path)

    assert captured["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_uses_50_max_turns(tmp_path):
    conflict_file = tmp_path / "foo.py"
    conflict_file.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\n")

    captured = {}

    async def fake_claude(prompt, *, model, max_turns, cwd):
        captured["max_turns"] = max_turns
        conflict_file.write_text("resolved")
        return _make_claude_result(0)

    with patch("conductor.integration.merge.run_claude", side_effect=fake_claude):
        await resolve_conflicts_with_claude(["foo.py"], "desc", tmp_path)

    assert captured["max_turns"] == 50
