"""Unit tests for orchestrator helper functions.

Covers:
- compute_progress_hash (last 20 lines only)
- pre_reset_speccer_status (EXPLORING->INIT, SPECCING->NEEDS_INPUT)
- speccer_exit_code_handler (exit 0, non-zero recoverable, non-zero unrecoverable, no exit file)
- Post-run learnings parsing (<<<FILE: path>>>...<<<END>>> format)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
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


def _write_activity_log(wt: Path, fname: str, lines: list[str]) -> Path:
    """Write activity.log where the runner writes it.

    The orchestrator launches the runner with --storage-dir {wt}/docs/{fname}
    and the runner appends to <storage-dir>/activity.log.
    """
    activity_log = wt / "docs" / fname / "activity.log"
    activity_log.parent.mkdir(parents=True, exist_ok=True)
    activity_log.write_text("\n".join(lines), encoding="utf-8")
    return activity_log


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
    first_20 = [f"line-{i:03d}" for i in range(20)]
    last_20 = [f"line-{i:03d}" for i in range(20, 40)]
    _write_activity_log(wt, fname, first_20 + last_20)

    h = compute_progress_hash(state, 0, 0, _make_storage())

    expected = hashlib.md5("\n".join(last_20).encode()).hexdigest()
    assert h == expected


def test_compute_progress_hash_different_first_same_last_equal(tmp_path):
    """Two logs with identical last 20 lines but different first lines produce the same hash."""
    state, wt = _make_state_with_worktree(tmp_path)
    stage = state.runs[0].stages[0]
    stage.status = StageStatus.EXECUTING

    fname = "run-0"
    tail = [f"tail-line-{i}" for i in range(20)]

    _write_activity_log(wt, fname, ["different-prefix"] + tail)
    ha = compute_progress_hash(state, 0, 0, _make_storage())
    _write_activity_log(wt, fname, tail)
    hb = compute_progress_hash(state, 0, 0, _make_storage())

    assert ha is not None
    assert ha == hb


def test_compute_progress_hash_no_activity_log_returns_none(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    state.runs[0].stages[0].status = StageStatus.EXECUTING

    assert not (wt / "docs" / "run-0" / "activity.log").exists()
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


def _ipc_storage(tmp_path: Path) -> SimpleNamespace:
    """Storage stub whose conductor_dir is a real directory under tmp_path."""
    return SimpleNamespace(conductor_dir=lambda name: tmp_path / "conductor-home" / name)


def _write_speccer_exit(storage, fname: str, code: str) -> Path:
    exit_file = orch._speccer_exit_file(storage, "proj", fname)
    exit_file.write_text(code)
    return exit_file


def test_exit_handler_exit_0_calls_sync(tmp_path):
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "COMPLETE")
    storage = _ipc_storage(tmp_path)
    _write_speccer_exit(storage, "run-0", "0")

    with patch.object(orch, "sync_speccer_status") as mock_sync:
        speccer_exit_code_handler(state, 0, 0, "run-0", storage)
    mock_sync.assert_called_once()


def test_exit_handler_nonzero_recoverable_calls_sync(tmp_path):
    """Non-zero exit + PROGRESS shows NEEDS_INPUT -> sync (not failed)."""
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "NEEDS_INPUT")
    storage = _ipc_storage(tmp_path)
    _write_speccer_exit(storage, "run-0", "1")

    with patch.object(orch, "sync_speccer_status") as mock_sync:
        speccer_exit_code_handler(state, 0, 0, "run-0", storage)
    mock_sync.assert_called_once()
    assert state.runs[0].stages[0].status != StageStatus.FAILED


def test_exit_handler_nonzero_nonrecoverable_sets_failed(tmp_path):
    """Non-zero exit + PROGRESS shows INIT -> stage set to FAILED."""
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "INIT")
    storage = _ipc_storage(tmp_path)
    _write_speccer_exit(storage, "run-0", "2")

    speccer_exit_code_handler(state, 0, 0, "run-0", storage)
    assert state.runs[0].stages[0].status == StageStatus.FAILED


def test_exit_handler_no_exit_file_recoverable_status_syncs(tmp_path):
    """No exit file + PROGRESS shows GENERATED -> sync."""
    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "GENERATED")

    with patch.object(orch, "sync_speccer_status") as mock_sync:
        speccer_exit_code_handler(state, 0, 0, "run-0", _ipc_storage(tmp_path))

    mock_sync.assert_called_once()


def test_exit_handler_no_exit_file_no_progress_sets_failed(tmp_path):
    """No exit file + no PROGRESS.md -> FAILED."""
    stage = StageState(name="stage-0", spec_mode="full")
    stage.worktree = str(tmp_path)
    run = RunState(
        index=0, name="run-0", description="t", stages=[stage], status=RunStatus.ACTIVE
    )
    state = ConductorState(project_name="proj", base_branch="main", runs=[run])

    speccer_exit_code_handler(state, 0, 0, "run-0", _ipc_storage(tmp_path))

    assert state.runs[0].stages[0].status == StageStatus.FAILED


# ---------------------------------------------------------------------------
# Exit/fail files are per project, not global /tmp names
# ---------------------------------------------------------------------------


def test_exit_files_of_two_projects_do_not_collide(tmp_path):
    """Project B must not pick up project A's result for a same-named run."""
    storage_a = _ipc_storage(tmp_path / "repo-a")
    storage_b = _ipc_storage(tmp_path / "repo-b")
    _write_speccer_exit(storage_a, "run-0", "0")

    state, wt = _make_state_with_worktree(tmp_path)
    _write_progress(wt, "run-0", "SPECCING")  # B's speccer is still mid-run
    speccer_exit_code_handler(state, 0, 0, "run-0", storage_b)

    assert state.runs[0].stages[0].status == StageStatus.FAILED
    for path in (
        orch._runner_exit_file(storage_b, "proj", "run-0"),
        orch._runner_fail_log(storage_b, "proj", "run-0"),
        orch._speccer_exit_file(storage_b, "proj", "run-0"),
    ):
        assert path.is_relative_to(tmp_path / "repo-b"), path


