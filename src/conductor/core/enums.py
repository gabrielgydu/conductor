from enum import Enum


class RunStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"


class StageStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    FAILED = "failed"
    RETRYING = "retrying"
    WAITING = "waiting"
    CANCELLED = "cancelled"
    PARTIAL = "partial"
    REVIEWING = "reviewing"


class SpeccerStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    REVIEWING = "reviewing"


class IntegrationStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    CONFLICT = "conflict"


class BrainAction(str, Enum):
    PROCEED = "proceed"
    WAIT = "wait"
    BLOCK = "block"
    RETRY = "retry"
    ESCALATE = "escalate"


class FixerStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    RETRYING = "retrying"
    CANCELLED = "cancelled"
