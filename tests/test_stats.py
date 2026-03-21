"""Tests for conductor.core.stats — TDD Phase 2."""
import json
import pytest

from conductor.core.stats import (
    ModelPricing,
    TokenStats,
    StatsEntry,
    resolve_model,
    get_pricing,
    extract_stats,
    calculate_cost,
    record_stats,
    format_tokens,
    format_duration,
)

# ---------------------------------------------------------------------------
# Sample fixture
# ---------------------------------------------------------------------------

SAMPLE_STREAM_JSON = "\n".join([
    '{"type":"assistant","content":"Hello"}',
    '{"type":"result","result":{"usage":{"input_tokens":1000,"output_tokens":500,"cache_read_input_tokens":200,"cache_creation_input_tokens":100}}}',
])


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------

def test_resolve_model_opus():
    assert resolve_model("opus") == "claude-opus-4-6"


def test_resolve_model_sonnet():
    assert resolve_model("sonnet") == "claude-sonnet-4-6"


def test_resolve_model_haiku():
    assert resolve_model("haiku") == "claude-haiku-4-5-20251001"


def test_resolve_model_passthrough():
    assert resolve_model("claude-opus-4-6[1m]") == "claude-opus-4-6[1m]"


# ---------------------------------------------------------------------------
# get_pricing
# ---------------------------------------------------------------------------

def test_get_pricing_opus():
    pricing = get_pricing("claude-opus-4-6")
    assert pricing.input_per_mtok == 15.00
    assert pricing.output_per_mtok == 75.00
    assert pricing.cache_read_per_mtok == 1.50
    assert pricing.cache_write_per_mtok == 18.75


def test_get_pricing_sonnet_default():
    pricing = get_pricing("unknown-model-xyz")
    assert pricing.input_per_mtok == 3.00
    assert pricing.output_per_mtok == 15.00
    assert pricing.cache_read_per_mtok == 0.30
    assert pricing.cache_write_per_mtok == 3.75


# ---------------------------------------------------------------------------
# extract_stats
# ---------------------------------------------------------------------------

def test_extract_stats_from_stream_json():
    stats = extract_stats(SAMPLE_STREAM_JSON)
    assert stats.input == 1000
    assert stats.output == 500
    assert stats.cache_read == 200
    assert stats.cache_write == 100


def test_extract_stats_no_result_event():
    output = '{"type":"assistant","content":"hi"}\n'
    stats = extract_stats(output)
    assert stats.input == 0
    assert stats.output == 0
    assert stats.cache_read == 0
    assert stats.cache_write == 0


def test_extract_stats_mixed_content():
    output = "\n".join([
        "not-json",
        '{"type":"assistant","content":"hi"}',
        "also not json",
        '{"type":"result","result":{"usage":{"input_tokens":42,"output_tokens":7,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}',
    ])
    stats = extract_stats(output)
    assert stats.input == 42
    assert stats.output == 7


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
    tokens = TokenStats(input=1_000_000, output=500_000, cache_read=200_000, cache_write=100_000)
    cost = calculate_cost(tokens, pricing)
    expected = (1_000_000 * 3.00 + 500_000 * 15.00 + 200_000 * 0.30 + 100_000 * 3.75) / 1_000_000
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
