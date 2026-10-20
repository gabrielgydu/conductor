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
    auto_approve_shortcuts=False,
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


def _assistant_text(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": text}]}}
    )


def _tool_use(name: str, tool_input) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-5-5",
                "content": [{"type": "tool_use", "name": name, "input": tool_input}],
            },
        }
    )


def _result_event(text: str = "", *, is_error: bool = False, subtype: str = "success") -> str:
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "result": text})


PASS_RESULT = """<manual-result status="pass">
  <summary>Works</summary>
  <evidence><screenshot>artifacts/screenshots/pass.png</screenshot></evidence>
</manual-result>
<manual-pass/>"""


class _FakeTmux:
    def __init__(self, **_kwargs):
        self.windows: list[str] = []
        self.killed: list[str] = []

    async def ensure_session(self):
        return None

    async def spawn_in_window(self, name, _cmd, **_kwargs):
        self.windows.append(name)

    async def kill_window(self, name):
        self.killed.append(name)


def _fake_claude(monkeypatch, outputs):
    """Make every generation "run" by writing the next canned (stdout, exit, stderr).

    Replaces loop._wait_for_exit, which _run_generation imports at call time,
    so the real prompt/output/exit-file handling in _run_generation still runs.
    """
    queue = list(outputs)

    async def fake_wait_for_exit(exit_file, output_file, _tmux, _window, _log, _audit):
        stdout, exit_code, stderr = queue.pop(0)
        output_file.write_text(stdout, encoding="utf-8")
        stderr_file = output_file.with_name(
            output_file.name.replace("manual-output-", "manual-stderr-")
        ).with_suffix(".log")
        if stderr_file.exists() or stderr:
            stderr_file.write_text(stderr, encoding="utf-8")
        exit_file.write_text(f"{exit_code}\n", encoding="utf-8")

    monkeypatch.setattr("conductor.core.loop._wait_for_exit", fake_wait_for_exit)
    return queue


def _new_state(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN_TABLE, encoding="utf-8")
    conductor_dir = tmp_path / "conductor" / "checkout-manual"
    state = initialize_manual_state(
        name="checkout-manual",
        project_dir=repo,
        plan_file=plan,
        conductor_dir=conductor_dir,
    )
    return repo, conductor_dir, state


def _audit_events(conductor_dir: Path, event: str) -> list[dict]:
    lines = (conductor_dir / "MANUAL-TEST-AUDIT.jsonl").read_text(encoding="utf-8").splitlines()
    return [record for record in map(json.loads, lines) if record["event"] == event]


@pytest.mark.asyncio
async def test_run_generation_lists_auto_approved_flags(tmp_path, monkeypatch):
    repo, conductor_dir, state = _new_state(tmp_path)
    output = "\n".join(
        [_tool_use("Bash", {"command": "mysql -e 'select 1'"}), _assistant_text(PASS_RESULT)]
    )
    _fake_claude(monkeypatch, [(output, 0, "")])

    signal, _ = await manual_test._run_generation(
        state=state,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=_FakeTmux(),
        prompt="prompt",
        label="scenario-0",
        model="sonnet",
        scenario=state.scenarios[0],
        log_path=conductor_dir / "MANUAL-TEST-LOG.md",
        audit_path=conductor_dir / "MANUAL-TEST-AUDIT.jsonl",
        policy=ManualTestPolicy(),
    )

    assert signal.valid is True
    [end] = _audit_events(conductor_dir, "MANUAL_GENERATION_END")
    assert end["auto_approved"] == ["database cli: mysql -e 'select 1'"]


def test_every_manual_test_event_is_registered():
    import ast

    from conductor.core.logging import EVENT_REGISTRY

    tree = ast.parse(Path(manual_test.__file__).read_text(encoding="utf-8"))
    emitted = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_log", "live_log"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }

    assert "MANUAL_GENERATION_END" in emitted
    assert sorted(emitted - EVENT_REGISTRY.keys()) == []


@pytest.mark.asyncio
async def test_run_generation_writes_markdown_log(tmp_path, monkeypatch):
    repo, conductor_dir, state = _new_state(tmp_path)
    _fake_claude(monkeypatch, [(_assistant_text(PASS_RESULT), 0, "")])

    await manual_test._run_generation(
        state=state,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=_FakeTmux(),
        prompt="prompt",
        label="scenario-0",
        model="sonnet",
        scenario=state.scenarios[0],
        log_path=conductor_dir / "MANUAL-TEST-LOG.md",
        audit_path=conductor_dir / "MANUAL-TEST-AUDIT.jsonl",
    )

    log = (conductor_dir / "MANUAL-TEST-LOG.md").read_text(encoding="utf-8")
    assert "MANUAL_GENERATION_START" in log
    assert "MANUAL_GENERATION_END" in log


@pytest.mark.asyncio
async def test_run_generation_closes_its_window(tmp_path, monkeypatch):
    repo, conductor_dir, state = _new_state(tmp_path)
    _fake_claude(monkeypatch, [(_assistant_text(PASS_RESULT), 0, "")])
    tmux = _FakeTmux()

    await manual_test._run_generation(
        state=state,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=tmux,
        prompt="prompt",
        label="scenario-0",
        model="sonnet",
        scenario=state.scenarios[0],
        log_path=conductor_dir / "MANUAL-TEST-LOG.md",
        audit_path=conductor_dir / "MANUAL-TEST-AUDIT.jsonl",
    )

    assert tmux.windows and tmux.killed == tmux.windows


@pytest.mark.asyncio
async def test_same_named_runs_in_two_projects_do_not_share_exit_files(tmp_path, monkeypatch):
    import conductor.core.loop as loop

    _fake_claude(monkeypatch, [(_assistant_text(PASS_RESULT), 0, "")] * 2)
    fake_wait = loop._wait_for_exit
    exit_files = []

    async def recording_wait(exit_file, *args):
        exit_files.append(exit_file)
        await fake_wait(exit_file, *args)

    monkeypatch.setattr(loop, "_wait_for_exit", recording_wait)

    for project in ["shop", "blog"]:
        (tmp_path / project).mkdir()
        repo, conductor_dir, state = _new_state(tmp_path / project)
        await manual_test._run_generation(
            state=state,
            project_dir=repo,
            conductor_dir=conductor_dir,
            tmux=_FakeTmux(),
            prompt="prompt",
            label="scenario-0",
            model="sonnet",
            scenario=state.scenarios[0],
            log_path=conductor_dir / "MANUAL-TEST-LOG.md",
            audit_path=conductor_dir / "MANUAL-TEST-AUDIT.jsonl",
        )
        assert exit_files[-1].parent == conductor_dir / "logs"

    assert exit_files[0] != exit_files[1]


