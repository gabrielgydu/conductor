import json
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import conductor.core.manual_test as manual_test
from conductor.core.manual_test import (
    ManualEvidence,
    ManualFinding,
    build_coverage_prompt,
    build_scenario_prompt,
    detect_relevant_check_commands,
    format_manual_status,
    generate_manual_report,
    initialize_manual_state,
    load_manual_state,
    manual_state_path,
    parse_manual_signal,
    parse_scenarios_from_markdown,
    save_manual_state,
    scan_policy_shortcuts,
)
from conductor.core.presets import CheckCommandRule, FatalPattern, ManualTestPolicy
from conductor.core.storage import StorageResolver


PLAN_TABLE = """# Manual Plan

| Area | Scenario | Primary Evidence |
|---|---|---|
| Admin rollout | Enable showNewCheckout via UI | Admin screenshot, config readback |
| Mailer UI | Create SMTP mailer | UI screenshot, MailHog test email |
"""

POLICY = ManualTestPolicy(
    policy_text=(
        "- CRITICAL: Feature flags showNewCheckout/useNewCheckout must ALWAYS be toggled through\n"
        "  the admin UI. NEVER change them via SQL."
    ),
    coverage_focus="Cover the admin tenant settings pages.",
    fatal_patterns=[
        FatalPattern(
            pattern=r"\b(showNewCheckout|useNewCheckout)\b",
            reason="feature flags must be toggled through the admin UI",
        )
    ],
    check_commands=[
        CheckCommandRule(argv=["make", "phpstan-api"], path_prefix="api/", suffixes=[".php"]),
        CheckCommandRule(argv=["make", "phpstan-app"], path_prefix="app/", suffixes=[".php"]),
        CheckCommandRule(
            argv=["make", "build-app"],
            path_prefix="app/",
            suffixes=[".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".vue"],
        ),
        CheckCommandRule(argv=["make", "e2e", "{path}"], suffixes=[".spec.ts", ".spec.js"]),
    ],
    blocked_reason_words=["data", "fixture", "customer", "cart"],
)


def _run_main(main_fn, args):
    stdout_buf = StringIO()
    stderr_buf = StringIO()
    exit_code = 0
    with patch("sys.argv", args):
        with patch("sys.stdout", stdout_buf):
            with patch("sys.stderr", stderr_buf):
                try:
                    main_fn()
                except SystemExit as exc:
                    exit_code = exc.code if exc.code is not None else 0
    return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


def _patch_storage(tmp_path: Path, repo_path: Path):
    storage_base = tmp_path / "storage"

    def patched_init(self, _repo_path):
        self.repo_root = repo_path
        self._project_key = "test-project"
        self.base_dir = storage_base

    return patch.object(StorageResolver, "__init__", patched_init)


def test_parse_scenarios_from_table():
    scenarios = parse_scenarios_from_markdown(PLAN_TABLE)

    assert [scenario.index for scenario in scenarios] == [0, 1]
    assert scenarios[0].area == "Admin rollout"
    assert scenarios[0].name == "Enable showNewCheckout via UI"
    assert "Admin screenshot" in scenarios[0].description


def test_parse_scenarios_from_checklist_fallback():
    scenarios = parse_scenarios_from_markdown(
        """# Scenario Checklist

- [ ] **Admin rollout** - Enable flags through the admin UI.
- [ ] Bulk group wizard - Verify duplicate recipient handling.
"""
    )

    assert len(scenarios) == 2
    assert scenarios[0].area == "Checklist"
    assert scenarios[0].name == "Admin rollout"
    assert scenarios[1].name == "Bulk group wizard"


def test_initialize_manual_state_serializes_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")
    conductor_dir = tmp_path / "conductor" / "checkout-manual"

    state = initialize_manual_state(
        name="checkout-manual",
        project_dir=repo,
        plan_file=plan,
        preset="base",
        conductor_dir=conductor_dir,
    )
    loaded = load_manual_state(conductor_dir)

    assert manual_state_path(conductor_dir).exists()
    assert (conductor_dir / "MANUAL-TEST-SCENARIOS.md").exists()
    assert (conductor_dir / "MANUAL-TEST-FINDINGS.md").exists()
    assert (conductor_dir / "artifacts" / "screenshots").is_dir()
    assert loaded.name == state.name
    assert len(loaded.scenarios) == 2


def test_save_manual_state_can_preserve_scenario_markdown(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")
    conductor_dir = tmp_path / "conductor" / "checkout-manual"
    state = initialize_manual_state(
        name="checkout-manual",
        project_dir=repo,
        plan_file=plan,
        conductor_dir=conductor_dir,
    )
    scenarios_path = conductor_dir / "MANUAL-TEST-SCENARIOS.md"
    discovered = """# Manual Test Scenarios

| # | Status | Priority | Area | Scenario | Evidence Target | Findings |
|---|---|---|---|---|---|---|
| 0 | pending | medium | Admin rollout | Enable showNewCheckout via UI | Admin screenshot | |
| 1 | pending | medium | Discovered | New route from coverage | Screenshot | |
"""
    scenarios_path.write_text(discovered, encoding="utf-8")
    state.status = "coverage"

    save_manual_state(state, conductor_dir, write_markdown=False)

    assert "New route from coverage" in scenarios_path.read_text(encoding="utf-8")


def test_prompt_builders_include_policy_and_required_tags(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")
    conductor_dir = tmp_path / "conductor" / "checkout-manual"
    state = initialize_manual_state(
        name="checkout-manual",
        project_dir=repo,
        plan_file=plan,
        conductor_dir=conductor_dir,
    )

    coverage_prompt = build_coverage_prompt(state, PLAN_TABLE, conductor_dir, policy=POLICY)
    scenario_prompt = build_scenario_prompt(state, PLAN_TABLE, state.scenarios[0], conductor_dir, policy=POLICY)
    plain_coverage_prompt = build_coverage_prompt(state, PLAN_TABLE, conductor_dir)
    plain_scenario_prompt = build_scenario_prompt(state, PLAN_TABLE, state.scenarios[0], conductor_dir)
    handoff = conductor_dir / "generations" / "generation-001-handoff.md"
    handoff.write_text("Read this before retrying", encoding="utf-8")
    state.scenarios[0].handoff_notes = f"See {handoff}"
    handoff_prompt = build_scenario_prompt(state, PLAN_TABLE, state.scenarios[0], conductor_dir)

    assert "Coverage" in coverage_prompt or "coverage" in coverage_prompt
    assert "MANUAL-TEST-SCENARIOS.md" in coverage_prompt
    assert "<manual-handoff/>" in coverage_prompt
    assert "Data Setup Policy" in scenario_prompt
    assert "<manual-pass/>" in scenario_prompt
    assert "<manual-finding/>" in scenario_prompt
    assert "showNewCheckout/useNewCheckout" in scenario_prompt
    assert "Project Focus" in coverage_prompt
    assert POLICY.coverage_focus in coverage_prompt
    assert "NEVER change them via SQL" not in plain_scenario_prompt
    assert "Data Setup Policy" in plain_scenario_prompt
    assert "Project Focus" not in plain_coverage_prompt
    assert "Previous Handoff" in handoff_prompt
    assert "Read this before retrying" in handoff_prompt


def test_parse_manual_pass_requires_artifact_evidence():
    valid = parse_manual_signal(
        """<manual-result status="pass">
  <summary>Admin UI toggled flag.</summary>
  <evidence><url>https://app.example/settings</url><screenshot>artifacts/screenshots/admin.png</screenshot></evidence>
</manual-result>
<manual-pass/>"""
    )
    invalid = parse_manual_signal(
        """<manual-result status="pass">
  <summary>Looks good.</summary>
</manual-result>
<manual-pass/>"""
    )

    assert valid.valid is True
    assert valid.kind == "pass"
    assert invalid.valid is False
    assert "evidence" in "; ".join(invalid.errors)


def test_parse_manual_finding_and_blocked_validation():
    finding = parse_manual_signal(
        """<manual-result status="finding">
  <severity>ux</severity>
  <category>ux</category>
  <title>Sidebar link is hidden during visibility rollout</title>
  <reproduction><step>Enable showNewCheckout only</step><step>Open app sidebar</step></reproduction>
  <expected>Checkout settings link remains discoverable</expected>
  <actual>No checkout entry appears</actual>
  <evidence><screenshot>artifacts/screenshots/sidebar.png</screenshot></evidence>
</manual-result>
<manual-finding/>"""
    )
    blocked = parse_manual_signal(
        """<manual-result status="blocked">
  <reason>No data</reason>
  <code_read>app/routes.php</code_read>
  <data_attempts>Created cart fixture</data_attempts>
  <why_not_fixable_now>Environment unavailable</why_not_fixable_now>
</manual-result>
<manual-blocked/>"""
    )

    assert finding.valid is True
    assert finding.severity == "ux"
    assert finding.reproduction == ["Enable showNewCheckout only", "Open app sidebar"]
    assert blocked.valid is False
    assert "missing data" in "; ".join(blocked.errors).lower()


def test_blocked_requires_evidence_and_rejects_missing_data_variants():
    no_evidence = parse_manual_signal(
        """<manual-result status="blocked">
  <reason>Environment unavailable</reason>
  <code_read>app/routes.php</code_read>
  <data_attempts>Created cart fixture</data_attempts>
  <why_not_fixable_now>Docker is down</why_not_fixable_now>
</manual-result>
<manual-blocked/>"""
    )
    missing_fixture = parse_manual_signal(
        """<manual-result status="blocked">
  <reason>Missing fixture data for carts</reason>
  <code_read>app/routes.php</code_read>
  <data_attempts>Checked seed data</data_attempts>
  <why_not_fixable_now>Could not create it</why_not_fixable_now>
  <evidence><note>Seed table was empty</note></evidence>
</manual-result>
<manual-blocked/>"""
    )

    assert no_evidence.valid is False
    assert "requires evidence" in "; ".join(no_evidence.errors)
    assert missing_fixture.valid is False
    assert "missing data" in "; ".join(missing_fixture.errors).lower()


def test_policy_scanner_requires_approved_shortcut_for_flagged_tool_use():
    tool_event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "bash",
                        "input": {"command": "mysql -e 'insert into fixtures values (1)'"},
                    }
                ]
            },
        }
    )
    text_without_shortcut = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<manual-result status=\"pass\"><evidence><db_readback>row exists</db_readback></evidence></manual-result><manual-pass/>",
                    }
                ]
            },
        }
    )
    text_with_shortcut = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": """<manual-result status="pass">
  <evidence><db_readback>row exists</db_readback></evidence>
  <shortcut><reason>Large fixture setup</reason><command_or_route>mysql -e 'insert into fixtures values (1)'</command_or_route><user_flow_supported>Bulk group scenario</user_flow_supported><approved_by_policy>true</approved_by_policy></shortcut>