def test_runner_fail_log_matches_tmux_wrapper_naming(tmp_path):
    """spawn_runner_in_window writes the fail log next to the exit file it is given."""
    storage = _ipc_storage(tmp_path)
    exit_file = orch._runner_exit_file(storage, "proj", "run-0")
    derived = str(exit_file).replace("conductor-exit-", "conductor-fail-") + ".log"
    assert Path(derived) == orch._runner_fail_log(storage, "proj", "run-0")


# ---------------------------------------------------------------------------
# Post-run learnings parsing — <<<FILE: path>>>...<<<END>>> format
# ---------------------------------------------------------------------------


def test_learnings_parsing_single_file():
    response = "<<<FILE: .claude/CLAUDE.md>>>\nNew learning content here\n<<<END>>>\n"

    assert orch._parse_learning_blocks(response) == {
        ".claude/CLAUDE.md": "New learning content here"
    }


def test_learnings_parsing_multiple_files():
    response = (
        "<<<FILE: a.md>>>\nLearning A\n<<<END>>>\n"
        "Some other text\n"
        "<<<FILE: b.md>>>\nLearning B\n<<<END>>>\n"
    )

    assert orch._parse_learning_blocks(response) == {"a.md": "Learning A", "b.md": "Learning B"}


def test_learnings_parsing_merges_repeated_blocks():
    response = (
        "<<<FILE: CLAUDE.md>>>\n- first\n<<<END>>>\n"
        "<<<FILE: CLAUDE.md>>>\n- second\n<<<END>>>\n"
    )

    assert orch._parse_learning_blocks(response) == {"CLAUDE.md": "- first\n- second"}


def test_learnings_parsing_no_changes_and_empty_blocks():
    assert orch._parse_learning_blocks("<<<NO_CHANGES>>>") == {}
    assert orch._parse_learning_blocks("<<<FILE: empty.md>>>\n\n<<<END>>>\n") == {}


def test_learnings_extend_the_existing_section_without_repeating_lines():
    existing = "# Project\n\n## Conductor Learnings\n\n- rule A\n\n## Other\n\ntext\n"

    updated = orch._with_learnings(existing, "## Conductor Learnings\n- rule A\n- rule B")

    assert updated.count("## Conductor Learnings") == 1
    assert updated.count("- rule A") == 1
    assert updated.index("- rule B") < updated.index("## Other")