USAGE_LIMIT = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5"}),
        json.dumps(
            {
                "type": "assistant",
                "error": "rate_limit",
                "message": {
                    "model": "<synthetic>",
                    "content": [{"type": "text", "text": "Claude AI usage limit reached|1760990400"}],
                },
            }
        ),
        _result_event("Claude AI usage limit reached|1760990400", is_error=True),
    ]
)


def _run_main_with(monkeypatch, conductor_dir, outputs):
    """Run the real _manual_main against canned Claude outputs."""
    _fake_claude(monkeypatch, outputs)
    monkeypatch.setattr("conductor.core.tmux.TmuxManager", _FakeTmux)
    monkeypatch.setattr(manual_test, "INFRA_RETRY_DELAY_SECS", 0)

    class _Storage:
        def conductor_dir(self, _name):
            return conductor_dir

    return _Storage()


def test_claude_failure_classifies_infrastructure_errors():
    worked = _assistant_text("Opened the settings page.")
    max_turns = "\n".join([worked, _result_event(subtype="error_max_turns", is_error=True)])
    too_long = "\n".join(
        [
            worked,
            json.dumps(
                {
                    "type": "assistant",
                    "error": "invalid_request",
                    "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Prompt is too long"}]},
                }
            ),
            _result_event("Prompt is too long", is_error=True),
        ]
    )
    mid_session_limit = "\n".join([worked, USAGE_LIMIT])

    assert "usage limit reached" in manual_test._claude_failure(USAGE_LIMIT, 1, "")
    assert "usage limit reached" in manual_test._claude_failure(mid_session_limit, 1, "")
    assert manual_test._claude_failure("", 1, "Error: unknown model x\n") == "exit 1: Error: unknown model x"
    assert manual_test._claude_failure("", 0, "") == "exit 0: no output"
    # The session did real work and ran out of budget: an attempt, not an outage.
    assert manual_test._claude_failure(max_turns, 1, "") == ""
    assert manual_test._claude_failure(too_long, 1, "") == ""
    assert manual_test._claude_failure(worked, 0, "") == ""


@pytest.mark.asyncio
async def test_claude_failures_pause_the_run_without_burning_scenarios(tmp_path, monkeypatch):
    repo, conductor_dir, state = _new_state(tmp_path)
    storage = _run_main_with(
        monkeypatch,
        conductor_dir,
        [
            (_assistant_text("Matrix updated.\n<manual-handoff/>"), 0, ""),
            (_assistant_text(PASS_RESULT), 0, ""),
            (USAGE_LIMIT, 1, ""),
            (USAGE_LIMIT, 1, ""),
            ("", 1, "API Error: 401 invalid x-api-key\n"),
        ],
    )

    await manual_test._manual_main(state, repo, storage)

    saved = load_manual_state(conductor_dir)
    assert saved.status == "paused"
    assert saved.paused_from == "running"
    assert [s.status for s in saved.scenarios] == ["passed", "pending"]
    assert saved.scenarios[1].attempts == 0
    assert "401 invalid x-api-key" in saved.last_error
    assert "Last error:" in format_manual_status(saved)
    assert [g.summary for g in saved.generations][-3:] == ["infra-error"] * 3
    assert len(_audit_events(conductor_dir, "MANUAL_INFRA_RETRY")) == 2
    assert len(_audit_events(conductor_dir, "MANUAL_TEST_PAUSED")) == 1
    stderr_logs = list((conductor_dir / "logs").glob("manual-stderr-*.log"))
    assert [p.name for p in stderr_logs] == ["manual-stderr-005-scenario-1.log"]

    # Resuming runs the scenario again, now that Claude is back.
    storage = _run_main_with(monkeypatch, conductor_dir, [(_assistant_text(PASS_RESULT), 0, "")])
    await manual_test._manual_main(saved, repo, storage)

    final = load_manual_state(conductor_dir)
    assert final.status == "completed"
    assert final.last_error == ""
    assert [s.status for s in final.scenarios] == ["passed", "passed"]
    assert final.scenarios[1].attempts == 1


@pytest.mark.asyncio
async def test_pause_during_coverage_keeps_the_matrix_and_reruns_coverage(tmp_path, monkeypatch):
    repo, conductor_dir, state = _new_state(tmp_path)
    matrix = conductor_dir / "MANUAL-TEST-SCENARIOS.md"
    edited = matrix.read_text(encoding="utf-8") + "| 2 | pending | high | Discovered | Coverage row | Screenshot | |\n"
    storage = _run_main_with(monkeypatch, conductor_dir, [("", 1, "")] * 3)
    # The first coverage attempt edits the matrix before Claude fails.
    matrix.write_text(edited, encoding="utf-8")

    await manual_test._manual_main(state, repo, storage)

    saved = load_manual_state(conductor_dir)
    assert saved.status == "paused"
    assert saved.paused_from == "coverage"
    assert matrix.read_text(encoding="utf-8") == edited

    storage = _run_main_with(
        monkeypatch,
        conductor_dir,
        [(_assistant_text("<manual-handoff/>"), 0, "")] + [(_assistant_text(PASS_RESULT), 0, "")] * 3,
    )
    await manual_test._manual_main(saved, repo, storage)

    final = load_manual_state(conductor_dir)
    assert final.status == "completed"
    assert [s.name for s in final.scenarios][-1] == "Coverage row"


def _apply(state, conductor_dir, scenario, signal_text):
    """One scenario generation as _run_manual_phases records it."""
    scenario.attempts += 1
    generation = manual_test.ManualGeneration(
        index=len(state.generations) + 1,
        started_at=manual_test.datetime.now(manual_test.timezone.utc),
        scenario_index=scenario.index,
    )
    state.generations.append(generation)
    manual_test._apply_signal_to_scenario(
        state=state,
        conductor_dir=conductor_dir,
        scenario=scenario,
        generation=generation,
        signal=parse_manual_signal(signal_text),
        output_text=signal_text,
    )


def test_scenario_prompt_inlines_the_end_of_a_long_handoff(tmp_path):
    _, conductor_dir, state = _new_state(tmp_path)
    handoff = conductor_dir / "generations" / "generation-007-handoff.md"
    work_log = "\n".join(f"step {i}: clicked through the admin settings page" for i in range(150))
    handoff.write_text(
        f"# Manual Test Generation Handoff\n\n```text\n{work_log}\n"
        "HANDOFF SUMMARY: tenant 42 created; next step is the invoice page\n```\n",
        encoding="utf-8",
    )
    state.scenarios[0].handoff_notes = f"See {handoff}"

    prompt = build_scenario_prompt(state, PLAN_TABLE, state.scenarios[0], conductor_dir)

    assert "HANDOFF SUMMARY: tenant 42 created" in prompt
    assert f"Read {handoff} for all of it" in prompt
    assert "step 0: clicked" not in prompt


