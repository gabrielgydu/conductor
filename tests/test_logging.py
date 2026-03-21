"""Tests for conductor.core.logging — TDD Phase 2."""

import io
import json
import pytest
from unittest.mock import patch

from conductor.core.logging import live_log, header, error, die, EVENT_REGISTRY


# ---------------------------------------------------------------------------
# live_log — terminal output
# ---------------------------------------------------------------------------


def test_live_log_terminal_output(tmp_path):
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=True):
            live_log("CONDUCTOR_START", "Starting up")
    out = buf.getvalue()
    # icon and message should both be present
    assert "●" in out
    assert "Starting up" in out


def test_live_log_markdown_significant_event(tmp_path):
    log_path = tmp_path / "run.md"
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            live_log("CONDUCTOR_START", "Phase begins", log_path=log_path)
    content = log_path.read_text()
    assert "CONDUCTOR_START" in content
    assert "Phase begins" in content


def test_live_log_markdown_insignificant_event(tmp_path):
    log_path = tmp_path / "run.md"
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            live_log("STALL_CHECK", "checking stall", log_path=log_path)
    # STALL_CHECK has log_to_markdown=False, file should not be created
    assert not log_path.exists()


def test_live_log_audit_all_events(tmp_path):
    audit_path = tmp_path / "audit.ndjson"
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            live_log("CONDUCTOR_START", "start", audit_path=audit_path)
            live_log("STALL_CHECK", "stall", audit_path=audit_path)
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_live_log_audit_json_format(tmp_path):
    audit_path = tmp_path / "audit.ndjson"
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            live_log("RUN_COMPLETE", "done", audit_path=audit_path)
    line = audit_path.read_text().strip()
    data = json.loads(line)
    assert "ts" in data
    assert data["event"] == "RUN_COMPLETE"
    assert data["message"] == "done"


def test_live_log_audit_extra_data(tmp_path):
    audit_path = tmp_path / "audit.ndjson"
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            live_log(
                "BRAIN_CALL",
                "calling brain",
                audit_path=audit_path,
                audit_data={"model": "opus", "tokens": 100},
            )
    data = json.loads(audit_path.read_text().strip())
    assert data["model"] == "opus"
    assert data["tokens"] == 100


def test_live_log_no_paths():
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            # Should not raise, just write to terminal
            live_log("RUNNER_START", "runner up")
    assert "runner up" in buf.getvalue()


# ---------------------------------------------------------------------------
# header / error / die
# ---------------------------------------------------------------------------


def test_header_output():
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=True):
            header("My Header")
    out = buf.getvalue()
    assert "My Header" in out
    # should contain cyan bold ANSI
    assert "\033[" in out


def test_error_output():
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=True):
            error("Something went wrong")
    out = buf.getvalue()
    assert "Something went wrong" in out
    assert "\033[31m" in out  # red


def test_die_exits():
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                die("fatal error", exit_code=2)
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# ANSI stripping when not TTY
# ---------------------------------------------------------------------------


def test_ansi_stripped_when_not_tty():
    buf = io.StringIO()
    with patch("sys.stderr", buf):
        with patch.object(buf, "isatty", return_value=False):
            live_log("FAILURE", "it broke")
    out = buf.getvalue()
    assert "\033[" not in out
    assert "it broke" in out


# ---------------------------------------------------------------------------
# Event registry completeness
# ---------------------------------------------------------------------------


def test_event_registry_completeness():
    """All registered events must have icon and log_to_markdown attributes."""
    assert len(EVENT_REGISTRY) >= 19, "Expected at least 19 events in registry"
    for name, cfg in EVENT_REGISTRY.items():
        assert hasattr(cfg, "icon"), f"{name} missing icon"
        assert hasattr(cfg, "log_to_markdown"), f"{name} missing log_to_markdown"
        assert isinstance(cfg.icon, str) and len(cfg.icon) > 0, f"{name} has empty icon"
