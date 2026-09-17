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
    PRODUCTION_CREATE_AUTHORIZED = "PRODUCTION_CREATE_AUTHORIZED"
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


EXECUTION_TRANSITIONS: Dict[ExecutionState, FrozenSet[ExecutionState]] = {
    ExecutionState.NONE: frozenset(
        {
            ExecutionState.TEST_WRITE_AUTHORIZED,
            ExecutionState.PRODUCTION_CREATE_AUTHORIZED,
        }
    ),
    ExecutionState.TEST_WRITE_AUTHORIZED: frozenset(
        {ExecutionState.WRITE_IN_PROGRESS, ExecutionState.NONE}
    ),
    ExecutionState.PRODUCTION_CREATE_AUTHORIZED: frozenset(
        {ExecutionState.WRITE_IN_PROGRESS, ExecutionState.NONE}
    ),
    ExecutionState.WRITE_IN_PROGRESS: frozenset(
        {
            ExecutionState.PARTIALLY_CREATED_PAUSED,
            ExecutionState.PAUSE_FAILED_EMERGENCY,
            ExecutionState.CREATE_PAUSED,
            ExecutionState.READBACK_VERIFIED,
            ExecutionState.READBACK_MISMATCH,
            ExecutionState.RECONCILE_REQUIRED,
            ExecutionState.RECOVERY_REQUIRED,
            ExecutionState.RELOCK_FAILED,
            ExecutionState.WAITING_RELEASE_AUTHORIZATION,
        }
    ),
    ExecutionState.PARTIALLY_CREATED_PAUSED: frozenset(
        {ExecutionState.RECONCILE_REQUIRED, ExecutionState.RECOVERY_REQUIRED}
    ),
    ExecutionState.PAUSE_FAILED_EMERGENCY: frozenset(
        {ExecutionState.RECONCILE_REQUIRED, ExecutionState.RECOVERY_REQUIRED}
    ),
    ExecutionState.CREATE_PAUSED: frozenset(
        {
            ExecutionState.READBACK_VERIFIED,
            ExecutionState.READBACK_MISMATCH,
            ExecutionState.RECONCILE_REQUIRED,
            ExecutionState.RELOCK_FAILED,
        }
    ),
    ExecutionState.READBACK_VERIFIED: frozenset(
        {
            ExecutionState.TEST_WRITE_AUTHORIZED,
            ExecutionState.WAITING_RELEASE_AUTHORIZATION,
        }
    ),
    ExecutionState.READBACK_MISMATCH: frozenset(
        {ExecutionState.RECONCILE_REQUIRED, ExecutionState.RECOVERY_REQUIRED}
    ),
    ExecutionState.RECONCILE_REQUIRED: frozenset(
        {
            ExecutionState.READBACK_VERIFIED,
            ExecutionState.RECOVERY_REQUIRED,
            ExecutionState.RELOCK_FAILED,
        }
    ),
    ExecutionState.RECOVERY_REQUIRED: frozenset(
        {
            ExecutionState.RECONCILE_REQUIRED,
            ExecutionState.TEST_WRITE_AUTHORIZED,
            ExecutionState.PRODUCTION_CREATE_AUTHORIZED,
        }
    ),
    ExecutionState.RELOCK_FAILED: frozenset(
        {ExecutionState.RECOVERY_REQUIRED, ExecutionState.RECONCILE_REQUIRED}
    ),
    ExecutionState.WAITING_RELEASE_AUTHORIZATION: frozenset(
        {ExecutionState.TEST_WRITE_AUTHORIZED}
    ),
}


DELIVERY_TRANSITIONS: Dict[DeliveryState, FrozenSet[DeliveryState]] = {
    DeliveryState.NOT_RELEASED: frozenset(
        {DeliveryState.FEED_LEARNING, DeliveryState.CLOSED}
    ),
    DeliveryState.FEED_LEARNING: frozenset(
        {DeliveryState.SEARCH_ELIGIBLE, DeliveryState.STABLE, DeliveryState.CLOSING}
    ),
    DeliveryState.SEARCH_ELIGIBLE: frozenset(
        {DeliveryState.STABLE, DeliveryState.CLOSING}
    ),
    DeliveryState.STABLE: frozenset({DeliveryState.CLOSING}),
    DeliveryState.CLOSING: frozenset({DeliveryState.CLOSED}),
    DeliveryState.CLOSED: frozenset(),
}


def validate_project_transition(current: ProjectState, target: ProjectState) -> None:
    if target not in PROJECT_TRANSITIONS[current]:
        raise SeedingError(
            "INVALID_STATE_TRANSITION",
            f"project transition {current.value} -> {target.value} is not allowed",
            status_code=409,
        )


def validate_execution_transition(
    current: ExecutionState, target: ExecutionState
) -> None:
    if target not in EXECUTION_TRANSITIONS[current]:
        raise SeedingError(
            "INVALID_EXECUTION_STATE_TRANSITION",
            f"execution transition {current.value} -> {target.value} is not allowed",
            status_code=409,
        )


def validate_delivery_transition(current: DeliveryState, target: DeliveryState) -> None:
    if target not in DELIVERY_TRANSITIONS[current]:
        raise SeedingError(
            "INVALID_DELIVERY_STATE_TRANSITION",
            f"delivery transition {current.value} -> {target.value} is not allowed",
            status_code=409,
        )
