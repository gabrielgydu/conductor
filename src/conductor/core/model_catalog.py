"""Model catalog: the one place that maps model aliases to Claude model IDs.

Every alias lookup goes through resolve_model() here, so runner phases, fix
sessions, loop mode and the orchestrator agree on what "opus" means. (They
used to resolve through two copies of the map that disagreed on it.)
"""
from __future__ import annotations

# Short names accepted wherever conductor takes a model: presets, --model and
# run.sh's CONDUCTOR_MODEL. Anything that is not an alias is passed to
# `claude --model` unchanged, so full IDs such as "claude-opus-5-5" just work.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-8[1m]",
    "opus-200k": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def resolve_model(name: str) -> str:
    """Resolve a short alias (case-insensitive) to a model ID; pass anything else through."""
    return MODEL_ALIASES.get(name.strip().lower(), name)
