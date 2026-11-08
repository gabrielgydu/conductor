"""One effort level reaches every Claude session a project starts."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import conductor.core.orchestrator as orch
from conductor import cli
from conductor.core import manual_test
from conductor.core.claude import run_claude, run_claude_steerable
from conductor.core.launch import claude_session_script
from conductor.core.loop import LoopState, run_loop_in_tmux
from conductor.core.model_catalog import (
    DEFAULT_EFFORT,
    EFFORT_ENV,
    effort_args,
    set_effort,
)
from conductor.core.models import ConductorState
from conductor.core.orchestrator import ConductorConfig, conductor_run_loop
from conductor.core.presets import load_preset
from conductor.core.storage import StorageResolver
from conductor.core.tmux import forwarded_vars
from runner.steerable import SteerableSession
from tests.cli_support import (  # noqa: F401
    NAME,
    repo_fixture,
    run_main,
    seed_state,
    write_brief,
)


def _write_preset(repo: Path, effort: str) -> None:
    preset = repo / ".conductor" / "preset.toml"
    preset.parent.mkdir(exist_ok=True)
    preset.write_text(f'name = "project"\n\n[config]\neffort = "{effort}"\n')


# -- the setting ---------------------------------------------------------------


def test_without_a_setting_sessions_get_conductors_default(monkeypatch):
    monkeypatch.delenv(EFFORT_ENV)

    assert effort_args() == ["--effort", DEFAULT_EFFORT]


def test_set_effort_decides_the_arguments():
    set_effort("low")

    assert effort_args() == ["--effort", "low"]


def test_set_effort_without_a_level_clears_an_earlier_one():
    set_effort("low")

    set_effort(None)

    assert effort_args() == ["--effort", DEFAULT_EFFORT]


def test_tmux_windows_receive_the_level():
    # speccer and runner run in windows of a tmux server that may predate us.
    set_effort("medium")

    assert forwarded_vars()[EFFORT_ENV] == "medium"


# -- every way of starting a session -------------------------------------------


class _Started(Exception):
    """Stops a launcher once it has built the claude command."""


async def _argv_of(launch) -> list[str]:
    spawn = AsyncMock(side_effect=_Started)
    with patch("asyncio.create_subprocess_exec", spawn), pytest.raises(_Started):
        await launch()
    return list(spawn.call_args.args)


@pytest.mark.parametrize(
    "launch",
    [
        lambda: run_claude("p", model="m"),
        lambda: run_claude_steerable("p", model="m"),
        lambda: SteerableSession.launch("p", model="m", activity_log=Path("/dev/null")),
    ],
    ids=["run_claude", "run_claude_steerable", "runner_steerable"],
)
async def test_process_sessions_pass_the_effort(launch):
    set_effort("high")

    argv = await _argv_of(launch)

    assert argv[argv.index("--effort") + 1] == "high"


async def test_a_session_without_a_model_still_gets_the_effort():
    # settings.json would otherwise decide the effort of these sessions.
    set_effort("high")

    argv = await _argv_of(lambda: run_claude("p"))

    assert "--model" not in argv
    assert argv[argv.index("--effort") + 1] == "high"


def test_loop_session_script_passes_the_effort():
    set_effort("medium")

    script = claude_session_script(
        cwd="/w", prompt_file="/p", output_file="/o", exit_file="/e", model="m", max_turns=1,
    )

    assert "--effort medium" in script


async def test_manual_test_generation_passes_the_effort(tmp_path, monkeypatch):
    set_effort("low")
    conductor_dir = tmp_path / "c"
    conductor_dir.mkdir()
    state = manual_test.ManualTestState(name="m", project_dir=str(tmp_path), plan_file="p")
    tmux = MagicMock()
    tmux.spawn_in_window = AsyncMock(side_effect=_Started)

    with pytest.raises(_Started):
        await manual_test._run_generation(
            state=state, project_dir=tmp_path, conductor_dir=conductor_dir, tmux=tmux,
            prompt="p", label="l", model="sonnet", scenario=None,
            log_path=conductor_dir / "log", audit_path=conductor_dir / "audit",
        )

    assert "--effort low" in shlex.split(tmux.spawn_in_window.call_args.args[1])[-1]


# -- which level a command uses ------------------------------------------------


@pytest.fixture
def run_loop_env(repo, monkeypatch):
    """conductor_run_loop down to its first step, with effort seen at ensure_session."""
    seen: list[str] = []
    tmux = MagicMock()

    async def ensure_session():
        seen.append(os.environ[EFFORT_ENV])
        raise _Started

    tmux.ensure_session = ensure_session
    monkeypatch.setattr(orch._tmux_module, "TmuxManager", MagicMock(return_value=tmux))
    return seen


@pytest.mark.parametrize(
    ("saved", "preset", "expected"),
    [("low", "high", "low"), (None, "high", "high"), (None, None, DEFAULT_EFFORT)],
    ids=["saved-beats-preset", "preset", "default"],
)
async def test_run_loop_settles_the_effort_before_tmux_starts(repo, run_loop_env, saved, preset, expected):
    if preset:
        _write_preset(repo, preset)
    state = ConductorState(project_name=NAME, effort=saved, preset="project" if preset else None)

    with pytest.raises(_Started):
        await conductor_run_loop(state, ConductorConfig(project_root=repo))

    assert run_loop_env == [expected]


def test_plan_uses_the_presets_effort(repo, monkeypatch):
    _write_preset(repo, "medium")
    run_main(["init", "--name", NAME, "--project-dir", str(repo), "--preset", "project"])
    write_brief(repo)
    seen = []

    def fake_generate(*_args):
        seen.append(os.environ[EFFORT_ENV])
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_generate_and_review_plan", fake_generate)

    run_main(["plan", "--name", NAME, "--project-dir", str(repo)])

    assert seen == ["medium"]


def test_go_flag_applies_to_planning(repo, monkeypatch):
    _write_preset(repo, "medium")
    seen = []

    def fake_generate(*_args):
        seen.append(os.environ[EFFORT_ENV])
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_generate_and_review_plan", fake_generate)
    StorageResolver(repo).conductor_dir(NAME).mkdir(parents=True, exist_ok=True)
    write_brief(repo)

    run_main(["go", "--name", NAME, "--project-dir", str(repo), "--effort", "low"])

    assert seen == ["low"]


def test_loop_saved_effort_beats_the_preset(repo, monkeypatch):
    _write_preset(repo, "high")
    state = LoopState(name="fix", base_branch="main", plan_file=str(repo / "plan.md"), effort="low")
    (repo / "plan.md").write_text("- [ ] t\n")
    seen = []

    class _Tmux:
        def __init__(self, **_kwargs):
            pass

        async def ensure_session(self):
            seen.append(os.environ[EFFORT_ENV])
            raise _Started

    monkeypatch.setattr("conductor.core.tmux.TmuxManager", _Tmux)

    with pytest.raises(_Started):
        run_loop_in_tmux(state, repo, StorageResolver(repo))

    assert seen == ["low"]


def test_loop_resume_flag_replaces_the_saved_effort(repo, monkeypatch):
    storage = StorageResolver(repo)
    (repo / "plan.md").write_text("- [ ] t\n")
    state_path = storage.conductor_dir("fix") / "LOOP-STATE.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        LoopState(name="fix", base_branch="main", plan_file=str(repo / "plan.md"), effort="low").model_dump_json()
    )
    started = []
    monkeypatch.setattr("conductor.core.loop.run_loop_in_tmux", lambda state, *_: started.append(state.effort))

    run_main(["loop", "--inside-tmux", "--name", "fix", "--project-dir", str(repo), "--effort", "high"])

    assert started == ["high"]


def test_validate_uses_the_saved_effort(repo, monkeypatch):
    seed_state(repo, effort="low")
    set_effort("max")

    run_main(["validate", "--name", NAME, "--project-dir", str(repo)])

    assert os.environ[EFFORT_ENV] == "low"


# -- presets -------------------------------------------------------------------


def test_preset_reads_effort(repo):
    _write_preset(repo, "max")

    assert load_preset("project", repo).config.effort == "max"


def test_preset_rejects_an_unknown_effort(repo):
    _write_preset(repo, "turbo")

    with pytest.raises(ValueError, match="effort must be one of"):
        load_preset("project", repo)
