"""Tests for conductor.core.model_catalog — the single alias map."""

import pytest

from conductor.core import claude, stats
from conductor.core.model_catalog import MODEL_ALIASES, resolve_model


@pytest.mark.parametrize(
    "alias, model_id",
    [
        ("opus", "claude-opus-4-8[1m]"),
        ("opus-200k", "claude-opus-4-8"),
        ("sonnet", "claude-sonnet-4-6"),
        ("haiku", "claude-haiku-4-5"),
    ],
)
def test_aliases(alias, model_id):
    assert resolve_model(alias) == model_id


@pytest.mark.parametrize("alias", ["Opus", "OPUS", " opus "])
def test_alias_lookup_ignores_case_and_whitespace(alias):
    assert resolve_model(alias) == "claude-opus-4-8[1m]"


@pytest.mark.parametrize(
    "model_id",
    ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-4-8[1m]", "claude-custom", ""],
)
def test_full_ids_pass_through_unchanged(model_id):
    assert resolve_model(model_id) == model_id


def test_claude_and_stats_share_one_resolver():
    # Runner phases resolve through stats, fix sessions and the orchestrator
    # through claude; they used to carry separate maps that disagreed on "opus".
    assert stats.resolve_model is resolve_model
    assert claude.resolve_model is resolve_model
    for alias in MODEL_ALIASES:
        assert stats.resolve_model(alias) == claude.resolve_model(alias)
