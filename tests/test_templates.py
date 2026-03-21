"""Tests for conductor.core.templates — TDD Phase 2."""
import pytest
from pathlib import Path

from conductor.core.templates import load_template, render_template


# ---------------------------------------------------------------------------
# load_template
# ---------------------------------------------------------------------------

def test_load_template(tmp_path):
    tpl = tmp_path / "my.tpl"
    tpl.write_text("Hello {NAME}", encoding="utf-8")
    content = load_template(tpl)
    assert content == "Hello {NAME}"


def test_load_template_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_template(tmp_path / "nonexistent.tpl")


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------

def test_variable_substitution():
    result = render_template("Hello {NAME}", variables={"NAME": "world"})
    assert result == "Hello world"


def test_variable_unmatched_left_as_is():
    result = render_template("{UNKNOWN}", variables={})
    assert result == "{UNKNOWN}"


# ---------------------------------------------------------------------------
# Conditionals
# ---------------------------------------------------------------------------

def test_conditional_true():
    result = render_template("{IF INIT}content{ENDIF INIT}", conditions={"INIT": True})
    assert result == "content"


def test_conditional_false():
    result = render_template("{IF INIT}content{ENDIF INIT}", conditions={"INIT": False})
    assert result == ""


def test_conditional_missing():
    result = render_template("{IF INIT}content{ENDIF INIT}", conditions={})
    assert result == ""


# ---------------------------------------------------------------------------
# Injections
# ---------------------------------------------------------------------------

def test_injection():
    result = render_template("{INJECT:SPEC}", injections={"SPEC": "injected content"})
    assert result == "injected content"


def test_injection_missing():
    result = render_template("{INJECT:MISSING}", injections={})
    assert result == ""


def test_injection_multiline():
    content = "line one\nline two\nline three"
    result = render_template("{INJECT:BODY}", injections={"BODY": content})
    assert result == content


# ---------------------------------------------------------------------------
# Combined / ordering
# ---------------------------------------------------------------------------

def test_combined():
    template = "{IF SHOW}Hello {NAME}{ENDIF SHOW}\n{INJECT:FOOTER}"
    result = render_template(
        template,
        variables={"NAME": "Alice"},
        conditions={"SHOW": True},
        injections={"FOOTER": "end"},
    )
    assert result == "Hello Alice\nend"


def test_processing_order():
    # Variables inside active conditional blocks should be resolved
    template = "{IF X}Value: {VAL}{ENDIF X}"
    result = render_template(
        template,
        variables={"VAL": "42"},
        conditions={"X": True},
    )
    assert result == "Value: 42"


def test_conditional_with_variable_inside():
    template = "{IF X}Hello {NAME}{ENDIF X}"
    result = render_template(
        template,
        variables={"NAME": "Bob"},
        conditions={"X": True},
    )
    assert result == "Hello Bob"