def test_honest_handoffs_have_their_own_budget(tmp_path):
    _, conductor_dir, state = _new_state(tmp_path)
    scenario = state.scenarios[0]

    for _ in range(manual_test.MAX_SCENARIO_HANDOFFS - 1):
        _apply(state, conductor_dir, scenario, "Halfway, context is running low.\n<manual-handoff/>")
    assert scenario.status == "pending"
    assert scenario.handoffs == manual_test.MAX_SCENARIO_HANDOFFS - 1

    _apply(state, conductor_dir, scenario, "Still going.\n<manual-handoff/>")
    assert scenario.status == "blocked"
    assert "handed off 5 times" in scenario.evidence.summary


def test_invalid_results_block_after_three_attempts_despite_handoffs(tmp_path):
    _, conductor_dir, state = _new_state(tmp_path)
    scenario = state.scenarios[0]

    _apply(state, conductor_dir, scenario, "Halfway.\n<manual-handoff/>")
    _apply(state, conductor_dir, scenario, "I forgot the tag")
    _apply(state, conductor_dir, scenario, "I forgot the tag again")
    assert scenario.status == "pending"

    _apply(state, conductor_dir, scenario, "and again")
    assert scenario.status == "blocked"
    assert scenario.evidence.summary == "Scenario did not produce a valid result after 3 attempts"
    assert scenario.evidence.notes == ["missing required manual signal tag"]


FINDING_RESULT = """<manual-result status="finding">
  <severity>medium</severity>
  <category>app_bug</category>
  <title>Save button does nothing</title>
  <reproduction><step>Open settings</step><step>Click save</step></reproduction>
  <expected>Settings saved</expected>
  <actual>Nothing happens</actual>
  <evidence><screenshot>artifacts/screenshots/save.png</screenshot></evidence>
</manual-result>"""


def test_signal_parser_ignores_quoted_tags_and_later_text_turns():
    # Shapes seen in real runs: the previous attempt's tag quoted in backticks,
    # and a background-task notice after the final tag.
    quoted = "\n".join(
        [
            _assistant_text("Last attempt was rejected for emitting `<manual-pass/>` twice."),
            _tool_use("Bash", {"command": "ls"}),
            _assistant_text(PASS_RESULT),
        ]
    )
    background = "\n".join(
        [
            _assistant_text(PASS_RESULT),
            _tool_use("TodoWrite", {"todos": [{"content": "Verify", "status": "completed"}]}),
            _assistant_text("Background task completed, no action needed."),
        ]
    )
    repeated = "\n".join(
        [_assistant_text("I will finish with <manual-pass/> once done."), _assistant_text(PASS_RESULT)]
    )
    fenced = "```xml\n" + PASS_RESULT + "\n```"

    for output in [quoted, background, repeated, fenced, PASS_RESULT + "."]:
        signal = parse_manual_signal(output)
        assert (signal.kind, signal.valid, signal.errors) == ("pass", True, [])


def test_signal_parser_still_rejects_work_after_the_tag_and_conflicting_tags():
    tool_after = "\n".join([_assistant_text(PASS_RESULT), _tool_use("Bash", {"command": "ls"})])
    text_after = PASS_RESULT + "\nChanged files: app/Foo.php"
    conflicting = "\n".join([_assistant_text("Found a bug: <manual-finding/>"), _assistant_text(PASS_RESULT)])

    assert parse_manual_signal(tool_after).errors == ["manual signal tag must be the final output"]
    assert parse_manual_signal(text_after).errors == ["manual signal tag must be the final output"]
    ambiguous = parse_manual_signal(conflicting)
    assert ambiguous.kind is None
    assert ambiguous.errors == ["expected exactly one manual signal tag, found: manual-finding, manual-pass"]


def test_signal_parser_uses_the_last_result_block_matching_the_tag():
    finding_then_pass = FINDING_RESULT + "\n" + PASS_RESULT.replace("<manual-pass/>", "") + "\n<manual-finding/>"
    draft_then_pass = FINDING_RESULT.replace("Save button", "Draft") + "\nActually it works.\n" + PASS_RESULT

    finding = parse_manual_signal(finding_then_pass)
    passed = parse_manual_signal(draft_then_pass)

    assert finding.valid is True
    assert finding.title == "Save button does nothing"
    assert passed.valid is True
    assert passed.kind == "pass"


def test_blocked_reason_ignores_nested_shortcut_reason():
    signal = parse_manual_signal(
        """<manual-result status="blocked">
  <shortcut><reason>bulk seed</reason><command_or_route>make seed</command_or_route><user_flow_supported>setup</user_flow_supported><approved_by_policy>true</approved_by_policy></shortcut>
  <reason>SSO provider sandbox is down</reason>
  <code_read>app/Auth/SsoController.php</code_read>
  <data_attempts>Retried login three times</data_attempts>
  <why_not_fixable_now>External sandbox outage</why_not_fixable_now>
  <evidence><screenshot>artifacts/screenshots/sso.png</screenshot></evidence>
</manual-result>
<manual-blocked/>"""
    )

    assert signal.valid is True
    assert signal.blocked_reason == "SSO provider sandbox is down"


def test_handoff_file_does_not_quote_signal_tags(tmp_path):
    _, conductor_dir, state = _new_state(tmp_path)
    generation = manual_test.ManualGeneration(
        index=1, started_at=manual_test.datetime.now(manual_test.timezone.utc)
    )

    path = manual_test._write_generation_handoff(
        state=state,
        conductor_dir=conductor_dir,
        generation=generation,
        scenario=state.scenarios[0],
        reason="duplicate tag",
        output_text=PASS_RESULT + "\n<manual-pass/>",
    )

    content = path.read_text(encoding="utf-8")
    assert "<manual-pass/>" not in content
    assert "[manual-pass]" in content


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


@pytest.mark.parametrize(
    "evidence",
    [
        "<note>looked fine</note>",
        "<note></note>",
        "<screenshot></screenshot><note>...</note>",
        "<url>...</url><screenshot> … </screenshot>",
    ],
    ids=["note-only", "empty-note", "empty-screenshot", "placeholders"],
)
def test_pass_and_finding_need_evidence_beyond_notes(evidence):
    passed = parse_manual_signal(
        f"""<manual-result status="pass"><summary>Fine</summary><evidence>{evidence}</evidence></manual-result>
<manual-pass/>"""
    )
    finding = parse_manual_signal(
        FINDING_RESULT.replace("<screenshot>artifacts/screenshots/save.png</screenshot>", evidence)
        + "\n<manual-finding/>"
    )

    assert passed.valid is False
    assert "pass result requires evidence beyond notes" in "; ".join(passed.errors)
    assert finding.valid is False
    assert "finding requires evidence beyond notes" in "; ".join(finding.errors)


