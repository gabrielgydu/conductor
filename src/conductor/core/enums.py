from enum import Enum


class RunStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"


class StageStatus(str, Enum):
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


class SpeccerStatus(str, Enum):
    INIT = "init"
    EXPLORING = "exploring"
    NEEDS_INPUT = "needs_input"
    SPECCING = "speccing"
    COMPLETE = "complete"
    GENERATED = "generated"


class IntegrationStatus(str, Enum):
    PENDING = "pending"
    MERGING = "merging"
    CONFLICT_RESOLVING = "conflict_resolving"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"


class BrainAction(str, Enum):
    RETRY = "retry"
    BLOCK = "block"
    STEER = "steer"
    RESET = "reset"
    IGNORE = "ignore"


class FixerStatus(str, Enum):
    WAITING_CI = "waiting_ci"
    FIXING_CONFLICT = "fixing_conflict"
    FIXING = "fixing"
    CLEAN = "clean"
    NO_CHANGES = "no_changes"
    DONE = "done"
    CONFLICT_UNRESOLVABLE = "conflict_unresolvable"
    PUSH_FAILED = "push_failed"