</manual-result><manual-pass/>""",
                    }
                ]
            },
        }
    )

    flags = scan_policy_shortcuts(f"{tool_event}\n{text_without_shortcut}")
    invalid = parse_manual_signal(f"{tool_event}\n{text_without_shortcut}", policy_flags=flags)
    valid = parse_manual_signal(f"{tool_event}\n{text_with_shortcut}", policy_flags=flags)

    assert flags
    assert invalid.valid is False
    assert "DataShortcut" in "; ".join(invalid.errors)
    assert valid.valid is True
    assert valid.shortcuts[0].approved_by_policy is True


def test_policy_scanner_rejects_incomplete_and_forbidden_shortcuts():
    rollout_tool = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "bash",
                        "input": {"command": "mysql -e 'update tenants set useNewCheckout=1'"},
                    }
                ]
            },
        }
    )
    rollout_result = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": """<manual-result status="pass">
  <evidence><db_readback>flag updated</db_readback></evidence>
  <shortcut><reason>Fast setup</reason><command_or_route>mysql -e 'update tenants set useNewCheckout=1'</command_or_route><user_flow_supported>Admin rollout</user_flow_supported><approved_by_policy>true</approved_by_policy></shortcut>
</manual-result><manual-pass/>""",
                    }
                ]
            },
        }
    )
    incomplete_tool = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "bash",
                        "input": {"command": "mysql -e 'insert into fixtures values (1)'"},
                    }
                ]
            },
        }
    )
    incomplete_result = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": """<manual-result status="pass">
  <evidence><db_readback>row exists</db_readback></evidence>
  <shortcut><command_or_route>mysql -e 'insert into fixtures values (1)'</command_or_route><approved_by_policy>true</approved_by_policy></shortcut>
