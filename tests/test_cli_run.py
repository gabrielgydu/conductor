"""Tests for `conductor run` / `conductor go` flag handling (orchestrator loop mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conductor.core.models import ConductorState, atomic_save
from conductor.core.storage import StorageResolver
from tests.cli_support import (  # noqa: F401
    NAME,
    loop_calls_fixture,
    repo_fixture,
    run_main,
    saved_state,
    seed_state,
    write_brief,
)
from tests.helpers import make_run_state


def _inside(repo, *extra: str) -> list[str]:
    return ["run", "--inside-tmux", "--name", NAME, "--project-dir", str(repo), *extra]


# ── overnight ─────────────────────────────────────────────────────────────────


def test_run_no_overnight_reaches_orchestrator_config(repo, loop_calls):
    seed_state(repo)

    _, _, code = run_main(_inside(repo, "--no-overnight"))

    assert code == 0
    state, config = loop_calls[0]
    assert state.overnight is False
    # The loop reads `config.overnight or state.overnight`; both must be off.
    assert config.overnight is False


def test_run_defaults_to_overnight(repo, loop_calls):
    seed_state(repo, overnight=False)

    _, _, code = run_main(_inside(repo))

    assert code == 0
    state, config = loop_calls[0]
    assert state.overnight is True
    assert config.overnight is True


# ── settings saved in state vs. flags ────────────────────────────────────────

def _go_inside(repo, *extra: str) -> list[str]:
    return ["go", "--inside-tmux", "--name", NAME, "--project-dir", str(repo), *extra]


def test_go_no_quick_overrides_saved_quick(repo, loop_calls):
    seed_state(repo, quick=True)
    write_brief(repo)

    _, _, code = run_main(_go_inside(repo, "--no-quick"))

    assert code == 0
    assert loop_calls[0][0].quick is False
    assert saved_state(repo).quick is False


def test_go_resume_without_flags_keeps_saved_settings(repo, loop_calls):
    seed_state(repo, quick=False, max_parallel=3)
    write_brief(repo)

    _, _, code = run_main(_go_inside(repo))

    assert code == 0
    state, config = loop_calls[0]
    assert state.quick is False
    assert state.max_parallel == 3
    assert config.max_parallel == 3


def test_run_no_quick_and_max_parallel_override_state(repo, loop_calls):
    seed_state(repo, quick=True, max_parallel=3)

    _, _, code = run_main(_inside(repo, "--no-quick", "--max-parallel", "2"))

    assert code == 0
    state, config = loop_calls[0]
    assert (state.quick, state.max_parallel, config.max_parallel) == (False, 2, 2)


def test_run_without_quick_flag_keeps_saved_quick(repo, loop_calls):
    seed_state(repo, quick=True)

    run_main(_inside(repo))

    assert loop_calls[0][0].quick is True


@pytest.fixture
def fake_plan(monkeypatch):
    """Replace _cmd_plan with one that saves a single-run plan without Claude."""
    import conductor.cli as cli

    def plan(args):
        path = StorageResolver(Path(args.project_dir)).conductor_state(args.name)
        state = ConductorState.model_validate_json(path.read_text(encoding="utf-8"))
        state.runs = [make_run_state(0, "r0")]
        atomic_save(state, path)

    monkeypatch.setattr(cli, "_cmd_plan", plan)


@pytest.mark.parametrize(("flag", "expected"), [((), True), (("--no-quick",), False)])
def test_go_turns_quick_on_for_a_project_it_plans(repo, loop_calls, fake_plan, flag, expected):
    write_brief(repo)
    state = seed_state(repo, quick=False)
    state.runs = []
    atomic_save(state, StorageResolver(repo).conductor_state(NAME))

    _, _, code = run_main(_go_inside(repo, *flag))

    assert code == 0
    assert loop_calls[0][0].quick is expected


def test_deprecated_overnight_flag_is_accepted_as_a_no_op(repo, loop_calls):
    seed_state(repo)

    _, err, code = run_main(_inside(repo, "--overnight"))

    assert code == 0
    assert "deprecated" in err
    state, config = loop_calls[0]
    assert state.overnight is True and config.overnight is True


def test_no_overnight_wins_over_deprecated_overnight(repo, loop_calls):
    seed_state(repo)

    run_main(_inside(repo, "--overnight", "--no-overnight"))

    assert loop_calls[0][1].overnight is False
