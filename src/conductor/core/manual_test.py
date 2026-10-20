"""Manual-test orchestration mode.

This mode is intentionally separate from ``loop``: it tracks scenario coverage,
browser evidence, findings, policy shortcuts, and generation handoffs for long
manual QA runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, ConfigDict, Field

from conductor.core.claude import resolve_model
from conductor.core.launch import stream_filter_pipe
from conductor.core.logging import live_log
from conductor.core.models import atomic_save
from conductor.core.presets import ManualTestPolicy, load_preset
from conductor.core.storage import StorageResolver

if TYPE_CHECKING:
    from conductor.core.tmux import TmuxManager

MANUAL_STATE_FILE = "MANUAL-TEST-STATE.json"
MANUAL_LOG_FILE = "MANUAL-TEST-LOG.md"
MANUAL_AUDIT_FILE = "MANUAL-TEST-AUDIT.jsonl"
MANUAL_SCENARIOS_FILE = "MANUAL-TEST-SCENARIOS.md"
MANUAL_FINDINGS_FILE = "MANUAL-TEST-FINDINGS.md"
MANUAL_REPORT_FILE = "MANUAL-TEST-REPORT.md"

PASS_TAG = "manual-pass"
FINDING_TAG = "manual-finding"
BLOCKED_TAG = "manual-blocked"
HANDOFF_TAG = "manual-handoff"
SIGNAL_TAGS = {
    PASS_TAG: "pass",
    FINDING_TAG: "finding",
    BLOCKED_TAG: "blocked",
    HANDOFF_TAG: "handoff",
}

ALLOWED_SEVERITIES = {"high", "medium", "low", "ux"}
ALLOWED_CATEGORIES = {"app_bug", "ux", "test_gap", "flaky", "env", "data"}
TERMINAL_SCENARIO_STATUSES = {"passed", "failed", "blocked", "policy_violation"}
FIXABLE_FINDING_CATEGORIES = {"app_bug", "ux"}

# Claude infrastructure failures (no output, auth/model errors, usage limits)
# are retried after a pause; this many in a row pause the whole run instead.
MAX_CONSECUTIVE_INFRA_FAILURES = 3
INFRA_RETRY_DELAY_SECS = 60

# A scenario is blocked after this many generations without a valid signal, or
# after this many honest <manual-handoff/>s (long scenarios need several).
MAX_SCENARIO_INVALID_RESULTS = 3
MAX_SCENARIO_HANDOFFS = 5
# How much of the previous handoff file is inlined into the next prompt.
HANDOFF_EXCERPT_CHARS = 4000
# A finding is handed to a human after this many failed fixer attempts.
MAX_FIX_ATTEMPTS = 3


class ManualEvidence(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    summary: str = ""
    urls: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    traces: list[str] = Field(default_factory=list)
    mailhog_messages: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    db_readbacks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def has_any(self) -> bool:
        return any(
            [
                self.summary.strip(),
                self.urls,
                self.screenshots,
                self.traces,
                self.mailhog_messages,
                self.console_errors,
                self.db_readbacks,
                self.notes,
            ]
        )


class DataShortcut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    scenario_index: int = -1
    reason: str
    command_or_route: str
    user_flow_supported: str
    approved_by_policy: bool = False


class ManualFixAttempt(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    number: int
    # Why the attempt failed; empty while it runs and when it was verified.
    outcome: str = ""
    # Files the fixer changed, relative to the repo root.
    files: list[str] = Field(default_factory=list)
    # Stash commit holding a failed attempt's changes, and the ref keeping it
    # (off the user's stash list; empty if it could not be written).
    stash: str = ""
    stash_ref: str = ""
    # Cut short by a paused run: not the fixer's failure, so it does not count.
    interrupted: bool = False

    @property
    def failed(self) -> bool:
        return bool(self.outcome) and not self.interrupted

    @property
    def stash_label(self) -> str:
        return self.stash_ref or f"stash {self.stash[:12]}"


class ManualFinding(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    scenario_index: int
    severity: str
    category: str
    title: str
    reproduction: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""
    evidence: ManualEvidence | None = None
    suspected_files: list[str] = Field(default_factory=list)
    status: str = "open"
    fix_commits: list[str] = Field(default_factory=list)
    fix_attempts: list[ManualFixAttempt] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)


class ManualScenario(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    index: int
    area: str
    name: str
    description: str
    status: str = "pending"
    priority: str = "medium"
    # Generations run for this scenario; `handoffs` of them ended in a valid
    # <manual-handoff/>, the rest of the unfinished ones had no valid signal.
    attempts: int = 0
    handoffs: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    evidence: ManualEvidence | None = None
    findings: list[str] = Field(default_factory=list)
    shortcuts: list[DataShortcut] = Field(default_factory=list)
    handoff_notes: str = ""


class ManualGeneration(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    index: int
    started_at: datetime
    completed_at: datetime | None = None
    scenario_index: int | None = None
    handoff_file: str | None = None
    summary: str = ""


class ManualTestState(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    name: str
    project_dir: str
    plan_file: str
    preset: str | None = None
    status: str = "pending"
    # Status to return to when a "paused" run is resumed ("coverage" or "running").
    paused_from: str = ""
    last_error: str = ""
    consecutive_infra_failures: int = 0
    current_scenario_index: int = 0
    session_count: int = 0
    model: str | None = None
    max_turns: int = 200
    scenarios: list[ManualScenario] = Field(default_factory=list)
    findings: list[ManualFinding] = Field(default_factory=list)
    generations: list[ManualGeneration] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PolicyFlag(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    pattern: str
    value: str
    source: str = "tool"
    fatal_reason: str = ""
    # The fatal hit clearly made the forbidden change (see _fatal_tool_input),
    # rather than only naming the protected thing in a call that may just read it.
    fatal_mutation: bool = False


class ManualSignal(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    kind: str | None = None
    result_status: str | None = None
    valid: bool = False
    errors: list[str] = Field(default_factory=list)
    # Problems recorded for the audit log that do not invalidate the result.
    warnings: list[str] = Field(default_factory=list)
    evidence: ManualEvidence | None = None
    severity: str | None = None
    category: str | None = None
    title: str = ""
    reproduction: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""
    blocked_reason: str = ""
    code_read: str = ""
    data_attempts: str = ""
    why_not_fixable_now: str = ""
    suspected_files: list[str] = Field(default_factory=list)
    shortcuts: list[DataShortcut] = Field(default_factory=list)
    policy_flags: list[PolicyFlag] = Field(default_factory=list)
    raw_text: str = ""
    # Set by _run_generation when Claude itself failed (see _claude_failure);
    # such a generation says nothing about the scenario or finding.
    infra_error: str = ""


class ManualRunPaused(Exception):
    """Stop the run, resumably: raised when Claude keeps failing to run at all."""


def manual_state_path(conductor_dir: Path) -> Path:
    return conductor_dir / MANUAL_STATE_FILE


def manual_log_path(conductor_dir: Path) -> Path:
    return conductor_dir / MANUAL_LOG_FILE


def manual_audit_path(conductor_dir: Path) -> Path:
    return conductor_dir / MANUAL_AUDIT_FILE


def manual_scenarios_path(conductor_dir: Path) -> Path:
    return conductor_dir / MANUAL_SCENARIOS_FILE


def manual_findings_path(conductor_dir: Path) -> Path:
    return conductor_dir / MANUAL_FINDINGS_FILE


def manual_report_path(conductor_dir: Path) -> Path:
    return conductor_dir / MANUAL_REPORT_FILE


def ensure_manual_test_layout(conductor_dir: Path) -> None:
    conductor_dir.mkdir(parents=True, exist_ok=True)
    for relative in [
        "artifacts/screenshots",
        "artifacts/traces",
        "artifacts/mailhog",
        "artifacts/console",
        "generations",
        "logs",
    ]:
        (conductor_dir / relative).mkdir(parents=True, exist_ok=True)

    if not manual_log_path(conductor_dir).exists():
        manual_log_path(conductor_dir).write_text("# Manual Test Log\n", encoding="utf-8")
    if not manual_audit_path(conductor_dir).exists():
        manual_audit_path(conductor_dir).write_text("", encoding="utf-8")


def _split_table_cells(line: str) -> list[str]:
    """Cells of a markdown table row; ``\\|`` inside a cell is a literal pipe."""
    row = line.strip()
    row = row.removeprefix("|")
    row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _is_table_separator(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in cells)


_EVIDENCE_TARGET_PREFIX = "Primary evidence target: "


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _reindex_scenarios(scenarios: list[ManualScenario]) -> list[ManualScenario]:
    for index, scenario in enumerate(scenarios):
        scenario.index = index
        for shortcut in scenario.shortcuts:
            shortcut.scenario_index = index
    return scenarios


def _parse_table_scenarios(markdown_text: str) -> list[ManualScenario]:
    lines = markdown_text.splitlines()
    scenarios: list[ManualScenario] = []
    i = 0

    while i < len(lines) - 1:
        line = lines[i].strip()
        if not line.startswith("|"):
            i += 1
            continue

        header = _split_table_cells(line)
        separator = _split_table_cells(lines[i + 1])
        if not _is_table_separator(separator):
            i += 1
            continue

        normalized = [cell.lower().strip(" #") for cell in header]
        if "area" not in normalized or "scenario" not in normalized:
            i += 2
            continue

        area_idx = normalized.index("area")
        scenario_idx = normalized.index("scenario")
        evidence_idx = next(
            (idx for idx, cell in enumerate(normalized) if "evidence" in cell),
            None,
        )
        priority_idx = next(
            (idx for idx, cell in enumerate(normalized) if cell == "priority"),
            None,
        )
        status_idx = next(
            (idx for idx, cell in enumerate(normalized) if cell == "status"),
            None,
        )

        j = i + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = _split_table_cells(lines[j])
            if _is_table_separator(cells):
                j += 1
                continue
            if len(cells) <= max(area_idx, scenario_idx):
                j += 1
                continue

            area = _clean_cell(cells[area_idx])
            name = _clean_cell(cells[scenario_idx])
            if not area or not name:
                j += 1
                continue

            evidence_target = ""
            if evidence_idx is not None and len(cells) > evidence_idx:
                evidence_target = _clean_cell(cells[evidence_idx])
            priority = "medium"
            if priority_idx is not None and len(cells) > priority_idx:
                priority = _clean_cell(cells[priority_idx]).lower() or "medium"
            status = "pending"
            if status_idx is not None and len(cells) > status_idx:
                status = _clean_cell(cells[status_idx]).lower() or "pending"
                status = {
                    "[x]": "passed",
                    "[ ]": "pending",
                    "[>]": "in_progress",
                    "[!]": "failed",
                }.get(status, status)

            # The rendered matrix stores the description (with this prefix)
            # in its Evidence Target column; do not stack the prefix again.
            while evidence_target.startswith(_EVIDENCE_TARGET_PREFIX):
                evidence_target = evidence_target[len(_EVIDENCE_TARGET_PREFIX):].strip()
            description = (
                f"{_EVIDENCE_TARGET_PREFIX}{evidence_target}"
                if evidence_target
                else name
            )
            scenarios.append(
                ManualScenario(
                    index=len(scenarios),
                    area=area,
                    name=name,
                    description=description,
                    priority=priority,
                    status=status,
                )
            )
            j += 1

        i = j

    return _reindex_scenarios(scenarios)


_CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$", re.MULTILINE)


def _parse_checklist_scenarios(markdown_text: str) -> list[ManualScenario]:
    scenarios: list[ManualScenario] = []
    for match in _CHECKLIST_RE.finditer(markdown_text):
        text = _clean_cell(match.group(1))
        bold_match = re.match(r"\*\*(.+?)\*\*\s*[—–-]\s*(.+)", text, re.DOTALL)
        if bold_match:
            name = _clean_cell(bold_match.group(1))
            description = _clean_cell(bold_match.group(2))
        else:
            parts = re.split(r"\s+[—–-]\s+", text, maxsplit=1)
            name = _clean_cell(parts[0])
            description = _clean_cell(parts[1]) if len(parts) > 1 else name
        if not name:
            continue
        scenarios.append(
            ManualScenario(
                index=len(scenarios),
                area="Checklist",
                name=name,
                description=description,
            )
        )
    return _reindex_scenarios(scenarios)


def parse_scenarios_from_markdown(markdown_text: str) -> list[ManualScenario]:
    """Parse manual scenarios from a markdown table or fallback checklist."""
    table_scenarios = _parse_table_scenarios(markdown_text)
    if table_scenarios:
        return table_scenarios
    return _parse_checklist_scenarios(markdown_text)


def render_scenarios_markdown(state: ManualTestState) -> str:
    rows = []
    for scenario in state.scenarios:
        findings = ", ".join(scenario.findings) if scenario.findings else ""
        rows.append(
            "| "
            f"{scenario.index} | {scenario.status} | {scenario.priority} | "
            f"{_table_cell(scenario.area)} | {_table_cell(scenario.name)} | "
            f"{_table_cell(scenario.description)} | {findings} |"
        )

    body = "\n".join(rows) if rows else "| | | | | | | |"
    return (
        "# Manual Test Scenarios\n\n"
        "| # | Status | Priority | Area | Scenario | Evidence Target | Findings |\n"
        "|---|---|---|---|---|---|---|\n"
        f"{body}\n"
    )


def render_findings_markdown(state: ManualTestState) -> str:
    if not state.findings:
        return "# Manual Test Findings\n\nNo findings recorded.\n"

    sections = ["# Manual Test Findings\n"]
    for finding in state.findings:
        evidence_lines: list[str] = []
        if finding.evidence:
            evidence_lines = _format_evidence_lines(finding.evidence)
        sections.append(
            f"## {finding.id}: {finding.title}\n\n"
            f"- Scenario: {finding.scenario_index}\n"
            f"- Severity: {finding.severity}\n"
            f"- Category: {finding.category}\n"
            f"- Status: {finding.status}\n"
            f"- Expected: {finding.expected}\n"
            f"- Actual: {finding.actual}\n"
            f"- Reproduction: {'; '.join(finding.reproduction) if finding.reproduction else 'n/a'}\n"
            f"- Evidence: {'; '.join(evidence_lines) if evidence_lines else 'n/a'}\n"
        )
    return "\n".join(sections)


def save_manual_state(
    state: ManualTestState,
    conductor_dir: Path,
    *,
    write_markdown: bool = True,
) -> None:
    state.updated_at = datetime.now(timezone.utc)
    ensure_manual_test_layout(conductor_dir)
    atomic_save(state, manual_state_path(conductor_dir))
    if not write_markdown:
        return
    manual_scenarios_path(conductor_dir).write_text(
        render_scenarios_markdown(state), encoding="utf-8"
    )
    manual_findings_path(conductor_dir).write_text(
        render_findings_markdown(state), encoding="utf-8"
    )


def load_manual_state(conductor_dir: Path) -> ManualTestState:
    return ManualTestState.model_validate_json(
        manual_state_path(conductor_dir).read_text(encoding="utf-8")
    )


def initialize_manual_state(
    *,
    name: str,
    project_dir: Path,
    plan_file: Path,
    preset: str | None = None,
    model: str | None = None,
    max_turns: int = 200,
    conductor_dir: Path,
) -> ManualTestState:
    plan_path = plan_file if plan_file.is_absolute() else project_dir / plan_file
    if not plan_path.exists():
        raise FileNotFoundError(f"Plan file not found: {plan_path}")

    plan_content = plan_path.read_text(encoding="utf-8")
    scenarios = parse_scenarios_from_markdown(plan_content)
    if not scenarios:
        raise ValueError("Could not extract manual-test scenarios from plan")

    now = datetime.now(timezone.utc)
    state = ManualTestState(
        name=name,
        project_dir=str(project_dir),
        plan_file=str(plan_path),
        preset=preset,
        model=model,
        max_turns=max_turns,
        scenarios=scenarios,
        created_at=now,
        updated_at=now,
    )
    save_manual_state(state, conductor_dir)
    return state


def format_manual_status(state: ManualTestState) -> str:
    completed = sum(1 for s in state.scenarios if s.status == "passed")
    failed = sum(1 for s in state.scenarios if s.status == "failed")
    blocked = sum(1 for s in state.scenarios if s.status == "blocked")
    policy = sum(1 for s in state.scenarios if s.status == "policy_violation")

    lines = [
        f"Manual Test: {state.name}",
        f"Status: {state.status}",
        f"Sessions: {state.session_count}",
        f"Scenarios: {completed} passed, {failed} failed, {blocked} blocked, {policy} policy violations / {len(state.scenarios)} total",
        f"Findings: {len(state.findings)}",
        "",
    ]
    if state.last_error:
        lines.insert(2, f"Last error: {state.last_error}")
    for scenario in state.scenarios:
        marker = {
            "passed": "[x]",
            "in_progress": "[>]",
            "failed": "[!]",
            "blocked": "[b]",
            "policy_violation": "[p]",
        }.get(scenario.status, "[ ]")
        extra = f" ({len(scenario.findings)} findings)" if scenario.findings else ""
        lines.append(f"  {marker} {scenario.index}: {scenario.area} - {scenario.name}{extra}")
    return "\n".join(lines)


def _format_evidence_lines(evidence: ManualEvidence) -> list[str]:
    lines: list[str] = []
    if evidence.summary:
        lines.append(evidence.summary)
    lines.extend(f"url={url}" for url in evidence.urls)
    lines.extend(f"screenshot={path}" for path in evidence.screenshots)
    lines.extend(f"trace={path}" for path in evidence.traces)
    lines.extend(f"mail={msg}" for msg in evidence.mailhog_messages)
    lines.extend(f"console={err}" for err in evidence.console_errors)
    lines.extend(f"db={readback}" for readback in evidence.db_readbacks)
    lines.extend(evidence.notes)
    return lines


def generate_manual_report(state: ManualTestState) -> str:
    scenario_counts: dict[str, int] = {}
    for scenario in state.scenarios:
        scenario_counts[scenario.status] = scenario_counts.get(scenario.status, 0) + 1

    severity_counts: dict[str, int] = {}
    finding_status_counts: dict[str, int] = {}
    for finding in state.findings:
        severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1
        finding_status_counts[finding.status] = finding_status_counts.get(finding.status, 0) + 1

    shortcuts = [shortcut for scenario in state.scenarios for shortcut in scenario.shortcuts]
    evidence_count = sum(1 for scenario in state.scenarios if scenario.evidence and scenario.evidence.has_any())

    lines = [
        "# Manual Test Report",
        "",
        f"- Name: {state.name}",
        f"- Project: {state.project_dir}",
        f"- Plan: {state.plan_file}",
        f"- Status: {state.status}",
        f"- Sessions: {state.session_count}",
        f"- Evidence-bearing scenarios: {evidence_count}",
        "",
        "## Scenario Counts",
        "",
    ]
    if state.last_error:
        lines.insert(6, f"- Last error: {state.last_error}")
    if scenario_counts:
        for status in sorted(scenario_counts):
            lines.append(f"- {status}: {scenario_counts[status]}")
    else:
        lines.append("- none")

    lines.extend(["", "## Findings By Severity", ""])
    if severity_counts:
        for severity in sorted(severity_counts):
            lines.append(f"- {severity}: {severity_counts[severity]}")
    else:
        lines.append("- none")

    lines.extend(["", "## Findings By Status", ""])
    if finding_status_counts:
        for status in sorted(finding_status_counts):
            lines.append(f"- {status}: {finding_status_counts[status]}")
    else:
        lines.append("- none")

    lines.extend(["", "## Scenario Matrix", ""])
    lines.extend(render_scenarios_markdown(state).splitlines()[2:])

    lines.extend(["", "## Scenario Evidence", ""])
    evidence_rows = [
        (scenario, _format_evidence_lines(scenario.evidence))
        for scenario in state.scenarios
        if scenario.evidence and scenario.evidence.has_any()
    ]
    if evidence_rows:
        for scenario, evidence_lines in evidence_rows:
            lines.append(
                f"- Scenario {scenario.index}: {scenario.area} - {scenario.name}: "
                + "; ".join(evidence_lines)
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Findings", ""])
    if state.findings:
        for finding in state.findings:
            lines.append(
                f"- {finding.id}: {finding.severity}/{finding.category}, "
                f"scenario {finding.scenario_index}, {finding.status}: {finding.title}"
            )
            for attempt in finding.fix_attempts:
                if attempt.outcome:
                    stash = f"; changes in {attempt.stash_label}" if attempt.stash else ""
                    lines.append(f"  - fix attempt {attempt.number}: {attempt.outcome}{stash}")
    else:
        lines.append("- none")

    lines.extend(["", "## Shortcuts Used", ""])
    if shortcuts:
        for shortcut in shortcuts:
            approved = "approved" if shortcut.approved_by_policy else "not approved"
            lines.append(
                f"- Scenario {shortcut.scenario_index}: {approved}; "
                f"{shortcut.command_or_route}; reason: {shortcut.reason}; "
                f"supports: {shortcut.user_flow_supported}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Checks Run", ""])
    checks = [
        check
        for finding in state.findings
        for check in finding.verification
        if check.strip()
    ]
    if checks:
        for check in checks:
            lines.append(f"- {check}")
    else:
        lines.append("- none")

    lines.extend(["", "## Residual Risks", ""])
    open_items = [
        s for s in state.scenarios if s.status not in TERMINAL_SCENARIO_STATUSES
    ]
    if open_items:
        for scenario in open_items:
            lines.append(f"- Scenario {scenario.index} still {scenario.status}: {scenario.name}")
    else:
        lines.append("- none recorded")

    return "\n".join(lines).rstrip() + "\n"


def write_manual_report(state: ManualTestState, conductor_dir: Path) -> Path:
    path = manual_report_path(conductor_dir)
    path.write_text(generate_manual_report(state), encoding="utf-8")
    return path


def _stream_events(output: str) -> list[dict]:
    """The JSON object events of a stream-json transcript; other lines are skipped."""
    events: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _assistant_blocks(output: str) -> list[tuple[str, str]]:
    """Assistant ("text", text) and ("tool_use", tool name) blocks, in session order.

    Output that is not stream-json (or has no assistant text) is one text block.
    """
    blocks: list[tuple[str, str]] = []
    for event in _stream_events(output):
        if event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                blocks.append(("text", str(block.get("text", ""))))
            elif block.get("type") == "tool_use":
                blocks.append(("tool_use", str(block.get("name", ""))))
    if not any(kind == "text" for kind, _ in blocks):
        return [("text", output)]
    return blocks


def _extract_text_from_stream_json(output: str) -> str:
    return "\n".join(text for kind, text in _assistant_blocks(output) if kind == "text")


def _tag_values(text: str, tag: str) -> list[str]:
    values = []
    for match in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.DOTALL | re.I):
        values.append(match.group(1).strip())
    return values


def _tag_value(text: str, tag: str) -> str:
    values = _tag_values(text, tag)
    return values[0] if values else ""


def _manual_result_block(text: str, kind: str | None = None) -> tuple[str, str | None]:
    """The first <manual-result> whose status is *kind*, else the first one.

    Sessions sometimes draft a result of another kind before the final one,
    or report a finding and then summarise with a pass block. Several blocks
    of the same kind have so far been several findings, of which only the
    first is recorded.
    """
    blocks: list[tuple[str, str | None]] = []
    for match in re.finditer(r"<manual-result\b([^>]*)>(.*?)</manual-result>", text, re.DOTALL | re.I):
        status_match = re.search(r"status=[\"']([^\"']+)[\"']", match.group(1), re.I)
        status = status_match.group(1).strip().lower() if status_match else None
        blocks.append((match.group(2), status))
    if not blocks:
        return "", None
    return next((block for block in blocks if block[1] == kind), blocks[0])


_NESTED_RESULT_TAGS = ("shortcut", "data-shortcut", "evidence")


def _top_level(block: str) -> str:
    """*block* without nested <shortcut>/<evidence> elements, for top-level fields."""
    for tag in _NESTED_RESULT_TAGS:
        block = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", block, flags=re.DOTALL | re.I)
    return block


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "approved"}


# Empty tags and the template's "..." placeholders are not evidence.
_PLACEHOLDER_VALUE = re.compile(r"[.\s…]*")


def _evidence_values(block: str, *tags: str) -> list[str]:
    return [
        value
        for tag in tags
        for value in _tag_values(block, tag)
        if not _PLACEHOLDER_VALUE.fullmatch(value)
    ]


def _parse_evidence(block: str) -> ManualEvidence:
    evidence_block = _tag_value(block, "evidence") or _top_level(block)
    return ManualEvidence(
        summary=_tag_value(_top_level(block), "summary"),
        urls=_evidence_values(evidence_block, "url"),
        screenshots=_evidence_values(evidence_block, "screenshot"),
        traces=_evidence_values(evidence_block, "trace"),
        # <mail> for any mail catcher; <mailhog> is the older tag.
        mailhog_messages=_evidence_values(evidence_block, "mail", "mailhog", "mailhog_message"),
        console_errors=_evidence_values(evidence_block, "console", "console_error"),
        db_readbacks=_evidence_values(evidence_block, "db", "db_readback"),
        notes=_evidence_values(evidence_block, "note"),
    )


def _has_required_evidence(evidence: ManualEvidence | None, *, notes_count: bool = True) -> bool:
    """Whether *evidence* has something to show.

    A pass or finding needs an observation (URL, screenshot, trace, mail,
    console output or DB readback); a note alone is just a claim. A blocked
    result may rest on notes (``notes_count``).
    """
    if evidence is None:
        return False
    return any(
        [
            evidence.urls,
            evidence.screenshots,
            evidence.traces,
            evidence.mailhog_messages,
            evidence.console_errors,
            evidence.db_readbacks,
            evidence.notes if notes_count else [],
        ]
    )


def _parse_reproduction(block: str) -> list[str]:
    steps = _tag_values(block, "step")
    if steps:
        return [_clean_cell(step) for step in steps if step.strip()]
    raw = _tag_value(block, "reproduction")
    if not raw.strip():
        return []
    return [
        _clean_cell(line.lstrip("-0123456789. "))
        for line in raw.splitlines()
        if line.strip()
    ]


def _parse_shortcuts(block: str) -> list[DataShortcut]:
    shortcuts: list[DataShortcut] = []
    shortcut_blocks = _tag_values(block, "shortcut") + _tag_values(block, "data-shortcut")
    for shortcut_block in shortcut_blocks:
        reason = _tag_value(shortcut_block, "reason")
        command = _tag_value(shortcut_block, "command_or_route") or _tag_value(
            shortcut_block, "command"
        )
        user_flow = _tag_value(shortcut_block, "user_flow_supported") or _tag_value(
            shortcut_block, "user_flow"
        )
        approved = _parse_bool(_tag_value(shortcut_block, "approved_by_policy"))
        if reason or command or user_flow:
            shortcuts.append(
                DataShortcut(
                    reason=reason,
                    command_or_route=command,
                    user_flow_supported=user_flow,
                    approved_by_policy=approved,
                )
            )
    return shortcuts


_POLICY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "database cli",
        re.compile(r"\b(mysql|mariadb|mycli|psql|pgcli|sqlite3|mongo|mongosh|redis-cli)\b", re.I),
    ),
    (
        "mutating http",
        re.compile(
            r"\b(curl|httpie|wget)\b.*(\b(POST|PUT|PATCH|DELETE)\b"
            r"|(?-i:\s(-d|--data\S*|--json|-F|--form\S*|-T|--upload-file|--post-data|--post-file))(\s|=|$))",
            re.I | re.S,
        ),
    ),
    (
        "framework console",
        # Running the test suite (`artisan test --filter=<flag>`) is not a shortcut.
        re.compile(
            r"\b(artisan|bin/console|doctrine|manage\.py|django-admin|rake"
            r"|rails\s+(runner|r|console|c|dbconsole|db:\S+))\b(?!\s+(test|spec)\b)",
            re.I,
        ),
    ),
    (
        "fixture command",
        # A dump command, not json.dump() or PHP's dump()/var_dump().
        re.compile(r"\b(seed|seeder|factory|fixture|reset-db)\b|(?<![.\w])dump\b(?!\s*\()", re.I),
    ),
    (
        "browser storage mutation",
        re.compile(r"\b(localStorage|sessionStorage|document\.cookie)\b", re.I),
    ),
    (
        "test route",
        re.compile(r"(https?://\S*/_test/\S*|/_test/[A-Za-z0-9_./-]*|\b_test/[A-Za-z0-9_./-]+)", re.I),
    ),
]


def _fatal_policy_reason(value: str, policy: ManualTestPolicy | None = None) -> str:
    """Return the preset's reason when *value* matches a fatal policy pattern."""
    for fatal in (policy or ManualTestPolicy()).fatal_patterns:
        if re.search(fatal.pattern, value, re.I):
            return fatal.reason
    return ""


