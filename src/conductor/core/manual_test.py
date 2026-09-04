"""Manual-test orchestration mode.

This mode is intentionally separate from ``loop``: it tracks scenario coverage,
browser evidence, findings, policy shortcuts, and generation handoffs for long
manual QA runs.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from conductor.core.claude import resolve_model
from conductor.core.logging import live_log
from conductor.core.models import atomic_save
from conductor.core.presets import ManualTestPolicy, load_preset
from conductor.core.storage import StorageResolver


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
    verification: list[str] = Field(default_factory=list)


class ManualScenario(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    index: int
    area: str
    name: str
    description: str
    status: str = "pending"
    priority: str = "medium"
    attempts: int = 0
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


class ManualSignal(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    kind: str | None = None
    result_status: str | None = None
    valid: bool = False
    errors: list[str] = Field(default_factory=list)
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
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_separator(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


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

            description = (
                f"Primary evidence target: {evidence_target}"
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
        description = scenario.description.replace("|", "\\|").replace("\n", " ")
        rows.append(
            "| "
            f"{scenario.index} | {scenario.status} | {scenario.priority} | "
            f"{scenario.area} | {scenario.name} | {description} | {findings} |"
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
    lines.extend(f"mailhog={msg}" for msg in evidence.mailhog_messages)
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


def _extract_text_from_stream_json(output: str) -> str:
    text_parts: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
    if text_parts:
        return "\n".join(text_parts)
    return output


def _tag_values(text: str, tag: str) -> list[str]:
    values = []
    for match in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.DOTALL | re.I):
        values.append(match.group(1).strip())
    return values


def _tag_value(text: str, tag: str) -> str:
    values = _tag_values(text, tag)
    return values[0] if values else ""


def _manual_result_block(text: str) -> tuple[str, str | None]:
    match = re.search(r"<manual-result\b([^>]*)>(.*?)</manual-result>", text, re.DOTALL | re.I)
    if not match:
        return "", None
    attrs = match.group(1)
    status_match = re.search(r"status=[\"']([^\"']+)[\"']", attrs, re.I)
    status = status_match.group(1).strip().lower() if status_match else None
    return match.group(2), status


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "approved"}


def _parse_evidence(block: str) -> ManualEvidence:
    evidence_block = _tag_value(block, "evidence") or block
    return ManualEvidence(
        summary=_tag_value(block, "summary"),
        urls=_tag_values(evidence_block, "url"),
        screenshots=_tag_values(evidence_block, "screenshot"),
        traces=_tag_values(evidence_block, "trace"),
        mailhog_messages=(
            _tag_values(evidence_block, "mailhog")
            + _tag_values(evidence_block, "mailhog_message")
        ),
        console_errors=(
            _tag_values(evidence_block, "console")
            + _tag_values(evidence_block, "console_error")
        ),
        db_readbacks=(
            _tag_values(evidence_block, "db")
            + _tag_values(evidence_block, "db_readback")
        ),
        notes=_tag_values(evidence_block, "note"),
    )


def _has_required_evidence(evidence: ManualEvidence | None) -> bool:
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
            evidence.notes,
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
    ("database cli", re.compile(r"\b(mysql|psql|sqlite3|mongo|redis-cli)\b", re.I)),
    (
        "mutating http",
        re.compile(r"\b(curl|httpie|wget)\b.*\b(POST|PUT|PATCH|DELETE)\b", re.I | re.S),
    ),
    (
        "db framework command",
        re.compile(r"\b(artisan\s+tinker|php\s+artisan\s+db|doctrine|bin/console\s+doctrine)\b", re.I),
    ),
    ("fixture command", re.compile(r"\b(seed|seeder|factory|fixture|dump|reset-db)\b", re.I)),
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


def _is_auto_approved(flag: "PolicyFlag") -> bool:
    """Auto-approve all non-fatal policy flags.

    Only fatal flags (the preset's ``[[manual_test.fatal_patterns]]``) remain
    enforced. Everything else — DB commands, test routes, mutating HTTP,
    fixture commands, browser storage — is auto-approved because the tester
    operates entirely within the dev environment.
    """
    return not flag.fatal_reason


def _iter_tool_inputs(output: str) -> list[str]:
    values: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_input = block.get("input", {})
            if isinstance(tool_input, dict):
                for key in ["command", "cmd", "url", "query", "script", "code"]:
                    value = tool_input.get(key)
                    if isinstance(value, str):
                        values.append(value)
            elif isinstance(tool_input, str):
                values.append(tool_input)
    return values


def scan_policy_shortcuts(
    output: str, policy: ManualTestPolicy | None = None
) -> list[PolicyFlag]:
    flags: list[PolicyFlag] = []
    seen: set[tuple[str, str]] = set()
    candidates = [("tool", value) for value in _iter_tool_inputs(output)]
    text = _extract_text_from_stream_json(output)
    if text.strip():
        candidates.append(("text", text))

    for source, value in candidates:
        for label, pattern in _POLICY_PATTERNS:
            match = pattern.search(value)
            if match:
                flagged_value = value if source == "tool" else match.group(0)
                key = (label, flagged_value)
                if key in seen:
                    continue
                seen.add(key)
                flags.append(
                    PolicyFlag(
                        pattern=label,
                        value=flagged_value,
                        source=source,
                        fatal_reason=_fatal_policy_reason(flagged_value, policy),
                    )
                )
    return flags


def _signal_tags(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for tag in SIGNAL_TAGS:
        for match in re.finditer(rf"<{tag}\s*/>", text, re.I):
            found.append((match.start(), tag))
    return [tag for _, tag in sorted(found)]


def _signal_tag_is_final(text: str, tag: str) -> bool:
    return re.search(rf"<{tag}\s*/>\s*$", text, re.I) is not None


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

    tags = _signal_tags(text)
    if not tags:
        signal.errors.append("missing required manual signal tag")
        return signal
    if len(tags) > 1:
        signal.errors.append(f"expected exactly one manual signal tag, found: {', '.join(tags)}")
        return signal

    tag = tags[0]
    signal.kind = SIGNAL_TAGS[tag]
    if not _signal_tag_is_final(text, tag):
        signal.errors.append("manual signal tag must be the final output")
    block, result_status = _manual_result_block(text)
    signal.result_status = result_status

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
    signal.errors.extend(_shortcut_errors(signal.shortcuts))

    if signal.kind == "pass":
        if not _has_required_evidence(signal.evidence):
            signal.errors.append("pass result requires at least one evidence item")

    elif signal.kind == "finding":
        signal.severity = _tag_value(block, "severity").lower()
        signal.category = _tag_value(block, "category").lower()
        signal.title = _tag_value(block, "title")
        signal.reproduction = _parse_reproduction(block)
        signal.expected = _tag_value(block, "expected")
        signal.actual = _tag_value(block, "actual")
        signal.suspected_files = _tag_values(block, "suspected_file")

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
        if not _has_required_evidence(signal.evidence):
            signal.errors.append("finding requires evidence")

    elif signal.kind == "blocked":
        signal.blocked_reason = _tag_value(block, "reason")
        signal.code_read = _tag_value(block, "code_read")
        signal.data_attempts = _tag_value(block, "data_attempts")
        signal.why_not_fixable_now = _tag_value(block, "why_not_fixable_now")
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

    fatal_flags = [flag for flag in signal.policy_flags if flag.fatal_reason]
    if fatal_flags:
        signal.errors.append(
            "policy scanner flagged forbidden shortcut: "
            + "; ".join(
                f"{flag.fatal_reason}: {flag.value}" for flag in fatal_flags
            )
        )

    missing_shortcuts = [
        flag
        for flag in signal.policy_flags
        if not _is_auto_approved(flag)
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
- Every SQL/test-route/mutating HTTP shortcut must include a <shortcut> entry in the result:
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
    <mailhog>...</mailhog>
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
) -> str:
    evidence = _format_evidence_lines(finding.evidence) if finding.evidence else []
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

End with <manual-handoff/> and summarize changed files and checks run.

## Original Plan

{plan_content}
"""


def build_verifier_prompt(
    state: ManualTestState,
    plan_content: str,
    finding: ManualFinding,
    conductor_dir: Path,
) -> str:
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
    return "\n".join(lines[-limit:])


def _scenario_handoff_section(scenario: ManualScenario) -> str:
    if not scenario.handoff_notes.strip():
        return ""

    content = ""
    match = re.search(r"(/\S*generation-\d{3}-handoff\.md)", scenario.handoff_notes)
    if match:
        handoff_path = Path(match.group(1))
        if handoff_path.exists():
            content = handoff_path.read_text(encoding="utf-8", errors="replace")[:4000]

    section = f"## Previous Handoff\n\n{scenario.handoff_notes}\n"
    if content:
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


_TEST_ARTIFACT_PATTERNS = re.compile(
    r"\.yaml$|\.yml\.snap$|/artifacts/|/screenshots/|/mailhog/|/generations/"
)


def _is_test_artifact(path: str) -> bool:
    return bool(_TEST_ARTIFACT_PATTERNS.search(path))


def _git_changed_files(cwd: Path, exclude_test_artifacts: bool = False) -> list[str]:
    files: list[str] = []
    for args in [
        ["git", "diff", "--name-only", "--diff-filter=ACMRTD"],
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRTD"],
    ]:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        files.extend(line.strip() for line in result.stdout.splitlines() if line.strip())

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in status.stdout.splitlines():
        if line.startswith("?? "):
            files.append(line[3:].strip())

    all_files = sorted(set(files))
    if exclude_test_artifacts:
        return [f for f in all_files if not _is_test_artifact(f)]
    return all_files


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
    if not changed_files:
        return None

    subprocess.run(
        ["git", "add", "--", *changed_files],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRTD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if not staged.stdout.strip():
        return None

    message = (
        f"Fix {finding.id}: {finding.title}\n\n"
        f"Verified manual-test scenario {finding.scenario_index}."
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


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
    return (
        finding.status in {"open", "fixing"}
        and finding.category in FIXABLE_FINDING_CATEGORIES
        and finding.severity in {"high", "medium", "low", "ux"}
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
) -> tuple[ManualSignal, str]:
    from conductor.core.loop import _wait_for_exit

    state.session_count += 1
    generation = ManualGeneration(
        index=state.session_count,
        started_at=datetime.now(timezone.utc),
        scenario_index=scenario.index if scenario else None,
    )
    state.generations.append(generation)
    save_manual_state(state, conductor_dir)

    safe = _safe_name(state.name)
    label_safe = _safe_name(label)
    prompt_file = conductor_dir / "logs" / f"manual-prompt-{generation.index:03d}-{label_safe}.md"
    output_file = conductor_dir / "logs" / f"manual-output-{generation.index:03d}-{label_safe}.jsonl"
    exit_file = Path(f"/tmp/conductor-manual-exit-{safe}-{generation.index:03d}-{label_safe}")
    prompt_file.write_text(prompt, encoding="utf-8")
    exit_file.unlink(missing_ok=True)
    output_file.unlink(missing_ok=True)

    quoted_prompt = shlex.quote(str(prompt_file))
    quoted_output = shlex.quote(str(output_file))
    quoted_exit = shlex.quote(str(exit_file))
    filter_mod = "conductor.core.stream_filter"
    pipeline = (
        f"claude -p - --dangerously-skip-permissions "
        f"--max-turns {state.max_turns} --model {shlex.quote(model)} "
        f"--output-format stream-json --verbose < {quoted_prompt} 2>/dev/null "
        f"| python3 -m {filter_mod} {quoted_output}; "
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

    try:
        exit_code = int(exit_file.read_text().strip())
    except (OSError, ValueError):
        exit_code = 1

    output_text = ""
    if output_file.exists():
        output_text = output_file.read_text(encoding="utf-8", errors="replace")

    policy_flags = scan_policy_shortcuts(output_text, policy)
    signal = parse_manual_signal(output_text, policy_flags=policy_flags, policy=policy)
    generation.completed_at = datetime.now(timezone.utc)
    generation.summary = signal.kind or "no-signal"

    _log(
        "MANUAL_GENERATION_END",
        f"Generation {generation.index}: exit {exit_code}, signal {signal.kind}, valid={signal.valid}",
        log_path,
        audit_path,
        exit_code=exit_code,
        signal=signal.kind,
        valid=signal.valid,
        errors=signal.errors,
    )

    exit_file.unlink(missing_ok=True)
    save_manual_state(state, conductor_dir, write_markdown=write_markdown_on_finish)
    return signal, output_text


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

    reason = "handoff" if signal.kind == "handoff" else "; ".join(signal.errors)
    handoff = _write_generation_handoff(
        state=state,
        conductor_dir=conductor_dir,
        generation=generation,
        scenario=scenario,
        reason=reason or "no valid manual signal",
        output_text=output_text,
    )
    scenario.handoff_notes = f"See {handoff}"
    if scenario.attempts >= 3:
        scenario.status = "blocked"
        scenario.evidence = ManualEvidence(
            summary="Scenario did not produce a valid result after 3 attempts",
            notes=signal.errors or ["No valid manual signal"],
        )
        scenario.completed_at = now
        state.current_scenario_index = scenario.index + 1
    else:
        scenario.status = "pending"


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

    finding.status = "fixing"
    save_manual_state(state, conductor_dir)

    signal, output_text = await _run_generation(
        state=state,
        project_dir=project_dir,
        conductor_dir=conductor_dir,
        tmux=tmux,
        prompt=build_fixer_prompt(state, plan_content, finding, conductor_dir),
        label=f"fix-{finding.id}",
        model=model,
        scenario=_scenario_for_finding(state, finding),
        log_path=log_path,
        audit_path=audit_path,
        policy=policy,
    )
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
        finding.status = "open"
        finding.verification.append("Fixer did not produce a valid handoff signal")
        save_manual_state(state, conductor_dir)
        return

    all_changed = _git_changed_files(project_dir)
    fixer_files = [f for f in all_changed if f not in preexisting_files]
    if not fixer_files:
        finding.status = "open"
        finding.verification.append("Fixer produced no new file changes")
        save_manual_state(state, conductor_dir)
        return

    checks_passed, check_summaries = _run_relevant_checks(project_dir, fixer_files, policy=policy)
    finding.verification.extend(check_summaries)
    if not checks_passed:
        finding.status = "open"
        save_manual_state(state, conductor_dir)
        return

    finding.status = "fixed"
    save_manual_state(state, conductor_dir)

    verifier_signal, verifier_output = await _run_generation(
        state=state,
        project_dir=project_dir,
        conductor_dir=conductor_dir,
        tmux=tmux,
        prompt=build_verifier_prompt(state, plan_content, finding, conductor_dir),
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
            commit_hash = _commit_verified_finding(project_dir, finding, fixer_files)
        except subprocess.CalledProcessError as exc:
            commit_hash = None
            finding.verification.append(f"Commit failed: {exc}")
        if commit_hash:
            finding.fix_commits.append(commit_hash)
        save_manual_state(state, conductor_dir)
        return

    if verifier_signal.kind == "finding" and verifier_signal.valid:
        finding.status = "open"
        finding.verification.append(
            f"Verifier still failing: {verifier_signal.title or verifier_signal.actual}"
        )
        save_manual_state(state, conductor_dir)
        return

    _write_generation_handoff(
        state=state,
        conductor_dir=conductor_dir,
        generation=verifier_generation,
        scenario=scenario,
        reason="verifier generation did not produce valid pass/finding signal",
        output_text=verifier_output,
    )
    finding.status = "open"
    finding.verification.append("Verifier did not produce a valid pass/finding signal")
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

    _log(
        "MANUAL_TEST_START",
        f"Starting manual-test {state.name} with {len(state.scenarios)} scenarios",
        log_path,
        audit_path,
    )

    if state.status in {"pending", "coverage"}:
        state.status = "coverage"
        save_manual_state(state, conductor_dir, write_markdown=False)
        signal, output_text = await _run_generation(
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
            state.status = "coverage"
            save_manual_state(state, conductor_dir, write_markdown=False)
            return
        scenarios_text = manual_scenarios_path(conductor_dir).read_text(encoding="utf-8")
        discovered = parse_scenarios_from_markdown(scenarios_text)
        if discovered:
            state.scenarios = discovered
        state.status = "running"
        save_manual_state(state, conductor_dir)

    # Fix existing open findings from prior runs before processing new scenarios.
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

        signal, output_text = await _run_generation(
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

    failed = sum(1 for s in state.scenarios if s.status == "failed")
    blocked = sum(1 for s in state.scenarios if s.status == "blocked")
    policy = sum(1 for s in state.scenarios if s.status == "policy_violation")
    state.status = "completed" if failed == 0 and blocked == 0 and policy == 0 else "failed"
    save_manual_state(state, conductor_dir)
    report = write_manual_report(state, conductor_dir)

    _log(
        "MANUAL_TEST_DONE",
        f"Manual-test finished: {state.status}; report {report}",
        log_path,
        audit_path,
        report=str(report),
    )

    sep = "=" * 50
    print(f"\n{sep}", file=sys.stderr)
    print(f"  MANUAL TEST COMPLETE: {state.name}", file=sys.stderr)
    print(f"  Status: {state.status}", file=sys.stderr)
    print(f"  Report: {report}", file=sys.stderr)
    print(f"{sep}\n", file=sys.stderr)