def test_exit_file_poll_targets_tmux_windows_exactly(tmp_path, monkeypatch):
    """A prefix target would let run0:api resolve to its sibling run0:api-exec."""
    state, _ = _make_state_with_worktree(tmp_path)
    state.runs[0].stages[0].status = StageStatus.EXECUTING
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        out = "run0:stage-0-exec\n" if cmd[1] == "list-windows" else "12345\n"
        return SimpleNamespace(returncode=0, stdout=out)

    monkeypatch.setattr(orch.subprocess, "run", fake_run)
    monkeypatch.setattr(orch._tmux_module, "check_pstree_depth", lambda pid: True)

    assert not orch._any_runner_exit_file_ready(state, _ipc_storage(tmp_path), "conductor-proj")

    targets = [cmd[cmd.index("-t") + 1] for cmd in calls]
    assert targets == ["=conductor-proj", "=conductor-proj:=run0:stage-0-exec"]


# ---------------------------------------------------------------------------
# Single-run push and PR
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, rc: int, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self._out, self._err = out.encode(), err.encode()

    async def communicate(self):
        return self._out, self._err


def _fake_exec(monkeypatch, results: dict[str, list[_Proc]]):
    calls: list[tuple] = []

    async def exec_(*cmd, **kwargs):
        calls.append(cmd)
        return results[cmd[0]].pop(0)

    monkeypatch.setattr(orch.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(orch.asyncio, "sleep", AsyncMock())
    return calls


def _single_run():
    return RunState(index=0, name="feat", description="d", stages=[])


@pytest.mark.asyncio
async def test_pr_creation_failure_is_an_error_not_a_pr(monkeypatch, tmp_path):
    _fake_exec(monkeypatch, {"git": [_Proc(0)], "gh": [_Proc(1, err="HTTP 502")] * 3})
    storage = SimpleNamespace(repo_root=tmp_path)

    with pytest.raises(RuntimeError, match="gh pr create failed"):
        await orch._push_and_pr_for_branch("b", "main", "proj", _single_run(), storage)


@pytest.mark.asyncio
async def test_failed_push_is_an_error(monkeypatch, tmp_path):
    calls = _fake_exec(monkeypatch, {"git": [_Proc(1, err="no such remote 'origin'")]})
    storage = SimpleNamespace(repo_root=tmp_path)

    with pytest.raises(RuntimeError, match="no such remote"):
        await orch._push_and_pr_for_branch("b", "main", "proj", _single_run(), storage)
    assert [c[0] for c in calls] == ["git"]


@pytest.mark.asyncio
async def test_existing_pr_for_the_branch_counts_as_created(monkeypatch, tmp_path):
    err = 'a pull request for branch "b" into branch "main" already exists:\nhttps://github.com/o/r/pull/7'
    _fake_exec(monkeypatch, {"git": [_Proc(0)], "gh": [_Proc(1, err=err)]})
    storage = SimpleNamespace(repo_root=tmp_path)

    url = await orch._push_and_pr_for_branch("b", "main", "proj", _single_run(), storage)

    assert url == "https://github.com/o/r/pull/7"


@pytest.mark.asyncio
async def test_restart_regenerates_an_unusable_manifest(tmp_path):
    """run.sh whose phase prompt is missing: regenerate, don't rerun the runner."""
    state, wt = _make_state_with_worktree(tmp_path)
    stage = state.runs[0].stages[0]
    stage.status = StageStatus.FAILED
    _write_progress(wt, "run-0", "GENERATED")
    run_sh = wt / "docs" / "run-0" / "run.sh"
    run_sh.write_text('PHASES[1]="phase-1.md"\n')  # prompts/phase-1.md never written

    await orch.restart_stage(state, 0, 0, _ipc_storage(tmp_path))

    assert stage.status == StageStatus.SPEC_COMPLETE
    assert not run_sh.exists()


def test_ordinary_runner_failure_is_diagnosed_not_blindly_restarted(tmp_path):
    """The runner writes activity.log until it exits 1 (max iterations, gates)."""
    docs = tmp_path / "wt" / "docs" / "feat"
    docs.mkdir(parents=True)
    (docs / "activity.log").write_text("phase 2: max iterations reached\n")
    stage = StageState(name="s", spec_mode="full", worktree=str(tmp_path / "wt"))

    assert orch.classify_failure("feat", 1, stage, []) == "logic"
    assert orch.classify_failure("feat", 143, stage, []) == "infra"
