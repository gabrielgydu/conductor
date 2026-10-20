"""Stats & cost tracking for conductor."""
from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from conductor.core.model_catalog import ModelPricing, pricing_for

# Re-exported: the runner resolves phase models through conductor.core.stats.
from conductor.core.model_catalog import resolve_model as resolve_model


@dataclass
class TokenStats:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class StatsEntry:
    type: str
    iteration: int
    timestamp: str
    duration_s: float
    tokens: TokenStats
    cost_usd: float
    phase: str
    model: str


def get_pricing(model: str) -> ModelPricing | None:
    """List prices for a model, or None when the catalog has no price for it."""
    return pricing_for(model)


def find_result_event(stream_json_output: str) -> dict | None:
    """Return the last `result` event in Claude CLI stream-json output, or None.

    The CLI ends every `-p` session with one result event. Its `result` field is
    the final text (absent on some errors); `usage`, `total_cost_usd`,
    `modelUsage`, `num_turns`, `is_error` and `subtype` are top-level fields.
    See tests/fixtures/stream_json/ for captured examples.
    """
    for line in reversed(stream_json_output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def result_token_stats(result_event: dict | None) -> TokenStats:
    """Token counts from a result event's top-level `usage` (zeros if absent)."""
    usage = result_event.get("usage") if result_event else None
    if not isinstance(usage, dict):
        return TokenStats()
    return TokenStats(
        input=usage.get("input_tokens") or 0,
        output=usage.get("output_tokens") or 0,
        cache_read=usage.get("cache_read_input_tokens") or 0,
        cache_write=usage.get("cache_creation_input_tokens") or 0,
    )


def result_cost_usd(result_event: dict | None) -> float | None:
    """The CLI's own `total_cost_usd` for the session, or None if it reported none."""
    cost = result_event.get("total_cost_usd") if result_event else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return float(cost)


def extract_stats(stream_json_output: str) -> TokenStats:
    """Token usage from the result event in stream-json output (zeros if there is none)."""
    return result_token_stats(find_result_event(stream_json_output))


def extract_reported_cost(stream_json_output: str) -> float | None:
    """The CLI-reported session cost from stream-json output, or None."""
    return result_cost_usd(find_result_event(stream_json_output))


def calculate_cost(tokens: TokenStats, pricing: ModelPricing | None) -> float:
    """Calculate cost in USD from token counts and pricing.

    pricing=None (get_pricing knows no price for the model) gives 0.0 rather
    than a guess. Prefer the CLI-reported cost (result_cost_usd), and use
    estimate_cost, which returns None there, to tell "unknown" from "free".
    """
    if pricing is None:
        return 0.0
    return (
        tokens.input * pricing.input_per_mtok
        + tokens.output * pricing.output_per_mtok
        + tokens.cache_read * pricing.cache_read_per_mtok
        + tokens.cache_write * pricing.cache_write_per_mtok
    ) / 1_000_000


def estimate_cost(tokens: TokenStats, model: str | None) -> float | None:
    """Estimate a session's cost from list prices; None if the model has no known price.

    Only a fallback for when the CLI reported no total_cost_usd.
    """
    pricing = pricing_for(model) if model else None
    if pricing is None:
        return None
    return calculate_cost(tokens, pricing)


def _entry_to_dict(entry: StatsEntry) -> dict[str, Any]:
    d = asdict(entry)
    # flatten tokens dict
    tok = d.pop("tokens")
    d["tokens"] = tok
    return d


def record_stats(stats_path: Path, entry: StatsEntry) -> None:
    """Append a StatsEntry to a JSON array file, creating it if needed."""
    entries: list[dict] = []
    if stats_path.exists():
        try:
            entries = json.loads(stats_path.read_text("utf-8"))
            if not isinstance(entries, list):
                raise ValueError("not a list")
        except (json.JSONDecodeError, ValueError):
            warnings.warn(f"Corrupt stats file {stats_path}, replacing.", stacklevel=2)
            entries = []
    entries.append(_entry_to_dict(entry))
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(json.dumps(entries, indent=2), "utf-8")


def format_tokens(n: int) -> str:
    """Human-readable token count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_duration(seconds: float) -> str:
    """Human-readable duration."""
    s = int(seconds)
    if s >= 3600:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h{m:02d}m"
    if s >= 60:
        m = s // 60
        sec = s % 60
        return f"{m}m{sec:02d}s"
    return f"{s}s"
