"""Header authentication and RBAC kept separate from business grants."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import FrozenSet, Mapping, Optional

from fastapi import Header

from .errors import SeedingError


@dataclass(frozen=True)
class Principal:
    principal_id: str
    roles: FrozenSet[str]

    def require(self, *allowed: str) -> None:
        if not self.roles.intersection(allowed):
            raise SeedingError(
                "RBAC_FORBIDDEN",
                "principal does not have the required role",
                status_code=403,
            )


class StaticAuthenticator:
    """Injectable authenticator; production callers should use a vault-backed source."""

    def __init__(
        self,
        principals: Mapping[str, Mapping[str, object]],
        *,
        allowed_sessions: Optional[FrozenSet[str]] = None,
    ) -> None:
        self._principals = dict(principals)
        self._allowed_sessions = allowed_sessions

    def authenticate(self, operator_id: str, token: str, session_id: str) -> Principal:
        if not operator_id or not token or not session_id:
            raise SeedingError(
                "AUTHENTICATION_REQUIRED", "authentication required", status_code=401
            )
        if (
            self._allowed_sessions is not None
            and session_id not in self._allowed_sessions
        ):
            raise SeedingError(
                "AUTHENTICATION_REQUIRED", "authentication required", status_code=401
            )
        record = self._principals.get(operator_id)
        expected = str(record.get("token")) if record else ""
        if not expected or not hmac.compare_digest(expected, token):
            raise SeedingError(
                "AUTHENTICATION_REQUIRED", "authentication required", status_code=401
            )
        roles = frozenset(str(role) for role in record.get("roles", ()))
        return Principal(principal_id=operator_id, roles=roles)

    def dependency(
        self,
        x_cid_operator_id: str = Header(default="", alias="X-CID-Operator-Id"),
        x_cid_operator_token: str = Header(default="", alias="X-CID-Operator-Token"),
        x_cid_session_id: str = Header(default="", alias="X-CID-Session-Id"),
    ) -> Principal:
        return self.authenticate(
            x_cid_operator_id, x_cid_operator_token, x_cid_session_id
        )


class DenyAllAuthenticator(StaticAuthenticator):
    def __init__(self) -> None:
        super().__init__({})
