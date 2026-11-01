"""Tests for conductor.core.stats — TDD Phase 2."""

import json
from pathlib import Path

import pytest

from conductor.core.stats import (
    ModelPricing,
    TokenStats,
    StatsEntry,
    resolve_model,
    get_pricing,
    extract_stats,
    extract_reported_cost,
    find_result_event,
    result_events,
    session_num_turns,
    calculate_cost,
    estimate_cost,
    record_stats,
    format_tokens,
    format_duration,
)

# ---------------------------------------------------------------------------
# Real stream-json transcripts (Claude Code CLI 2.1.304, claude-haiku-5-5),
# captured with `claude -p - --output-format stream-json --verbose` and
# scrubbed of local paths and IDs.
# ---------------------------------------------------------------------------

STREAM_JSON_FIXTURES = Path(__file__).parent / "fixtures" / "stream_json"


def load_stream_json(name: str) -> str:
    return (STREAM_JSON_FIXTURES / f"{name}.jsonl").read_text("utf-8")


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


def test_resolve_model_opus():
    # Same 1M-context Opus that fix sessions and the orchestrator get.
    assert resolve_model("opus") == "claude-opus-4-8[1m]"


def test_resolve_model_opus_200k():
    assert resolve_model("opus-200k") == "claude-opus-4-8"


def test_resolve_model_sonnet():
    assert resolve_model("sonnet") == "claude-sonnet-4-6"


def test_resolve_model_haiku():
    assert resolve_model("haiku") == "claude-haiku-4-5"


def test_resolve_model_passthrough():
    assert resolve_model("claude-opus-4-8[1m]") == "claude-opus-4-8[1m]"


# ---------------------------------------------------------------------------
# get_pricing
# ---------------------------------------------------------------------------


def test_get_pricing_opus():
    pricing = get_pricing("claude-opus-4-8")
    assert pricing.input_per_mtok == 5.00
    assert pricing.output_per_mtok == 25.00
    assert pricing.cache_read_per_mtok == 0.50
    assert pricing.cache_write_per_mtok == 10.00


def test_get_pricing_resolves_aliases_and_variants():
    opus = get_pricing("claude-opus-4-8")
    assert get_pricing("opus") == opus
    assert get_pricing("claude-opus-4-8[1m]") == opus
    # modelUsage reports dated snapshot IDs
    assert get_pricing("claude-haiku-4-5-20251001") == get_pricing("haiku")


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
        "claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
        "claude-opus-4-7", "claude-opus-4-6",
    ],
)
def test_get_pricing_knows_current_models(model):
    assert get_pricing(model) is not None


def test_get_pricing_fable():
    fable_5_1 = get_pricing("claude-fable-5-1")
    assert (fable_5_1.input_per_mtok, fable_5_1.output_per_mtok) == (10.00, 50.00)
    assert fable_5_1.cache_read_per_mtok == 0.25
    assert fable_5_1.cache_write_per_mtok == 20.00
    assert get_pricing("claude-fable-5").cache_read_per_mtok == 1.00


@pytest.mark.parametrize("model", ["unknown-model-xyz", "claude-nonexistent-model-0", ""])
def test_get_pricing_unknown_model_is_none(model):
    # No guessing: an unknown model used to get Sonnet prices.
    assert get_pricing(model) is None


# ---------------------------------------------------------------------------
# extract_stats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture, expected",
    [
        # Read tool call + final answer, subtype success
        ("success", TokenStats(input=4, output=149, cache_read=28539, cache_write=5815)),
        # stopped by --max-turns 1: is_error, subtype error_max_turns, no `result` text
        ("error_max_turns", TokenStats(input=2, output=138, cache_read=17086, cache_write=0)),
    ],
)
def test_extract_stats_reads_top_level_usage(fixture, expected):
    assert extract_stats(load_stream_json(fixture)) == expected


@pytest.mark.parametrize(
    "fixture, cost",
    [("success", 0.00152329), ("error_max_turns", 0.00024006), ("model_not_found", 0.0)],
)
def test_extract_reported_cost(fixture, cost):
    assert extract_reported_cost(load_stream_json(fixture)) == cost


def test_find_result_event_shape():
    event = find_result_event(load_stream_json("error_max_turns"))
    assert event["subtype"] == "error_max_turns"
    assert event["is_error"] is True
    assert event["num_turns"] == 2
    assert "result" not in event
    assert event["errors"] == ["Reached maximum number of turns (1)"]
    assert set(event["modelUsage"]) == {"claude-haiku-5-5"}


def test_find_result_event_returns_last_result():
    output = "\n".join([
        json.dumps({"type": "result", "subtype": "success", "num_turns": 1}),
        json.dumps({"type": "assistant", "message": {"content": []}}),
        json.dumps({"type": "result", "subtype": "success", "num_turns": 2}),
    ])
    assert find_result_event(output)["num_turns"] == 2


# A background task's notification starts a second turn: two result events,
# each with its own turn's `usage` and `num_turns`, both with the session's
# running total_cost_usd and modelUsage (which also counts the subagent).
BACKGROUND_TASK_TOTALS = TokenStats(input=10, output=501, cache_read=74516, cache_write=42514)