_MUTATING_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE)\b", re.I
)


def _is_auto_approved(flag: "PolicyFlag", policy: ManualTestPolicy | None = None) -> bool:
    """True when *flag* does not need an approved ``<shortcut>`` declaration.

    Fatal flags (the preset's ``[[manual_test.fatal_patterns]]``) are never
    approved. Every other flag — DB commands, test routes, mutating HTTP,
    fixture commands, browser storage — is approved by default, because the
    tester works inside a dev environment. A preset can demand an approved
    DataShortcut for each of them with ``auto_approve_shortcuts = false``.
    """
    if flag.fatal_reason:
        return False
    return (policy or ManualTestPolicy()).auto_approve_shortcuts


# Tool-input keys the built-in _POLICY_PATTERNS are matched against.
_POLICY_INPUT_KEYS = ("command", "cmd", "url", "query", "script", "code", "function", "sql")

# Tools whose inputs never trip a fatal pattern: they read code, keep todos,
# delegate (a subagent's own tool calls are scanned), or drive the UI that the
# policy asks for, so naming a protected flag there is expected.
_FATAL_EXEMPT_TOOLS = {
    "Read", "Grep", "Glob", "LS", "NotebookRead", "WebSearch", "Task", "Agent",
    "ExitPlanMode", "ToolSearch", "Skill", "TodoWrite", "TodoRead",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
}
_UI_ACTION_TOOL_RE = re.compile(
    r"browser_(click|type|fill_form|select_option|hover|drag|press_key|snapshot|"
    r"take_screenshot|wait_for|tabs?|close|resize|console_messages|network_requests|"
    r"handle_dialog|file_upload|navigate_back|navigate_forward)"
)
# Written content is checked when a later tool call runs or reads the file
# (e.g. Write x.sql, then `mysql app < x.sql`), not when it is written.
_FILE_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

