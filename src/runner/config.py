"""Run configuration model for the runner.

The speccer writes a RUN-CONFIG.json into the feature dir.
The runner reads it on startup to know which phases to execute.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PhaseConfig:
    """Single phase definition."""

    # Number (1-based)
    number: int
    # Human-readable name, e.g. "Backend implementation"
    name: str
    # Path to the prompt/plan file (absolute or relative to project_dir)
    prompt_file: str
    # Promise token Claude must emit to signal phase completion
    token: str
    # Optional per-phase model override
    model: Optional[str] = None


@dataclass
class RunConfig:
    """Top-level configuration for a runner invocation.

    Written by the speccer into:
      <storage_base>/features/<feature_name>/RUN-CONFIG.json
    """

    feature_name: str
    project_dir: str
    phases: list[PhaseConfig] = field(default_factory=list)

    # Model selection
    model: Optional[str] = None

    # Preset name (base / acme / nodeapp)
    preset: Optional[str] = None

    # Fixer enabled?
    fixer_enabled: bool = False

    # Max Claude iterations per phase before giving up
    max_iterations: int = 10

    # Max quality-gate retries per phase
    max_gate_retries: int = 3

    # Use steerable session?
    steerable: bool = False

    # Quick mode: quality gate only between phases, full CI+review at end
    quick: bool = False

    # Local CI config
    local_ci_enabled: bool = False
    local_ci_command: str = ""
    local_ci_full_command: str = ""
    local_ci_max_retries: int = 3

    # Local review config
    local_review_enabled: bool = False
    local_review_command: str = ""
    local_review_full_command: str = ""
    local_review_max_retries: int = 2

    # Model for fix loops
    fix_model: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "RunConfig":
        data = json.loads(path.read_text("utf-8"))
        phases = [
            PhaseConfig(
                number=p["number"],
                name=p["name"],
                prompt_file=p["prompt_file"],
                token=p["token"],
                model=p.get("model"),
            )
            for p in data.get("phases", [])
        ]
        return cls(
            feature_name=data["feature_name"],
            project_dir=data["project_dir"],
            phases=phases,
            model=data.get("model"),
            preset=data.get("preset"),
            fixer_enabled=data.get("fixer_enabled", False),
            max_iterations=data.get("max_iterations", 10),
            max_gate_retries=data.get("max_gate_retries", 3),
            steerable=data.get("steerable", False),
            quick=data.get("quick", False),
            local_ci_enabled=data.get("local_ci_enabled", False),
            local_ci_command=data.get("local_ci_command", ""),
            local_ci_full_command=data.get("local_ci_full_command", ""),
            local_ci_max_retries=data.get("local_ci_max_retries", 3),
            local_review_enabled=data.get("local_review_enabled", False),
            local_review_command=data.get("local_review_command", ""),
            local_review_full_command=data.get("local_review_full_command", ""),
            local_review_max_retries=data.get("local_review_max_retries", 2),
            fix_model=data.get("fix_model"),
        )

    def save(self, path: Path) -> None:
        data = {
            "feature_name": self.feature_name,
            "project_dir": self.project_dir,
            "model": self.model,
            "preset": self.preset,
            "fixer_enabled": self.fixer_enabled,
            "max_iterations": self.max_iterations,
            "max_gate_retries": self.max_gate_retries,
            "steerable": self.steerable,
            "quick": self.quick,
            "local_ci_enabled": self.local_ci_enabled,
            "local_ci_command": self.local_ci_command,
            "local_ci_full_command": self.local_ci_full_command,
            "local_ci_max_retries": self.local_ci_max_retries,
            "local_review_enabled": self.local_review_enabled,
            "local_review_command": self.local_review_command,
            "local_review_full_command": self.local_review_full_command,
            "local_review_max_retries": self.local_review_max_retries,
            "fix_model": self.fix_model,
            "phases": [
                {
                    "number": p.number,
                    "name": p.name,
                    "prompt_file": p.prompt_file,
                    "token": p.token,
                    "model": p.model,
                }
                for p in self.phases
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), "utf-8")
