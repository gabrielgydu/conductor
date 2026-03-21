"""Tests for fixer supersede_older_fixers logic."""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runner.fixer import supersede_older_fixers, write_fixer_status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_status(log_dir: Path, phase: int, status: str, pid: int) -> Path:
    sf = log_dir / f".fixer-status-phase-{phase}"
    data = {
        "status": status,
        "phase": phase,
        "pid": pid,
        "phases": [phase],
        "timestamp": "2026-01-01T00:00:00Z",
        "detail": "",
    }
    sf.write_text(json.dumps(data), encoding="utf-8")
    return sf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_supersede_no_other_fixers(tmp_path):
    """Only current phase status file present -> adopted = [current_phase]."""
    current_sf = tmp_path / ".fixer-status-phase-3"
    current_sf.write_text(json.dumps({"status": "waiting_ci", "phase": 3, "pid": os.getpid(), "phases": [3], "timestamp": "", "detail": ""}))

    adopted = supersede_older_fixers(tmp_path, 3, current_sf)

    assert adopted == [3]


def test_supersede_dead_waiting_ci_fixer_adopted(tmp_path):
    """A dead fixer in waiting_ci state: adopt its phases even if process is dead."""
    current_sf = tmp_path / ".fixer-status-phase-5"
    current_sf.write_text(json.dumps({"status": "waiting_ci", "phase": 5, "pid": os.getpid(), "phases": [5], "timestamp": "", "detail": ""}))

    # PID that doesn't exist (very large number)
    dead_pid = 9_999_999
    _write_status(tmp_path, 2, "waiting_ci", dead_pid)

    adopted = supersede_older_fixers(tmp_path, 5, current_sf)

    assert 2 in adopted
    assert 5 in adopted


def test_supersede_non_waiting_ci_fixer_not_adopted(tmp_path):
    """A fixer in 'fixing' state (not waiting_ci) should not be adopted."""
    current_sf = tmp_path / ".fixer-status-phase-5"
    current_sf.write_text(json.dumps({"status": "waiting_ci", "phase": 5, "pid": os.getpid(), "phases": [5], "timestamp": "", "detail": ""}))

    dead_pid = 9_999_998
    _write_status(tmp_path, 2, "fixing", dead_pid)

    adopted = supersede_older_fixers(tmp_path, 5, current_sf)

    assert 2 not in adopted


def test_supersede_returns_sorted_unique_phases(tmp_path):
    """Adopted phases list is sorted and deduplicated."""
    current_sf = tmp_path / ".fixer-status-phase-5"
    current_sf.write_text(json.dumps({"status": "waiting_ci", "phase": 5, "pid": os.getpid(), "phases": [5], "timestamp": "", "detail": ""}))

    # Two dead fixers with waiting_ci
    for phase in [1, 3]:
        sf = tmp_path / f".fixer-status-phase-{phase}"
        sf.write_text(json.dumps({
            "status": "waiting_ci", "phase": phase, "pid": 9_999_990 + phase,
            "phases": [phase], "timestamp": "", "detail": ""
        }))

    adopted = supersede_older_fixers(tmp_path, 5, current_sf)

    assert adopted == sorted(set(adopted))
    assert 5 in adopted


def test_supersede_inherits_adopted_phases_from_old_fixer(tmp_path):
    """If old fixer had adopted_phases [1, 2], inherit both."""
    current_sf = tmp_path / ".fixer-status-phase-5"
    current_sf.write_text(json.dumps({"status": "waiting_ci", "phase": 5, "pid": os.getpid(), "phases": [5], "timestamp": "", "detail": ""}))

    old_sf = tmp_path / ".fixer-status-phase-2"
    old_data = {
        "status": "waiting_ci",
        "phase": 2,
        "pid": 9_999_994,
        "phases": [1, 2],  # this fixer already adopted phase 1
        "timestamp": "",
        "detail": "",
    }
    old_sf.write_text(json.dumps(old_data), encoding="utf-8")

    adopted = supersede_older_fixers(tmp_path, 5, current_sf)

    assert 1 in adopted
    assert 2 in adopted
    assert 5 in adopted


def test_supersede_corrupted_status_file_skipped(tmp_path):
    """Corrupted JSON file should not crash; just skip it."""
    current_sf = tmp_path / ".fixer-status-phase-5"
    current_sf.write_text(json.dumps({"status": "waiting_ci", "phase": 5, "pid": os.getpid(), "phases": [5], "timestamp": "", "detail": ""}))

    bad_sf = tmp_path / ".fixer-status-phase-1"
    bad_sf.write_text("not valid json", encoding="utf-8")

    adopted = supersede_older_fixers(tmp_path, 5, current_sf)

    assert adopted == [5]


def test_write_fixer_status_creates_valid_json(tmp_path):
    sf = tmp_path / ".fixer-status-phase-3"
    write_fixer_status(sf, "waiting_ci", 3, [2, 3], "some detail")

    data = json.loads(sf.read_text(encoding="utf-8"))
    assert data["status"] == "waiting_ci"
    assert data["phase"] == 3
    assert data["pid"] == os.getpid()
    assert data["phases"] == [2, 3]
    assert data["detail"] == "some detail"