</manual-result><manual-pass/>""",
                    }
                ]
            },
        }
    )

    rollout_flags = scan_policy_shortcuts(f"{rollout_tool}\n{rollout_result}", POLICY)
    rollout = parse_manual_signal(
        f"{rollout_tool}\n{rollout_result}", policy_flags=rollout_flags, policy=POLICY
    )
    unpoliced_flags = scan_policy_shortcuts(f"{rollout_tool}\n{rollout_result}")
    unpoliced = parse_manual_signal(
        f"{rollout_tool}\n{rollout_result}", policy_flags=unpoliced_flags
    )
    incomplete_flags = scan_policy_shortcuts(f"{incomplete_tool}\n{incomplete_result}")
    incomplete = parse_manual_signal(
        f"{incomplete_tool}\n{incomplete_result}",
        policy_flags=incomplete_flags,
    )

    assert rollout.valid is False
    assert "toggled through the admin UI" in "; ".join(rollout.errors)
    # Without a preset policy the same shortcut is merely a non-fatal, declared shortcut.
    assert unpoliced.valid is True
    assert incomplete.valid is False
    assert "DataShortcut" in "; ".join(incomplete.errors)


def test_policy_scanner_flags_test_routes_without_shortcut():
    tool_event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "playwright",
                        "input": {"url": "https://app.example/_test/checkout-process"},
                    }
                ]
            },
        }
    )
    result_event = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<manual-result status=\"pass\"><evidence><url>https://app.example/_test/checkout-process</url></evidence></manual-result><manual-pass/>",
                    }
                ]
            },
        }
    )

    flags = scan_policy_shortcuts(f"{tool_event}\n{result_event}")
    signal = parse_manual_signal(f"{tool_event}\n{result_event}", policy_flags=flags)

    assert flags
    assert signal.valid is False
    assert "DataShortcut" in "; ".join(signal.errors)


def test_status_and_report_output(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")
    conductor_dir = tmp_path / "conductor" / "checkout-manual"
    state = initialize_manual_state(
        name="checkout-manual",
        project_dir=repo,
        plan_file=plan,
        conductor_dir=conductor_dir,
    )
    state.scenarios[0].evidence = ManualEvidence(
        urls=["https://app.example/settings"],
        screenshots=["artifacts/screenshots/admin.png"],
    )
    state.findings.append(
        ManualFinding(
            id="MT-001",
            scenario_index=0,
            severity="medium",
            category="app_bug",
            title="Send button fails",
            status="verified",
            verification=["make phpstan-app: exit 0"],
        )
    )

    status = format_manual_status(state)
    report = generate_manual_report(state)

    assert "Manual Test: checkout-manual" in status
    assert "Scenarios:" in status
    assert "# Manual Test Report" in report
    assert "Scenario Matrix" in report
    assert "Findings By Status" in report
    assert "verified: 1" in report
    assert "Checks Run" in report
    assert "phpstan-app" in report
    assert "Scenario Evidence" in report
    assert "artifacts/screenshots/admin.png" in report


def test_detect_relevant_check_commands():
    changed = [
        "api/src/CheckoutService.php",
        "app/src/CheckoutController.php",
        "app/assets/checkout.ts",
        "app/tests/Playwright/checkout.spec.ts",
    ]
    commands = detect_relevant_check_commands(changed, POLICY)

    assert commands == [
        ["make", "phpstan-api"],
        ["make", "phpstan-app"],
        ["make", "build-app"],
        ["make", "e2e", "app/tests/Playwright/checkout.spec.ts"],
    ]
    # Without preset rules nothing is detected.
    assert detect_relevant_check_commands(changed) == []
    assert detect_relevant_check_commands(changed, ManualTestPolicy()) == []


def test_git_changed_files_includes_staged_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    path = repo / "app" / "src" / "CheckoutController.php"
    path.parent.mkdir(parents=True)
    path.write_text("<?php\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", str(path.relative_to(repo))], cwd=repo, check=True)

    assert "app/src/CheckoutController.php" in manual_test._git_changed_files(repo)


@pytest.mark.asyncio
async def test_fix_and_verify_finding_marks_verified(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")
    conductor_dir = tmp_path / "conductor" / "checkout-manual"
    state = initialize_manual_state(
        name="checkout-manual",
        project_dir=repo,
        plan_file=plan,
        conductor_dir=conductor_dir,
    )
    finding = ManualFinding(
        id="MT-001",
        scenario_index=0,
        severity="medium",
        category="app_bug",
        title="Send button fails",
        reproduction=["Open checkout preview", "Click send"],
        expected="Email sends",
        actual="Send fails",
        evidence=ManualEvidence(screenshots=["artifacts/screenshots/fail.png"]),
    )
    state.findings.append(finding)
    state.scenarios[0].status = "failed"
    state.scenarios[0].findings.append(finding.id)

    calls = []

    async def fake_run_generation(**kwargs):
        calls.append(kwargs["label"])
        generation = manual_test.ManualGeneration(
            index=len(state.generations) + 1,
            started_at=manual_test.datetime.now(manual_test.timezone.utc),
            scenario_index=0,
        )
        state.session_count += 1
        state.generations.append(generation)
        if kwargs["label"].startswith("fix-"):
            return manual_test.parse_manual_signal("<manual-handoff/>"), "fix handoff"
        return manual_test.parse_manual_signal(
            """<manual-result status="pass">
  <summary>Scenario now sends</summary>
  <evidence><screenshot>artifacts/screenshots/pass.png</screenshot></evidence>