_SHELL_OPERATOR_CHARS = frozenset(";&|<>()\n")
_READ_ONLY_PROGRAMS = {
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "cat", "head", "tail", "less", "more",
    "ls", "tree", "find", "wc", "sort", "uniq", "cut", "tr", "nl", "diff", "file", "stat",
    "pwd", "cd", "echo", "printf", "true", "jq", "which", "type",
}
_READ_ONLY_GIT_COMMANDS = {"grep", "log", "show", "diff", "blame", "status", "ls-files"}
# A database call is a readback when its SQL is inline and only reads.
_READ_SQL = re.compile(
    r"\bSELECT\b[\s\S]*\bFROM\b|\bSHOW\s+(TABLES|COLUMNS|CREATE|VARIABLES|DATABASES|INDEX)\b"
    r"|\b(DESCRIBE|EXPLAIN)\s+\w",
    re.I,
)
# SQL a shell command does not show: sourced, redirected in from a file (not
# a heredoc, here-string or process substitution) or piped into a client.
_SQL_SCRIPT = re.compile(
    r"\bsource\b|\\i\b|\.read\b|(?<!<)<(?![<(=>])|\|\s*\S*\b(mysql|mariadb|mycli|psql|pgcli|sqlite3)\b", re.I
)
# Browser-code tools (browser_evaluate, browser_run_code) act when the script
# writes storage or sends requests; reading a checkbox's state does not.
_BROWSER_CODE_TOOL_RE = re.compile(r"browser_(evaluate|run_code)")
_JS_MUTATION = re.compile(
    r"\b(localStorage|sessionStorage|document\.cookie|fetch|XMLHttpRequest|ajax|axios|sendBeacon)\b", re.I
)
# What makes a call clearly change state (see _fatal_tool_input). SQL writes
# are statement-shaped, so prose ("update the notes") and str.replace() do
# not count.
_SQL_WRITE = re.compile(
    r"\b(INSERT\s+(IGNORE\s+)?INTO|REPLACE\s+INTO|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+(TABLE|DATABASE|SCHEMA)"
    r"|TRUNCATE\s+(TABLE\s+)?[\w`\"]+|UPDATE\s+[\w`\".]+(\s+(AS\s+)?\w+)?\s+SET)\b",
    re.I,
)
_JS_WRITE = re.compile(
    r"\b(localStorage|sessionStorage)\s*(\.\s*(setItem|removeItem|clear)\s*\(|\.\s*\w+\s*=(?!=)|\[[^\]]*\]\s*=(?!=))"
    r"|\bdocument\.cookie\s*=(?!=)"
    r"|\bmethod\s*:\s*[\"'`](POST|PUT|PATCH|DELETE)\b"
    r"|\.open\(\s*[\"'`](POST|PUT|PATCH|DELETE)\b"
    r"|(\baxios|\bjQuery|\$)\s*\.\s*(post|put|patch|delete)\s*\("
    r"|\bsendBeacon\s*\(",
    re.I,
)
_MUTATING_HTTP = dict(_POLICY_PATTERNS)["mutating http"]
_TEST_ROUTE = dict(_POLICY_PATTERNS)["test route"]
_DB_CLIENTS = {"mysql", "mariadb", "mycli", "psql", "pgcli", "sqlite3", "mongo", "mongosh", "redis-cli"}
_HTTP_CLIENTS = {"curl", "wget", "http", "httpie", "xh"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_HEREDOC_RE = re.compile(
    r"(?<!<)<<-?[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\1(?P<rest>[^\n]*)\n"
    r"(?P<body>.*?)(?:\n[ \t]*\2[ \t]*(?=\n|$)|\Z)",
    re.S,
)
_FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
_COMMAND_PREFIXES = {"sudo", "env", "time", "nice", "nohup", "command", "builtin", "exec"}


def _shell_tokens(command: str) -> list[str] | None:
    """Shell words and operators of *command*, comments dropped; None if unparsable."""
    # A backslash-newline continues the command; shlex would make it a newline.
    command = command.replace("\\\n", " ")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="".join(_SHELL_OPERATOR_CHARS))
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return None


def _is_read_only_command(command: str) -> bool:
    """True for a shell command that only searches or reads.

    Covers grep/rg/cat-style programs (also behind xargs) and read-only git
    subcommands. Anything it cannot parse, any output redirection to a file,
    backtick substitution and any other program counts as not read-only.
    """
    tokens = _shell_tokens(command)
    if tokens is None or any("`" in token for token in tokens):
        return False

    for index, token in enumerate(tokens):
        if ">" in token and set(token) <= _SHELL_OPERATOR_CHARS:
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            if target != "/dev/null" and not target.isdigit():
                return False
    return all(_reads_only(words) for pipeline in _pipelines(tokens) for words in pipeline)


def _pipelines(tokens: list[str]) -> list[list[list[str]]]:
    """The pipelines in *tokens*, each a list of simple commands' words.

    A redirection's target stays with its command; a process substitution
    (`<(...)`) is a command of its own.
    """
    pipelines: list[list[list[str]]] = [[[]]]
    for token in tokens:
        if not set(token) <= _SHELL_OPERATOR_CHARS:
            pipelines[-1][-1].append(token)
        elif token in ("|", "|&"):
            pipelines[-1].append([])
        elif "(" in token or ")" in token or ("<" not in token and ">" not in token):
            pipelines.append([[]])
    return [[words for words in pipeline if words] for pipeline in pipelines if any(pipeline)]


def _reads_only(words: list[str]) -> bool:
    """True for a simple command (its shell words) that only searches or reads."""
    while words and (
        words[0] in _COMMAND_PREFIXES or re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0])
    ):
        words = words[1:]
    if not words:
        return True
    program, args = Path(words[0]).name, words[1:]
    if program == "git":
        return next((arg for arg in args if not arg.startswith("-")), "") in _READ_ONLY_GIT_COMMANDS
    if program == "find":
        return not _FIND_ACTIONS.intersection(args)
    if program == "xargs":
        return next((arg for arg in args if not arg.startswith("-")), "") in _READ_ONLY_PROGRAMS
    return program in _READ_ONLY_PROGRAMS


def _state_changing_calls(command: str, written: dict[str, str], depth: int = 0) -> list[str]:
    """What the shell command's clearly state-changing DB and HTTP calls send.

    A pipeline counts when it runs a DB client and its SQL (inline, piped in,
    a heredoc or a file in *written*) writes, or runs an HTTP client that
    sends a mutating request or calls a test route. A heredoc belongs to the
    command reading it, so notes or scripts written through one are not
    calls. A command string handed to a shell (`bash -c '...'`) or starting
    with a client (`docker exec app "curl ..."`) is looked into too.
    """
    bodies: dict[str, str] = {}

    def keep_body(match: re.Match[str]) -> str:
        key = f"heredoc-{len(bodies)}"
        bodies[key] = match.group("body")
        return f"<< {key}{match.group('rest')}"

    stripped = _HEREDOC_RE.sub(keep_body, command)
    tokens = _shell_tokens(stripped)
    calls: list[str] = []
    for pipeline in _pipelines(tokens if tokens is not None else stripped.split()):
        words = [word for simple in pipeline for word in simple]
        sent = " ".join(
            [*words, *(bodies[word] for word in words if word in bodies)]
            + [
                content
                for path, content in written.items()
                if any(path in word or Path(path).name in word for word in words)
            ]
        )
        acting = [simple for simple in pipeline if not _reads_only(simple)]
        programs = {Path(word).name for simple in acting for word in simple}
        if (programs & _DB_CLIENTS and _SQL_WRITE.search(sent)) or (
            programs & _HTTP_CLIENTS and (_MUTATING_HTTP.search(sent) or _TEST_ROUTE.search(sent))
        ):
            calls.append(sent)
        if depth >= 2:
            continue
        for simple in acting:
            for index, word in enumerate(simple):
                inner = word.split()
                if len(inner) < 2 or word in bodies:
                    continue
                handed_to_shell = (
                    index >= 2 and re.fullmatch(r"-\w*c", simple[index - 1]) is not None
                    and Path(simple[index - 2]).name in _SHELLS
                )
                if handed_to_shell or Path(inner[0]).name in _DB_CLIENTS | _HTTP_CLIENTS:
                    calls += _state_changing_calls(word, written, depth + 1)
    return calls


