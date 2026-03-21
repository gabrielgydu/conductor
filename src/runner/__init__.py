"""Runner — Python port of ralph/lib/runner.sh.

Drives Claude through implementation phases, monitors for promise tokens,
runs quality gates, commits on success, and writes an exit_code file that
the orchestrator polls.
"""
from runner.cli import main

__all__ = ["main"]
