"""runner.phase_loop against a scratch git repo, with Claude replaced by a script."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import runner.phase_loop as phase_loop
from runner.config import PhaseConfig, RunConfig

FEATURE = "feat"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _commits(repo: Path) -> list[str]:
    return _git(repo, "log", "--format=%s").splitlines()


def _files_in(repo: Path, rev: str) -> list[str]:
    return _git(repo, "show", "--name-only", "--format=", rev).split()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A repo laid out like an orchestrated stage worktree after the speccer ran."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    feature_dir = repo / "docs" / FEATURE
    (feature_dir / "prompts").mkdir(parents=True)
    for n in (1, 2):
        (feature_dir / "prompts" / f"phase-{n}.md").write_text(f"- [PENDING] step {n}\n")
    (feature_dir / "PRD.md").write_text("prd\n")
    (feature_dir / "RUN-CONFIG.json").write_text("{}\n")

    monkeypatch.setattr(phase_loop, "_RETRY_PAUSE_S", 0)
    monkeypatch.setattr(phase_loop, "_PHASE_PAUSE_S", 0)
    return repo, feature_dir


def _config(repo: Path, feature_dir: Path, phases: int = 2, **overrides) -> RunConfig:
    cfg = RunConfig(
        feature_name=FEATURE,
        project_dir=str(repo),
        phases=[
            PhaseConfig(
                number=n,
                name=f"Step {n}",
                prompt_file=str(feature_dir / "prompts" / f"phase-{n}.md"),
                token=f"P{n}_DONE",
            )
            for n in range(1, phases + 1)
        ],
        steerable=True,
        max_iterations=3,
        max_gate_retries=2,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class FakeClaude:
    """Stands in for _run_steerable_iteration; one scripted action per call.

    Each action gets (repo, phase_number) and returns the assistant text. It
    always appends to the activity log, as the real session does.
    """

    def __init__(self, repo: Path, actions):
        self.repo = repo
        self.actions = list(actions)
        self.calls: list[int] = []
        self.prompts: list[str] = []

    async def __call__(self, prompt, model, max_turns, cwd, activity_log, **_kw):
        phase = int(re.search(r"This is phase (\d+) of", prompt).group(1))
        self.calls.append(phase)
        self.prompts.append(prompt)
        _append(activity_log, f"phase {phase} iteration\n")
        text = self.actions.pop(0)(self.repo, phase)
        return text, text, {"usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0}


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(text)


def write_code_and_finish(repo: Path, phase: int) -> str:
    (repo / f"phase{phase}.py").write_text(f"VALUE = {phase}\n")
    _append(repo / "docs" / FEATURE / "LEARNINGS.md", f"## phase {phase} gotcha\n")
    return f"done <promise>P{phase}_DONE</promise>"


def finish_without_changes(repo: Path, phase: int) -> str:
    return f"nothing to do <promise>P{phase}_DONE</promise>"


async def _run(cfg: RunConfig, feature_dir: Path, fake: FakeClaude, monkeypatch, **kwargs) -> int:
    monkeypatch.setattr(phase_loop, "_run_steerable_iteration", fake)
    return await phase_loop.run_phase_loop(cfg, feature_dir, **kwargs)


# ─── Bookkeeping stays out of commits (F53) ─────────────────────────────────


async def test_phase_commits_contain_only_feature_changes(project, monkeypatch):
    repo, feature_dir = project
    fake = FakeClaude(repo, [write_code_and_finish, write_code_and_finish])

    assert await _run(_config(repo, feature_dir), feature_dir, fake, monkeypatch) == 0

    assert _commits(repo) == ["Phase 2: Step 2", "Phase 1: Step 1", "init"]
    assert _files_in(repo, "HEAD~1") == ["phase1.py"]
    assert _files_in(repo, "HEAD") == ["phase2.py"]
    # Nothing tracked is left modified or deleted; the runner's own files and
    # the speccer/learnings docs stay untracked.
    status = _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    assert all(line.startswith("??") for line in status), status
    untracked = {line[3:] for line in status}
    for name in ("activity.log", "STATS.json", "exit_code", "LEARNINGS.md", "PRD.md"):
        assert f"docs/{FEATURE}/{name}" in untracked


async def test_phase_without_changes_makes_no_commit(project, monkeypatch):
    repo, feature_dir = project
    fake = FakeClaude(repo, [finish_without_changes])

    assert await _run(_config(repo, feature_dir, phases=1), feature_dir, fake, monkeypatch) == 0

    assert _commits(repo) == ["init"]


# ─── Preset quality gate runs before every commit (F11) ─────────────────────


def _gate_preset(tmp_path: Path, command: str) -> str:
    preset = tmp_path / "gate.toml"
    preset.write_text(
        'name = "gated"\n\n[[quality_gate.checks]]\n'
        f'name = "unit"\ncommand = {command!r}\n'
    )
    return str(preset)


def write_code_without_fix(repo: Path, phase: int) -> str:
    (repo / "app.py").write_text("BROKEN = True\n")
    return f"done <promise>P{phase}_DONE</promise>"


def write_fix(repo: Path, phase: int) -> str:
    (repo / "app.py").write_text("BROKEN = False\n")
    (repo / "fixed.txt").write_text("ok\n")
    return f"fixed <promise>P{phase}_DONE</promise>"


async def test_failing_gate_blocks_commit_and_fails_phase(project, tmp_path, monkeypatch):
    repo, feature_dir = project
    cfg = _config(repo, feature_dir, phases=1, preset=_gate_preset(tmp_path, "sh -c 'echo boom >&2; exit 3'"))
    fake = FakeClaude(repo, [write_code_without_fix] * 3)

    assert await _run(cfg, feature_dir, fake, monkeypatch) == 1

    assert _commits(repo) == ["init"]
    assert fake.calls == [1, 1]  # max_gate_retries=2: one fix attempt, then give up
    assert "QUALITY GATE FAILED" in fake.prompts[1]
    assert "unit: exit 3" in fake.prompts[1] and "boom" in fake.prompts[1]
    assert (feature_dir / "exit_code").read_text() == "1"


async def test_gate_failure_is_fed_back_until_it_passes(project, tmp_path, monkeypatch):
    repo, feature_dir = project
    cfg = _config(repo, feature_dir, phases=1, preset=_gate_preset(tmp_path, "test -f fixed.txt"))
    fake = FakeClaude(repo, [write_code_without_fix, write_fix])

    assert await _run(cfg, feature_dir, fake, monkeypatch) == 0

    assert "QUALITY GATE FAILED" not in fake.prompts[0]
    assert "unit: exit 1" in fake.prompts[1]
    assert _commits(repo) == ["Phase 1: Step 1", "init"]
    assert sorted(_files_in(repo, "HEAD")) == ["app.py", "fixed.txt"]


async def test_no_op_phase_skips_the_gate(project, tmp_path, monkeypatch):
    # Nothing changed, so there is nothing new to gate; a base branch that
    # already fails the gate must not fail phases that did not touch it.
    repo, feature_dir = project
    cfg = _config(repo, feature_dir, phases=1, preset=_gate_preset(tmp_path, "false"))
    fake = FakeClaude(repo, [finish_without_changes])

    assert await _run(cfg, feature_dir, fake, monkeypatch) == 0
    assert _commits(repo) == ["init"]