# Tool-input keys that describe a call rather than make it.
_TOOL_METADATA_KEYS = {"description", "timeout", "run_in_background", "activeForm"}


def _is_sql_readback(sql: str) -> bool:
    return bool(_READ_SQL.search(sql)) and not _MUTATING_SQL.search(sql)


def _is_acting_command(command: str, text: str) -> bool:
    """True when a shell command matches a shortcut detector (other than a DB readback).

    *text* is what runs (the command without comments). A DB call is a
    readback when all its SQL is in the command, heredocs included, and only
    reads.
    """
    without_heredocs = _HEREDOC_RE.sub(lambda match: "<< heredoc" + match.group("rest"), command)
    readback = _is_sql_readback(text) and not _SQL_SCRIPT.search(without_heredocs)
    return any(
        pattern.search(text) and not (label == "database cli" and readback)
        for label, pattern in _POLICY_PATTERNS
    )


def _string_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [
            leaf
            for key, item in value.items()
            if key not in _TOOL_METADATA_KEYS
            for leaf in _string_leaves(item)
        ]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


def _tool_uses(output: str) -> list[tuple[str, object]]:
    uses: list[tuple[str, object]] = []
    for event in _stream_events(output):
        if event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append((str(block.get("name", "")), block.get("input", {})))
    return uses


def _excerpt(value: str, pattern: str, width: int = 150) -> str:
    match = re.search(pattern, value, re.I)
    if match is None or len(value) <= 2 * width:
        return value
    start, end = max(match.start() - width, 0), match.end() + width
    return ("..." if start else "") + value[start:end] + ("..." if end < len(value) else "")


def _shorten(text: str, limit: int = 300) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _fatal_tool_input(
    name: str,
    tool_input: object,
    written: dict[str, str],
    policy: ManualTestPolicy,
    check_written_files: bool,
) -> tuple[str, str, bool]:
    """(reason, offending text, mutation) when a tool call matches a preset fatal pattern.

    Only calls that can change state count. Shell commands must match a
    shortcut detector (DB CLI other than a readback, mutating HTTP, framework
    console, fixtures, browser storage, test route); then the command and any
    file this session wrote that it refers to are checked. Browser-code tools
    count when the script writes storage or sends requests. Any other tool
    that does not just read or drive the UI is checked in full, minus SQL
    readbacks. A bare flag name in a pattern would otherwise reject every
    screenshot named after the flag and every grep or GET readback of it.

    *mutation* is True when the match is in what a clearly state-changing
    call sent: SQL that writes through a DB client or SQL tool, a mutating
    HTTP request or test-route call (see _state_changing_calls), or a
    browser script that writes storage or sends a mutating request. A
    console, script or readback that names the flag may only be reading it.
    """
    tool = name.rsplit("__", 1)[-1]
    if tool in _FATAL_EXEMPT_TOOLS or tool in _FILE_EDIT_TOOLS or _UI_ACTION_TOOL_RE.fullmatch(tool):
        return "", "", False
    # (text checked, what the call's state-changing parts sent)
    candidates: list[tuple[str, list[str]]]
    if tool == "Bash":
        command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else str(tool_input)
        # Match what runs: shell comments are not part of it.
        tokens = _shell_tokens(command)
        text = " ".join(tokens) if tokens is not None else command
        if _is_read_only_command(command) or not _is_acting_command(command, text):
            return "", "", False
        calls = _state_changing_calls(command, written if check_written_files else {})
        candidates = [(text, calls)]
        if check_written_files:
            candidates += [
                (content, calls)
                for path, content in written.items()
                if path in text or Path(path).name in text
            ]
    elif _BROWSER_CODE_TOOL_RE.fullmatch(tool):
        candidates = [
            (leaf, [leaf] if _JS_WRITE.search(leaf) else [])
            for leaf in _string_leaves(tool_input)
            if _JS_MUTATION.search(leaf)
        ]
    else:
        candidates = [
            (leaf, [leaf] if _SQL_WRITE.search(leaf) or _TEST_ROUTE.fullmatch(leaf.strip()) else [])
            for leaf in _string_leaves(tool_input)
            if not _is_sql_readback(leaf)
        ]
    # A clear mutation anywhere in the call outranks an earlier hit that may only read.
    first_hit = ("", "", False)
    for value, sent in candidates:
        for fatal in policy.fatal_patterns:
            if not re.search(fatal.pattern, value, re.I):
                continue
            if any(re.search(fatal.pattern, part, re.I) for part in sent):
                return fatal.reason, _excerpt(value, fatal.pattern), True
            if not first_hit[0]:
                first_hit = (fatal.reason, _excerpt(value, fatal.pattern), False)
    return first_hit


def scan_policy_shortcuts(
    output: str,
    policy: ManualTestPolicy | None = None,
    *,
    fatal_scope: str = "all",
) -> list[PolicyFlag]:
    """Flag shortcut-like tool calls and prose, and fatal policy violations.

    Built-in _POLICY_PATTERNS only flag (for the audit log, or for a strict
    preset's <shortcut> requirement). The preset's fatal patterns are checked
    against the inputs of every tool call that acts (see _fatal_tool_input),
    never against prose: describing a UI flow that names a protected flag is
    not a violation. *fatal_scope* is "all", "commands" (the fixer: editing
    code is its job, so content it wrote is not checked when it runs checks
    on it) or "none" (coverage, which only reads code and edits the matrix).
    """
    policy = policy or ManualTestPolicy()
    check_fatal = bool(policy.fatal_patterns) and fatal_scope != "none"
    flags: list[PolicyFlag] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, value: str, source: str, fatal_reason: str = "", fatal_mutation: bool = False) -> None:
        if (label, value) not in seen:
            seen.add((label, value))
            flags.append(
                PolicyFlag(
                    pattern=label, value=value, source=source,
                    fatal_reason=fatal_reason, fatal_mutation=fatal_mutation,
                )
            )

    written: dict[str, str] = {}
    for name, tool_input in _tool_uses(output):
        fatal_reason, fatal_value, fatal_mutation = "", "", False
        if check_fatal:
            fatal_reason, fatal_value, fatal_mutation = _fatal_tool_input(
                name, tool_input, written, policy, fatal_scope == "all"
            )
        if isinstance(tool_input, dict):
            values = [tool_input[key] for key in _POLICY_INPUT_KEYS if isinstance(tool_input.get(key), str)]
        else:
            values = [tool_input] if isinstance(tool_input, str) else []
        flagged = False
        for value in values:
            for label, pattern in _POLICY_PATTERNS:
                if pattern.search(value):
                    flagged = True
                    add(label, value, "tool", fatal_reason, fatal_mutation)
        if fatal_reason and not flagged:
            add("fatal pattern", fatal_value, "tool", fatal_reason, fatal_mutation)
        if name.rsplit("__", 1)[-1] in _FILE_EDIT_TOOLS and isinstance(tool_input, dict):
            path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
            if path:
                content = "\n".join(leaf for leaf in _string_leaves(tool_input) if leaf != path)
                written[path] = written.get(path, "") + "\n" + content

    text = _extract_text_from_stream_json(output)
    for label, pattern in _POLICY_PATTERNS:
        match = pattern.search(text)
        if match:
            add(label, match.group(0), "text")
    return flags


