"""Prompt template rendering for conductor."""
from __future__ import annotations

import re
from pathlib import Path


def load_template(path: Path) -> str:
    """Read a template file as UTF-8 text. Raises FileNotFoundError if missing."""
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_template(
    template: str,
    variables: dict[str, str] | None = None,
    conditions: dict[str, bool] | None = None,
    injections: dict[str, str] | None = None,
) -> str:
    """Render a template through three phases: conditionals → injections → variables."""
    vars_ = variables or {}
    conds = conditions or {}
    injs = injections or {}

    # Phase 1: Conditionals
    def _conditional(m: re.Match) -> str:
        name = m.group(1)
        content = m.group(2)
        return content if conds.get(name, False) else ""

    result = re.sub(
        r"\{IF (\w+)\}(.*?)\{ENDIF \1\}",
        _conditional,
        template,
        flags=re.DOTALL,
    )

    # Phase 2: Injections
    def _injection(m: re.Match) -> str:
        name = m.group(1)
        return injs.get(name, "")

    result = re.sub(r"\{INJECT:(\w+)\}", _injection, result)

    # Phase 3: Variables
    def _variable(m: re.Match) -> str:
        name = m.group(1)
        return vars_.get(name, m.group(0))

    result = re.sub(r"\{(\w+)\}", _variable, result)

    return result
