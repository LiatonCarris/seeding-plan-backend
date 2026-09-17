"""Separated state surfaces and explicit transition policy."""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet

from .errors import SeedingError


class ProjectState(str, Enum):
    DRAFT = "DRAFT"
    INPUT_VALIDATED = "INPUT_VALIDATED"
    THREE_CHAIN_READY = "THREE_CHAIN_READY"
    MATRIX_READY = "MATRIX_READY"
    WAITING_BUDGET = "WAITING_BUDGET"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    ADVISORY_READY = "ADVISORY_READY"
    FAILED_CLOSED = "FAILED_CLOSED"
    CLOSED = "CLOSED"


class ExecutionState(str, Enum):
    NONE = "NONE"
    TEST_WRITE_AUTHORIZED = "TEST_WRITE_AUTHORIZED"
    WRITE_IN_PROGRESS = "WRITE_IN_PROGRESS"
    PARTIALLY_CREATED_PAUSED = "PARTIALLY_CREATED_PAUSED"
    PAUSE_FAILED_EMERGENCY = "PAUSE_FAILED_EMERGENCY"
    CREATE_PAUSED = "CREATE_PAUSED"
    READBACK_VERIFIED = "READBACK_VERIFIED"
    READBACK_MISMATCH = "READBACK_MISMATCH"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RELOCK_FAILED = "RELOCK_FAILED"
    WAITING_RELEASE_AUTHORIZATION = "WAITING_RELEASE_AUTHORIZATION"


class DeliveryState(str, Enum):
    NOT_RELEASED = "NOT_RELEASED"
    FEED_LEARNING = "FEED_LEARNING"
    SEARCH_ELIGIBLE = "SEARCH_ELIGIBLE"
    STABLE = "STABLE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


PROJECT_TRANSITIONS: Dict[ProjectState, FrozenSet[ProjectState]] = {
    ProjectState.DRAFT: frozenset(
        {ProjectState.INPUT_VALIDATED, ProjectState.FAILED_CLOSED}
    ),
    ProjectState.INPUT_VALIDATED: frozenset(
        {ProjectState.THREE_CHAIN_READY, ProjectState.FAILED_CLOSED}
    ),
    ProjectState.THREE_CHAIN_READY: frozenset(
        {
            ProjectState.MATRIX_READY,
            ProjectState.WAITING_BUDGET,
            ProjectState.FAILED_CLOSED,
        }
    ),
    ProjectState.MATRIX_READY: frozenset(
        {
            ProjectState.WAITING_CONFIRMATION,
            ProjectState.WAITING_BUDGET,
            ProjectState.FAILED_CLOSED,
        }
    ),
    ProjectState.WAITING_BUDGET: frozenset(
        {ProjectState.INPUT_VALIDATED, ProjectState.MATRIX_READY, ProjectState.CLOSED}
    ),
    ProjectState.WAITING_CONFIRMATION: frozenset(
        {ProjectState.ADVISORY_READY, ProjectState.FAILED_CLOSED}
    ),
    ProjectState.ADVISORY_READY: frozenset(
        {ProjectState.INPUT_VALIDATED, ProjectState.CLOSED, ProjectState.FAILED_CLOSED}
    ),
    ProjectState.FAILED_CLOSED: frozenset(
        {ProjectState.INPUT_VALIDATED, ProjectState.CLOSED}
    ),
    ProjectState.CLOSED: frozenset(),
}


def validate_project_transition(current: ProjectState, target: ProjectState) -> None:
    if target not in PROJECT_TRANSITIONS[current]:
        raise SeedingError(
            "INVALID_STATE_TRANSITION",
            f"project transition {current.value} -> {target.value} is not allowed",
            status_code=409,
        )