_SIGNAL_TAG_RE = re.compile(
    "<(" + "|".join(re.escape(tag) for tag in SIGNAL_TAGS) + r")\s*/>", re.I
)
# Mentions like `<manual-pass/>` in prose (often quoting a previous attempt)
# are not signals.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# What may follow the tag in its text block: a closing code fence or a full stop.
_AFTER_SIGNAL_RE = re.compile(r"[\s`.!]*")
# Tool calls that may follow the tag: todo bookkeeping, not work.
_BOOKKEEPING_TOOLS = {"TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}


def _without_inline_code(text: str) -> str:
    return _INLINE_CODE_RE.sub(lambda match: " " * len(match.group(0)), text)


def _find_signal_tag(output: str) -> tuple[str | None, list[str]]:
    """The authoritative signal tag of a session, and any errors about it.

    The last bare tag wins. It must end its text block, and no tool call may
    follow it; later text-only turns (e.g. background-task notices) are fine.
    Earlier mentions of the same tag are ignored; a different one makes the
    signal ambiguous (no tag).
    """
    blocks = _assistant_blocks(output)
    hits: list[tuple[int, int, str]] = []
    for index, (kind, text) in enumerate(blocks):
        if kind != "text":
            continue
        for match in _SIGNAL_TAG_RE.finditer(_without_inline_code(text)):
            hits.append((index, match.end(), match.group(1).lower()))
    if not hits:
        return None, ["missing required manual signal tag"]

    index, end, tag = hits[-1]
    conflicting = sorted({other for _, _, other in hits[:-1] if other != tag})
    if conflicting:
        return None, [
            f"expected exactly one manual signal tag, found: {', '.join(conflicting + [tag])}"
        ]
    trailing = _without_inline_code(blocks[index][1])[end:]
    later_tool_use = any(
        kind == "tool_use" and name not in _BOOKKEEPING_TOOLS for kind, name in blocks[index + 1:]
    )
    if not _AFTER_SIGNAL_RE.fullmatch(trailing) or later_tool_use:
        return tag, ["manual signal tag must be the final output"]
    return tag, []


def _shortcut_errors(shortcuts: list[DataShortcut]) -> list[str]:
    errors: list[str] = []
    for index, shortcut in enumerate(shortcuts, start=1):
        missing = []
        if not shortcut.reason.strip():
            missing.append("reason")
        if not shortcut.command_or_route.strip():
            missing.append("command_or_route")
        if not shortcut.user_flow_supported.strip():
            missing.append("user_flow_supported")
        if missing:
            errors.append(f"DataShortcut {index} missing {', '.join(missing)}")
        if not shortcut.approved_by_policy:
            errors.append(f"DataShortcut {index} must set approved_by_policy=true")
    return errors


def _shortcut_covers_flag(shortcut: DataShortcut, flag: PolicyFlag) -> bool:
    if not shortcut.approved_by_policy:
        return False
    command = shortcut.command_or_route.strip()
    if not command:
        return False
    return command == flag.value or flag.value in command or command in flag.value


def _is_missing_data_reason(reason: str, policy: ManualTestPolicy | None) -> bool:
    """True when a blocked reason only complains about missing data/fixtures."""
    words = (policy or ManualTestPolicy()).blocked_reason_words
    if not words:
        return False
    alternatives = "|".join(re.escape(w) for w in words)
    pattern = (
        rf"\b(no|missing|unavailable|lacking|without)\b.*\b({alternatives})s?\b"
        rf"|\b({alternatives})s?\b.*\b(missing|unavailable)\b"
    )
    return re.search(pattern, reason.strip(), re.I) is not None


def parse_manual_signal(
    output: str,
    policy_flags: list[PolicyFlag] | None = None,
    policy: ManualTestPolicy | None = None,
) -> ManualSignal:
    """Parse and validate a manual-test result from raw or stream-json output."""
    text = _extract_text_from_stream_json(output)
    signal = ManualSignal(raw_text=text, policy_flags=policy_flags or [])

    tag, tag_errors = _find_signal_tag(output)
    signal.errors.extend(tag_errors)
    if tag is None:
        return signal

    signal.kind = SIGNAL_TAGS[tag]
    block, result_status = _manual_result_block(text, signal.kind)
    signal.result_status = result_status

    fatal_flags = [flag for flag in signal.policy_flags if flag.fatal_reason]
    if fatal_flags:
        signal.errors.append(
            "policy scanner flagged forbidden shortcut: "
            + "; ".join(f"{flag.fatal_reason}: {_shorten(flag.value)}" for flag in fatal_flags)
        )

    if signal.kind == "handoff":
        signal.valid = not signal.errors
        return signal

    if not block:
        signal.errors.append("missing <manual-result> block")
        return signal
    if result_status != signal.kind:
        signal.errors.append(
            f"manual-result status must be {signal.kind!r}, got {result_status!r}"
        )

    signal.evidence = _parse_evidence(block)
    signal.shortcuts = _parse_shortcuts(block)
    # With auto-approval (the default) declaring a shortcut is optional, so an
    # incomplete or honest "not approved" declaration must not cost more than
    # staying silent would.
    if (policy or ManualTestPolicy()).auto_approve_shortcuts:
        signal.warnings.extend(_shortcut_errors(signal.shortcuts))
    else:
        signal.errors.extend(_shortcut_errors(signal.shortcuts))

    if signal.kind == "pass":
        if not _has_required_evidence(signal.evidence, notes_count=False):
            signal.errors.append(
                "pass result requires evidence beyond notes "
                "(url, screenshot, trace, mail, console or db_readback)"
            )

    elif signal.kind == "finding":
        top = _top_level(block)
        signal.severity = _tag_value(top, "severity").lower()
        signal.category = _tag_value(top, "category").lower()
        signal.title = _tag_value(top, "title")
        signal.reproduction = _parse_reproduction(top)
        signal.expected = _tag_value(top, "expected")
        signal.actual = _tag_value(top, "actual")
        signal.suspected_files = _tag_values(top, "suspected_file")

        if signal.severity not in ALLOWED_SEVERITIES:
            signal.errors.append("finding severity must be high, medium, low, or ux")
        if signal.category not in ALLOWED_CATEGORIES:
            signal.errors.append(
                "finding category must be app_bug, ux, test_gap, flaky, env, or data"
            )
        if not signal.title.strip():
            signal.errors.append("finding requires a title")
        if not signal.reproduction:
            signal.errors.append("finding requires reproduction steps")
        if not signal.expected.strip():
            signal.errors.append("finding requires expected behavior")
        if not signal.actual.strip():
            signal.errors.append("finding requires actual behavior")
        if not _has_required_evidence(signal.evidence, notes_count=False):
            signal.errors.append(
                "finding requires evidence beyond notes "
                "(url, screenshot, trace, mail, console or db_readback)"
            )

    elif signal.kind == "blocked":
        top = _top_level(block)
        signal.blocked_reason = _tag_value(top, "reason")
        signal.code_read = _tag_value(top, "code_read")
        signal.data_attempts = _tag_value(top, "data_attempts")
        signal.why_not_fixable_now = _tag_value(top, "why_not_fixable_now")
        if not signal.blocked_reason.strip():
            signal.errors.append("blocked result requires a reason")
        if not signal.code_read.strip():
            signal.errors.append("blocked result requires code_read proof")
        if not signal.data_attempts.strip():
            signal.errors.append("blocked result requires data_attempts proof")
        if not signal.why_not_fixable_now.strip():
            signal.errors.append("blocked result requires why_not_fixable_now")
        if not _has_required_evidence(signal.evidence):
            signal.errors.append("blocked result requires evidence")
        if _is_missing_data_reason(signal.blocked_reason, policy):
            signal.errors.append("blocked reason cannot be only missing data")

    missing_shortcuts = [
        flag
        for flag in signal.policy_flags
        if not flag.fatal_reason  # already an error above
        and not _is_auto_approved(flag, policy)
        and not any(_shortcut_covers_flag(shortcut, flag) for shortcut in signal.shortcuts)
    ]
    if missing_shortcuts:
        signal.errors.append(
            "policy scanner flagged shortcuts without approved DataShortcut: "
            + "; ".join(flag.value for flag in missing_shortcuts)
        )

    signal.valid = not signal.errors
    return signal


def build_coverage_prompt(
    state: ManualTestState,
    plan_content: str,
    conductor_dir: Path,
    policy: ManualTestPolicy | None = None,
) -> str:
    scenarios_path = manual_scenarios_path(conductor_dir)
    policy = policy or ManualTestPolicy()
    focus_section = f"## Project Focus\n\n{policy.coverage_focus}\n\n" if policy.coverage_focus else ""
    return f"""\
You are the coverage discovery generation for Conductor manual-test mode.

Target project: {state.project_dir}
Manual-test state: {manual_state_path(conductor_dir)}
Scenario matrix: {scenarios_path}
Artifacts directory: {conductor_dir / 'artifacts'}

## Job

Discover the complete user-facing surface of the feature under test before execution.
Read routes, controllers, components, templates, tests, notes, and relevant configuration. Extend
the scenario matrix in {scenarios_path} when the plan misses user-facing behavior.

Do not execute browser scenarios in this generation. This generation is for coverage
only. Preserve existing scenario rows unless they are obvious duplicates.

## Coverage Rules

1. Read code before claiming behavior.
2. Prefer real UI surfaces over test helpers when identifying scenarios.
3. Include setup/admin flows, permission and capability gates, notification/document
   flows, list and detail behavior, retry/error handling, edge cases, and UX audit areas.
4. Keep scenarios atomic enough that one later generation can execute one scenario.
5. End with exactly <manual-handoff/> after updating the scenario matrix.

{focus_section}## Original Plan

{plan_content}
"""


def build_scenario_prompt(
    state: ManualTestState,
    plan_content: str,
    scenario: ManualScenario,
    conductor_dir: Path,
    policy: ManualTestPolicy | None = None,
) -> str:
    artifacts_dir = conductor_dir / "artifacts"
    handoff_section = _scenario_handoff_section(scenario)
    policy = policy or ManualTestPolicy()
    project_rules = f"\n{policy.policy_text}" if policy.policy_text else ""
    if policy.auto_approve_shortcuts:
        shortcut_rule = (
            "- Declare each SQL/test-route/mutating HTTP shortcut in the result with a <shortcut>\n"
            "  entry, for the audit report:"
        )
    else:
        shortcut_rule = (
            "- Every SQL/test-route/mutating HTTP shortcut must include a <shortcut> entry in the\n"
            "  result, with approved_by_policy=true, or the result is rejected:"
        )
    return f"""\
You are a senior manual QA engineer running Conductor manual-test mode.

Target project: {state.project_dir}
Manual-test state file: {manual_state_path(conductor_dir)}
Scenario index: {scenario.index}
Area: {scenario.area}
Scenario: {scenario.name}
Description: {scenario.description}
Artifacts directory: {artifacts_dir}

{handoff_section}

## Required Behavior

1. Execute this scenario through the real UI/browser wherever the UI behavior is under test.
2. Read relevant code before claiming behavior or declaring blocked.
3. Create missing data when practical. "No test data" is not a valid block by itself.
4. Record evidence for every pass, finding, or blocked result. Save screenshots, traces,
   mail-catcher exports, console logs, or DB/API readbacks under the artifacts directory.
5. Distinguish app bugs, UX issues, flaky tests, data issues, and environment issues.
6. If you find a bug or UX issue, produce a structured finding instead of silently fixing it.

## Data Setup Policy

- UI behavior under test must be executed through the UI.
- Configuration that users normally change through the UI must be changed through the UI
  at least once.
- SQL/test routes may be used only for non-user-facing preconditions, bulk fixture creation,
  cleanup, or impossible setup.
{shortcut_rule}
  <shortcut><reason>...</reason><command_or_route>...</command_or_route><user_flow_supported>...</user_flow_supported><approved_by_policy>true</approved_by_policy></shortcut>
- If a UI setup step fails, record a finding. Do not silently bypass it with SQL.{project_rules}

## Required Final Tags

CRITICAL: Your output must contain EXACTLY ONE signal tag, and it must be the very last
non-whitespace content you produce. Do NOT emit the tag more than once. Do NOT place any
text after the tag. Duplicate tags cause validation failure.

End with exactly one of these tags:
<manual-pass/>
<manual-finding/>
<manual-blocked/>
<manual-handoff/>

Pass format:
<manual-result status="pass">
  <scenario>{scenario.index}: {scenario.name}</scenario>
  <summary>...</summary>
  <evidence>
    <url>...</url>
    <screenshot>...</screenshot>
    <trace>...</trace>
    <mail>...</mail>
    <db_readback>...</db_readback>
    <note>...</note>
  </evidence>
</manual-result>
<manual-pass/>

Finding format:
<manual-result status="finding">
  <severity>high|medium|low|ux</severity>
  <category>app_bug|ux|test_gap|flaky|env|data</category>
  <title>...</title>
  <reproduction><step>...</step></reproduction>
  <expected>...</expected>
  <actual>...</actual>
  <evidence>...</evidence>
</manual-result>
<manual-finding/>

Blocked format:
<manual-result status="blocked">
  <reason>...</reason>
  <code_read>files inspected</code_read>
  <data_attempts>what was attempted</data_attempts>
  <why_not_fixable_now>...</why_not_fixable_now>
  <evidence><screenshot>...</screenshot><note>...</note></evidence>
</manual-result>
<manual-blocked/>

If context is running out, write a concise handoff summary and end with <manual-handoff/>.

## Original Plan

{plan_content}
"""


def build_fixer_prompt(
    state: ManualTestState,
    plan_content: str,
    finding: ManualFinding,
    conductor_dir: Path,
    policy: ManualTestPolicy | None = None,
) -> str:
    policy = policy or ManualTestPolicy()
    # The fixer's commands are checked against the preset's fatal patterns too.
    project_rules = f"\n## Project Rules\n\n{policy.policy_text}\n" if policy.policy_text else ""
    evidence = _format_evidence_lines(finding.evidence) if finding.evidence else []
    attempts = [attempt for attempt in finding.fix_attempts if attempt.outcome]
    previous = ""
    if attempts:
        rows = []
        for attempt in attempts:
            row = f"- Attempt {attempt.number}: {attempt.outcome}"
            if attempt.stash:
                row += (
                    f"\n  Its changes are stashed: `git stash show -p --include-untracked {attempt.stash}`"
                )
            rows.append(row)
        previous = (
            "\n## Previous Fix Attempts\n\n"
            "These attempts failed and were taken out of the worktree. Do not repeat them blindly.\n\n"
            + "\n".join(rows)
            + "\n"
        )
    return f"""\
You are fixing a manual-test finding with minimal code changes.

Target project: {state.project_dir}
Finding: {finding.id} - {finding.title}
Severity/category: {finding.severity}/{finding.category}
Scenario: {finding.scenario_index}
Expected: {finding.expected}
Actual: {finding.actual}
Reproduction: {'; '.join(finding.reproduction)}
Evidence: {'; '.join(evidence) if evidence else 'n/a'}
Artifacts directory: {conductor_dir / 'artifacts'}

Read code first, make the smallest correct fix, and run the relevant focused checks.
Do not mark this verified; a verifier generation must rerun the scenario.

Do not commit; Conductor commits the fix once a verifier generation confirms it.

Summarize the changed files and the checks you ran, then put <manual-handoff/> on
the last line, with nothing after it.
{project_rules}{previous}
## Original Plan

{plan_content}
"""


def build_verifier_prompt(
    state: ManualTestState,
    plan_content: str,
    finding: ManualFinding,
    conductor_dir: Path,
    policy: ManualTestPolicy | None = None,
) -> str:
    policy = policy or ManualTestPolicy()
    project_rules = f"\n## Project Rules\n{policy.policy_text}\n" if policy.policy_text else ""
    return f"""\
You are verifying a fixed manual-test finding.

Target project: {state.project_dir}
Finding: {finding.id} - {finding.title}
Scenario: {finding.scenario_index}
Reproduction to rerun: {'; '.join(finding.reproduction)}
Artifacts directory: {conductor_dir / 'artifacts'}

Rerun the exact reproduction through the UI/browser, then run the relevant focused checks.
Record fresh evidence. End with a full manual-result block plus <manual-pass/> if verified
or <manual-finding/> if it still fails.
{project_rules}
Pass format:
<manual-result status="pass">
  <summary>...</summary>
  <evidence><url>...</url><screenshot>...</screenshot><note>...</note></evidence>
</manual-result>
<manual-pass/>

Finding format:
<manual-result status="finding">
  <severity>high|medium|low|ux</severity>
  <category>app_bug|ux|test_gap|flaky|env|data</category>
  <title>...</title>
  <reproduction><step>...</step></reproduction>
  <expected>...</expected>
  <actual>...</actual>
  <evidence><screenshot>...</screenshot><note>...</note></evidence>
</manual-result>
<manual-finding/>

## Original Plan

{plan_content}
"""


def _log(
    event: str,
    message: str,
    log_path: Path | None,
    audit_path: Path | None,
    **kw,
) -> None:
    live_log(event, message, audit_data=kw or None, log_path=log_path, audit_path=audit_path)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "manual-test"


def _output_tail(text: str, limit: int = 120) -> str:
    lines = _extract_text_from_stream_json(text).splitlines()
    # The next generation reads this file; a quoted signal tag there tends to
    # come back as a duplicate tag in its own output.
    return _SIGNAL_TAG_RE.sub(r"[\1]", "\n".join(lines[-limit:]))


def _scenario_handoff_section(scenario: ManualScenario) -> str:
    if not scenario.handoff_notes.strip():
        return ""

    content, handoff_path = "", None
    match = re.search(r"(/\S*generation-\d{3}-handoff\.md)", scenario.handoff_notes)
    if match:
        handoff_path = Path(match.group(1))
        if handoff_path.exists():
            content = handoff_path.read_text(encoding="utf-8", errors="replace")

    section = f"## Previous Handoff\n\n{scenario.handoff_notes}\n"
    if content:
        # The newest output, including any handoff summary, is at the end.
        if len(content) > HANDOFF_EXCERPT_CHARS:
            content = (
                f"...(earlier output omitted; Read {handoff_path} for all of it)\n"
                + content[-HANDOFF_EXCERPT_CHARS:]
            )
        section += f"\n```markdown\n{content}\n```\n"
    return section + "\n"


def _next_open_scenario(state: ManualTestState) -> ManualScenario | None:
    for scenario in state.scenarios:
        if scenario.status not in TERMINAL_SCENARIO_STATUSES:
            return scenario
    return None


def _finding_id(state: ManualTestState) -> str:
    return f"MT-{len(state.findings) + 1:03d}"


def _finding_from_signal(
    state: ManualTestState,
    scenario: ManualScenario,
    signal: ManualSignal,
) -> ManualFinding:
    return ManualFinding(
        id=_finding_id(state),
        scenario_index=scenario.index,
        severity=signal.severity or "medium",
        category=signal.category or "app_bug",
        title=signal.title,
        reproduction=signal.reproduction,
        expected=signal.expected,
        actual=signal.actual,
        evidence=signal.evidence,
        suspected_files=signal.suspected_files,
    )


def detect_relevant_check_commands(
    changed_files: list[str], policy: ManualTestPolicy | None = None
) -> list[list[str]]:
    """Return focused verification commands for changed target-project files.

    Rules come from the preset's ``[[manual_test.check_commands]]``; ``{path}``
    in a command is replaced with the matching changed file.
    """
    rules = (policy or ManualTestPolicy()).check_commands
    commands: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    for file_name in changed_files:
        path = file_name.strip()
        if not path:
            continue
        for rule in rules:
            if not rule.matches(path):
                continue
            command = [part.replace("{path}", path) for part in rule.argv]
            key = tuple(command)
            if key not in seen:
                seen.add(key)
                commands.append(command)

    return commands


# Output browser and test tooling leaves in the target repo while testing:
# Playwright CLI/MCP page snapshots and Playwright's report directories.
_TEST_ARTIFACT_PATTERNS = re.compile(
    r"(^|/)(\.playwright-(cli|mcp)|test-results|playwright-report|blob-report)/"
    r"|(^|/)page-\d{4}-\d\d-\d\dT[\d-]+Z?\.(ya?ml|png|jpe?g)$"
    r"|\.yml\.snap$"
)


def _is_test_artifact(path: str) -> bool:
    return bool(_TEST_ARTIFACT_PATTERNS.search(path))


# Paths are passed to git as literal names, never as globs.
_GIT = ["git", "--literal-pathspecs"]


def _git_root(cwd: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, check=False
    )
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else cwd


def _git_changed_files(cwd: Path) -> list[str]:
    """Every path with staged, unstaged or untracked changes, relative to the repo root.

    Files inside untracked directories are listed one by one, and names come
    back unquoted, so paths with spaces or non-ASCII characters work.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    return sorted({os.fsdecode(entry[3:]) for entry in result.stdout.split(b"\0") if len(entry) > 3})


def _format_files_for_log(files: list[str], limit: int = 12) -> str:
    if len(files) <= limit:
        return ", ".join(files)
    return ", ".join(files[:limit]) + f", ... ({len(files)} total)"


def _run_relevant_checks(
    cwd: Path, changed_files: list[str], policy: ManualTestPolicy | None = None
) -> tuple[bool, list[str]]:
    commands = detect_relevant_check_commands(changed_files, policy)
    if not commands:
        return True, ["No focused check commands detected for changed files"]

    passed = True
    summaries: list[str] = []
    for command in commands:
        command_text = " ".join(shlex.quote(part) for part in command)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            passed = False
            summaries.append(f"{command_text}: failed ({exc})")
            continue

        output = (result.stdout + result.stderr).strip().splitlines()
        tail = " | ".join(output[-8:]) if output else "no output"
        summaries.append(f"{command_text}: exit {result.returncode}; {tail}")
        if result.returncode != 0:
            passed = False

    return passed, summaries


def _commit_verified_finding(
    cwd: Path,
    finding: ManualFinding,
    changed_files: list[str],
) -> str | None:
    """Commit exactly *changed_files* (root-relative) and return the short hash.

    Anything else the user has staged stays staged and out of the commit.
    Returns None when none of the files has changes left. If staging or the
    commit fails (a pre-commit hook, say), the files are unstaged again so a
    later commit cannot sweep them in, and CalledProcessError propagates.
    """
    root = _git_root(cwd)
    pending = set(_git_changed_files(root))
    files = [path for path in changed_files if path in pending]
    if not files:
        return None
    # New and modified files must be added first; deletions commit as they are.
    present = [path for path in files if os.path.lexists(root / path)]
    message = (
        f"Fix {finding.id}: {finding.title}\n\n"
        f"Verified manual-test scenario {finding.scenario_index}."
    )
    try:
        if present:
            subprocess.run([*_GIT, "add", "-A", "--", *present], cwd=root, check=True, capture_output=True)
        subprocess.run(
            [*_GIT, "commit", "--only", "-m", message, "--", *files],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run([*_GIT, "reset", "-q", "--", *files], cwd=root, check=False, capture_output=True)
        raise
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def _stash_files(cwd: Path, files: list[str], message: str, ref: str) -> tuple[str, str]:
    """Stash the changes to *files* (new files included) and keep the stash as *ref*.

    Returns (stash commit, ref), or ("", "") when there was nothing to stash.
    The stash then leaves the user's stash list, so their `git stash pop`
    cannot apply it; if *ref* cannot be written it stays there (ref "").
    """
    root = _git_root(cwd)

    def stash_head() -> str:
        result = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "refs/stash"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()

    before = stash_head()
    subprocess.run(
        [*_GIT, "stash", "push", "--include-untracked", "-m", message, "--", *files],
        cwd=root,
        check=True,
        capture_output=True,
    )
    after = stash_head()
    if after == before:
        return "", ""
    try:
        subprocess.run(
            ["git", "update-ref", "--create-reflog", "-m", message, ref, after],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return after, ""
    subprocess.run(["git", "stash", "drop", "-q"], cwd=root, check=False, capture_output=True)
    return after, ref


def _process_error_text(exc: subprocess.CalledProcessError) -> str:
    output = exc.stderr or exc.stdout or b""
    text = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
    return _clip(text, 300) or str(exc)


def _scenario_for_finding(
    state: ManualTestState,
    finding: ManualFinding,
) -> ManualScenario | None:
    return next(
        (scenario for scenario in state.scenarios if scenario.index == finding.scenario_index),
        None,
    )


def _finding_by_id(state: ManualTestState, finding_id: str) -> ManualFinding | None:
    return next((finding for finding in state.findings if finding.id == finding_id), None)


def _scenario_has_open_findings(state: ManualTestState, scenario: ManualScenario) -> bool:
    for finding_id in scenario.findings:
        finding = _finding_by_id(state, finding_id)
        if finding and finding.status not in {"verified", "wont_fix"}:
            return True
    return False


def _should_fix_finding(finding: ManualFinding) -> bool:
    """Open, fixable and not out of attempts.

    "needs_human" (attempts exhausted) is not retried but still counts as an
    open finding for its scenario.
    """
    return (
        finding.status in {"open", "fixing"}
        and finding.category in FIXABLE_FINDING_CATEGORIES
        and finding.severity in {"high", "medium", "low", "ux"}
        and sum(1 for attempt in finding.fix_attempts if attempt.failed) < MAX_FIX_ATTEMPTS
    )


def _write_generation_handoff(
    *,
    state: ManualTestState,
    conductor_dir: Path,
    generation: ManualGeneration,
    scenario: ManualScenario | None,
    reason: str,
    output_text: str,
) -> Path:
    scenario_line = (
        f"Scenario {scenario.index}: {scenario.area} - {scenario.name}"
        if scenario
        else "Coverage discovery"
    )
    path = conductor_dir / "generations" / f"generation-{generation.index:03d}-handoff.md"
    path.write_text(
        "# Manual Test Generation Handoff\n\n"
        f"- Generation: {generation.index}\n"
        f"- Reason: {reason}\n"
        f"- {scenario_line}\n"
        f"- State: {state.status}\n\n"
        "## Output Tail\n\n"
        "```text\n"
        f"{_output_tail(output_text)}\n"
        "```\n",
        encoding="utf-8",
    )
    generation.handoff_file = str(path)
    return path


# API errors that say nothing about the work: the call was refused or failed.
# Context exhaustion ("Prompt is too long", reported as invalid_request) and
# error_max_turns are deliberately absent: those sessions did real work and ran
# out of budget, which is an attempt like any other.
_INFRA_API_ERRORS = {"authentication_failed", "billing_error", "rate_limit", "server_error"}


def _clip(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def _claude_failure(output: str, exit_code: int, stderr: str) -> str:
    """Describe a Claude infrastructure failure, or return "" for a real attempt.

    It is an infrastructure failure when the model never produced a turn of its
    own (empty output, bad model, auth error, usage limit before the first
    turn), or when the session ended on an API error from _INFRA_API_ERRORS.
    """
    worked = False
    error_kind = ""
    error_text = ""
    for event in _stream_events(output):
        if event.get("type") == "assistant":
            message = event.get("message") or {}
            blocks = [b for b in message.get("content") or [] if isinstance(b, dict)]
            if event.get("error") or message.get("model") == "<synthetic>":
                error_kind = str(event.get("error") or "")
                error_text = "\n".join(str(b.get("text", "")) for b in blocks) or error_text
            elif any(b.get("type") in {"text", "tool_use"} for b in blocks):
                worked = True
        elif event.get("type") == "result" and event.get("is_error"):
            if event.get("subtype") == "success" and isinstance(event.get("result"), str):
                error_text = event["result"] or error_text

    detail = _clip(error_text or stderr) or "no output"
    if not worked:
        return f"exit {exit_code}: {detail}"
    if error_kind in _INFRA_API_ERRORS:
        return f"{error_kind}: {detail}"
    return ""


def _read_generation_result(
    exit_file: Path, output_file: Path, stderr_file: Path
) -> tuple[int, str, str]:
    try:
        exit_code = int(exit_file.read_text().strip())
    except (OSError, ValueError):
        exit_code = 1
    output_text = ""
    if output_file.exists():
        output_text = output_file.read_text(encoding="utf-8", errors="replace")
    stderr_text = ""
    if stderr_file.exists():
        stderr_text = stderr_file.read_text(encoding="utf-8", errors="replace")
        if not stderr_text.strip():
            stderr_file.unlink()
    return exit_code, output_text, stderr_text


async def _run_generation(
    *,
    state: ManualTestState,
    project_dir: Path,
    conductor_dir: Path,
    tmux: "TmuxManager",
    prompt: str,
    label: str,
    model: str,
    scenario: ManualScenario | None,
    log_path: Path,
    audit_path: Path,
    write_markdown_on_finish: bool = True,
    policy: ManualTestPolicy | None = None,
    fatal_scope: str = "all",
) -> tuple[ManualSignal, str]:
    from conductor.core.loop import _wait_for_exit

    state.session_count += 1
    generation = ManualGeneration(
        index=state.session_count,
        started_at=datetime.now(timezone.utc),
        scenario_index=scenario.index if scenario else None,
    )
    state.generations.append(generation)
    # A coverage generation edits MANUAL-TEST-SCENARIOS.md itself; re-rendering
    # it here would wipe a previous coverage attempt's matrix on retry.
    save_manual_state(state, conductor_dir, write_markdown=write_markdown_on_finish)

    label_safe = _safe_name(label)
    prompt_file = conductor_dir / "logs" / f"manual-prompt-{generation.index:03d}-{label_safe}.md"
    output_file = conductor_dir / "logs" / f"manual-output-{generation.index:03d}-{label_safe}.jsonl"
    stderr_file = conductor_dir / "logs" / f"manual-stderr-{generation.index:03d}-{label_safe}.log"
    # Per run directory, so two projects using the same --name cannot collide.
    exit_file = conductor_dir / "logs" / f"manual-exit-{generation.index:03d}-{label_safe}"
    prompt_file.write_text(prompt, encoding="utf-8")
    exit_file.unlink(missing_ok=True)
    output_file.unlink(missing_ok=True)

    quoted_prompt = shlex.quote(str(prompt_file))
    quoted_exit = shlex.quote(str(exit_file))
    quoted_stderr = shlex.quote(str(stderr_file))
    pipeline = (
        f"claude -p - --dangerously-skip-permissions "
        f"--max-turns {state.max_turns} --model {shlex.quote(model)} "
        f"--output-format stream-json --verbose < {quoted_prompt} 2> {quoted_stderr} "
        f"| {stream_filter_pipe(output_file)}; "
        f"echo ${{PIPESTATUS[0]}} > {quoted_exit}"
    )
    wrapped_cmd = f"bash -lc {shlex.quote(pipeline)}"
    window_name = f"manual-{generation.index:03d}-{label_safe}"[:80]

    _log(
        "MANUAL_GENERATION_START",
        f"Generation {generation.index}: {label}",
        log_path,
        audit_path,
        scenario_index=scenario.index if scenario else None,
    )
    tmux_log = conductor_dir / "logs" / f"tmux-{generation.index:03d}-{label_safe}.log"
    await tmux.spawn_in_window(
        window_name,
        wrapped_cmd,
        cwd=str(project_dir),
        detached=True,
        log_file=tmux_log,
    )
    await _wait_for_exit(exit_file, output_file, tmux, window_name, log_path, audit_path)
    exit_code, output_text, stderr_text = _read_generation_result(exit_file, output_file, stderr_file)
    # The window stays open after exit (remain-on-exit), keeping its pipe-pane
    # helper alive; everything it showed is in the output, stderr and tmux logs.
    await tmux.kill_window(window_name)

    policy_flags = scan_policy_shortcuts(output_text, policy, fatal_scope=fatal_scope)
    signal = parse_manual_signal(output_text, policy_flags=policy_flags, policy=policy)
    if not signal.valid:
        signal.infra_error = _claude_failure(output_text, exit_code, stderr_text)
    if signal.infra_error:
        state.consecutive_infra_failures += 1
    else:
        state.consecutive_infra_failures = 0
    generation.completed_at = datetime.now(timezone.utc)
    generation.summary = "infra-error" if signal.infra_error else (signal.kind or "no-signal")

    outcome = (
        f"Claude failed ({signal.infra_error})"
        if signal.infra_error
        else f"signal {signal.kind}, valid={signal.valid}"
    )
    if signal.warnings:
        outcome += f"; warnings: {'; '.join(signal.warnings)}"
    _log(
        "MANUAL_GENERATION_END",
        f"Generation {generation.index}: exit {exit_code}, {outcome}",
        log_path,
        audit_path,
        exit_code=exit_code,
        signal=signal.kind,
        valid=signal.valid,
        errors=signal.errors,
        warnings=signal.warnings,
        infra_error=signal.infra_error,
        auto_approved=[
            f"{flag.pattern}: {flag.value}"
            for flag in policy_flags
            if _is_auto_approved(flag, policy)
        ],
    )

    exit_file.unlink(missing_ok=True)
    save_manual_state(state, conductor_dir, write_markdown=write_markdown_on_finish)
    return signal, output_text


async def _run_generation_retrying(**kwargs) -> tuple[ManualSignal, str]:
    """Run a generation, retrying it while Claude itself fails to run.

    Raises ManualRunPaused after MAX_CONSECUTIVE_INFRA_FAILURES failures in a
    row, so a usage limit or auth error stops the run instead of being counted
    against scenarios and findings.
    """
    state: ManualTestState = kwargs["state"]
    while True:
        signal, output_text = await _run_generation(**kwargs)
        if not signal.infra_error:
            return signal, output_text
        if state.consecutive_infra_failures >= MAX_CONSECUTIVE_INFRA_FAILURES:
            raise ManualRunPaused(
                f"{state.consecutive_infra_failures} Claude failures in a row; "
                f"last ({kwargs['label']}): {signal.infra_error}"
            )
        _log(
            "MANUAL_INFRA_RETRY",
            f"{kwargs['label']}: Claude failed ({signal.infra_error}); "
            f"retrying in {INFRA_RETRY_DELAY_SECS}s",
            kwargs["log_path"],
            kwargs["audit_path"],
            error=signal.infra_error,
            consecutive=state.consecutive_infra_failures,
        )
        await asyncio.sleep(INFRA_RETRY_DELAY_SECS)


def _apply_signal_to_scenario(
    *,
    state: ManualTestState,
    conductor_dir: Path,
    scenario: ManualScenario,
    generation: ManualGeneration,
    signal: ManualSignal,
    output_text: str,
) -> None:
    now = datetime.now(timezone.utc)
    for shortcut in signal.shortcuts:
        shortcut.scenario_index = scenario.index
    scenario.shortcuts.extend(signal.shortcuts)

    fatal_flags = [flag for flag in signal.policy_flags if flag.fatal_reason]
    if any(flag.fatal_mutation for flag in fatal_flags):
        # The forbidden change has already happened, so a retry would only
        # test against polluted state (and could then "pass"). Any other
        # fatal hit (e.g. a console script that may only read the flag) just
        # invalidates the result, which is retried below like any other.
        scenario.status = "policy_violation"
        scenario.evidence = ManualEvidence(
            summary="Generation used a shortcut the preset forbids",
            notes=[f"{flag.fatal_reason}: {_shorten(flag.value)}" for flag in fatal_flags],
        )
        scenario.completed_at = now
        state.current_scenario_index = scenario.index + 1
        return

    if signal.kind == "pass" and signal.valid:
        scenario.status = "passed"
        scenario.evidence = signal.evidence
        scenario.completed_at = now
        state.current_scenario_index = scenario.index + 1
        return

    if signal.kind == "finding" and signal.valid:
        finding = _finding_from_signal(state, scenario, signal)
        state.findings.append(finding)
        scenario.status = "failed"
        scenario.evidence = signal.evidence
        scenario.findings.append(finding.id)
        scenario.completed_at = now
        state.current_scenario_index = scenario.index + 1
        return

    if signal.kind == "blocked" and signal.valid:
        scenario.status = "blocked"
        evidence = signal.evidence or ManualEvidence()
        if not evidence.summary:
            evidence.summary = signal.blocked_reason
        evidence.notes.extend(
            [
                f"Code read: {signal.code_read}",
                f"Data attempts: {signal.data_attempts}",
                f"Why not fixable now: {signal.why_not_fixable_now}",
            ]
        )
        scenario.evidence = evidence
        scenario.completed_at = now
        state.current_scenario_index = scenario.index + 1
        return

    reason = "handoff" if signal.kind == "handoff" and signal.valid else "; ".join(signal.errors)
    handoff = _write_generation_handoff(
        state=state,
        conductor_dir=conductor_dir,
        generation=generation,
        scenario=scenario,
        reason=reason or "no valid manual signal",
        output_text=output_text,
    )
    scenario.handoff_notes = f"See {handoff}"
    if signal.kind == "handoff" and signal.valid:
        scenario.handoffs += 1
    invalid_results = scenario.attempts - scenario.handoffs
    if scenario.handoffs >= MAX_SCENARIO_HANDOFFS:
        summary = f"Scenario handed off {scenario.handoffs} times without reaching a result"
        notes = [f"Last handoff: {handoff}"]
    elif invalid_results >= MAX_SCENARIO_INVALID_RESULTS:
        summary = f"Scenario did not produce a valid result after {invalid_results} attempts"
        notes = signal.errors or ["No valid manual signal"]
    else:
        scenario.status = "pending"
        return
    scenario.status = "blocked"
    scenario.evidence = ManualEvidence(summary=summary, notes=notes)
    scenario.completed_at = now
    state.current_scenario_index = scenario.index + 1


async def _fix_and_verify_finding(
    *,
    state: ManualTestState,
    project_dir: Path,
    conductor_dir: Path,
    tmux: "TmuxManager",
    plan_content: str,
    finding: ManualFinding,
    log_path: Path,
    audit_path: Path,
    model: str,
    policy: ManualTestPolicy | None = None,
) -> None:
    if not _should_fix_finding(finding):
        return

    preexisting_files = set(_git_changed_files(project_dir))
    # Browser tooling leaves untracked snapshots behind; anything else that is
    # already dirty would be mixed into (or hidden from) the fixer's commit.
    dirty_files = sorted(f for f in preexisting_files if not _is_test_artifact(f))
    if dirty_files:
        finding.status = "open"
        finding.verification.append(
            "Skipped automatic fix: worktree has pre-existing changes "
            f"({_format_files_for_log(dirty_files)}); commit or stash them first"
        )
        _log(
            "MANUAL_FIX_SKIPPED",
            f"{finding.id}: worktree has pre-existing changes",
            log_path,
            audit_path,
            finding_id=finding.id,
            files=dirty_files,
        )
        save_manual_state(state, conductor_dir)
        return

    attempt = ManualFixAttempt(number=len(finding.fix_attempts) + 1)
    finding.fix_attempts.append(attempt)
    finding.status = "fixing"
    save_manual_state(state, conductor_dir)

    def fail(outcome: str, interrupted: bool = False) -> None:
        attempt.interrupted = interrupted
        _shelve_failed_fix(
            state=state,
            project_dir=project_dir,
            conductor_dir=conductor_dir,
            finding=finding,
            attempt=attempt,
            outcome=outcome,
            files=_fixer_changed_files(project_dir, preexisting_files),
            log_path=log_path,
            audit_path=audit_path,
        )

    try:
        signal, output_text = await _run_generation_retrying(
            state=state,
            project_dir=project_dir,
            conductor_dir=conductor_dir,
            tmux=tmux,
            prompt=build_fixer_prompt(state, plan_content, finding, conductor_dir, policy=policy),
            label=f"fix-{finding.id}",
            model=model,
            scenario=_scenario_for_finding(state, finding),
            log_path=log_path,
            audit_path=audit_path,
            policy=policy,
            fatal_scope="commands",
        )
    except ManualRunPaused as exc:
        # Claude could not run, which is not the fixer's failure. Clear the
        # tree so the next attempt starts clean on resume.
        fail(f"Interrupted, the run paused: {exc}", interrupted=True)
        raise
    generation = state.generations[-1]
    if not signal.valid:
        _write_generation_handoff(
            state=state,
            conductor_dir=conductor_dir,
            generation=generation,
            scenario=_scenario_for_finding(state, finding),
            reason="fixer generation did not produce valid handoff signal",
            output_text=output_text,
        )
        detail = f": {'; '.join(signal.errors)}" if signal.errors else ""
        fail(f"Fixer did not produce a valid handoff signal{detail}")
        return

    fixer_files = _fixer_changed_files(project_dir, preexisting_files)
    attempt.files = fixer_files
    if not fixer_files:
        fail("Fixer produced no new file changes")
        return

    checks_passed, check_summaries = _run_relevant_checks(project_dir, fixer_files, policy=policy)
    finding.verification.extend(check_summaries)
    if not checks_passed:
        failed_checks = [line for line in check_summaries if ": exit 0;" not in line]
        fail(_clip("Focused checks failed: " + " / ".join(failed_checks), 600))
        return

    # Persisted with attempt.files, so a run that stops before the verifier
    # finishes verifies this fix on resume (_resume_verification).
    finding.status = "fixed"
    save_manual_state(state, conductor_dir)
    await _verify_and_commit(
        state=state,
        project_dir=project_dir,
        conductor_dir=conductor_dir,
        tmux=tmux,
        plan_content=plan_content,
        finding=finding,
        fix_files=fixer_files,
        fail=fail,
        log_path=log_path,
        audit_path=audit_path,
        model=model,
        policy=policy,
    )


async def _resume_verification(
    *,
    state: ManualTestState,
    project_dir: Path,
    conductor_dir: Path,
    tmux: "TmuxManager",
    plan_content: str,
    finding: ManualFinding,
    log_path: Path,
    audit_path: Path,
    model: str,
    policy: ManualTestPolicy | None = None,
) -> None:
    """Verify a "fixed" finding whose verification never finished (killed or paused run)."""
    attempt = finding.fix_attempts[-1] if finding.fix_attempts else None
    if attempt is None or not attempt.files:
        # Fixed before fix files were recorded: there is nothing safe to commit.
        finding.status = "open"
        finding.verification.append(
            "Verification was interrupted and the fix's files are unknown; "
            "check the worktree for its changes"
        )
        save_manual_state(state, conductor_dir)
        return

    def fail(outcome: str) -> None:
        pending = set(_git_changed_files(project_dir))
        _shelve_failed_fix(
            state=state,
            project_dir=project_dir,
            conductor_dir=conductor_dir,
            finding=finding,
            attempt=attempt,
            outcome=outcome,
            files=[path for path in attempt.files if path in pending],
            log_path=log_path,
            audit_path=audit_path,
        )

    _log(
        "FINDING_FIX_RETRY",
        f"Verifying {finding.id} after an interrupted verification: {finding.title}",
        log_path,
        audit_path,
        finding_id=finding.id,
        files=attempt.files,
    )
    await _verify_and_commit(
        state=state,
        project_dir=project_dir,
        conductor_dir=conductor_dir,
        tmux=tmux,
        plan_content=plan_content,
        finding=finding,
        fix_files=attempt.files,
        fail=fail,
        log_path=log_path,
        audit_path=audit_path,
        model=model,
        policy=policy,
    )


async def _verify_and_commit(
    *,
    state: ManualTestState,
    project_dir: Path,
    conductor_dir: Path,
    tmux: "TmuxManager",
    plan_content: str,
    finding: ManualFinding,
    fix_files: list[str],
    fail: Callable[[str], None],
    log_path: Path,
    audit_path: Path,
    model: str,
    policy: ManualTestPolicy | None = None,
) -> None:
    """Rerun the finding's scenario on the fix; commit it if it passes, else *fail*."""
    # A pause here leaves the finding "fixed"; resume verifies it again.
    verifier_signal, verifier_output = await _run_generation_retrying(
        state=state,
        project_dir=project_dir,
        conductor_dir=conductor_dir,
        tmux=tmux,
        prompt=build_verifier_prompt(state, plan_content, finding, conductor_dir, policy=policy),
        label=f"verify-{finding.id}",
        model=model,
        scenario=_scenario_for_finding(state, finding),
        log_path=log_path,
        audit_path=audit_path,
        policy=policy,
    )
    verifier_generation = state.generations[-1]
    scenario = _scenario_for_finding(state, finding)

    if verifier_signal.kind == "pass" and verifier_signal.valid:
        finding.status = "verified"
        finding.verification.append("Verifier scenario passed")
        if verifier_signal.evidence:
            finding.verification.extend(_format_evidence_lines(verifier_signal.evidence))
        if scenario and not _scenario_has_open_findings(state, scenario):
            scenario.status = "passed"
            scenario.evidence = verifier_signal.evidence
            scenario.completed_at = datetime.now(timezone.utc)

        try:
            commit_hash = _commit_verified_finding(project_dir, finding, fix_files)
        except subprocess.CalledProcessError as exc:
            commit_hash = None
            error = _process_error_text(exc)
            # Verification did pass, so the status stays; the fix is left
            # unstaged in the worktree for the user to commit.
            finding.verification.append(
                f"Commit failed, fix left uncommitted ({_format_files_for_log(fix_files)}): {error}"
            )
            _log(
                "MANUAL_COMMIT_FAILED",
                f"{finding.id}: commit failed, fix left uncommitted: {error}",
                log_path,
                audit_path,
                finding_id=finding.id,
                files=fix_files,
                error=error,
            )
        if commit_hash:
            finding.fix_commits.append(commit_hash)
        save_manual_state(state, conductor_dir)
        return

    if verifier_signal.kind == "finding" and verifier_signal.valid:
        fail(f"Verifier still failing: {verifier_signal.title or verifier_signal.actual}")
        return

    _write_generation_handoff(
        state=state,
        conductor_dir=conductor_dir,
        generation=verifier_generation,
        scenario=scenario,
        reason="verifier generation did not produce valid pass/finding signal",
        output_text=verifier_output,
    )
    fail("Verifier did not produce a valid pass/finding signal: " + "; ".join(verifier_signal.errors))


def _fixer_changed_files(project_dir: Path, preexisting_files: set[str]) -> list[str]:
    return [
        path
        for path in _git_changed_files(project_dir)
        if path not in preexisting_files and not _is_test_artifact(path)
    ]


def _shelve_failed_fix(
    *,
    state: ManualTestState,
    project_dir: Path,
    conductor_dir: Path,
    finding: ManualFinding,
    attempt: ManualFixAttempt,
    outcome: str,
    files: list[str],
    log_path: Path,
    audit_path: Path,
) -> None:
    """End a failed fix attempt: stash its *files* and reopen (or give up on) the finding.

    The fixer only runs on a tree without other changes, so its changes are
    exactly the non-artifact files that changed since. Stashing them gives
    the next attempt a clean tree (and later scenarios working code), keeps
    the attempt recoverable under refs/conductor/manual-test/ (not on the
    user's stash list), and lets the next fixer prompt point at it.
    An interrupted attempt is reopened without counting against the limit.
    """
    if files:
        attempt.files = files
        try:
            attempt.stash, attempt.stash_ref = _stash_files(
                project_dir,
                files,
                f"conductor manual-test: failed fix {finding.id} attempt {attempt.number}",
                f"refs/conductor/manual-test/{_safe_name(state.name)}/"
                f"{_safe_name(finding.id)}-attempt-{attempt.number}",
            )
        except subprocess.CalledProcessError as exc:
            finding.verification.append(
                f"Could not stash the failed fix ({_format_files_for_log(files)}): "
                f"{_process_error_text(exc)}"
            )
        if attempt.stash:
            finding.verification.append(
                f"Fix attempt {attempt.number} changes stashed in {attempt.stash_label} "
                f"({_format_files_for_log(files)})"
            )
            _log(
                "MANUAL_FIX_STASHED",
                f"{finding.id}: attempt {attempt.number} changes stashed in {attempt.stash_label}",
                log_path,
                audit_path,
                finding_id=finding.id,
                attempt=attempt.number,
                stash=attempt.stash,
                ref=attempt.stash_ref,
                files=files,
            )
    attempt.outcome = outcome
    finding.verification.append(f"Fix attempt {attempt.number}: {outcome}")
    failed = sum(1 for item in finding.fix_attempts if item.failed)
    if failed >= MAX_FIX_ATTEMPTS:
        finding.status = "needs_human"
        finding.verification.append(f"Gave up after {failed} failed fix attempts")
    else:
        finding.status = "open"
    save_manual_state(state, conductor_dir)


def run_manual_test_in_tmux(
    state: ManualTestState,
    project_dir: Path,
    storage: StorageResolver,
) -> None:
    asyncio.run(_manual_main(state, project_dir, storage))


async def _manual_main(
    state: ManualTestState,
    project_dir: Path,
    storage: StorageResolver,
) -> None:
    from conductor.core.tmux import TmuxManager

    conductor_dir = storage.conductor_dir(state.name)
    ensure_manual_test_layout(conductor_dir)
    log_path = manual_log_path(conductor_dir)
    audit_path = manual_audit_path(conductor_dir)

    plan_path = Path(state.plan_file)
    if not plan_path.is_absolute():
        plan_path = project_dir / plan_path
    plan_content = plan_path.read_text(encoding="utf-8")

    preset = load_preset(state.preset, project_dir)
    policy = preset.manual_test
    model = resolve_model(state.model or preset.config.model or "opus")
    tmux = TmuxManager(session_name=f"conductor-manual-test-{state.name}")
    await tmux.ensure_session()

    resumed = ""
    if state.status == "paused":
        resumed = f" (resuming after: {state.last_error})"
        state.status = state.paused_from or "running"
        state.paused_from = ""
        state.last_error = ""
        state.consecutive_infra_failures = 0

    _log(
        "MANUAL_TEST_START",
        f"Starting manual-test {state.name} with {len(state.scenarios)} scenarios{resumed}",
        log_path,
        audit_path,
    )

    try:
        await _run_manual_phases(
            state=state,
            project_dir=project_dir,
            conductor_dir=conductor_dir,
            tmux=tmux,
            plan_content=plan_content,
            log_path=log_path,
            audit_path=audit_path,
            model=model,
            policy=policy,
        )
    except ManualRunPaused as exc:
        state.paused_from = state.status
        state.status = "paused"
        state.last_error = str(exc)
        # A pause during coverage must not re-render the matrix coverage edited.
        save_manual_state(state, conductor_dir, write_markdown=state.paused_from != "coverage")
        report = write_manual_report(state, conductor_dir)
        _log(
            "MANUAL_TEST_PAUSED",
            f"Manual-test paused: {exc}",
            log_path,
            audit_path,
            error=str(exc),
            report=str(report),
        )
        _print_banner(
            f"MANUAL TEST PAUSED: {state.name}",
            [
                f"Reason: {exc}",
                "Re-run the same `conductor manual-test` command to resume.",
                f"Report: {report}",
            ],
        )
        return

    failed = sum(1 for s in state.scenarios if s.status == "failed")
    blocked = sum(1 for s in state.scenarios if s.status == "blocked")
    violations = sum(1 for s in state.scenarios if s.status == "policy_violation")
    state.status = "completed" if failed == 0 and blocked == 0 and violations == 0 else "failed"
    save_manual_state(state, conductor_dir)
    report = write_manual_report(state, conductor_dir)

    _log(
        "MANUAL_TEST_DONE",
        f"Manual-test finished: {state.status}; report {report}",
        log_path,
        audit_path,
        report=str(report),
    )
    _print_banner(
        f"MANUAL TEST COMPLETE: {state.name}",
        [f"Status: {state.status}", f"Report: {report}"],
    )


def _print_banner(title: str, lines: list[str]) -> None:
    sep = "=" * 50
    print(f"\n{sep}", file=sys.stderr)
    print(f"  {title}", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    print(f"{sep}\n", file=sys.stderr)


async def _run_manual_phases(
    *,
    state: ManualTestState,
    project_dir: Path,
    conductor_dir: Path,
    tmux: "TmuxManager",
    plan_content: str,
    log_path: Path,
    audit_path: Path,
    model: str,
    policy: ManualTestPolicy,
) -> None:
    """Coverage, then retries of open findings, then every open scenario.

    Raises ManualRunPaused to stop early; state is resumable at that point.
    """
    if state.status in {"pending", "coverage"}:
        state.status = "coverage"
        save_manual_state(state, conductor_dir, write_markdown=False)
        signal, output_text = await _run_generation_retrying(
            state=state,
            project_dir=project_dir,
            conductor_dir=conductor_dir,
            tmux=tmux,
            prompt=build_coverage_prompt(state, plan_content, conductor_dir, policy=policy),
            label="coverage",
            model=model,
            scenario=None,
            log_path=log_path,
            audit_path=audit_path,
            write_markdown_on_finish=False,
            policy=policy,
            fatal_scope="none",
        )
        generation = state.generations[-1]
        if not signal.valid:
            _write_generation_handoff(
                state=state,
                conductor_dir=conductor_dir,
                generation=generation,
                scenario=None,
                reason="coverage generation did not produce valid handoff signal",
                output_text=output_text,
            )
            raise ManualRunPaused(
                "coverage generation did not produce a valid handoff signal: "
                + "; ".join(signal.errors)
            )
        scenarios_text = manual_scenarios_path(conductor_dir).read_text(encoding="utf-8")
        discovered = parse_scenarios_from_markdown(scenarios_text)
        if discovered:
            state.scenarios = discovered
        state.status = "running"
        save_manual_state(state, conductor_dir)

    # Finish verifications a stopped run left behind, then retry open findings
    # from prior runs, before processing new scenarios.
    for finding in list(state.findings):
        if finding.status == "fixed":
            await _resume_verification(
                state=state,
                project_dir=project_dir,
                conductor_dir=conductor_dir,
                tmux=tmux,
                plan_content=plan_content,
                finding=finding,
                log_path=log_path,
                audit_path=audit_path,
                model=model,
                policy=policy,
            )
    for finding in list(state.findings):
        if _should_fix_finding(finding):
            _log(
                "FINDING_FIX_RETRY",
                f"Retrying fix for {finding.id}: {finding.title}",
                log_path,
                audit_path,
            )
            await _fix_and_verify_finding(
                state=state,
                project_dir=project_dir,
                conductor_dir=conductor_dir,
                tmux=tmux,
                plan_content=plan_content,
                finding=finding,
                log_path=log_path,
                audit_path=audit_path,
                model=model,
                policy=policy,
            )

    while True:
        scenario = _next_open_scenario(state)
        if scenario is None:
            break

        state.current_scenario_index = scenario.index
        scenario.status = "in_progress"
        scenario.attempts += 1
        scenario.started_at = scenario.started_at or datetime.now(timezone.utc)
        save_manual_state(state, conductor_dir)

        try:
            signal, output_text = await _run_generation_retrying(
                state=state,
                project_dir=project_dir,
                conductor_dir=conductor_dir,
                tmux=tmux,
                prompt=build_scenario_prompt(state, plan_content, scenario, conductor_dir, policy=policy),
                label=f"scenario-{scenario.index}",
                model=model,
                scenario=scenario,
                log_path=log_path,
                audit_path=audit_path,
                policy=policy,
            )
        except ManualRunPaused:
            # Claude never ran the scenario; this was not an attempt.
            scenario.attempts -= 1
            scenario.status = "pending"
            raise
        generation = state.generations[-1]
        _apply_signal_to_scenario(
            state=state,
            conductor_dir=conductor_dir,
            scenario=scenario,
            generation=generation,
            signal=signal,
            output_text=output_text,
        )
        save_manual_state(state, conductor_dir)

        for finding_id in list(scenario.findings):
            finding = _finding_by_id(state, finding_id)
            if finding is None:
                continue
            await _fix_and_verify_finding(
                state=state,
                project_dir=project_dir,
                conductor_dir=conductor_dir,
                tmux=tmux,
                plan_content=plan_content,
                finding=finding,
                log_path=log_path,
                audit_path=audit_path,
                model=model,
                policy=policy,
            )
