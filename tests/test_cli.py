"""Tests for conductor.cli and speccer.cli"""

from io import StringIO
from unittest.mock import patch


def _run_main(main_fn, args):
    """Run a CLI main function with given args, returning (stdout, stderr, exit_code)."""
    stdout_buf = StringIO()
    stderr_buf = StringIO()
    exit_code = 0
    with patch("sys.argv", args):
        with patch("sys.stdout", stdout_buf):
            with patch("sys.stderr", stderr_buf):
                try:
                    main_fn()
                except SystemExit as e:
                    exit_code = e.code if e.code is not None else 0
    return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


# ── conductor CLI ──────────────────────────────────────────────────────────────


def test_conductor_help():
    from conductor.cli import main

    stdout, stderr, code = _run_main(main, ["conductor", "--help"])
    assert code == 0
    combined = stdout + stderr
    assert (
        "init" in combined
        or "subcommand" in combined.lower()
        or "conductor" in combined
    )


def test_conductor_init_stub():
    from conductor.cli import main

    stdout, stderr, code = _run_main(main, ["conductor", "init", "--name", "test"])
    assert code == 0
    assert "Not implemented yet" in stdout


def test_conductor_unknown_subcommand():
    from conductor.cli import main

    _, _, code = _run_main(main, ["conductor", "bogus"])
    assert code != 0


# ── speccer CLI ────────────────────────────────────────────────────────────────


def test_speccer_help():
    from speccer.cli import main

    stdout, stderr, code = _run_main(main, ["speccer", "--help"])
    assert code == 0
    combined = stdout + stderr
    assert (
        "init" in combined or "subcommand" in combined.lower() or "speccer" in combined
    )


def test_speccer_init_creates_progress(tmp_path):
    from speccer.cli import main

    spec_dir = tmp_path / "spec"
    stdout, stderr, code = _run_main(
        main, ["speccer", "init", "--feature", "test", "--mode", "backend", "--spec-dir", str(spec_dir)]
    )
    assert code == 0
    assert (spec_dir / "PROGRESS.md").exists()
