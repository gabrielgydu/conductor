"""Tests for the integration line of `conductor status`."""

from __future__ import annotations

from conductor.core.models import IntegrationState
from tests.cli_support import NAME, repo_fixture, run_main, seed_state  # noqa: F401


def _status(repo) -> str:
    out, _, code = run_main(["status", "--name", NAME, "--project-dir", str(repo)])
    assert code == 0
    return out


def test_status_shows_the_integration_pr(repo):
    seed_state(repo, integration=IntegrationState(
        status="done", branch="integration/p", pr_url="https://x/pull/7", published=True,
    ))

    assert "Integration: done (branch: integration/p, PR: https://x/pull/7)" in _status(repo)


def test_status_says_when_the_integration_is_not_published_and_how_it_is_retried(repo):
    seed_state(repo, integration=IntegrationState(
        status="needs_fix", branch="integration/p", published=False,
        failure="publish", failure_detail="gh pr create failed: gh auth login", failed_attempts=1,
    ))

    out = _status(repo)

    assert "Integration: needs_fix (branch: integration/p, not published)" in out
    assert "gh auth login; retried on the next run (1 of 3 attempts made)" in out


def test_status_tells_how_to_retry_a_failure_that_is_not_retried(repo):
    seed_state(repo, integration=IntegrationState(
        status="done", branch="integration/p", pr_url="https://x/pull/7", published=True,
        failure="setup", failure_detail="integration/p is checked out in /repo", failed_attempts=3,
    ))

    out = _status(repo)

    assert "PR: https://x/pull/7" in out
    assert "with --reintegrate" in out
