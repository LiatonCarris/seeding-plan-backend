"""Stable SEEDING domain errors."""

from __future__ import annotations

from typing import Any, Dict, Optional


class SeedingError(Exception):
    """A machine-readable, fail-closed domain error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class ExecutionLocked(SeedingError):
    def __init__(self, message: str = "SEEDING platform execution is locked") -> None:
        super().__init__(
            "FORMAL_EXECUTION_LOCKED",
            message,
            status_code=423,
            details={"platform_business_write_count": 0},
        )
