"""runner.fixer CI-check handling against a fake `gh` that validates --json like gh 2.96."""

from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

import runner.fixer as fixer

# `gh pr checks --json` fields in gh 2.96; anything else exits 1 like the real CLI.
_FAKE_GH = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys

    args = sys.argv[1:]
    checks = json.loads(os.environ.get("FAKE_GH_CHECKS", "[]"))
    allowed = {{"bucket", "completedAt", "description", "event", "link", "name",
               "startedAt", "state", "workflow"}}
    if args[:2] == ["pr", "checks"]:
        fields = args[args.index("--json") + 1].split(",")
        unknown = [f for f in fields if f not in allowed]
        if unknown:
            print(f'Unknown JSON field: "{{unknown[0]}}"', file=sys.stderr)
            sys.exit(1)
        print(json.dumps([{{f: c.get(f, "") for f in fields}} for c in checks]))
    elif args[:2] == ["pr", "view"]:
        print("abc123")
    elif args[:1] == ["api"]:
        pass  # no annotations
    else:
        print(f"fake gh: unsupported {{args}}", file=sys.stderr)
        sys.exit(1)
    """
)


def _check(name: str, state: str, bucket: str) -> dict:
    return {"name": name, "state": state, "bucket": bucket, "link": f"https://ci.example/{name}"}


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(_FAKE_GH.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(fixer.time, "sleep", lambda _s: None)

    def set_checks(*checks: dict) -> None:
        monkeypatch.setenv("FAKE_GH_CHECKS", json.dumps(list(checks)))

    return set_checks


def test_failure_logs_name_every_blocking_check(fake_gh, tmp_path):
    fake_gh(
        _check("tests", "FAILURE", "fail"),
        _check("e2e", "TIMED_OUT", "fail"),
        _check("lint", "SUCCESS", "pass"),
        _check("codecov/patch", "FAILURE", "fail"),  # matches the default skip pattern
    )

    logs = fixer.get_ci_failure_logs(7, "o", "r", tmp_path)

    assert "=== FAILED CHECK: tests (https://ci.example/tests) ===" in logs
    assert "=== FAILED CHECK: e2e" in logs
    assert "lint" not in logs and "codecov" not in logs


def test_wait_for_ci_uses_gh_buckets(fake_gh, tmp_path):
    fake_gh(_check("build", "TIMED_OUT", "fail"), _check("lint", "SUCCESS", "pass"))
    assert fixer.wait_for_ci(7, tmp_path, poll_interval=1, max_wait=33) == "CI_FAILED"

    fake_gh(_check("deploy", "WAITING", "pending"), _check("lint", "SUCCESS", "pass"))
    assert fixer.wait_for_ci(7, tmp_path, poll_interval=1, max_wait=33) == "CI_TIMEOUT"

    fake_gh(_check("lint", "SUCCESS", "pass"), _check("docs", "CANCELLED", "cancel"))
    assert fixer.wait_for_ci(7, tmp_path, poll_interval=1, max_wait=33) == "CI_PASSED"


async def test_unreadable_ci_failure_is_not_reported_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(fixer, "_get_repo_info", lambda _cwd: ("o", "r"))
    monkeypatch.setattr(fixer, "wait_for_ci", lambda *_a, **_k: "CI_FAILED")
    monkeypatch.setattr(fixer, "get_unresolved_threads", lambda *_a: [])
    monkeypatch.setattr(fixer, "get_ci_failure_logs", lambda *_a: "")
    cfg = fixer.FixerConfig(
        project_dir=tmp_path, branch="feat", feature_name="feat", phase=1, pr_number=7, sync_mode=True,
    )

    await fixer.run_fixer(cfg)

    status_file = tmp_path / "storage" / "logs" / "feat-build" / ".fixer-status-phase-1"
    status = json.loads(status_file.read_text())
    assert status["status"] == "error"
