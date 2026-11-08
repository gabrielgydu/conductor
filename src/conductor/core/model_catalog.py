"""Model catalog: the one place that knows Claude model aliases, IDs and prices.

Every alias lookup goes through resolve_model() here, so runner phases, fix
sessions, loop mode and the orchestrator agree on what "opus" means. (They
used to resolve through two copies of the map that disagreed on it.)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Short names accepted wherever conductor takes a model: presets, --model and
# run.sh's CONDUCTOR_MODEL. Anything that is not an alias is passed to
# `claude --model` unchanged, so full IDs such as "claude-sonnet-5-5" just work.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5-5",
    # Opus 5.5 only comes with a 1M context window, so this alias stays on 4.8.
    "opus-200k": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


@dataclass(frozen=True)
class ModelPricing:
    """List prices in USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


def _list_price(input_per_mtok: float, output_per_mtok: float, cache_read_per_mtok: float) -> ModelPricing:
    # Claude Code writes 1-hour prompt-cache entries (every cache write in the
    # CLI 2.1.304 transcripts is usage.cache_creation.ephemeral_1h_input_tokens),
    # which bill at twice the input price.
    return ModelPricing(input_per_mtok, output_per_mtok, cache_read_per_mtok, 2 * input_per_mtok)


# Known models and their Anthropic first-party list prices (as of 2026-10).
# Only a fallback: the CLI reports each session's cost itself
# (`total_cost_usd` on the result event), and that figure is authoritative.
KNOWN_MODELS: dict[str, ModelPricing] = {
    "claude-opus-4-8": _list_price(5.00, 25.00, 0.50),
    "claude-sonnet-4-6": _list_price(3.00, 15.00, 0.30),
    "claude-haiku-4-5": _list_price(1.00, 5.00, 0.10),
    "claude-opus-5-5": _list_price(4.00, 20.00, 0.20),
    "claude-sonnet-5-5": _list_price(2.00, 10.00, 0.20),
    # Prompts over 100K tokens bill at $0.50/$2.50, which session totals
    # cannot reveal, so estimates for long Haiku 5.5 sessions run low.
    "claude-haiku-5-5": _list_price(0.10, 0.50, 0.01),
    # Fable 5.1 cache reads cost a quarter of Fable 5's.
    "claude-fable-5-1": _list_price(10.00, 50.00, 0.25),
    "claude-fable-5": _list_price(10.00, 50.00, 1.00),
    "claude-opus-5": _list_price(5.00, 25.00, 0.50),
    "claude-sonnet-5": _list_price(2.00, 10.00, 0.20),
    "claude-opus-4-7": _list_price(5.00, 25.00, 0.50),
    "claude-opus-4-6": _list_price(5.00, 25.00, 0.50),
}

# Levels `claude --effort` accepts. Every session conductor starts passes one,
# so a run does not depend on the effortLevel in the user's settings.json. The
# CLI lowers a level the model lacks (Sonnet 4.6 stops at high) and drops it for
# models without effort (Haiku 4.5), so one level works for every session.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
# Carries the level to speccer and runner: tmux forwards CONDUCTOR_* variables.
EFFORT_ENV = "CONDUCTOR_EFFORT"

_SNAPSHOT_DATE = re.compile(r"-\d{8}$")


def resolve_model(name: str) -> str:
    """Resolve a short alias (case-insensitive) to a model ID; pass anything else through."""
    return MODEL_ALIASES.get(name.strip().lower(), name)


def set_effort(level: str | None) -> None:
    """Make *level* (or the default) the effort of every session from here on."""
    os.environ[EFFORT_ENV] = level or DEFAULT_EFFORT


def effort_args() -> list[str]:
    """The `claude` arguments that set this run's effort."""
    return ["--effort", os.environ.get(EFFORT_ENV) or DEFAULT_EFFORT]


def canonical_model_id(model: str) -> str:
    """Resolve an alias and drop what does not change the price.

    That is case, the "[1m]" context suffix and a "-YYYYMMDD" snapshot date
    (modelUsage keys look like "claude-haiku-4-5-20251001").
    """
    model_id = resolve_model(model).strip().lower().removesuffix("[1m]")
    return _SNAPSHOT_DATE.sub("", model_id)


def pricing_for(model: str) -> ModelPricing | None:
    """List prices for a model ID or alias, or None for a model we have no price for."""
    return KNOWN_MODELS.get(canonical_model_id(model))
