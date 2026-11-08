"""Tests for `conductor plan` (Claude mocked out)."""

from __future__ import annotations

import subprocess

import pytest

import conductor.cli as cli
from conductor.core.enums import IntegrationStatus, RunStatus
from conductor.core.models import IntegrationState
from conductor.core.storage import conductor_home
from tests.cli_support import (  # noqa: F401
    NAME,
    repo_fixture,
    run_main,
    saved_state,
    seed_state,
    write_brief,
)
from tests.helpers import make_run_state

PLAN = [
    {
        "name": "health-api",
        "description": "Health endpoint",
        "depends_on": [],
        "stages": [{"name": "backend", "spec_mode": "backend"}],
    }
]
DESCRIPTIONS = {"run-0-backend": "Build GET /health."}


@pytest.fixture
def claude_calls(monkeypatch):
    calls = []

    def fake_generate(prompt, brief, repo_path, run_claude):
        calls.append(prompt)
        return PLAN, DESCRIPTIONS

    monkeypatch.setattr(cli, "_generate_and_review_plan", fake_generate)
    return calls


def _plan(repo, *extra):
    return run_main(["plan", "--name", NAME, "--project-dir", str(repo), *extra])


def _write_user_preset(name: str) -> None:
    presets = conductor_home() / "presets"
    presets.mkdir(exist_ok=True)
    (presets / f"{name}.toml").write_text(f'name = "{name}"\n')


def test_plan_refuses_a_missing_preset_before_calling_claude(repo, claude_calls):
    seed_state(repo, runs=[], preset="gone")
    write_brief(repo)

    out, _, code = _plan(repo)

    assert code == 1
    assert "gone" in out
    assert claude_calls == []


def test_plan_keeps_the_base_branch_chosen_at_init(repo, claude_calls):
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature-x"], check=True)
    run_main(["init", "--name", NAME, "--project-dir", str(repo), "--base-branch", "develop"])
    write_brief(repo)

    _, _, code = _plan(repo)

    assert code == 0
    assert saved_state(repo).base_branch == "develop"


def test_replan_keeps_project_settings_and_resets_integration(repo, claude_calls):
    seed_state(
        repo,
        runs=[make_run_state(0, "old", status=RunStatus.DONE)],
        preset="myapp",
        overnight=False,
        quick=True,
        max_parallel=3,
        worktrees_base="/srv/wt",
        check_interval_s=30,
        integration=IntegrationState(branch="integration/proj", status=IntegrationStatus.NEEDS_FIX, pr_url="u"),
    )
    _write_user_preset("myapp")
    write_brief(repo)

    _, _, code = _plan(repo, "--force")

    assert code == 0
    state = saved_state(repo)
    assert [r.name for r in state.runs] == ["health-api"]
    assert state.integration is None
    assert (state.preset, state.overnight, state.quick) == ("myapp", False, True)
    assert (state.max_parallel, state.worktrees_base, state.check_interval_s) == (3, "/srv/wt", 30)
    assert state.base_branch == "main"


@pytest.mark.parametrize("started", ["status", "worktree", "integration"])
def test_replan_refuses_to_drop_started_runs_without_force(repo, claude_calls, started):
    run = make_run_state(0, "old")
    integration = None
    if started == "status":
        run.status = RunStatus.ACTIVE
    elif started == "worktree":
        run.stages[0].worktree = "/srv/wt/old-stage-0"
        run.stages[0].branch = "conductor/proj/old/stage-0"
    else:
        integration = IntegrationState(branch="integration/proj")
    seed_state(repo, runs=[run], integration=integration)
    write_brief(repo)

    out, _, code = _plan(repo)

    assert code == 1
    assert "--force" in out and "cleanup" in out
    assert claude_calls == []
    assert [r.name for r in saved_state(repo).runs] == ["old"]


def test_replan_of_an_unstarted_plan_needs_no_force(repo, claude_calls):
    seed_state(repo, runs=[make_run_state(0, "old")])
    write_brief(repo)

    _, _, code = _plan(repo)

    assert code == 0
    assert [r.name for r in saved_state(repo).runs] == ["health-api"]


# ── plan output parsing and validation ───────────────────────────────────────


def _stage(name, spec_mode=None, wiring=None):
    return {"name": name, "spec_mode": spec_mode or name, "context_wiring": wiring}


def _run(name, *stages, depends_on=()):
    return {"name": name, "depends_on": list(depends_on), "constitution": [], "stages": list(stages)}