</manual-result><manual-pass/>"""
        ), "verify pass"

    changed_calls = []

    def fake_git_changed_files(_cwd):
        changed_calls.append(True)
        if len(changed_calls) == 1:
            return []
        return ["app/src/CheckoutController.php"]

    monkeypatch.setattr(manual_test, "_run_generation", fake_run_generation)
    monkeypatch.setattr(manual_test, "_git_changed_files", fake_git_changed_files)
    monkeypatch.setattr(
        manual_test,
        "_run_relevant_checks",
        lambda _cwd, _files, policy=None: (True, ["phpstan-app: exit 0"]),
    )
    monkeypatch.setattr(manual_test, "_commit_verified_finding", lambda _cwd, _finding, _files: "abc123")

    await manual_test._fix_and_verify_finding(
        state=state,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=object(),
        plan_content=PLAN_TABLE,
        finding=finding,
        log_path=conductor_dir / "MANUAL-TEST-LOG.md",
        audit_path=conductor_dir / "MANUAL-TEST-AUDIT.jsonl",
        model="sonnet",
    )

    assert calls == ["fix-MT-001", "verify-MT-001"]
    assert finding.status == "verified"
    assert finding.fix_commits == ["abc123"]
    assert state.scenarios[0].status == "passed"
    assert "phpstan-app: exit 0" in finding.verification


@pytest.mark.asyncio
async def test_fix_and_verify_skips_when_worktree_is_dirty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")
    conductor_dir = tmp_path / "conductor" / "checkout-manual"
    state = initialize_manual_state(
        name="checkout-manual",
        project_dir=repo,
        plan_file=plan,
        conductor_dir=conductor_dir,
    )
    finding = ManualFinding(
        id="MT-001",
        scenario_index=0,
        severity="medium",
        category="app_bug",
        title="Send button fails",
        reproduction=["Open checkout preview", "Click send"],
        expected="Email sends",
        actual="Send fails",
        evidence=ManualEvidence(screenshots=["artifacts/screenshots/fail.png"]),
    )
    state.findings.append(finding)
    calls = []

    async def fake_run_generation(**kwargs):
        calls.append(kwargs["label"])
        return manual_test.parse_manual_signal("<manual-handoff/>"), ""

    monkeypatch.setattr(manual_test, "_run_generation", fake_run_generation)
    monkeypatch.setattr(manual_test, "_git_changed_files", lambda _cwd: ["app/src/Unrelated.php"])

    await manual_test._fix_and_verify_finding(
        state=state,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=object(),
        plan_content=PLAN_TABLE,
        finding=finding,
        log_path=conductor_dir / "MANUAL-TEST-LOG.md",
        audit_path=conductor_dir / "MANUAL-TEST-AUDIT.jsonl",
        model="sonnet",
    )

    assert calls == []
    assert finding.status == "open"
    assert "pre-existing changes" in finding.verification[0]


def test_manual_test_cli_init_status_report(tmp_path):
    from conductor.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")

    with _patch_storage(tmp_path, repo):
        stdout, _, code = _run_main(
            main,
            [
                "conductor",
                "manual-test",
                "--name",
                "checkout-manual",
                "--project-dir",
                str(repo),
                "--plan",
                str(plan),
                "--init-only",
            ],
        )
        status_stdout, _, status_code = _run_main(
            main,
            [
                "conductor",
                "manual-test-status",
                "--name",
                "checkout-manual",
                "--project-dir",
                str(repo),
            ],
        )
        report_stdout, _, report_code = _run_main(
            main,
            [
                "conductor",
                "manual-test-report",
                "--name",
                "checkout-manual",
                "--project-dir",
                str(repo),
            ],
        )

    state_path = tmp_path / "storage" / "conductor" / "checkout-manual" / "MANUAL-TEST-STATE.json"
    assert code == 0
    assert "Initialized manual-test" in stdout
    assert state_path.exists()
    assert status_code == 0
    assert "Manual Test: checkout-manual" in status_stdout
    assert report_code == 0
    assert "Report written:" in report_stdout
    assert "# Manual Test Report" in report_stdout
