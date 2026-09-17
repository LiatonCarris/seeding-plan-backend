"""Cryptographic integrity helpers for execution and release grants."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Any, Mapping

from .errors import SeedingError
from .identity import canonical_json


class GrantSigner:
    """HMAC signer backed by a runtime-only key.

    The signer establishes server-side grant integrity. Approver identity and
    role are supplied by authenticated API context and included in the signed
    payload; the key is never serialized into a grant or log record.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("grant signing key must contain at least 32 bytes")
        self._key = key

    @classmethod
    def from_environment(cls) -> "GrantSigner":
        filename = os.getenv("SEEDING_GRANT_SIGNING_KEY_FILE", "").strip()
        if not filename:
            raise SeedingError(
                "GRANT_SIGNING_KEY_MISSING",
                "SEEDING_GRANT_SIGNING_KEY_FILE is not configured",
                status_code=503,
            )
        try:
            key = Path(filename).read_bytes().strip()
        except OSError as exc:
            raise SeedingError(
                "GRANT_SIGNING_KEY_UNAVAILABLE",
                "grant signing key file cannot be read",
                status_code=503,
            ) from exc
        try:
            return cls(key)
        except ValueError as exc:
            raise SeedingError(
                "GRANT_SIGNING_KEY_INVALID",
                "grant signing key does not meet the minimum strength requirement",
                status_code=503,
            ) from exc

    def sign(self, payload: Mapping[str, Any]) -> str:
        return hmac.new(
            self._key,
            canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify(self, payload: Mapping[str, Any], signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)