def _descs(plan):
    return {f"run-{i}-{s['name']}": "text" for i, r in enumerate(plan) for s in r["stages"]}


def _backend_wiring(run=0, stage=0):
    return {"type": "backend-context", "source_run": run, "source_stage": stage, "source_path": "API-CONTRACTS.md"}


def test_descriptions_parse_whatever_the_line_layout():
    text = (
        '```conductor-state\n[]\n```\n'
        '<description name="run-0-backend">One line only.</description>\n'
        "<description name='run-0-frontend'>Starts here\nand goes on.\n</description>\n"
        '<description name="run-0-testing">\nThe template layout.\n</description>\n'
    )

    _, descriptions = cli._parse_plan_output(text)

    assert descriptions == {
        "run-0-backend": "One line only.",
        "run-0-frontend": "Starts here\nand goes on.",
        "run-0-testing": "The template layout.",
    }


def test_a_well_formed_plan_has_no_problems():
    plan = [
        _run("api", _stage("backend"), _stage("frontend", wiring=_backend_wiring())),
        _run("ui", _stage("frontend", wiring=_backend_wiring(0, 0)), depends_on=[0]),
        _run("ext", _stage("testing", wiring={"type": "external", "source_path": "/abs/scenarios"})),
    ]

    assert cli._plan_problems(plan, _descs(plan)) == []


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ({"runs": []}, "non-empty JSON array"),
        ([_run("User Auth", _stage("backend"))], "kebab-case"),
        ([_run("a", _stage("backend")), _run("a", _stage("backend"))], "used twice"),
        ([_run("a", _stage("e2e"))], "spec_mode 'e2e'"),
        ([_run("a", _stage("backend", wiring=_backend_wiring(0, 5)))], "does not exist"),
        ([_run("a", _stage("backend", wiring=_backend_wiring(0, 0)))], "earlier stage"),
        ([_run("a", _stage("backend")), _run("b", _stage("frontend", wiring=_backend_wiring(0, 0)))], "depends_on"),
        ([_run("a", _stage("backend"), depends_on=[1]), _run("b", _stage("backend"), depends_on=[0])], "cycle"),
        ([{**_run("a", _stage("backend")), "index": 3}], "numbered 0..n-1"),
    ],
)
def test_plan_problems_are_reported(plan, expected):
    descriptions = _descs(plan) if isinstance(plan, list) else {}

    problems = cli._plan_problems(plan, descriptions)

    assert any(expected in p for p in problems), problems


def test_a_missing_description_is_a_problem():
    plan = [_run("a", _stage("backend"), _stage("frontend", wiring=_backend_wiring()))]

    problems = cli._plan_problems(plan, {"run-0-backend": "text"})

    assert problems == ['missing <description name="run-0-frontend"> block']


class FakeClaude:
    """run_claude stand-in that answers each call with the next scripted text."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def __call__(self, prompt, **kwargs):
        import json
        from types import SimpleNamespace

        self.prompts.append(prompt)
        event = {"type": "assistant", "message": {"content": [{"type": "text", "text": self.answers.pop(0)}]}}
        return SimpleNamespace(exit_code=0, output=json.dumps(event) + "\n")


GOOD = (
    '```conductor-state\n[{"name": "api", "depends_on": [], "stages": [{"name": "backend"}]}]\n```\n'
    '<description name="run-0-backend">Build it.</description>'
)


@pytest.mark.parametrize(
    "bad",
    [
        GOOD.replace('{"name": "backend"}]', '{"name": "backend"},]'),  # trailing comma: invalid JSON
        GOOD.split("<description")[0],  # description missing
    ],
)
def test_malformed_plan_output_gets_one_corrective_retry(tmp_path, bad):
    claude = FakeClaude(bad, GOOD)

    runs, descriptions = cli._generate_valid_plan("PROMPT", tmp_path, claude)

    assert [r["name"] for r in runs] == ["api"]
    assert descriptions == {"run-0-backend": "Build it."}
    assert len(claude.prompts) == 2
    assert claude.prompts[1].startswith("PROMPT") and "Problems with a previous attempt" in claude.prompts[1]


def test_plan_output_still_malformed_after_the_retry_fails(tmp_path, capsys):
    bad = GOOD.split("<description")[0]
    claude = FakeClaude(bad, bad)

    with pytest.raises(SystemExit) as exc:
        cli._generate_valid_plan("PROMPT", tmp_path, claude)

    assert exc.value.code == 1
    assert 'missing <description name="run-0-backend">' in capsys.readouterr().out
