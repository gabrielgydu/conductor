"""Tests for resolve_conflicts_with_claude."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from conductor.integration.merge import resolve_conflicts_with_claude
from conductor.core.claude import ClaudeResult


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
async def test_successful_resolution(tmp_path):
    conflict_file = tmp_path / "src" / "foo.py"
    conflict_file.parent.mkdir(parents=True)
    conflict_file.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")

    async def fake_claude(prompt, *, model, max_turns, cwd):
        # Simulate Claude resolving the conflict by writing clean content
        conflict_file.write_text("resolved content\n")
        return _make_claude_result(exit_code=0)

    with patch("conductor.integration.merge.run_claude", side_effect=fake_claude):
        result = await resolve_conflicts_with_claude(
            ["src/foo.py"], "run description", tmp_path
        )

    assert result is True


@pytest.mark.asyncio
async def test_failed_resolution_markers_remain(tmp_path):
    conflict_file = tmp_path / "src" / "foo.py"
    conflict_file.parent.mkdir(parents=True)
    conflict_content = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    conflict_file.write_text(conflict_content)

    # Claude runs but doesn't fix the markers
    with patch(
        "conductor.integration.merge.run_claude",
        new_callable=AsyncMock,
        return_value=_make_claude_result(0),
    ):
        result = await resolve_conflicts_with_claude(
            ["src/foo.py"], "run description", tmp_path
        )

    assert result is False


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
async def test_uses_opus_model(tmp_path):
    conflict_file = tmp_path / "foo.py"
    conflict_file.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> br\n")

    captured = {}

    async def fake_claude(prompt, *, model, max_turns, cwd):
        captured["model"] = model
        conflict_file.write_text("resolved")
        return _make_claude_result(0)

    with patch("conductor.integration.merge.run_claude", side_effect=fake_claude):
        await resolve_conflicts_with_claude(["foo.py"], "desc", tmp_path)

    assert captured["model"] == "claude-opus-4-6"


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
