"""Unit tests for orchestrator helper functions.

Covers:
- compute_progress_hash (last 20 lines only)
- pre_reset_speccer_status (EXPLORING->INIT, SPECCING->NEEDS_INPUT)
- speccer_exit_code_handler (exit 0, non-zero recoverable, non-zero unrecoverable, no exit file)
- Post-run learnings parsing (<<<FILE: path>>>...<<<END>>> format)
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conductor.core.enums import RunStatus, StageStatus
from conductor.core.models import ConductorState, RunState, StageState
import conductor.core.orchestrator as orch
from conductor.core.orchestrator import (
    compute_progress_hash,
    pre_reset_speccer_status,
    speccer_exit_code_handler,
    sync_speccer_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state_with_worktree(tmp_path: Path) -> tuple[ConductorState, Path]:
    """Create a state with a stage whose worktree is set to tmp_path."""
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    stage = StageState(
        name="stage-0", spec_mode="full", status=StageStatus.SPEC_RUNNING
    )
    stage.worktree = str(wt)
    run = RunState(
        index=0,
        name="run-0",
        description="test",
        stages=[stage],
        status=RunStatus.ACTIVE,
    )
    state = ConductorState(project_name="proj", base_branch="main", runs=[run])
    return state, wt


def _write_progress(wt: Path, fname: str, status: str) -> Path:
    spec_dir = wt / "docs" / fname / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    pf = spec_dir / "PROGRESS.md"
    pf.write_text(f"STATUS: {status}\nMODE: full\n", encoding="utf-8")
    return pf


def _make_storage() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# compute_progress_hash — hashes last 20 lines only
# ---------------------------------------------------------------------------


def test_compute_progress_hash_uses_last_20_lines(tmp_path):
    """Hash must be derived from the last 20 lines, NOT the whole file."""
    state, wt = _make_state_with_worktree(tmp_path)
    stage = state.runs[0].stages[0]
    stage.status = StageStatus.EXECUTING

    # Create activity log with 40 distinct lines
    fname = "run-0"
    activity_log = tmp_path / f"activity-{fname}-001.log"
    first_20 = [f"line-{i:03d}" for i in range(20)]
    last_20 = [f"line-{i:03d}" for i in range(20, 40)]
    activity_log.write_text("\n".join(first_20 + last_20), encoding="utf-8")

    import glob as _glob

    with patch("glob.glob", return_value=[str(activity_log)]):
        h = compute_progress_hash(state, 0, 0, _make_storage())

    expected = hashlib.md5("\n".join(last_20).encode()).hexdigest()
    assert h == expected


def test_compute_progress_hash_different_first_same_last_equal(tmp_path):
    """Two logs with identical last 20 lines but different first lines produce the same hash."""
    state, wt = _make_state_with_worktree(tmp_path)
    stage = state.runs[0].stages[0]
    stage.status = StageStatus.EXECUTING

    fname = "run-0"
    log_a = tmp_path / f"activity-{fname}-a.log"
    log_b = tmp_path / f"activity-{fname}-b.log"

    tail = [f"tail-line-{i}" for i in range(20)]
    log_a.write_text("\n".join(["different-prefix"] + tail), encoding="utf-8")
    log_b.write_text("\n".join(tail), encoding="utf-8")

    import glob as _glob

    with patch("glob.glob", return_value=[str(log_a)]):
        ha = compute_progress_hash(state, 0, 0, _make_storage())
    with patch("glob.glob", return_value=[str(log_b)]):
        hb = compute_progress_hash(state, 0, 0, _make_storage())

    assert ha == hb


def test_compute_progress_hash_no_activity_log_returns_none(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    state.runs[0].stages[0].status = StageStatus.EXECUTING

    with patch("glob.glob", return_value=[]):
        result = compute_progress_hash(state, 0, 0, _make_storage())

    assert result is None


def test_compute_progress_hash_spec_running_uses_progress_md(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    stage = state.runs[0].stages[0]
    stage.status = StageStatus.SPEC_RUNNING
    fname = "run-0"
    pf = _write_progress(wt, fname, "INIT")

    result = compute_progress_hash(state, 0, 0, _make_storage())

    expected = hashlib.md5(pf.read_bytes()).hexdigest()
    assert result == expected


# ---------------------------------------------------------------------------
# pre_reset_speccer_status
# ---------------------------------------------------------------------------


def test_pre_reset_exploring_becomes_init(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "EXPLORING")

    pre_reset_speccer_status(state, 0, 0, _make_storage())

    pf = wt / "docs" / "run-0" / "spec" / "PROGRESS.md"
    assert "STATUS: INIT" in pf.read_text(encoding="utf-8")


def test_pre_reset_speccing_becomes_needs_input(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "SPECCING")

    pre_reset_speccer_status(state, 0, 0, _make_storage())

    pf = wt / "docs" / "run-0" / "spec" / "PROGRESS.md"
    assert "STATUS: NEEDS_INPUT" in pf.read_text(encoding="utf-8")


def test_pre_reset_other_statuses_unchanged(tmp_path):
    for i, status in enumerate(["COMPLETE", "GENERATED", "NEEDS_INPUT", "INIT"]):
        sub = tmp_path / f"iter-{i}"
        sub.mkdir()
        state, wt = _make_state_with_worktree(sub)
        _write_progress(wt, "run-0", status)

        pre_reset_speccer_status(state, 0, 0, _make_storage())

        pf = wt / "docs" / "run-0" / "spec" / "PROGRESS.md"
        assert f"STATUS: {status}" in pf.read_text(encoding="utf-8"), (
            f"Status {status} was unexpectedly mutated"
        )


def test_pre_reset_no_progress_file_is_noop(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    # No PROGRESS.md written
    pre_reset_speccer_status(state, 0, 0, _make_storage())
    # Should not raise


# ---------------------------------------------------------------------------
# speccer_exit_code_handler
# ---------------------------------------------------------------------------


def test_exit_handler_exit_0_calls_sync(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "COMPLETE")

    fname = "run-0"
    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.write_text("0")

    try:
        with patch.object(orch, "sync_speccer_status") as mock_sync:
            speccer_exit_code_handler(state, 0, 0, fname, _make_storage())
        mock_sync.assert_called_once()
    finally:
        exit_file.unlink(missing_ok=True)


def test_exit_handler_nonzero_recoverable_calls_sync(tmp_path):
    """Non-zero exit + PROGRESS shows NEEDS_INPUT -> sync (not failed)."""
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "NEEDS_INPUT")

    fname = "run-0"
    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.write_text("1")

    try:
        with patch.object(orch, "sync_speccer_status") as mock_sync:
            speccer_exit_code_handler(state, 0, 0, fname, _make_storage())
        mock_sync.assert_called_once()
        assert state.runs[0].stages[0].status != StageStatus.FAILED
    finally:
        exit_file.unlink(missing_ok=True)


def test_exit_handler_nonzero_nonrecoverable_sets_failed(tmp_path):
    """Non-zero exit + PROGRESS shows INIT -> stage set to FAILED."""
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "INIT")

    fname = "run-0"
    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.write_text("2")

    try:
        speccer_exit_code_handler(state, 0, 0, fname, _make_storage())
        assert state.runs[0].stages[0].status == StageStatus.FAILED
    finally:
        exit_file.unlink(missing_ok=True)


def test_exit_handler_no_exit_file_recoverable_status_syncs(tmp_path):
    """No exit file + PROGRESS shows GENERATED -> sync."""
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "GENERATED")

    fname = "run-0"
    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.unlink(missing_ok=True)

    with patch.object(orch, "sync_speccer_status") as mock_sync:
        speccer_exit_code_handler(state, 0, 0, fname, _make_storage())

    mock_sync.assert_called_once()


def test_exit_handler_no_exit_file_no_progress_sets_failed(tmp_path):
    """No exit file + no PROGRESS.md -> FAILED."""
    stage = StageState(name="stage-0", spec_mode="full")
    stage.worktree = str(tmp_path)
    run = RunState(
        index=0, name="run-0", description="t", stages=[stage], status=RunStatus.ACTIVE
    )
    state = ConductorState(project_name="proj", base_branch="main", runs=[run])

    fname = "run-0"
    exit_file = Path(f"/tmp/conductor-speccer-exit-{fname}")
    exit_file.unlink(missing_ok=True)

    speccer_exit_code_handler(state, 0, 0, fname, _make_storage())

    assert state.runs[0].stages[0].status == StageStatus.FAILED


# ---------------------------------------------------------------------------
# Post-run learnings parsing — <<<FILE: path>>>...<<<END>>> format
# ---------------------------------------------------------------------------


def _parse_learnings_blocks(response_text: str, project_dir: Path) -> dict[str, str]:
    """Replicate the regex logic from _review_learnings for testing."""
    updates: dict[str, str] = {}
    for file_path in re.findall(r"<<<FILE:\s*([^>]+)>>>", response_text):
        file_path = file_path.strip()
        full_path = project_dir / file_path
        if not full_path.exists():
            continue
        content_match = re.search(
            rf"<<<FILE:\s*{re.escape(file_path)}\s*>>>(.*?)<<<END>>>",
            response_text,
            re.DOTALL,
        )
        if content_match:
            addition = content_match.group(1).strip()
            if addition:
                updates[file_path] = addition
    return updates


def test_learnings_parsing_single_file(tmp_path):
    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# Existing content\n", encoding="utf-8")

    response = "<<<FILE: .claude/CLAUDE.md>>>\nNew learning content here\n<<<END>>>\n"

    updates = _parse_learnings_blocks(response, tmp_path)

    assert ".claude/CLAUDE.md" in updates
    assert "New learning content here" in updates[".claude/CLAUDE.md"]


def test_learnings_parsing_multiple_files(tmp_path):
    f1 = tmp_path / "a.md"
    f2 = tmp_path / "b.md"
    f1.write_text("existing a\n")
    f2.write_text("existing b\n")

    response = (
        "<<<FILE: a.md>>>\nLearning A\n<<<END>>>\n"
        "Some other text\n"
        "<<<FILE: b.md>>>\nLearning B\n<<<END>>>\n"
    )

    updates = _parse_learnings_blocks(response, tmp_path)

    assert "a.md" in updates
    assert "b.md" in updates
    assert updates["a.md"] == "Learning A"
    assert updates["b.md"] == "Learning B"


def test_learnings_parsing_file_not_on_disk_skipped(tmp_path):
    response = "<<<FILE: nonexistent.md>>>\nsome content\n<<<END>>>\n"

    updates = _parse_learnings_blocks(response, tmp_path)

    assert len(updates) == 0


def test_learnings_parsing_no_changes_marker(tmp_path):
    response = "<<<NO_CHANGES>>>"

    assert "<<<NO_CHANGES>>>" in response
    updates = _parse_learnings_blocks(response, tmp_path)
    assert len(updates) == 0


def test_learnings_parsing_empty_block_skipped(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("existing\n")

    response = "<<<FILE: empty.md>>>\n\n<<<END>>>\n"
    updates = _parse_learnings_blocks(response, tmp_path)

    assert "empty.md" not in updates


def test_learnings_parsing_multiline_content(tmp_path):
    f = tmp_path / "multi.md"
    f.write_text("existing\n")

    response = "<<<FILE: multi.md>>>\nLine 1\nLine 2\nLine 3\n<<<END>>>\n"

    updates = _parse_learnings_blocks(response, tmp_path)

    assert "multi.md" in updates
    content = updates["multi.md"]
    assert "Line 1" in content
    assert "Line 2" in content
    assert "Line 3" in content
