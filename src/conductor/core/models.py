from __future__ import annotations

import os
import tempfile
from collections import deque
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

    stall_count: int = 0
    last_progress_hash: str | None = None
    last_check_ts: datetime | None = None
    last_heartbeat_ts: datetime | None = None
    retry_count: int = 0
    ci_pass_count: int = 0
    ci_fail_count: int = 0
    last_ci_url: str | None = None


class ContextWiring(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    sources: list[str] = []
    targets: list[str] = []


class StageState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    name: str
    spec_mode: str
    status: StageStatus = StageStatus.PENDING
    worktree: str | None = None
    branch: str | None = None
    context_wiring: ContextWiring | None = None
    pid: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    feature_suffix: str = ""
    feature_description_file: str | None = None
    retries: int = 0
    infra_retries: int = 0
    transient_retries: int = 0
    first_transient_failure_ts: datetime | None = None
    backoff_until: datetime | None = None
    last_exit_code: int | None = None


class RunState(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    index: int
    name: str
    description: str
    depends_on: list[int] = []
    constitution: list[str] = []
    stages: list[StageState] = []
    current_stage: int = 0
    status: RunStatus = RunStatus.PENDING
    monitor: MonitorState = MonitorState()
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DomainState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    index: int
    name: str
    status: str = "pending"
    file: Optional[str] = None


class SpeccerState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    feature_name: str
    status: SpeccerStatus = SpeccerStatus.INIT
    iteration: int = 0
    mode: str
    preset: str
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
    branch: str
    merged_runs: list[int] = []
    conflicts_resolved: list[ConflictRecord] = []
    conflicts_unresolved: list[ConflictRecord] = []
    e2e: Optional[E2ETestState] = None
    pr_url: str | None = None


class ConductorState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    project_name: str
    base_branch: str = "main"
    check_interval_s: int = 120
    runs: list[RunState] = []
    integration: Optional[IntegrationState] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    preset: Optional[str] = None
    overnight: bool = True
    quick: bool = False
    max_parallel: int = 1
    worktrees_base: Optional[str] = None


# ---------------------------------------------------------------------------
# DAG validation
# ---------------------------------------------------------------------------


def validate_dag(runs: list[RunState]) -> None:
    if not runs:
        return

    index_set = {run.index for run in runs}

    if len(index_set) != len(runs):
        seen: set[int] = set()
        for run in runs:
            if run.index in seen:
                raise ValueError(f"Duplicate run index: {run.index}")
            seen.add(run.index)

    for run in runs:
        if run.index in run.depends_on:
            raise ValueError(f"Run {run.index} depends on itself")
        for dep in run.depends_on:
            if dep not in index_set:
                raise ValueError(f"Run {run.index} depends on non-existent run {dep}")

    graph: dict[int, list[int]] = {run.index: [] for run in runs}
    in_degree: dict[int, int] = {run.index: 0 for run in runs}

    for run in runs:
        for dep in run.depends_on:
            graph[dep].append(run.index)
            in_degree[run.index] += 1

    queue: deque[int] = deque(idx for idx in index_set if in_degree[idx] == 0)
    processed = 0

    while queue:
        node = queue.popleft()
        processed += 1
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if processed != len(runs):
        unprocessed = sorted(idx for idx in index_set if in_degree[idx] > 0)
        raise ValueError(f"Dependency cycle detected among runs: {unprocessed}")


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def atomic_save(state: BaseModel, path: Path) -> None:
    json_bytes = state.model_dump_json(indent=2).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
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
