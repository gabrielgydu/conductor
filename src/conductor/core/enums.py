# StrEnum (not `str, Enum`) so str() and f-strings render the value, e.g. in log
# lines, instead of 'StageStatus.GENERATED'.
from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"


class StageStatus(StrEnum):
    PENDING = "pending"
    SPEC_INIT = "spec_init"
    SPEC_RUNNING = "spec_running"
    SPEC_NEEDS_INPUT = "spec_needs_input"
    SPEC_COMPLETE = "spec_complete"
    GENERATED = "generated"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    STALLED = "stalled"
    BLOCKED = "blocked"


class SpeccerStatus(StrEnum):
    INIT = "init"
    EXPLORING = "exploring"
    NEEDS_INPUT = "needs_input"
    SPECCING = "speccing"
    COMPLETE = "complete"
    GENERATED = "generated"


class IntegrationStatus(StrEnum):
    PENDING = "pending"
    MERGING = "merging"
    CONFLICT_RESOLVING = "conflict_resolving"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    NEEDS_FIX = "needs_fix"


class IntegrationFailure(StrEnum):
    """Why an integration attempt did not deliver."""

    SETUP = "setup"  # no worktree, or the branch is checked out elsewhere
    DIVERGED = "diverged"  # local and origin integration branches diverged
    NOTHING_MERGEABLE = "nothing_mergeable"  # the merge loop merged no run
    PUBLISH = "publish"  # merged, but the push or the PR failed


class BrainAction(StrEnum):
    RETRY = "retry"
    BLOCK = "block"
    STEER = "steer"
    RESET = "reset"
    IGNORE = "ignore"


class FixerStatus(StrEnum):
    WAITING_CI = "waiting_ci"
    FIXING_CONFLICT = "fixing_conflict"
    FIXING = "fixing"
    CLEAN = "clean"
    NO_CHANGES = "no_changes"
    DONE = "done"
    CONFLICT_UNRESOLVABLE = "conflict_unresolvable"
    PUSH_FAILED = "push_failed"