def test_blocked_results_may_rest_on_notes():
    blocked = parse_manual_signal(
        """<manual-result status="blocked">
  <reason>Payment provider sandbox is down for maintenance</reason>
  <code_read>PaymentGateway::charge calls the sandbox API directly</code_read>
  <data_attempts>Tried the admin UI and the seeded tenant</data_attempts>
  <why_not_fixable_now>Third-party outage</why_not_fixable_now>
  <evidence><note>Provider status page says maintenance until 18:00</note></evidence>
</manual-result>
<manual-blocked/>"""
    )

    assert blocked.errors == []
    assert blocked.valid is True


def test_mail_evidence_tag_counts_for_any_mail_catcher():
    signal = parse_manual_signal(
        """<manual-result status="pass">
  <summary>Invoice mail arrived</summary>
  <evidence><mail>Mailpit message 42: "Your invoice"</mail></evidence>
</manual-result>
<manual-pass/>"""
    )

    assert signal.valid is True
    assert signal.evidence.mailhog_messages == ['Mailpit message 42: "Your invoice"']
    assert manual_test._format_evidence_lines(signal.evidence) == [
        "Invoice mail arrived",
        'mail=Mailpit message 42: "Your invoice"',
    ]


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
    invalid = parse_manual_signal(
        f"{tool_event}\n{text_without_shortcut}", policy_flags=flags, policy=POLICY
    )
    valid = parse_manual_signal(
        f"{tool_event}\n{text_with_shortcut}", policy_flags=flags, policy=POLICY
    )

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
    # Auto-approval (the default): an incomplete declaration is only a warning.
    assert incomplete.valid is True
    assert "DataShortcut 1 missing reason, user_flow_supported" in incomplete.warnings


@pytest.mark.parametrize("auto_approve", [True, False])
def test_shortcut_declaration_problems_only_reject_in_strict_mode(auto_approve, tmp_path):
    policy = ManualTestPolicy(auto_approve_shortcuts=auto_approve)
    final = """<manual-result status="pass">
  <evidence><summary>Seeded via fixture, checked in UI</summary><screenshot>artifacts/screenshots/p.png</screenshot></evidence>
  <shortcut><reason>Seed a tenant</reason><command_or_route>make fixtures</command_or_route><user_flow_supported>No signup UI yet</user_flow_supported><approved_by_policy>false</approved_by_policy></shortcut>
</manual-result>
<manual-pass/>"""

    signal = _signal_for(policy=policy, final=final)
    _, conductor_dir, state = _new_state(tmp_path)
    prompt = build_scenario_prompt(state, "plan", state.scenarios[0], conductor_dir, policy)

    message = "DataShortcut 1 must set approved_by_policy=true"
    if auto_approve:
        assert signal.valid is True
        assert signal.warnings == [message]
        assert "for the audit report" in prompt
        assert "or the result is rejected" not in prompt
    else:
        assert signal.valid is False
        assert message in signal.errors
        assert "or the result is rejected" in prompt


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
    signal = parse_manual_signal(f"{tool_event}\n{result_event}", policy_flags=flags, policy=POLICY)

    assert flags
    assert signal.valid is False
    assert "DataShortcut" in "; ".join(signal.errors)


