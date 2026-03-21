from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from conductor.core.enums import (
    RunStatus,
    StageStatus,
    SpeccerStatus,
    IntegrationStatus,
    BrainAction,
    FixerStatus,
)


class MonitorState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    ci_pass_count: int = 0
    ci_fail_count: int = 0
    last_checked_at: Optional[datetime] = None
    last_ci_url: Optional[str] = None


class ContextWiring(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    sources: list[str] = []
    targets: list[str] = []


class StageState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    name: str
    status: StageStatus = StageStatus.PENDING
    wiring: Optional[ContextWiring] = None
    attempt: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class RunState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    feature: str
    status: RunStatus = RunStatus.PENDING
    stages: list[StageState] = []
    monitor: Optional[MonitorState] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DomainState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    index: int
    name: str
    status: str = "pending"
    file: Optional[str] = None


class SpeccerState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    feature: str
    status: SpeccerStatus = SpeccerStatus.PENDING
    mode: Optional[str] = None
    preset: Optional[str] = None
    iteration: int = 0
    domains: list[DomainState] = []


class ConflictRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    file: str
    feature_a: str
    feature_b: str
    description: str


class E2ETestState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    last_run_at: Optional[datetime] = None


class IntegrationState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    status: IntegrationStatus = IntegrationStatus.PENDING
    conflicts: list[ConflictRecord] = []
    e2e: Optional[E2ETestState] = None
    branch: Optional[str] = None


class ConductorState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    check_interval_s: int = 900
    runs: list[RunState] = []
    integration: Optional[IntegrationState] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def atomic_save(state: BaseModel, path: Path) -> None:
    json_bytes = state.model_dump_json(indent=2).encode("utf-8")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, delete=False, suffix=".tmp"
        ) as f:
            tmp_path = Path(f.name)
            f.write(json_bytes)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def load_state(path: Path, model_class: type) -> BaseModel:
    data = path.read_text(encoding="utf-8")
    return model_class.model_validate_json(data)


def save_speccer_state(state: SpeccerState, json_path: Path, progress_md_path: Path) -> None:
    atomic_save(state, json_path)

    status_val = state.status
    if hasattr(status_val, "value"):
        status_str = status_val.value.upper()
    else:
        status_str = str(status_val).upper()

    rows = []
    for d in state.domains:
        file_display = d.file if d.file else "—"
        rows.append(f"| {d.index:02d} | {d.name} | {d.status} | {file_display} |")

    domain_table = "\n".join(rows) if rows else ""
    header = "| # | Domain | Status | File |\n|---|--------|--------|------|"
    if domain_table:
        table_section = f"{header}\n{domain_table}"
    else:
        table_section = header

    content = (
        f"STATUS: {status_str}\n"
        f"MODE: {state.mode}\n"
        f"PRESET: {state.preset}\n"
        f"ITERATION: {state.iteration}\n"
        f"\n"
        f"## Domain Progress\n"
        f"\n"
        f"{table_section}\n"
    )

    md_bytes = content.encode("utf-8")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=progress_md_path.parent, delete=False, suffix=".tmp"
        ) as f:
            tmp_path = Path(f.name)
            f.write(md_bytes)
        os.replace(tmp_path, progress_md_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
