"""Stats & cost tracking for conductor."""
from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


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


_PRICING_TABLE: list[tuple[str, ModelPricing]] = [
    ("opus", ModelPricing(15.00, 75.00, 1.50, 18.75)),
    ("haiku", ModelPricing(0.80, 4.00, 0.08, 1.00)),
]
_DEFAULT_PRICING = ModelPricing(3.00, 15.00, 0.30, 3.75)

_MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def resolve_model(alias: str) -> str:
    """Resolve short alias to full model name, or passthrough if not an alias."""
    return _MODEL_ALIASES.get(alias.lower(), alias)


def get_pricing(model: str) -> ModelPricing:
    """Return pricing for a model, defaulting to sonnet pricing for unknowns."""
    lower = model.lower()
    for pattern, pricing in _PRICING_TABLE:
        if pattern in lower:
            return pricing
    return _DEFAULT_PRICING


def extract_stats(stream_json_output: str) -> TokenStats:
    """Parse stream-json output and return token usage from the result event."""
    for line in stream_json_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if event.get("type") == "result":
            usage = event.get("result", {}).get("usage", {})
            return TokenStats(
                input=usage.get("input_tokens", 0),
                output=usage.get("output_tokens", 0),
                cache_read=usage.get("cache_read_input_tokens", 0),
                cache_write=usage.get("cache_creation_input_tokens", 0),
            )
    return TokenStats()


def calculate_cost(tokens: TokenStats, pricing: ModelPricing) -> float:
    """Calculate cost in USD from token counts and pricing."""
    return (
        tokens.input * pricing.input_per_mtok
        + tokens.output * pricing.output_per_mtok
        + tokens.cache_read * pricing.cache_read_per_mtok
        + tokens.cache_write * pricing.cache_write_per_mtok
    ) / 1_000_000


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