def test_policy_scanner_auto_approve_is_default_and_keeps_fatal_patterns():
    def tool(command):
        return json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "bash", "input": {"command": command}}]
                },
            }
        )

    result = json.dumps(
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
    relaxed = ManualTestPolicy(
        auto_approve_shortcuts=True, fatal_patterns=POLICY.fatal_patterns
    )

    undeclared = tool("mysql -e 'insert into fixtures values (1)'") + "\n" + result
    forbidden = tool("mysql -e 'update tenants set useNewCheckout=1'") + "\n" + result

    strict_signal = parse_manual_signal(
        undeclared, policy_flags=scan_policy_shortcuts(undeclared, POLICY), policy=POLICY
    )
    relaxed_signal = parse_manual_signal(
        undeclared, policy_flags=scan_policy_shortcuts(undeclared, relaxed), policy=relaxed
    )
    relaxed_fatal = parse_manual_signal(
        forbidden, policy_flags=scan_policy_shortcuts(forbidden, relaxed), policy=relaxed
    )

    assert ManualTestPolicy().auto_approve_shortcuts is True
    assert strict_signal.valid is False
    assert relaxed_signal.valid is True
    assert relaxed_fatal.valid is False
    assert "toggled through the admin UI" in "; ".join(relaxed_fatal.errors)


FLAG_POLICY = ManualTestPolicy(fatal_patterns=POLICY.fatal_patterns)


def _signal_for(*events: str, policy: ManualTestPolicy = FLAG_POLICY, final: str = PASS_RESULT, **scan_kwargs):
    output = "\n".join([*events, _assistant_text(final)])
    flags = scan_policy_shortcuts(output, policy, **scan_kwargs)
    return parse_manual_signal(output, policy_flags=flags, policy=policy)


@pytest.mark.parametrize(
    "events",
    [
        [_tool_use("Bash", {"command": "php bin/console app:feature-flag:set useNewCheckout on"})],
        [_tool_use("Bash", {"command": "curl -d '{\"useNewCheckout\":true}' http://localhost/api/admin/flags"})],
        [_tool_use("Bash", {"command": "mariadb -e 'update tenants set useNewCheckout=1'"})],
        [_tool_use("Bash", {"command": "python manage.py shell -c 'Tenant.objects.update(useNewCheckout=True)'"})],
        [_tool_use("Bash", {"command": "grep -c x y && mysql -e 'update tenants set useNewCheckout=1'"})],
        [_tool_use("mcp__playwright__browser_evaluate", {"function": "() => localStorage.setItem('useNewCheckout', '1')"})],
        [_tool_use("mcp__playwright__browser_navigate", {"url": "http://app.test/_test/flags?useNewCheckout=1"})],
        [_tool_use("mcp__db__query", {"sql": "UPDATE tenants SET useNewCheckout = 1"})],
        [
            _tool_use("Write", {"file_path": "/tmp/enable.sql", "content": "UPDATE tenants SET useNewCheckout=1;"}),
            _tool_use("Bash", {"command": "mysql app < /tmp/enable.sql"}),
        ],
    ],
    ids=[
        "console", "curl-implicit-post", "mariadb", "django-shell", "chained", "browser-evaluate",
        "navigate", "mcp-sql", "written-sql-file",
    ],
)
def test_fatal_patterns_apply_to_every_acting_tool_input(events):
    signal = _signal_for(*events)

    assert signal.valid is False
    assert "toggled through the admin UI" in "; ".join(signal.errors)


@pytest.mark.parametrize(
    "events",
    [
        [_tool_use("Grep", {"pattern": "useNewCheckout", "path": "app/"})],
        [_tool_use("Bash", {"command": "grep -rn 'useNewCheckout|showNewCheckout' app/ 2>/dev/null | head -20"})],
        [_tool_use("Bash", {"command": "cd app && git log -S useNewCheckout --oneline"})],
        [_tool_use("Bash", {"command": "mysql -e 'select useNewCheckout from tenants where id = 4'"})],
        [_tool_use("mcp__playwright__browser_click", {"element": "useNewCheckout checkbox", "ref": "e12"})],
        [_tool_use("Task", {"prompt": "Toggle useNewCheckout through the admin UI"})],
        [_tool_use("Write", {"file_path": "/tmp/notes.md", "content": "useNewCheckout is on"})],
        [_tool_use("Bash", {"command": "make up", "description": "Start env before enabling useNewCheckout"})],
        [_tool_use("Bash", {"command": "# is useNewCheckout on?\nphp artisan config:show app"})],
        [_tool_use("Bash", {"command": "find app -name '*.php' | xargs grep -l useNewCheckout"})],
        [_tool_use("Bash", {"command": "docker exec db mysql -N -e 'SELECT useNewCheckout FROM tenants'"})],
        [_tool_use("Bash", {"command": "playwright-cli screenshot --filename=shots/useNewCheckout-on.png"})],
        [_tool_use("Bash", {"command": "curl -s http://app.test/api/config | jq .useNewCheckout"})],
        [_tool_use("mcp__playwright__browser_evaluate", {"function": "() => document.getElementById('useNewCheckout').checked"})],
        [_tool_use("mcp__db__query", {"sql": "SELECT useNewCheckout FROM tenants"})],
        [_tool_use("mcp__db__query", {"sql": "SELECT useNewCheckout FROM tenants WHERE id < 10"})],
        [_tool_use("Bash", {"command": "mysql app <<< 'SELECT useNewCheckout FROM tenants'"})],
        # Shapes from real runs: a PDO readback script written through a heredoc,
        # notes saved with json.dump(), and the fixer's focused test run.
        [
            _tool_use(
                "Bash",
                {
                    "command": (
                        "cat > /tmp/q.php << 'EOF'\n<?php\n$pdo = new PDO('mysql:host=db;dbname=app', 'root', 'root');\n"
                        "echo json_encode($pdo->query(\"SELECT useNewCheckout FROM tenants WHERE id = 4\")->fetch());\n"
                        "EOF\n./scripts/env.sh exec app \"php /tmp/q.php\""
                    )
                },
            )
        ],
        [
            _tool_use(
                "Bash",
                {
                    "command": (
                        "python3 << 'PYEOF'\nimport json\n"
                        "notes = {'summary': 'useNewCheckout enabled via the admin UI'}\n"
                        "json.dump(notes, open('/tmp/notes.json', 'w'))\nPYEOF"
                    )
                },
            )
        ],
        [_tool_use("Bash", {"command": "php artisan test --filter=useNewCheckout"})],
    ],
    ids=[
        "grep-tool", "bash-grep", "git-log", "select-readback", "ui-click", "subagent-prompt",
        "unused-write", "bash-description", "shell-comment", "xargs-grep", "docker-readback",
        "screenshot-name", "curl-get", "evaluate-read", "mcp-select", "mcp-select-comparison",
        "here-string-select", "heredoc-pdo-readback", "json-dump-notes", "test-run-filter",
    ],
)
def test_fatal_patterns_skip_reads_and_ui_actions(events):
    assert _signal_for(*events).valid is True


def test_fatal_patterns_ignore_prose_that_only_mentions_the_flag():
    prose = _assistant_text(
        "I checked with curl that the login page loads.\n"
        "Then I toggled useNewCheckout in the admin UI; the form did a POST and saved."
    )

    signal = _signal_for(prose, _tool_use("mcp__playwright__browser_click", {"element": "Save", "ref": "e3"}))

    assert signal.valid is True
    assert all(not flag.fatal_reason for flag in signal.policy_flags)


def test_fatal_patterns_invalidate_handoffs():
    signal = _signal_for(
        _tool_use("Bash", {"command": "mysql -e 'update tenants set useNewCheckout=1'"}),
        final="Set the flag up, continuing next generation.\n<manual-handoff/>",
    )

    assert signal.kind == "handoff"
    assert signal.valid is False


def test_fixer_may_run_code_it_edited():
    events = [
        _tool_use(
            "Edit",
            {"file_path": "/repo/db/seeds/flags.sql", "old_string": "x", "new_string": "INSERT INTO flags VALUES ('useNewCheckout')"},
        ),
        _tool_use("Bash", {"command": "mysql app_test < /repo/db/seeds/flags.sql"}),
    ]

    assert _signal_for(*events).valid is False
    assert _signal_for(*events, fatal_scope="commands").valid is True
    assert _signal_for(_tool_use("Bash", {"command": "mysql -e 'update t set useNewCheckout=1'"}), fatal_scope="commands").valid is False
    assert _signal_for(_tool_use("Bash", {"command": "mysql -e 'update t set useNewCheckout=1'"}), fatal_scope="none").valid is True


def _apply_scenario_generation(tmp_path: Path, *events: str, final: str):
    _, conductor_dir, state = _new_state(tmp_path)
    scenario = state.scenarios[0]
    output = "\n".join([*events, _assistant_text(final)])
    scenario.attempts += 1
    generation = manual_test.ManualGeneration(
        index=1, started_at=manual_test.datetime.now(manual_test.timezone.utc), scenario_index=0
    )
    manual_test._apply_signal_to_scenario(
        state=state,
        conductor_dir=conductor_dir,
        scenario=scenario,
        generation=generation,
        signal=parse_manual_signal(
            output, policy_flags=scan_policy_shortcuts(output, FLAG_POLICY), policy=FLAG_POLICY
        ),
        output_text=output,
    )
    return state, scenario


@pytest.mark.parametrize(
    "events",
    [
        [_tool_use("Bash", {"command": "mysql -e 'update tenants set useNewCheckout=1'"})],
        [_tool_use("Bash", {"command": "mysql app <<'SQL'\nUPDATE tenants SET useNewCheckout = 1;\nSQL"})],
        [_tool_use("Bash", {"command": "echo 'UPDATE tenants SET useNewCheckout=1' | docker exec -i db mysql app"})],
        [_tool_use("Bash", {"command": "docker exec db sh -c \"mysql -e 'UPDATE tenants SET useNewCheckout=1'\""})],
        [_tool_use("Bash", {"command": "curl -d '{\"useNewCheckout\":true}' http://localhost/api/admin/flags"})],
        [_tool_use("Bash", {"command": "curl -s \\\n  -X PATCH http://localhost/api/tenants/4 \\\n  -d useNewCheckout=1"})],
        [_tool_use("Bash", {"command": "curl -s http://app.test/_test/flags/useNewCheckout/on"})],
        [_tool_use("Bash", {"command": "./scripts/env.sh exec app \"curl -X POST http://app/_test/flags/useNewCheckout\""})],
        [_tool_use("mcp__playwright__browser_evaluate", {"function": "() => localStorage.setItem('useNewCheckout', '1')"})],
        [_tool_use("mcp__playwright__browser_navigate", {"url": "http://app.test/_test/flags?useNewCheckout=1"})],
        [_tool_use("mcp__db__query", {"sql": "UPDATE tenants SET useNewCheckout = 1"})],
        [
            _tool_use("Write", {"file_path": "/tmp/enable.sql", "content": "UPDATE tenants SET useNewCheckout=1;"}),
            _tool_use("Bash", {"command": "mysql app < /tmp/enable.sql"}),
        ],
    ],
    ids=["sql-update", "heredoc-sql", "piped-sql", "sql-in-sh-c", "curl-post", "curl-continued-lines", "test-route",
         "wrapped-curl", "storage-write", "navigate-test-route", "mcp-sql", "written-sql-file"],
)
@pytest.mark.parametrize("final", [PASS_RESULT, "Flag set, handing off.\n<manual-handoff/>"], ids=["pass", "handoff"])
def test_fatal_mutation_ends_the_scenario_as_policy_violation(tmp_path, events, final):
    state, scenario = _apply_scenario_generation(tmp_path, *events, final=final)

    assert scenario.status == "policy_violation"
    assert "toggled through the admin UI" in scenario.evidence.notes[0]
    assert manual_test._next_open_scenario(state).index == 1


# Shapes from real runs: the call names the flag but may only read it.
@pytest.mark.parametrize(
    "command",
    [
        # doctrine bootstrap in `php -r`, printing the flag
        (
            "docker exec app php -r \"require 'vendor/autoload.php'; "
            "\\$em = (require 'src/dependencies/doctrine.php')(\\$settings); "
            "\\$c = \\$em->getRepository(TenantConfig::class)->findOneBy(['tenant' => 4]); "
            "echo 'useNewCheckout: ' . var_export(\\$c->getUseNewCheckout(), true);\""
        ),
        # artisan tinker readback through the project's env wrapper
        (
            "./scripts/env.sh exec app \"php artisan tinker --execute=\\\"echo 'useNewCheckout: ' "
            ". var_export(app(Client::class)->tenantConfig(4)->getUseNewCheckout(), true);\\\"\" 2>&1"
        ),
        # clearing the mail catcher, then reading the flag off a page
        (
            "curl -s -X DELETE http://localhost:8025/api/v1/messages && "
            "curl -s http://app.test/admin/tenants/4 | grep -o 'useNewCheckout[^,]*'"
        ),
        # a script whose notes name the flag, a test route and SQL it did not run
        (
            "python3 << 'PYEOF'\nimport json\nstate = json.load(open('state.json'))\n"
            "state['notes'] = 'useNewCheckout enabled via the admin UI; not via POST /_test/flags "
            "or SQL UPDATE tenants SET useNewCheckout = 1'\n"
            "json.dump(state, open('state.json', 'w'))\nPYEOF"
        ),
        # notes documenting a forbidden call, written through a heredoc
        "cat > notes.md << 'EOF'\nNot used: curl -X POST http://app.test/_test/flags/useNewCheckout\nEOF",
    ],
    ids=["doctrine-readback", "tinker-readback", "mailcatcher-then-grep", "notes-in-script", "notes-heredoc"],
)
def test_fatal_hit_that_may_only_read_is_retried_not_terminal(tmp_path, command):
    state, scenario = _apply_scenario_generation(
        tmp_path, _tool_use("Bash", {"command": command}), final=PASS_RESULT
    )

    assert scenario.status == "pending"
    assert manual_test._next_open_scenario(state).index == 0
    handoff = Path(scenario.handoff_notes.removeprefix("See "))
    assert "toggled through the admin UI" in handoff.read_text(encoding="utf-8")


def test_read_only_command_detection():
    read_only = [
        "grep -rn foo src | head",
        "rg foo && cat a.txt",
        "FOO=1 git --no-pager log -p",
        "find . -name '*.php'",
        "git show HEAD 2>&1 | head",
        "ls # useNewCheckout",
    ]
    acting = [
        "grep foo src > out.txt",
        "find . -name x -delete",
        "git commit -m x",
        "mysql app < dump.sql",
        "psql -c 'select 1'",
        "echo $(curl -X POST x)",
        "sed -i s/a/b/ f",
        "echo 'unbalanced",
        "grep x\nmysql -e 'select 1'; php artisan tinker",
    ]

    assert [cmd for cmd in read_only if not manual_test._is_read_only_command(cmd)] == []
    assert [cmd for cmd in acting if manual_test._is_read_only_command(cmd)] == []


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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _committed_files(repo: Path, rev: str = "HEAD") -> list[str]:
    output = _git(repo, "show", "-z", "--name-only", "--format=", rev)
    return sorted(path.strip("\n") for path in output.split("\0") if path.strip("\n"))


def _git_project(tmp_path: Path, files: dict[str, str] | None = None):
    """A committed git repo plus manual-test state with one open, fixable finding."""
    repo, conductor_dir, state = _new_state(tmp_path)
    _git(repo, "init", "-q")
    for key, value in {
        "user.email": "tester@example.com",
        "user.name": "Tester",
        "commit.gpgsign": "false",
        "core.hooksPath": ".git/hooks",
    }.items():
        _git(repo, "config", key, value)
    for name, content in (files or {"app/Foo.php": "<?php // broken\n"}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
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
    return repo, conductor_dir, state, finding


def _script_generations(monkeypatch, state, steps, prompts=None):
    """Fake _run_generation: each call pops (final text, side effect) and records its label."""
    calls = []

    async def fake_run_generation(**kwargs):
        calls.append(kwargs["label"])
        if prompts is not None:
            prompts.append(kwargs["prompt"])
        state.generations.append(
            manual_test.ManualGeneration(
                index=len(state.generations) + 1,
                started_at=manual_test.datetime.now(manual_test.timezone.utc),
            )
        )
        final, action = steps.pop(0)
        if action:
            action()
        output = _assistant_text(final)
        return manual_test.parse_manual_signal(output, policy=kwargs.get("policy")), output

    monkeypatch.setattr(manual_test, "_run_generation", fake_run_generation)
    return calls


async def _fix(repo, conductor_dir, state, finding, policy=None):
    await manual_test._fix_and_verify_finding(
        state=state,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=object(),
        plan_content=PLAN_TABLE,
        finding=finding,
        log_path=manual_test.manual_log_path(conductor_dir),
        audit_path=manual_test.manual_audit_path(conductor_dir),
        model="sonnet",
        policy=policy,
    )


FIXER_HANDOFF = "Fixed the send handler.\n<manual-handoff/>"


def test_git_changed_files_lists_files_in_untracked_dirs_and_odd_names(tmp_path):
    repo, *_ = _git_project(tmp_path, {"app/Old.php": "old", "app/Bar.php": "old"})
    (repo / "newmod").mkdir()
    for name in ["newmod/a.php", "newmod/b.php", "app/my file.php", "app/Café.php"]:
        (repo / name).write_text("new", encoding="utf-8")
    (repo / "app/Old.php").unlink()
    (repo / "app/Bar.php").write_text("staged", encoding="utf-8")
    _git(repo, "add", "app/Bar.php")

    assert manual_test._git_changed_files(repo) == sorted(
        ["app/Bar.php", "app/Café.php", "app/Old.php", "app/my file.php", "newmod/a.php", "newmod/b.php"]
    )


@pytest.mark.parametrize(
    "path, expected",
    [
        (".playwright-cli/page-2026-05-02T22-30-40-289Z.yml", True),
        (".playwright-mcp/console.log", True),
        ("page-2026-05-02T22-30-40-289Z.png", True),
        ("test-results/checkout/trace.zip", True),
        ("app/playwright-report/index.html", True),
        ("config/services.yaml", False),
        ("docs/screenshots/admin.png", False),
        ("app/src/Checkout.php", False),
    ],
)
def test_test_artifact_filter_matches_tool_output_only(path, expected):
    assert manual_test._is_test_artifact(path) is expected


def test_commit_verified_finding_commits_only_the_fix(tmp_path):
    repo, _, _, finding = _git_project(
        tmp_path, {"app/Bar.php": "old", "app/Old.php": "old", "app/Unrelated.php": "old"}
    )
    (repo / "newmod").mkdir()
    (repo / "newmod/user.txt").write_text("user's untracked notes", encoding="utf-8")
    (repo / "app/Unrelated.php").write_text("user's staged work", encoding="utf-8")
    _git(repo, "add", "app/Unrelated.php")
    fix = ["app/Bar.php", "app/Café.php", "app/Old.php", "app/[id].php", "app/my file.php", "newmod/b.php"]
    (repo / "app/Bar.php").write_text("fixed", encoding="utf-8")
    (repo / "app/Old.php").unlink()
    for name in ["app/Café.php", "app/[id].php", "app/my file.php", "newmod/b.php"]:
        (repo / name).write_text("new", encoding="utf-8")

    assert manual_test._commit_verified_finding(repo, finding, fix)

    assert _committed_files(repo) == sorted(fix)
    assert _git(repo, "diff", "--cached", "--name-only") == "app/Unrelated.php\n"
    assert "newmod/user.txt" in manual_test._git_changed_files(repo)


@pytest.mark.asyncio
async def test_fix_commit_contains_only_the_fixers_source_changes(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)
    (repo / ".playwright-cli").mkdir()
    (repo / ".playwright-cli/page-2026-05-02T22-30-40-289Z.yml").write_text("snapshot", encoding="utf-8")

    def fixer_edits():
        (repo / "app/Foo.php").write_text("<?php // fixed\n", encoding="utf-8")
        (repo / "app/new dir").mkdir()
        (repo / "app/new dir/Helper.php").write_text("<?php\n", encoding="utf-8")
        (repo / "test-results").mkdir()
        (repo / "test-results/trace.zip").write_text("zip", encoding="utf-8")

    calls = _script_generations(monkeypatch, state, [(FIXER_HANDOFF, fixer_edits), (PASS_RESULT, None)])

    await _fix(repo, conductor_dir, state, finding)

    assert calls == ["fix-MT-001", "verify-MT-001"]
    assert finding.status == "verified"
    assert len(finding.fix_commits) == 1
    assert _committed_files(repo) == ["app/Foo.php", "app/new dir/Helper.php"]
    assert manual_test._git_changed_files(repo) == [
        ".playwright-cli/page-2026-05-02T22-30-40-289Z.yml",
        "test-results/trace.zip",
    ]


@pytest.mark.asyncio
async def test_failed_fix_commit_is_reported_and_leaves_nothing_staged(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)
    hook = repo / ".git/hooks/pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    def fixer_edits():
        (repo / "app/Foo.php").write_text("<?php // fixed\n", encoding="utf-8")

    _script_generations(monkeypatch, state, [(FIXER_HANDOFF, fixer_edits), (PASS_RESULT, None)])

    await _fix(repo, conductor_dir, state, finding)

    assert finding.status == "verified"
    assert finding.fix_commits == []
    assert "Commit failed" in finding.verification[-1]
    assert "lint failed" in finding.verification[-1]
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert _git(repo, "status", "--porcelain") == " M app/Foo.php\n"
    assert _audit_events(conductor_dir, "MANUAL_COMMIT_FAILED")[0]["files"] == ["app/Foo.php"]


GREP_CHECK = ManualTestPolicy(
    check_commands=[CheckCommandRule(argv=["grep", "-q", "fixed", "{path}"], suffixes=[".php"])]
)


def _edit(repo: Path, content: str, name: str = "app/Foo.php"):
    return lambda: (repo / name).write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_retry_after_a_failed_fix_starts_clean_and_commits_the_reedited_file(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)
    prompts: list[str] = []
    calls = _script_generations(
        monkeypatch,
        state,
        [
            (FIXER_HANDOFF, _edit(repo, "<?php // wrong guess\n")),
            (FIXER_HANDOFF, _edit(repo, "<?php // fixed\n")),
            (PASS_RESULT, None),
        ],
        prompts,
    )

    await _fix(repo, conductor_dir, state, finding, GREP_CHECK)

    assert finding.status == "open"
    first = finding.fix_attempts[0]
    assert first.outcome.startswith("Focused checks failed: grep -q fixed app/Foo.php: exit 1")
    assert first.files == ["app/Foo.php"]
    assert first.stash
    assert manual_test._git_changed_files(repo) == []
    assert "conductor manual-test: failed fix MT-001 attempt 1" in _git(repo, "stash", "list")
    assert _audit_events(conductor_dir, "MANUAL_FIX_STASHED")[0]["stash"] == first.stash

    await _fix(repo, conductor_dir, state, finding, GREP_CHECK)

    assert calls == ["fix-MT-001", "fix-MT-001", "verify-MT-001"]
    assert f"git stash show -p --include-untracked {first.stash}" in prompts[1]
    assert "Focused checks failed" in prompts[1]
    assert finding.status == "verified"
    assert _committed_files(repo) == ["app/Foo.php"]
    assert (repo / "app/Foo.php").read_text(encoding="utf-8") == "<?php // fixed\n"
    report = generate_manual_report(state)
    assert "  - fix attempt 1: Focused checks failed" in report
    assert f"changes in stash {first.stash[:12]}" in report


@pytest.mark.asyncio
async def test_finding_needs_a_human_after_three_failed_fixes(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)
    still_failing = FINDING_RESULT.replace("Save button does nothing", "Still broken") + "\n<manual-finding/>"
    steps = []
    for n in range(3):
        steps += [(FIXER_HANDOFF, _edit(repo, f"<?php // fixed, try {n}\n", f"app/Try{n}.php")), (still_failing, None)]
    calls = _script_generations(monkeypatch, state, steps)

    for _ in range(4):
        await _fix(repo, conductor_dir, state, finding)

    assert calls == ["fix-MT-001", "verify-MT-001"] * 3
    assert finding.status == "needs_human"
    assert [attempt.outcome for attempt in finding.fix_attempts] == ["Verifier still failing: Still broken"] * 3
    assert len({attempt.stash for attempt in finding.fix_attempts}) == 3
    assert manual_test._git_changed_files(repo) == []
    assert manual_test._should_fix_finding(finding) is False
    assert manual_test._scenario_has_open_findings(state, state.scenarios[0]) is True


@pytest.mark.asyncio
async def test_pause_during_a_fix_stashes_it_without_using_up_an_attempt(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)

    def edit_then_pause():
        (repo / "app/Foo.php").write_text("<?php // half done\n", encoding="utf-8")
        raise manual_test.ManualRunPaused("usage limit reached")

    _script_generations(monkeypatch, state, [(FIXER_HANDOFF, edit_then_pause)])

    with pytest.raises(manual_test.ManualRunPaused):
        await _fix(repo, conductor_dir, state, finding)

    attempt = finding.fix_attempts[0]
    assert finding.status == "open"
    assert attempt.interrupted and attempt.stash
    assert attempt.outcome == "Interrupted, the run paused: usage limit reached"
    assert manual_test._git_changed_files(repo) == []
    assert manual_test._should_fix_finding(finding) is True


@pytest.mark.asyncio
async def test_resume_verifies_a_fix_whose_verification_was_cut_short(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)
    state.status = "running"
    state.scenarios[1].status = "passed"

    def killed():
        raise KeyboardInterrupt  # Ctrl-C, or tmux kill-session, while verify-MT-001 runs

    _script_generations(monkeypatch, state, [(FIXER_HANDOFF, _edit(repo, "<?php // fixed\n")), (PASS_RESULT, killed)])
    with pytest.raises(KeyboardInterrupt):
        await _fix(repo, conductor_dir, state, finding)

    resumed = load_manual_state(conductor_dir)
    assert resumed.findings[0].status == "fixed"
    calls = _script_generations(monkeypatch, resumed, [(PASS_RESULT, None)])

    await manual_test._run_manual_phases(
        state=resumed,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=object(),
        plan_content=PLAN_TABLE,
        log_path=manual_test.manual_log_path(conductor_dir),
        audit_path=manual_test.manual_audit_path(conductor_dir),
        model="sonnet",
        policy=ManualTestPolicy(),
    )

    assert calls == ["verify-MT-001"]
    assert resumed.findings[0].status == "verified"
    assert len(resumed.findings[0].fix_commits) == 1
    assert resumed.scenarios[0].status == "passed"
    assert _committed_files(repo) == ["app/Foo.php"]


@pytest.mark.asyncio
async def test_resumed_fixed_finding_without_recorded_files_is_reopened(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)
    finding.status = "fixed"  # state written before fix attempts were recorded
    calls = _script_generations(monkeypatch, state, [])

    await manual_test._resume_verification(
        state=state,
        project_dir=repo,
        conductor_dir=conductor_dir,
        tmux=object(),
        plan_content=PLAN_TABLE,
        finding=finding,
        log_path=manual_test.manual_log_path(conductor_dir),
        audit_path=manual_test.manual_audit_path(conductor_dir),
        model="sonnet",
    )

    assert calls == []
    assert finding.status == "open"
    assert "fix's files are unknown" in finding.verification[-1]


@pytest.mark.asyncio
async def test_fixer_is_told_the_project_rules_it_is_checked_against(tmp_path, monkeypatch):
    repo, conductor_dir, state, finding = _git_project(tmp_path)
    prompts: list[str] = []
    _script_generations(monkeypatch, state, [("No change needed.\n<manual-handoff/>", None)], prompts)

    await _fix(repo, conductor_dir, state, finding, POLICY)

    assert "## Project Rules" in prompts[0]
    assert "NEVER change them via SQL" in prompts[0]


def test_scenario_matrix_survives_render_and_reparse(tmp_path):
    _, _, state = _new_state(tmp_path)
    state.scenarios[0].area = "Admin | rollout"
    state.scenarios[0].name = "Toggle a|b flag"
    original = [(s.area, s.name, s.description, s.priority) for s in state.scenarios]

    for _ in range(3):  # coverage re-parses the matrix the renderer wrote, every run
        state.scenarios = parse_scenarios_from_markdown(manual_test.render_scenarios_markdown(state))

    assert [(s.area, s.name, s.description, s.priority) for s in state.scenarios] == original
    assert state.scenarios[0].description == "Primary evidence target: Admin screenshot, config readback"


def test_scenario_table_accepts_short_separators():
    scenarios = parse_scenarios_from_markdown(
        "| Area | Scenario |\n|-|:-:|\n| Checkout | Pay with card |\n"
    )

    assert [(s.area, s.name) for s in scenarios] == [("Checkout", "Pay with card")]


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
    assert "app/src/Unrelated.php" in finding.verification[0]


@pytest.mark.asyncio
async def test_fix_and_verify_ignores_preexisting_test_artifacts(tmp_path, monkeypatch):
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
    snapshot = ".playwright-mcp/page-2026-05-02.yaml"
    calls = []

    async def fake_run_generation(**kwargs):
        calls.append(kwargs["label"])
        state.generations.append(
            manual_test.ManualGeneration(
                index=len(state.generations) + 1,
                started_at=manual_test.datetime.now(manual_test.timezone.utc),
            )
        )
        return manual_test.parse_manual_signal("<manual-handoff/>"), ""

    monkeypatch.setattr(manual_test, "_run_generation", fake_run_generation)
    # Only the browser snapshot exists before and after: the fixer runs, changes nothing.
    monkeypatch.setattr(manual_test, "_git_changed_files", lambda _cwd: [snapshot])

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

    assert calls == ["fix-MT-001"]
    assert finding.status == "open"
    assert finding.verification == ["Fix attempt 1: Fixer produced no new file changes"]


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