def test_session_totals_come_from_the_last_results_model_usage():
    output = load_stream_json("background_task")
    assert [e["usage"]["output_tokens"] for e in result_events(output)] == [445, 29]

    assert extract_stats(output) == BACKGROUND_TASK_TOTALS
    assert session_num_turns(result_events(output)) == 3
    assert extract_reported_cost(output) == 0.007896185


def test_session_totals_without_model_usage_sum_each_turns_usage():
    output = "\n".join([
        json.dumps({"type": "result", "num_turns": 4, "usage": {"input_tokens": 3, "output_tokens": 300}}),
        json.dumps({"type": "result", "num_turns": 1, "usage": {"input_tokens": 1, "output_tokens": 20}}),
    ])

    assert extract_stats(output) == TokenStats(input=4, output=320)
    assert session_num_turns(result_events(output)) == 5


def test_extract_stats_no_result_event():
    output = '{"type":"assistant","message":{"content":[]}}\n'
    assert find_result_event(output) is None
    assert extract_stats(output) == TokenStats()
    assert extract_reported_cost(output) is None


def test_extract_stats_skips_non_events():
    output = "\n".join(
        [
            "not-json",
            '["a", "list"]',
            "",
            '{"type":"result","subtype":"success","usage":{"input_tokens":42,"output_tokens":7},"total_cost_usd":0.5}',
            "also not json",
        ]
    )
    assert extract_stats(output) == TokenStats(input=42, output=7)
    assert extract_reported_cost(output) == 0.5


# ---------------------------------------------------------------------------
# calculate_cost
# ---------------------------------------------------------------------------


def test_calculate_cost():
    pricing = ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    )
    tokens = TokenStats(
        input=1_000_000, output=500_000, cache_read=200_000, cache_write=100_000
    )
    cost = calculate_cost(tokens, pricing)
    expected = (
        1_000_000 * 3.00 + 500_000 * 15.00 + 200_000 * 0.30 + 100_000 * 3.75
    ) / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_calculate_cost_zero_tokens():
    pricing = ModelPricing(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    )
    tokens = TokenStats()
    assert calculate_cost(tokens, pricing) == 0.0


def test_calculate_cost_without_pricing_is_zero():
    assert calculate_cost(TokenStats(input=1_000_000), None) == 0.0


@pytest.mark.parametrize("fixture", ["success", "error_max_turns"])
def test_estimate_cost_matches_cli_reported_cost(fixture):
    """The fallback prices reproduce the CLI's own total_cost_usd."""
    output = load_stream_json(fixture)
    estimate = estimate_cost(extract_stats(output), "claude-haiku-5-5")
    assert estimate == pytest.approx(extract_reported_cost(output), rel=1e-6)


@pytest.mark.parametrize("model", [None, "", "claude-nonexistent-model-0"])
def test_estimate_cost_unknown_model_is_none(model):
    assert estimate_cost(TokenStats(input=1000), model) is None


# ---------------------------------------------------------------------------
# record_stats
# ---------------------------------------------------------------------------


def test_record_stats_new_file(tmp_path):
    stats_path = tmp_path / "stats.json"
    entry = StatsEntry(
        type="brain",
        iteration=1,
        timestamp="2026-01-01T00:00:00Z",
        duration_s=10.0,
        tokens=TokenStats(input=100, output=50),
        cost_usd=0.01,
        phase="run",
        model="claude-sonnet-4-6",
    )
    record_stats(stats_path, entry)
    data = json.loads(stats_path.read_text())
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["type"] == "brain"
    assert data[0]["iteration"] == 1


def test_record_stats_append(tmp_path):
    stats_path = tmp_path / "stats.json"
    entry1 = StatsEntry(
        type="brain",
        iteration=1,
        timestamp="2026-01-01T00:00:00Z",
        duration_s=5.0,
        tokens=TokenStats(),
        cost_usd=0.0,
        phase="run",
        model="claude-sonnet-4-6",
    )
    entry2 = StatsEntry(
        type="runner",
        iteration=2,
        timestamp="2026-01-01T00:01:00Z",
        duration_s=10.0,
        tokens=TokenStats(input=200),
        cost_usd=0.002,
        phase="run",
        model="claude-sonnet-4-6",
    )
    record_stats(stats_path, entry1)
    record_stats(stats_path, entry2)
    data = json.loads(stats_path.read_text())
    assert len(data) == 2
    assert data[1]["type"] == "runner"


# ---------------------------------------------------------------------------
# format_tokens
# ---------------------------------------------------------------------------


def test_format_tokens_millions():
    assert format_tokens(1_500_000) == "1.5M"


def test_format_tokens_thousands():
    assert format_tokens(42_000) == "42.0k"


def test_format_tokens_small():
    assert format_tokens(500) == "500"


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


def test_format_duration_hours():
    assert format_duration(3720) == "1h02m"


def test_format_duration_minutes():
    assert format_duration(330) == "5m30s"
